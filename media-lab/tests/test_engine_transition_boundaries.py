"""Execute actual controller functions without importing startup services."""
import ast
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

APP = Path(__file__).parents[1] / 'app.py'

def function(name, **namespace):
    tree = ast.parse(APP.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), 'exec'), namespace)
    return namespace[name]

@pytest.mark.parametrize('message', ['timed out', 'Connection reset by peer', 'Remote end closed connection'])
def test_uncertain_render_never_restarts_or_reissues(tmp_path, message):
    import fcntl, re
    calls = []
    def request(*args, **kwargs):
        calls.append('request')
        raise TimeoutError(message)
    def restart(*args):
        calls.append('restart')
        return 'up'
    def hold(*args):
        # Ordering is the contract here. Canonical flock exclusion and stale-owner
        # rejection are exercised by the durable protocol tests.
        calls.append('hold')
    generate = function('_engine_generate_authorized', ENGINES={'h3': {'port': 1}},
                        preflight=lambda e,b:b, INFERENCE_LOCK=str(tmp_path/'lock'),
                        fcntl=fcntl, http_json=request, ensure_engine=restart,
                        H3_ENGINE_HTTP_TIMEOUT_S=100, re=re, H3_MIN_FRAMES=5,
                        H3_MAX_FRAMES=121, save_state=lambda:None,
                        hold_gpu_recovery=hold, gpu_recovery_pending=lambda:False,
                        gpu_render_ready=lambda e,t: object(),
                        delegation_headers=lambda lease: {},
                        _render_stage_label=lambda stage: stage or 'generating')
    job = {'id':'fixture', 'request':{}}
    result = generate('h3', {'prompt':'fixture'}, job)
    assert calls == ['restart', 'request', 'hold'], 'uncertain remote work must be reconciled, not restarted or reissued'
    assert result['ok'] is False
    assert job['recovery_required'] is True
    assert job['retryable'] is False


def test_fail_preserves_unknown_outcome_hold():
    fail = function('fail', INFRA_FAILURE_MARKS=['timed out'])
    job = {'recovery_required': True}
    fail(job, 'timed out')
    assert job['retryable'] is False


def test_admission_refuses_unresolved_previous_request_before_any_mutation():
    calls = []
    ensure = function('_ensure_engine_under_lease', gpu_recovery_pending=lambda:True,
                      stand_down_other_companions=lambda *a:calls.append('evict'))
    job = {}
    assert ensure('h3', job) == 'busy'
    assert calls == []


def test_queued_job_stays_visible_under_recovery_hold():
    job = {'id':'next','status':'queued'}
    run = function('run_queued_job', jobs={'next':job}, gpu_recovery_pending=lambda:True,
                   _gpu_cutover_ready=True)
    assert run('next') is False
    assert job['status'] == 'queued'


def test_picker_tolerates_queue_swap_race():
    pick = function('pick_next_job', queue=[], VIDEO_ENGINE_NAMES=(),
                    engine_up=lambda _name: False, jobs={}, job_engine=lambda _job: None)
    assert pick() is None


def test_cloud_execution_continues_while_local_recovery_is_held():
    import time
    calls = []
    job = {'id':'cloud','status':'queued','kind':'video','engine':'fal-video'}
    def cloud(j):
        calls.append(j['id'])
        j['status'] = 'done'
    run = function('run_online_job', jobs={'cloud':job}, time=time,
                   save_state=lambda:None, job_queue_lane=lambda j:'online',
                   RUNNERS={'video':cloud}, ensure_multi_scene_storyboard=lambda j:None,
                   eta_record=lambda j:None, notify_done=lambda j:None,
                   gpu_recovery_pending=lambda:True,
                   fail=lambda *a:pytest.fail('unexpected failure'))
    assert run('cloud') is True
    assert calls == ['cloud']
    assert job['status'] == 'done'


def test_persistent_hold_survives_new_controller_and_late_success(tmp_path):
    import json, os, time
    marker = tmp_path/'hold.json'
    hold = function('hold_gpu_recovery', GPU_RECOVERY_HOLD=marker,
                    json=json, os=os, time=time)
    job = {'id':'uncertain'}
    hold('unknown', job)
    original = marker.read_bytes()
    hold('newer-error', {'id':'another'})
    assert marker.read_bytes() == original
    # Fresh process globals and even a late successful row cannot clear it.
    pending = function('gpu_recovery_pending', GPU_RECOVERY_HOLD=marker,
                       _gpu_recovery_blocked=False, jobs={'uncertain':{'status':'done'}})
    assert pending() is True


