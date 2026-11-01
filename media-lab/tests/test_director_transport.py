import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager

import pytest
from media_lab_core.director_transport import LocalDirectorTransport, DirectorUncertain

MODEL = 'exact-test-revision'
MESSAGES = [{'role':'user','content':'Plan a game.'}]
REPLY = {'model':MODEL,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Use the victory video.'}}]}


@contextmanager
def runtime(responder):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            try:
                responder(self)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:yield server.server_port,calls
    finally:server.shutdown();server.server_close();thread.join(2)


def respond(handler, value=REPLY, status=200):
    body=json.dumps(value).encode()
    handler.send_response(status)
    handler.send_header('Content-Length',str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_exact_loopback_request_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY','http://invalid.example:1')
    with runtime(respond) as (port,calls):
        assert LocalDirectorTransport(port=port,model=MODEL).exchange_under_lease(MESSAGES)=='Use the victory video.'
    assert calls[0][0]=='/v1/chat/completions'
    assert calls[0][1]=={'model':MODEL,'messages':MESSAGES,'stream':False,'temperature':0,'max_tokens':1024,'chat_template_kwargs':{'enable_thinking':False}}


@pytest.mark.parametrize('value', [
    {**REPLY,'model':'substitute'},
    {**REPLY,'choices':[{'finish_reason':'length','message':{'role':'assistant','content':'partial'}}]},
    {**REPLY,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Done','tool_calls':[{}]}}]},
    {**REPLY,'choices':[]},
    {**REPLY,'padding':'x'*131073},
])
def test_unverified_response_signals_lease_uncertainty(value):
    with runtime(lambda handler:respond(handler,value)) as (port,_):
        with pytest.raises(DirectorUncertain,match='retain the inference lease'):
            LocalDirectorTransport(port=port,model=MODEL).exchange_under_lease(MESSAGES)


def test_redirect_never_followed():
    with runtime(lambda handler:respond(handler,{},302)) as (port,calls):
        with pytest.raises(DirectorUncertain):
            LocalDirectorTransport(port=port,model=MODEL).exchange_under_lease(MESSAGES)
        assert len(calls)==1


def test_deadline_interrupts_stalled_response_headers():
    release=threading.Event()
    def stall(handler):
        release.wait(2)
        respond(handler)
    with runtime(stall) as (port,_):
        started=time.monotonic()
        try:
            with pytest.raises(DirectorUncertain):
                LocalDirectorTransport(port=port,model=MODEL,timeout=.15).exchange_under_lease(MESSAGES)
            assert time.monotonic()-started<1
        finally:release.set()


def test_configuration_and_oversized_messages_refused_before_submission():
    for kwargs in ({'port':True,'model':MODEL},{'port':8004,'model':'media-lab-text'}, {'port':8004,'model':MODEL,'timeout':float('nan')}):
        with pytest.raises(ValueError):LocalDirectorTransport(**kwargs)
    with runtime(respond) as (port,calls):
        with pytest.raises(ValueError):LocalDirectorTransport(port=port,model=MODEL).exchange_under_lease([{'role':'user','content':'x'*100000}])
        assert not calls


def test_deadline_interrupts_stalled_body_after_headers():
    release=threading.Event()
    def stall(handler):
        handler.send_response(200)
        handler.send_header('Content-Length','1000')
        handler.end_headers()
        handler.wfile.write(b'{')
        handler.wfile.flush()
        release.wait(2)
    with runtime(stall) as (port,_):
        started=time.monotonic()
        try:
            with pytest.raises(DirectorUncertain):
                LocalDirectorTransport(port=port,model=MODEL,timeout=.15).exchange_under_lease(MESSAGES)
            assert time.monotonic()-started<1
        finally:release.set()
