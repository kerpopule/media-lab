"""Local OpenAI-compatible transport, to be called while holding the host lease.

This is not a configured director adapter. The caller owns canonical inference
exclusion and must retain/drain it after DirectorUncertain: an HTTP failure does
not prove the inference stopped. No service discovery, model swap or fallback.
"""
import http.client
import json
import math
import re
import socket
import threading
import time


class DirectorUncertain(RuntimeError):
    """A submitted request has no verified terminal response; retain its lease."""


class LocalDirectorTransport:
    def __init__(self, *, port: int, model: str, timeout: float = 45, max_tokens: int = 1024):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Choose a local runtime port.')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,255}', model):
            raise ValueError('Choose an explicit runtime model identifier.')
        if model in ('media-lab-text', 'default', 'auto'):
            raise ValueError('Mutable model aliases are not supported.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 50:
            raise ValueError('Choose a deadline of at most 50 seconds.')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ValueError('Choose a bounded reply length.')
        self.port, self.model, self.timeout, self.max_tokens = port, model, timeout, max_tokens

    def exchange_under_lease(self, messages: list[dict]) -> str:
        """Return a terminal plain-text reply, or signal uncertain upstream work.

        Uses only literal IPv4 loopback. HTTP redirects and environment proxies
        cannot change the destination. Exact model IDs are checked in responses;
        IDs alone are not proof of immutable weights or license qualification.
        """
        if not isinstance(messages, list) or not 1 <= len(messages) <= 24:
            raise ValueError('Supply a bounded conversation.')
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {'role', 'content'} or
                message['role'] not in ('system', 'user', 'assistant') or
                not isinstance(message['content'], str) or not message['content'].strip()):
                raise ValueError('Supply plain conversation messages only.')
        body = json.dumps({'model': self.model, 'messages': messages, 'stream': False,
            'temperature': 0, 'max_tokens': self.max_tokens,
            'chat_template_kwargs': {'enable_thinking': False}}, ensure_ascii=False).encode()
        if len(body) > 98304:
            raise ValueError('The director conversation is too large.')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        sockets = []
        expired = threading.Event()
        def expire():
            expired.set()
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        deadline = time.monotonic() + self.timeout
        timer.start()
        response = None
        try:
            connection.connect()
            sockets.append(connection.sock)
            if expired.is_set():
                raise TimeoutError()
            connection.request('POST', '/v1/chat/completions', body,
                               {'Content-Type': 'application/json', 'Accept': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('Runtime did not return a reply.')
            # read1 returns after available data, allowing a cumulative deadline
            # check even when a peer trickles the response body.
            data = bytearray()
            while True:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = response.read1(min(8192, 131073-len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 131072:
                    raise ValueError('Runtime response is too large.')
            result = json.loads(data)
            choices = result.get('choices')
            if result.get('model') != self.model or not isinstance(choices, list) or len(choices) != 1:
                raise ValueError('Runtime model or response changed.')
            choice = choices[0]
            message = choice.get('message', {})
            content = message.get('content')
            if (choice.get('finish_reason') != 'stop' or message.get('role') != 'assistant' or
                message.get('tool_calls') or message.get('function_call') or
                not isinstance(content, str) or not content.strip() or len(content) > 16000):
                raise ValueError('Runtime did not return a complete plain reply.')
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError()
            return content
        except Exception:
            # Never expose upstream bodies, messages, or private diagnostics.
            raise DirectorUncertain('Local director reply was not verified; retain the inference lease until the runtime is idle.') from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
