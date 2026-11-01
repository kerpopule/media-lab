import sys
import time
from pathlib import Path

import pytest
from media_lab_core import cut


@pytest.mark.parametrize('output', ['', 'print("out_time_us=1", flush=True)', 'sys.stderr.write("x"*200000);sys.stderr.flush()'])
def test_stalled_renderer_is_killed_reaped_and_partial_removed(tmp_path, monkeypatch, output):
    destination = tmp_path/'preview.mp4'
    destination.write_bytes(b'previous completed preview')
    code = 'import sys,time;from pathlib import Path;Path(sys.argv[-1]).write_bytes(b"partial");'+(output+';' if output else '')+'time.sleep(30)'
    monkeypatch.setattr(cut, 'plan_timeline_render', lambda *args, **kwargs: {
        'command':[sys.executable, '-c', code, str(kwargs['output'])], 'expected_seconds':1})
    original = cut.subprocess.Popen
    children = []
    def launch(*args, **kwargs):
        child = original(*args, **kwargs);children.append(child);return child
    monkeypatch.setattr(cut.subprocess, 'Popen', launch)
    started = time.monotonic()
    with pytest.raises(cut.CutError, match='timed out'):
        cut.render_timeline({}, media_dir=tmp_path, output=destination,
                            export_request={}, work_dir=tmp_path/'work', timeout_seconds=0.2,
                            progress=lambda _: None)
    assert time.monotonic()-started < 5
    assert len(children)==1 and children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed
    assert not list(tmp_path.glob('*.partial.mp4'))
    assert destination.read_bytes()==b'previous completed preview'


def test_failed_progress_callback_does_not_leave_renderer_running(tmp_path, monkeypatch):
    code='import time;print("out_time_us=1",flush=True);time.sleep(30)'
    monkeypatch.setattr(cut,'plan_timeline_render',lambda *args,**kwargs:{'command':[sys.executable,'-c',code,str(kwargs['output'])],'expected_seconds':1})
    def failed(_):raise RuntimeError('callback failed')
    started=time.monotonic()
    with pytest.raises(cut.CutError,match='progress reporting failed'):
        cut.render_timeline({},media_dir=tmp_path,output=tmp_path/'preview.mp4',export_request={},work_dir=tmp_path/'work',timeout_seconds=2,progress=failed)
    assert time.monotonic()-started<5