def test_partial_hold_file_is_fail_closed(tmp_path):
    marker = tmp_path/'hold.json'; marker.touch()
    pending = function('gpu_recovery_pending', GPU_RECOVERY_HOLD=marker,
                       _gpu_recovery_blocked=False, jobs={})
    assert pending() is True


def test_video_runner_has_no_cold_container_bypass():
    tree = ast.parse(APP.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_video')
    calls = {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    assert '_run_video_cold' not in calls


def test_cold_container_launcher_is_fail_closed():
    launcher = APP.parent / 'runner' / 'run_lab_render.sh'
    text = launcher.read_text()
    retired = text.index('FAIL=retired_unfenced_cold_path')
    docker_start = text.index('docker start')
    assert text.index('exit 77', retired) < docker_start


@pytest.mark.parametrize(('name', 'engine', 'task'), [
    ('run_music', 'music', 'generate'),
    ('_run_image', 'image', 'generate'),
    ('vb_generate', 'voice', 'generate'),
    ('run_stems', 'stems', 'separate'),
])
def test_every_local_gpu_family_enters_canonical_operation(name, engine, task):
    tree = ast.parse(APP.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    calls = [call for call in ast.walk(node)
             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
             and call.func.id == 'gpu_operation']
    assert any(len(call.args) >= 2
               and isinstance(call.args[0], ast.Constant) and call.args[0].value == engine
               and isinstance(call.args[1], ast.Constant) and call.args[1].value == task
               for call in calls)


def test_startup_adopts_exact_idle_runtime_or_fails_closed():
    text = ast.get_source_segment(APP.read_text(), next(
        n for n in ast.parse(APP.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'initialize_gpu_cutover'))
    assert text is not None
    assert 'protocol.adopt_recovered(' in text
    assert '_gpu_restart_adoption_proof(recovered)' in text
    assert 'hold_gpu_recovery(' in text
    assert '_gpu_cutover_ready = False' in text


def test_first_gpu_acquire_hands_legacy_pool_to_durable_controller():
    text = ast.get_source_segment(APP.read_text(), next(
        n for n in ast.parse(APP.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'gpu_operation'))
    assert text is not None
    handoff = text.index('pool_cmd("handoff")')
    acquire = text.index('protocol.acquire(')
    assert handoff < acquire
    assert 'pool_cmd("acquire")' in text[acquire:], 'failed durable acquire must restore legacy exclusion'


def test_preload_capacity_rejection_releases_without_uncertain_quarantine():
    text = ast.get_source_segment(APP.read_text(), next(
        n for n in ast.parse(APP.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'gpu_operation'))
    assert text is not None
    deterministic = text.index('except CapacityUnqualified')
    uncertain = text.index('except Exception as exc:', deterministic)
    assert deterministic < uncertain
    block = text[deterministic:uncertain]
    assert 'lease.phase == "reclaim"' in block
    assert 'protocol.release(lease, proof=reclaim_proof)' in block
    assert 'pool_cmd("acquire")' in block
    assert 'operation-uncertain' not in block


def test_pool_lock_manager_has_fail_closed_controller_handoff():
    text = (APP.parent / 'runner' / 'pool_lock.sh').read_text()
    handoff = text.index('handoff)')
    block = text[handoff:]
    assert 'stop media-lab-pool.service' in block
    assert 'stop media-lab-gpu-reservation.service' in block
    assert 'flock -n "$LOCK" -c true' in block
    assert 'echo BUSY; exit 62' in block


def test_h3_boot_binds_and_waits_for_exact_task_family():
    text = APP.read_text()
    boot = ast.get_source_segment(text, next(
        n for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == '_boot_engine'))
    switch = ast.get_source_segment(text, next(
        n for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == 'ensure_h3_variant'))
    assert boot is not None and switch is not None
    assert 'runtime_env["SOL_PRELOAD"] = task' in boot
    assert 'write_runtime_environment(runtime_dir / "media-lab-sol-h3.env", runtime_env)' in boot
    assert '["systemctl", "--user", "start", e["unit"]]' in boot
    assert 'sol_h3_control_guard_ready()' in boot
    assert 'health.get("loaded") is not True' in boot
    assert '"task": _gpu_task_for_engine("h3", j)' in switch
    assert 'task=target["task"]' in switch
    assert 'task=current.get("task")' in switch


def test_h3_warm_reuse_requires_complete_runtime_configuration():
    class H3Ref:
        @staticmethod
        def required_runtime_config(request):
            return {"variant": request.get("variant", "fl2va"),
                    "turbo_preset": request.get("turbo_preset")}

    live = {"variant": "fl2va", "task": "t2va", "turbo_preset": "turbo-a"}
    exact = function('_gpu_exact_warm', _gpu_exact_idle=lambda _engine: True,
                     _h3ref=H3Ref,
                     h3_resident_config=lambda: dict(live))
    job = {"request": {"variant": "fl2va", "turbo_preset": "turbo-a"}}
    assert exact("h3", "t2va", job) is True
    job["request"]["turbo_preset"] = "turbo-b"
    assert exact("h3", "t2va", job) is False


@pytest.mark.parametrize("health", [
    {"loaded": False, "busy": False, "variant": "fl2va", "task": "fl2va"},
    {"loaded": True, "busy": False, "variant": "fl2va", "task": None},
])
def test_h3_residency_rejects_cold_or_taskless_health(health):
    class H3Ref:
        H3_VARIANTS = ("fl2va", "ref2va", "fused_r1024")

    resident = function(
        'h3_resident_config',
        ENGINES={"h3": {"port": 8291, "health": "/health"}},
        http_json=lambda *_args, **_kwargs: dict(health),
        _h3ref=H3Ref,
    )
    assert resident() is None


def test_idle_h3_restore_passes_exact_preload_configuration():
    source = APP.read_text()
    block = source[source.index("    def start_model(self, model, detail):"):
                   source.index("    def model_healthy(self, model):")]
    assert '_h3ref.required_runtime_config({})' in block
    assert '"task": lease.task' in block
    assert '_boot_engine(\n                    model, variant=target["variant"]' in block


def test_reclaim_does_not_treat_unreachable_live_process_as_gone():
    stopped = []
    reclaim = function('_gpu_reclaim_all', COMPANION_ENGINE_NAMES=('h3',),
                       _gpu_process_identity=lambda _name: (4321, 'boot:4321:1'),
                       engine_up=lambda _name: False,
                       engine_busy=lambda _name: False,
                       stop_engine=lambda name: stopped.append(name),
                       _mem_available_gb=lambda *, strict=False: 80.0,
                       LeaseBusy=RuntimeError, Path=Path)
    proof = reclaim()
    assert stopped == []
    assert proof['processes_gone'] is False
    assert proof['memory_recovered'] is False
    assert proof['survivors'] == ['h3']


def test_gpu_process_identity_fails_closed_when_inspection_fails(monkeypatch):
    import app as studio

    failed = subprocess.CompletedProcess(
        ["docker", "inspect"], 2, stdout="", stderr="daemon unavailable"
    )
    monkeypatch.setattr(studio, "ENGINES", {
        "fixture": {"kind": "docker", "container": "fixture-container"}
    })
    monkeypatch.setattr(studio.subprocess, "run", lambda *_args, **_kwargs: failed)
    with pytest.raises(RuntimeError, match="inspection failed"):
        studio._gpu_process_identity("fixture")


def test_gpu_reclaim_requires_strict_memory_measurement(monkeypatch):
    import app as studio
    from unittest import mock

    measured = mock.Mock(return_value=80.0)
    monkeypatch.setattr(studio, "COMPANION_ENGINE_NAMES", ())
    monkeypatch.setattr(studio, "_mem_available_gb", measured)
    monkeypatch.setattr(studio, "release_voice_weights", lambda: True)
    proof = studio._gpu_reclaim_all()
    measured.assert_called_once_with(strict=True)
    assert proof["memory_recovered"] is True


def test_queued_job_closes_previous_task_context_before_retarget():
    events = []
    gpu_thread = threading.local()
    gpu_thread.lease = type('Lease', (), {
        'engine': 'yue2', 'task': 'transcribe', 'job_id': 'cover-1'})()
    gpu_thread.job_context = object()
    gpu_thread.job_target = ('yue2', 'transcribe')

    def finish(*_args):
        events.append('finish')
        gpu_thread.lease = None
        gpu_thread.job_context = None

    class Context:
        def __enter__(self): events.append('enter'); return object()
        def __exit__(self, *_args): events.append('exit')

    ensure = function('ensure_engine', _gpu_task_for_engine=lambda *_args: 'generate',
                      _gpu_thread=gpu_thread, _gpu_finish_job_operation=finish,
                      gpu_operation=lambda *_args: Context(),
                      _ensure_engine_under_lease=lambda *_args: 'up',
                      gpu_render_ready=lambda *_args: events.append('ready'),
                      LeaseBusy=RuntimeError)
    assert ensure('yue2', {'id': 'cover-1'}) == 'up'
    assert events[:3] == ['finish', 'enter', 'ready']


def test_yue2_shim_bootstraps_controller_import_path():
    text = (APP.parent / 'runner' / 'yue2_engine_server.py').read_text()
    assert 'Path(__file__).resolve().parents[1]' in text
    assert text.index('sys.path.insert(0, str(CONTROLLER_ROOT))') < text.index(
        'from media_lab_core.gpu_lease_runtime import ENV_TASK')


def test_nonresident_gpu_operation_never_attempts_warm_health_probe():
    proof = function('_gpu_warm_proof', ENGINES={},
                     _gpu_exact_warm=lambda *_args: (_ for _ in ()).throw(AssertionError()),
                     engine_busy=lambda *_args: (_ for _ in ()).throw(AssertionError()))
    assert proof('maestro', 'generate') == {
        'engine': 'maestro', 'task': 'generate', 'healthy': False, 'busy': False}


def test_render_body_task_is_bound_during_video_admission():
    seen = []

    @contextmanager
    def operation(engine, task, job):
        seen.append((engine, task, job.get('_gpu_task')))
        yield object()

    def authorized(_engine, _body, j=None, task=None, **_kwargs):
        assert j is not None
        seen.append(('authorized', task, j.get('_gpu_task')))
        return {'ok': True}

    generate = function('engine_generate', gpu_operation=operation,
                        _engine_generate_authorized=authorized,
                        _h3ref=__import__('runner.h3_reference', fromlist=['h3_reference']))
    job = {'id': 'talk-1', 'request': {}}
    assert generate('h3', {'start_image_b64': 'frame'}, job)['ok'] is True
    assert seen == [('h3', 'fl2va', 'fl2va'), ('authorized', 'fl2va', 'fl2va')]
    assert '_gpu_task' not in job


def test_idle_model_start_routes_cold_video_boot_through_durable_operation():
    text = APP.read_text()
    cls = next(n for n in ast.parse(text).body
               if isinstance(n, ast.ClassDef) and n.name == '_ResidencyRuntime')
    start = ast.get_source_segment(text, next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'start_model'))
    assert start is not None
    assert 'with gpu_operation(model, task, restore_job)' in start
    assert start.index('with gpu_operation(model, task, restore_job)') < start.index(
        'return _boot_engine(model)')


def test_maestro_is_ephemeral_and_reclaimed_instead_of_parked():
    text = APP.read_text()
    maestro = ast.get_source_segment(text, next(
        n for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == 'run_maestro_fenced'))
    assert maestro is not None
    assert 'gpu_operation("maestro", "generate", j, ephemeral=True)' in maestro


def test_h3_gpu_task_classifies_media_source_as_fl2va_before_admission():
    text = APP.read_text()
    task = ast.get_source_segment(text, next(
        n for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == '_gpu_task_for_engine'))
    assert task is not None
    assert 'request.get("source")' in task
    assert task.index('request.get("source")') < task.index('return "fl2va"')


def test_maestro_runner_consumes_exact_delegation_before_model_init():
    runner = (APP.parent / 'runner' / 'maestro_queue_runner.py').read_text()
    assert 'exact Maestro GPU lease delegation is required' in runner
    assert 'time.time() - float(delegated.get("issued_at")) <= 120' in runner
    assert runner.index('delegation_path.unlink()') < runner.index('session = init(')


def test_comfy_idle_proof_requires_empty_running_and_pending_queues():
    idle = function('_gpu_exact_idle', ENGINES={'image': {'port': 8195, 'health': '/system_stats'}},
                    http_json=lambda *_args, **_kwargs: {
                        'queue_running': [], 'queue_pending': []})
    assert idle('image') is True

    active = function('_gpu_exact_idle', ENGINES={'music': {'port': 8196, 'health': '/system_stats'}},
                      http_json=lambda *_args, **_kwargs: {
                          'queue_running': [[1, 'prompt']], 'queue_pending': []})
    assert active('music') is False


def test_comfy_system_stats_without_queue_evidence_is_not_idle():
    idle = function('_gpu_exact_idle', ENGINES={'image': {'port': 8195, 'health': '/system_stats'}},
                    http_json=lambda *_args, **_kwargs: {'devices': []})
    assert idle('image') is False


def test_direct_gpu_stage_closes_prior_queued_job_context_before_switch():
    text = ast.get_source_segment(APP.read_text(), next(
        n for n in ast.parse(APP.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'gpu_operation'))
    assert text is not None
    close_at = text.index('_gpu_finish_job_operation()')
    refuse_at = text.index('nested GPU operation cannot switch engine or task')
    assert close_at < refuse_at


def _stage_fixture(admission_label):
    """_engine_generate_authorized with an admission that relabels the job."""
    import fcntl, re
    seen = []
    saves = []
    def admit(eng, job):
        job['stage'] = admission_label
        return 'up'
    def request(url, body, timeout=None, headers=None):
        seen.append(job_ref[0]['stage'])
        return {'ok': True, 'file': 'x.mp4'}
    job_ref = [None]
    namespace = dict(ENGINES={'h3': {'port': 1}}, preflight=lambda e, b: b,
                     fcntl=fcntl, http_json=request, ensure_engine=admit,
                     H3_ENGINE_HTTP_TIMEOUT_S=100, re=re, H3_MIN_FRAMES=5,
                     H3_MAX_FRAMES=121, save_state=lambda: saves.append(1),
                     hold_gpu_recovery=lambda *a: None, gpu_recovery_pending=lambda: False,
                     gpu_render_ready=lambda e, t: object(),
                     delegation_headers=lambda lease: {})
    tree = ast.parse(APP.read_text())
    for name in ('ADMISSION_STAGE_PREFIXES',):
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, 'id', None) == name for t in n.targets))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), 'exec'), namespace)
    namespace['_render_stage_label'] = function('_render_stage_label', **namespace)
    generate = function('_engine_generate_authorized', **namespace)
    return generate, job_ref, seen, saves


