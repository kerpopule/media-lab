"""Explicit administrator enrollment and same-origin sessions for the independent host.

The local owner enrolls an administrator code with the CLI; it is stored only as
a salted PBKDF2 hash in a private host-owned file, separate from device pairing
credentials. Sessions live in this process, expire, and are revoked by bumping
the file's generation, so a Library ticket, pairing code or render token can
never be promoted into model-installation permission.
"""
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

ADMIN_FILE = 'admin.json'
COOKIE = 'vibex_setup_session'
COOKIE_PATH = '/api/setup'
REQUEST_HEADER = 'x-setup-request'
ITERATIONS = 600_000
MAX_AGE = 8 * 3600
IDLE_AGE = 3600
ATTEMPT_LIMIT = 10
ATTEMPT_WINDOW = 600


def _hash(code, salt):
    return hashlib.pbkdf2_hmac('sha256', code.strip().lower().encode(), bytes.fromhex(salt), ITERATIONS).hex()


def _write(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('The administrator file must not be a symbolic link.')
    fd, temporary = tempfile.mkstemp(prefix='.admin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(fd, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_admin(root):
    """Load the enrollment file, refusing shared, foreign or oversized files."""
    if os.name != 'posix':
        raise ValueError('Independent host administrator storage is not qualified on this platform.')
    fd = os.open(Path(root)/ADMIN_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('The administrator file must be a private regular file owned by this user.')
        data = json.load(stream)
    if (not isinstance(data, dict) or data.get('version') != 1 or set(data) != {'version', 'salt', 'code_hash', 'generation'}
            or not isinstance(data['generation'], int) or data['generation'] < 1
            or not isinstance(data['salt'], str) or len(data['salt']) != 32
            or not isinstance(data['code_hash'], str) or len(data['code_hash']) != 64):
        raise ValueError('Invalid administrator enrollment.')
    return data


def enroll(root, *, rotate=False):
    """Create (or rotate) the administrator code; returns it exactly once."""
    if os.name != 'posix':
        raise ValueError('Independent host administrator storage is not qualified on this platform.')
    root = Path(root)
    path = root/ADMIN_FILE
    if not (root/'credentials.json').is_file():
        raise ValueError('Initialize the independent host before enrolling an administrator.')
    generation = 1
    if path.exists() or path.is_symlink():
        if not rotate:
            raise FileExistsError(path)
        generation = read_admin(root)['generation'] + 1
    code = secrets.token_hex(32)
    salt = secrets.token_hex(16)
    _write(path, {'version': 1, 'salt': salt, 'code_hash': _hash(code, salt), 'generation': generation})
    return code


def revoke(root):
    """Invalidate every administrator session on every process reading this host."""
    data = read_admin(root)
    data['generation'] += 1
    _write(Path(root)/ADMIN_FILE, data)
    return data['generation']


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code: str = Field(min_length=1, max_length=128)


class AdminGate:
    """Process-local administrator sessions bound to the host's enrollment file."""

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.clock = clock if clock is not None else (lambda: time.monotonic())
        self.lock = threading.Lock()
        self.sessions = {}
        self.attempts = deque(maxlen=ATTEMPT_LIMIT)
        read_admin(self.root)  # Refuse to serve without an enrolled administrator.

    def _generation(self):
        try:
            return read_admin(self.root)['generation']
        except (OSError, ValueError):
            return None

    def _session(self, request):
        token = request.cookies.get(COOKIE, '')
        if not token or len(token) > 128:
            return None
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            now = self.clock()
            if now - session['issued'] > MAX_AGE or now - session['last'] > IDLE_AGE:
                del self.sessions[token]
                return None
            generation = self._generation()
            if generation is None or generation != session['generation']:
                self.sessions.clear()
                return None
            session['last'] = now
            return session

    @staticmethod
    def same_origin(request):
        """Reject cross-site writes even if a browser ignored SameSite."""
        if request.headers.get(REQUEST_HEADER) != '1':
            return False
        site = request.headers.get('sec-fetch-site')
        if site not in (None, 'same-origin', 'none'):
            return False
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            host = request.headers.get('host', '')
            if parsed.scheme != request.url.scheme or parsed.netloc.lower() != host.lower():
                return False
        return True

    def authorized(self, request):
        session = self._session(request)
        if session is None:
            return False
        if request.method != 'GET' and not self.same_origin(request):
            return False
        return True

    def login(self, code, request):
        with self.lock:
            now = self.clock()
            while self.attempts and now - self.attempts[0] >= ATTEMPT_WINDOW:
                self.attempts.popleft()
            if len(self.attempts) == self.attempts.maxlen:
                raise HTTPException(429, 'Too many sign-in attempts. Try again in ten minutes.',
                                    headers={'Retry-After': str(ATTEMPT_WINDOW)})
            self.attempts.append(now)
        data = read_admin(self.root)
        if not hmac.compare_digest(_hash(code, data['salt']).encode(), data['code_hash'].encode()):
            raise HTTPException(403, 'The administrator code is incorrect.')
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.attempts.clear()
            now = self.clock()
            self.sessions = {key: value for key, value in self.sessions.items()
                             if now - value['issued'] <= MAX_AGE and now - value['last'] <= IDLE_AGE}
            self.sessions[token] = {'issued': now, 'last': now, 'generation': data['generation']}
        return token

    def logout(self, request):
        token = request.cookies.get(COOKIE, '')
        with self.lock:
            self.sessions.pop(token, None)

    def router(self):
        api = APIRouter()
        gate = self

        def cookie(response, request, token, age):
            response.set_cookie(COOKIE, token, max_age=age, path=COOKIE_PATH, httponly=True,
                                samesite='strict', secure=request.url.scheme == 'https')
            response.headers['Cache-Control'] = 'no-store'

        @api.get('/api/setup/session')
        def inspect(request: Request, response: Response):
            session = gate._session(request)
            response.headers['Cache-Control'] = 'no-store'
            if session is None:
                return {'signedIn': False}
            return {'signedIn': True, 'expiresIn': int(MAX_AGE - (gate.clock() - session['issued']))}

        @api.post('/api/setup/session')
        def sign_in(body: Login, request: Request, response: Response):
            if not gate.same_origin(request):
                raise HTTPException(403, 'Sign in from the server setup page itself.')
            token = gate.login(body.code, request)
            cookie(response, request, token, MAX_AGE)
            return {'signedIn': True, 'expiresIn': MAX_AGE}

        @api.delete('/api/setup/session')
        def sign_out(request: Request, response: Response):
            if not gate.same_origin(request):
                raise HTTPException(403, 'Sign out from the server setup page itself.')
            gate.logout(request)
            cookie(response, request, '', 0)
            return {'signedIn': False}

        return api
