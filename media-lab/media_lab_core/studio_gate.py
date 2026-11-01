"""Scoped pairing for the independent development host.

Host configuration owns the secret and access code; this module neither mints
configuration nor reads legacy credentials. Run one application process: the
bounded pairing admission window is process-local, not a distributed limiter.
"""
from collections import deque
from dataclasses import dataclass, field
import hmac
import re
import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import studio_jobs, studio_library, studio_editing


class PairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code: str = Field(min_length=1, max_length=128)
    studio_library: bool = False
    studio_render: bool = False
    studio_edit: bool = False
    studio_device: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


@dataclass(frozen=True)
class Credentials:
    secret: str = field(repr=False)
    code: str = field(repr=False)

    def __post_init__(self):
        # Configuration should use independent secrets.token_hex(32) values.
        # No short human-chosen code is accepted by this development endpoint.
        if not re.fullmatch(r'[a-fA-F0-9]{64}', self.secret):
            raise ValueError('Use a separate 32-byte hexadecimal signing secret.')
        if not re.fullmatch(r'[a-fA-F0-9]{64}', self.code):
            raise ValueError('Use a 32-byte hexadecimal pairing code.')
        if self.secret.lower() == self.code.lower():
            raise ValueError('The signing secret and pairing code must differ.')

    def role_code(self, role):
        return self.code.upper() if role == 'user' else ''

    def authorize(self, token):
        # This host issues no admin tokens, including tokens signed elsewhere
        # with an empty role code.
        if not token.startswith('mlab-render-v1.user.'):
            return None
        return studio_jobs.identity(token, self.secret, self.role_code)

    def authorize_editing(self, token):
        return studio_editing.identity(token, self.secret, self.role_code('user'))

    def authorize_library(self, token):
        return token.startswith('mlab-library-v1.user.') and studio_library.valid_ticket(
            token, self.secret, self.role_code)


def router(credentials: Credentials, *, library_available: bool, editing_available: bool = False, clock=time.monotonic):
    api = APIRouter()
    attempts = deque(maxlen=120)
    lock = threading.Lock()

    @api.post('/api/gate')
    def pair(body: PairRequest):
        if not body.studio_library and not body.studio_render and not body.studio_edit:
            raise HTTPException(422, 'Choose Library, generation or editing permission.')
        if (body.studio_render or body.studio_edit) and body.studio_device is None:
            raise HTTPException(422, 'A device identity is required for generation or editing permission.')
        if body.studio_library and not library_available:
            raise HTTPException(409, 'This host has no Library configured.')
        if body.studio_edit and not editing_available:
            raise HTTPException(409, 'This host has no editor configured.')
        with lock:
            now = clock()
            while attempts and now - attempts[0] >= 60:
                attempts.popleft()
            if len(attempts) == attempts.maxlen:
                raise HTTPException(429, 'Pairing is busy. Try again in one minute.',
                                    headers={'Retry-After': '60'})
            attempts.append(now)
        if not hmac.compare_digest(body.code.strip().upper().encode(), credentials.code.upper().encode()):
            raise HTTPException(403, 'The access code is incorrect.')
        result = {'ok': True}
        if body.studio_library:
            result.update(scope='library:read', token=studio_library.ticket(
                credentials.secret, 'user', credentials.role_code('user')), expiresIn=studio_library.TOKEN_AGE)
        if body.studio_render:
            token = studio_jobs.ticket(credentials.secret, 'user', credentials.role_code('user'), body.studio_device)
            if body.studio_library:
                result.update(renderScope='jobs:own', renderToken=token, renderExpiresIn=studio_jobs.TOKEN_AGE)
            else:
                result.update(scope='jobs:own', token=token, expiresIn=studio_jobs.TOKEN_AGE)
        if body.studio_edit:
            result.update(editScope='editing:own', editToken=studio_editing.ticket(
                credentials.secret, credentials.role_code('user'), body.studio_device),
                editExpiresIn=studio_editing.TOKEN_AGE)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    return api
