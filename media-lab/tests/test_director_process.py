"""Real HTTP/SIGTERM acceptance; synthetic transport, no model or user services."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from .test_director_adapter import locked


SERVER = r'''
import sys, time
from pathlib import Path
import uvicorn
from media_lab_core.director_adapter import LocalDirectorAdapter
from media_lab_core.director_transport import LocalDirectorTransport, DirectorUncertain
from media_lab_core.studio_server import create_app
root=Path(sys.argv[1]); mode=sys.argv[2]
class Transport(LocalDirectorTransport):
    def exchange_under_lease(self, messages):
        (root/'entered').touch()
        if mode=='uncertain':
            (root/'unavailable').touch()
            raise DirectorUncertain('synthetic transport disconnect')
        deadline=time.monotonic()+15
        while not (root/'release').exists():
            if time.monotonic()>deadline:raise DirectorUncertain('fixture deadline')
            time.sleep(.02)
        return 'Finished'
def idle():
    if (root/'unavailable').exists():raise OSError('synthetic metrics outage')
    return True
adapter=LocalDirectorAdapter(transport=Transport(port=8004,model='fixture'),
    lease_path=root/'inference.lock',runtime_idle=idle)
app=create_app(state_root=root/'state',artifact_root=root/'artifacts',
    authorize=lambda token:'a'*32 if token=='fixture' else None,director_reply=adapter)
uvicorn.run(app,fd=int(sys.argv[3]),log_level='info')
'''


def eventually(check, timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if check():return
        time.sleep(.03)
    assert check(), 'Fixture did not reach expected state before deadline'


@pytest.mark.skipif(os.name!='posix',reason='POSIX inference lease and signals')
@pytest.mark.parametrize('mode',['active','uncertain'])
def test_sigterm_waits_for_director_and_same_root_restarts(tmp_path,mode):
    root=tmp_path.resolve()
    (root/'state').mkdir();(root/'artifacts').mkdir()
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    headers={'Authorization':'Bearer fixture'}
    for attempt in range(2):
        with socket.socket() as listener, (root/f'server-{attempt}.log').open('w+') as log:
            listener.bind(('127.0.0.1',0));listener.listen()
            url=f'http://127.0.0.1:{listener.getsockname()[1]}'
            process=subprocess.Popen([sys.executable,'-c',SERVER,str(root),mode,str(listener.fileno())],
                pass_fds=(listener.fileno(),),env=env,stdout=log,stderr=subprocess.STDOUT)
            # The parent must not keep the listening descriptor alive after shutdown.
            listener.close()
            try:
                with httpx.Client(base_url=url,headers=headers,timeout=12,trust_env=False) as client:
                    def ready():
                        assert process.poll() is None, (root/f'server-{attempt}.log').read_text()
                        try:return client.get('/api/studio/jobs').status_code==200
                        except httpx.TransportError:return False
                    eventually(ready)
                    if attempt==0:
                        with ThreadPoolExecutor() as pool:
                            reply=pool.submit(client.post,'/api/studio/director',json={
                                'messages':[{'role':'user','content':'Fixture request'}]})
                            try:
                                eventually(lambda:(root/'entered').exists())
                                if mode=='uncertain':assert reply.result(timeout=5).status_code==503
                                assert locked(root/'inference.lock')
                                process.send_signal(signal.SIGTERM)
                                eventually(lambda: 'Shutting down' in (root/f'server-{attempt}.log').read_text())
                                time.sleep(.3)
                                assert process.poll() is None
                                assert locked(root/'inference.lock')
                            finally:
                                (root/'release').touch()
                                (root/'unavailable').unlink(missing_ok=True)
                            assert reply.result(timeout=5).status_code==(200 if mode=='active' else 503)
                    else:process.send_signal(signal.SIGTERM)
                    process.wait(timeout=8)
                    assert process.returncode in (0,-signal.SIGTERM)
                    assert not locked(root/'inference.lock')
                    assert 'Application shutdown complete.' in (root/f'server-{attempt}.log').read_text()
            finally:
                (root/'release').touch();(root/'unavailable').unlink(missing_ok=True)
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        # Only this test-owned synthetic child, never an inference service.
                        process.kill();process.wait(timeout=5)
