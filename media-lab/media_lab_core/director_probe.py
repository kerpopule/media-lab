"""Bounded local vLLM identity/activity probe for director lease admission.

Only the reviewed vLLM scheduler contract is supported. Model IDs are checked,
not interpreted as weight hashes or installation/license qualification.
"""
import http.client
import json
import math
import re
import socket
import threading
import time

from .director_transport import LocalDirectorTransport

_NAMES = {'vllm:num_requests_running', 'vllm:num_requests_waiting'}
_NAME = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)')
_SAMPLE = re.compile(r'^(vllm:num_requests_(?:running|waiting))\{(.*)\}\s+(\S+)(?:\s+[+-]?\d+)?$')
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:\\.|[^"\\])*")(?:,|$)')


def exact_vllm_idle(raw: str, model: str) -> bool:
    seen=set()
    for line in raw.splitlines():
        line=line.strip()
        name=_NAME.match(line)
        if not name or name.group(1) not in _NAMES:
            continue
        sample=_SAMPLE.fullmatch(line)
        if not sample:
            return False
        labels={}
        position=0
        while position < len(sample[2]):
            label=_LABEL.match(sample[2],position)
            if not label or label[1] in labels:
                return False
            try:labels[label[1]]=json.loads(label[2])
            except ValueError:return False
            position=label.end()
        if labels.get('model_name') != model:
            return False
        try:value=float(sample[3])
        except ValueError:return False
        if not math.isfinite(value) or value != 0:
            return False
        seen.add(sample[1])
    return seen == _NAMES


class LocalVllmIdleProbe:
    def __init__(self, transport: LocalDirectorTransport):
        if not isinstance(transport,LocalDirectorTransport):
            raise ValueError('Use the director transport runtime selection.')
        self.transport=transport

    def _read(self,path,limit):
        connection=http.client.HTTPConnection('127.0.0.1',self.transport.port,timeout=2)
        sockets=[]
        expired=threading.Event()
        def expire():
            expired.set()
            for sock in sockets:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
        timer=threading.Timer(2,expire)
        timer.daemon=True
        deadline=time.monotonic()+2
        response=None
        timer.start()
        try:
            connection.connect()
            sockets.append(connection.sock)
            if expired.is_set():raise TimeoutError()
            connection.request('GET',path)
            response=connection.getresponse()
            if response.status!=200:raise ValueError('Runtime probe unavailable.')
            data=bytearray()
            while True:
                if expired.is_set() or time.monotonic()>=deadline:raise TimeoutError()
                chunk=response.read1(min(8192,limit+1-len(data)))
                if not chunk:break
                data.extend(chunk)
                if len(data)>limit:raise ValueError('Runtime probe too large.')
            if expired.is_set() or time.monotonic()>=deadline:raise TimeoutError()
            return data.decode('utf-8')
        finally:
            timer.cancel()
            if response is not None:response.close()
            connection.close()

    def _model_present(self):
        body=json.loads(self._read('/v1/models',65536))
        return any(row.get('id')==self.transport.model for row in body['data'])

    def __call__(self) -> bool:
        # Identity brackets the activity sample. The canonical lease must also
        # be respected by the runtime switcher; this is not a substitute for it.
        try:
            return (self._model_present() and
                    exact_vllm_idle(self._read('/metrics',1048576),self.transport.model) and
                    self._model_present())
        except Exception:
            return False