@pytest.mark.parametrize('caller_stage, expected', [
    ('generating', 'generating'),
    ('stage 1/2 · H3 draft', 'stage 1/2 · H3 draft'),
    ('starting', 'generating'),
    (None, 'generating'),
])
def test_warm_render_does_not_keep_the_admission_label(caller_stage, expected):
    # Live 2026-09-25: a warm H3 t2va render showed "reconciling qwen-h3
    # residency…" for its whole 70 s because admission relabelled the job and
    # nothing put the render label back before the engine call.
    generate, job_ref, seen, saves = _stage_fixture('reconciling qwen-h3 residency…')
    job = {'id': 'fixture', 'request': {}}
    if caller_stage is not None:
        job['stage'] = caller_stage
    job_ref[0] = job
    result = generate('h3', {'prompt': 'fixture'}, job)
    assert result['ok'] is True
    assert seen == [expected]
    assert job['stage'] == expected
    assert saves, 'the corrected label must be persisted for the queue view'


def test_caller_stage_survives_a_relabel_before_the_authorized_call():
    # engine_generate captures the caller's label before gpu_operation can
    # write "releasing idle image weights for warm video admission…".
    generate, job_ref, seen, _ = _stage_fixture('loading H3 ref2va…')
    job = {'id': 'fixture', 'request': {},
           'stage': 'releasing idle image weights for warm video admission…'}
    job_ref[0] = job
    generate('h3', {'prompt': 'fixture'}, job, render_stage='stage 1/2 · H3 draft')
    assert seen == ['stage 1/2 · H3 draft']


def test_admission_label_stays_when_admission_fails():
    import fcntl, re
    job = {'id': 'fixture', 'request': {}, 'stage': 'generating'}
    def refuse(eng, j):
        j['stage'] = 'reconciling qwen-h3 residency…'
        return 'busy'
    generate = function('_engine_generate_authorized', ENGINES={'h3': {'port': 1}},
                        preflight=lambda e, b: b, fcntl=fcntl,
                        http_json=lambda *a, **k: pytest.fail('no render after refusal'),
                        ensure_engine=refuse, H3_ENGINE_HTTP_TIMEOUT_S=100, re=re,
                        H3_MIN_FRAMES=5, H3_MAX_FRAMES=121, save_state=lambda: None,
                        hold_gpu_recovery=lambda *a: None, gpu_recovery_pending=lambda: False,
                        gpu_render_ready=lambda e, t: object(),
                        delegation_headers=lambda lease: {},
                        _render_stage_label=lambda s: s or 'generating')
    result = generate('h3', {'prompt': 'fixture'}, job)
    assert result['ok'] is False
