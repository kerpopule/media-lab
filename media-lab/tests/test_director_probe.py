import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading

import pytest
from media_lab_core.director_probe import exact_vllm_idle,LocalVllmIdleProbe
from media_lab_core.director_transport import LocalDirectorTransport

MODEL='exact-model'
IDLE='vllm:num_requests_running{engine="0",model_name="exact-model"} 0\nvllm:num_requests_waiting{engine="0",model_name="exact-model"} 0\n'


def test_both_exact_model_gauge_families_required():
    assert exact_vllm_idle(IDLE,MODEL)
    assert not exact_vllm_idle(IDLE,'different')
    assert not exact_vllm_idle(IDLE.splitlines()[0],MODEL)
    assert not exact_vllm_idle(IDLE.replace(' 0\n',' 1\n'),MODEL)
    assert not exact_vllm_idle('unrelated 0',MODEL)


@pytest.mark.parametrize('sample',[
    'vllm:num_requests_running{model_name="exact-model"} NaN',
    'vllm:num_requests_running{model_name="exact-model"} 0garbage',
    'vllm:num_requests_running{model_name="exact-model"} -1',
    'vllm:num_requests_running{model_name="other"} 0',
    'vllm:num_requests_running{engine="0"} 0',
    'vllm:num_requests_running{model_name="exact-model",model_name="exact-model"} 0',
    'vllm:num_requests_running{model_name="broken} 0',
])
def test_bad_samples_cannot_be_masked_by_valid_zeroes(sample):
    assert not exact_vllm_idle(IDLE+sample,MODEL)


def test_actual_http_probe_brackets_metrics_with_identity_and_refuses_switch(monkeypatch):
    state={'models':0,'switch':False,'paths':[]}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['paths'].append(self.path)
            if self.path=='/v1/models':
                state['models']+=1
                model='other' if state['switch'] and state['models']%2==0 else MODEL
                body=json.dumps({'data':[{'id':model}]}).encode()
            else:body=IDLE.encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('HTTP_PROXY','http://invalid.example:1')
    try:
        probe=LocalVllmIdleProbe(LocalDirectorTransport(port=server.server_port,model=MODEL))
        assert probe() is True
        assert state['paths']==['/v1/models','/metrics','/v1/models']
        state['switch']=True
        assert probe() is False
    finally:server.shutdown();server.server_close();thread.join(2)


def test_unresponsive_runtime_is_unknown_with_bounded_probe_time():
    import time
    release=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):release.wait(4)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        probe=LocalVllmIdleProbe(LocalDirectorTransport(port=server.server_port,model=MODEL))
        started=time.monotonic()
        assert probe() is False
        assert time.monotonic()-started<3
    finally:release.set();server.shutdown();server.server_close();thread.join(2)
