"""CPU-only wrapper tests: fake heavyweight runtime, real concurrency."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class SolSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        env = {"SOL_PKG": base, "SOL_ROOT": base,
               "SOL_H3_SPARK_RUNTIME_ROOT": base, "SOL_OUT_DIR": base,
               "XDG_RUNTIME_DIR": base}
        self.environ = patch.dict(os.environ, env)
        self.environ.start(); self.addCleanup(self.environ.stop)
        config = types.ModuleType('runtime.config'); config.load_paths = lambda *a, **k: {}
        pipeline = types.ModuleType('runtime.pipeline'); pipeline.Pipeline = object
        modules = patch.dict(sys.modules, {'runtime.config': config, 'runtime.pipeline': pipeline})
        modules.start(); self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location('sol_under_test', ROOT/'runner/sol_engine_server.py')
        self.s = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.s)
        self.s.warm_case = lambda task: {'task': task}
        boot_id = '11111111-1111-4111-8111-111111111111'
        (Path(base)/'boot-id').write_text(boot_id)
        os.environ['SOL_BOOT_ID_PATH'] = str(Path(base)/'boot-id')
        (Path(base)/'boot-clearance.json').write_text(
            '{"boot_id":"' + boot_id + '","approved":true}')

    def test_new_boot_without_clearance_refuses_allocation(self):
        s = self.s; calls = []
        (Path(s.SOL_ROOT)/'boot-clearance.json').unlink()
        class Pipeline:
            def __init__(self, *a, **k): calls.append('new')
            def start(self, case): pass
        s.Pipeline = Pipeline
        with self.assertRaises(RuntimeError): s.ensure_pipeline('t2va')
        self.assertEqual(calls, [])

    def test_stale_or_malformed_boot_clearance_is_fail_closed(self):
        root = Path(self.s.SOL_ROOT)
        for content in ('{}', '[]', '{', '{"approved":true,"boot_id":"old"}',
                        '{"approved":1,"boot_id":"11111111-1111-4111-8111-111111111111"}'):
            with self.subTest(content=content):
                (root/'boot-clearance.json').write_text(content)
                self.assertFalse(self.s.boot_cleared())
                self.assertTrue(self.s.safety_latched())

    def test_simulated_reboot_invalidates_previously_valid_clearance(self):
        self.assertTrue(self.s.boot_cleared())
        (Path(self.s.SOL_ROOT)/'boot-id').write_text('22222222-2222-4222-8222-222222222222')
        self.assertFalse(self.s.boot_cleared())
        calls = []
        self.s.Pipeline = lambda *a, **k: calls.append('new')
        for _ in range(20):
            self.s.STATE.update(blocked=False, loaded=False, pipe=None)
            with self.assertRaises(RuntimeError): self.s.ensure_pipeline('t2va')
        self.assertEqual(calls, [])

    def test_unreadable_boot_identity_is_fail_closed(self):
        (Path(self.s.SOL_ROOT)/'boot-id').unlink()
        self.assertFalse(self.s.boot_cleared())

    def test_preload_and_request_cannot_construct_two_pipelines(self):
        s = self.s; entered = threading.Event(); release = threading.Event(); second_started = threading.Event()
        made = []; errors = []
        class Pipeline:
            def __init__(self, *a, **k): made.append(self)
            def start(self, case):
                entered.set()
                if not release.wait(3): raise RuntimeError('test release timeout')
        s.Pipeline = Pipeline
        def run(second=False):
            if second: second_started.set()
            try: s.ensure_pipeline('fl2va')
            except Exception as exc: errors.append(exc)
        first = threading.Thread(target=run); second = threading.Thread(target=run,args=(True,))
        first.start(); self.assertTrue(entered.wait(2)); second.start(); self.assertTrue(second_started.wait(2))
        # bounded join lets the unsafe second constructor run, without deadlocking the fixed loader
        second.join(.1); release.set(); first.join(3); second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(made), 1, 'preload and request both constructed GPU pipelines')

    def test_task_switch_closes_even_when_finish_has_no_successful_batch(self):
        s = self.s; calls = []
        class Old:
            def finish(self):
                calls.append('finish'); raise RuntimeError('No complete successful batch')
            def close(self): calls.append('close')
        class New:
            def __init__(self, *a, **k): calls.append('new')
            def start(self, case): calls.append('start')
        s.STATE.update(pipe=Old(), task='fl2va', loaded=True)
        s.Pipeline = New
        s.ensure_pipeline('t2va')
        self.assertEqual(calls, ['finish', 'close', 'new', 'start'])

    def test_failed_close_blocks_new_load(self):
        s=self.s; calls=[]
        class Old:
            def finish(self): pass
            def close(self): raise RuntimeError('worker still alive')
        s.STATE.update(pipe=Old(), task='fl2va', loaded=True)
        s.Pipeline=lambda *a, **k: calls.append('new')
        with self.assertRaises(RuntimeError): s.ensure_pipeline('t2va')
        self.assertEqual(calls, [])
        self.assertTrue(s.STATE.get('blocked'))

    def test_failed_start_is_closed_and_latched(self):
        s=self.s; calls=[]
        class Partial:
            def __init__(self,*a,**k): calls.append('new')
            def start(self,case): raise RuntimeError('allocation failed')
            def close(self): calls.append('close')
        s.Pipeline=Partial
        with self.assertRaises(RuntimeError): s.ensure_pipeline('fl2va')
        with self.assertRaises(RuntimeError): s.ensure_pipeline('fl2va')
        self.assertEqual(calls,['new','close'])
        self.assertFalse(s.STATE['loaded'])
        self.assertTrue((Path(s.SOL_ROOT)/'safety-stop.json').is_file())

    def test_memwatch_latch_prevents_preload(self):
        s=self.s; calls=[]
        runtime=Path(s.SOL_ROOT)/'xdg';runtime.mkdir()
        (runtime/'flashnext-memwatch.latch').write_text('previous stop')
        s.Pipeline=lambda *a,**k: calls.append('new')
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR':str(runtime)}):
            with self.assertRaises(RuntimeError): s.ensure_pipeline('fl2va')
        self.assertEqual(calls,[])

    def test_surviving_worker_prevents_replacement(self):
        s=self.s; calls=[]
        class Old:
            stage1=types.SimpleNamespace(process=types.SimpleNamespace(pid=987654321,poll=lambda:None))
            def finish(self): pass
            def close(self): calls.append('close')
        s.STATE.update(pipe=Old(),task='fl2va',loaded=True)
        s.Pipeline=lambda *a,**k: calls.append('new')
        with self.assertRaises(RuntimeError): s.ensure_pipeline('t2va')
        self.assertEqual(calls,['close'])

    def test_health_reports_loading_as_busy(self):
        s=self.s; reply=[]
        handler=s.H.__new__(s.H);handler.path='/health'
        handler._send=lambda status, data: reply.append((status,data))
        s.STATE['loading']=True
        handler.do_GET()
        self.assertTrue(reply[0][1]['busy'])
        self.assertTrue(reply[0][1]['loading'])

    def test_health_fails_when_persistent_breaker_exists(self):
        s=self.s;reply=[]
        (Path(s.SOL_ROOT)/'safety-stop.json').write_text('{}')
        handler=s.H.__new__(s.H);handler.path='/health'
        handler._send=lambda status,data:reply.append((status,data))
        handler.do_GET()
        self.assertEqual(reply[0][0],503)
        self.assertFalse(reply[0][1]['ok'])

    def test_bounded_retries_do_not_reload_after_failure_or_restart(self):
        s=self.s; calls=[]
        class Failed:
            def __init__(self,*a,**k):calls.append('new')
            def start(self,case):raise RuntimeError('allocation failed')
            def close(self):calls.append('close')
        s.Pipeline=Failed
        for _ in range(10):
            with self.assertRaises(RuntimeError):s.ensure_pipeline('t2va')
        s.STATE.update(blocked=False,pipe=None,loaded=False)  # fresh process state
        for _ in range(10):
            with self.assertRaises(RuntimeError):s.ensure_pipeline('t2va')
        self.assertEqual(calls,['new','close'])

    def test_generation_failure_trips_circuit(self):
        import io,json
        s=self.s;reply=[]
        class Failed:
            def generate(self,case):raise RuntimeError('CUDA allocation failure')
        s.STATE.update(pipe=Failed(),task='t2va',loaded=True)
        handler=s.H.__new__(s.H);handler.path='/generate'
        raw=json.dumps({'prompt':'fixture','request_id':'fixture'}).encode()
        handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw)
        handler._send=lambda status,data:reply.append((status,data))
        with patch.object(s, 'authorize_values', return_value=True):
            handler.do_POST()
        self.assertEqual(reply[0][0],500)
        self.assertTrue(s.safety_latched())
        self.assertFalse(s.STATE['busy'])

    def _generate_ok(self, stage2):
        import io,json
        s=self.s;reply=[];out=Path(self.tmp.name)/'take.mp4';out.write_bytes(b'mp4')
        class Done:
            def __init__(self): self.stage2=stage2
            def generate(self,case): return {'output': str(out)}
        s.STATE.update(pipe=Done(),task='t2va',loaded=True)
        handler=s.H.__new__(s.H);handler.path='/generate'
        raw=json.dumps({'prompt':'fixture','request_id':'fixture-ok'}).encode()
        handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw)
        handler._send=lambda status,data:reply.append((status,data))
        with patch.object(s, 'authorize_values', return_value=True):
            handler.do_POST()
        return reply

    def test_finished_take_trims_stage2_cache_before_the_next_admission(self):
        calls=[]
        class Stage2:
            def call(self, op, **kw): calls.append((op, kw)); return {'status':'PASS'}
        reply=self._generate_ok(Stage2())
        self.assertEqual(reply[0][0],200)
        self.assertEqual(calls,[('release_idle_cache',{'qwen_resident':True})])
        self.assertFalse(self.s.safety_latched())

    def test_failed_idle_trim_is_not_a_safety_event(self):
        class Stage2:
            def call(self, op, **kw): raise RuntimeError('cache release requires an idle session')
        reply=self._generate_ok(Stage2())
        self.assertEqual(reply[0][0],200)
        self.assertFalse(self.s.safety_latched())

    def test_generation_without_exact_gpu_lease_is_rejected(self):
        import io,json
        s=self.s;reply=[]
        handler=s.H.__new__(s.H);handler.path='/generate'
        raw=json.dumps({'prompt':'fixture','request_id':'fixture'}).encode()
        handler.headers={'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw)
        handler._send=lambda status,data:reply.append((status,data))
        handler.do_POST()
        self.assertEqual(reply[0][0],403)
        self.assertIn('GPU lease', reply[0][1]['error'])

    def test_surviving_child_process_group_blocks_replacement(self):
        s=self.s
        process=types.SimpleNamespace(pid=987654321,poll=lambda:0)
        old=types.SimpleNamespace(stage1=types.SimpleNamespace(process=process),close=lambda:None)
        with patch.object(s.os,'killpg',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'process group survived'):
                s.close_pipeline(old)

    def test_exited_worker_and_group_allow_close(self):
        s=self.s
        process=types.SimpleNamespace(pid=987654321,poll=lambda:0)
        old=types.SimpleNamespace(stage1=types.SimpleNamespace(process=process),close=lambda:None)
        with patch.object(s.os,'killpg',side_effect=ProcessLookupError):
            s.close_pipeline(old)

if __name__ == '__main__': unittest.main()
