"""The local tool token: how scripts on the studio machine get past the door.

The studio used to let anything whose ``Host`` header said ``localhost`` (or
the bind / tailnet address) walk in without a code. Any client that could reach
the socket could simply send that header, so that trust is gone. The socket
address is no better: cloudflared and ``tailscale serve`` connect from this
very machine, so every public visitor would look local.

Tools that run ON the studio machine -- the queue watchdog, the deploy script's
queue probe, the verification and refinement runners, the Cut CLI -- prove they
are local the only way that cannot be faked from the network: by reading
``local-token.txt`` under the data root (mode 0600, created by the server at
start) and sending it in the ``X-Media-Lab-Local`` header. Reading that file
needs the same access as reading the door codes themselves, so it grants
nothing new. It carries the FAMILY permission set, never admin.

The token is only ever attached to requests for this machine's own studio:
one of its own addresses (loopback, the bind address, the tailnet address)
AND the studio's port. Other services on the same box are not the studio --
the text-model port, for one, is a bridge that forwards every request header
to another machine -- so a runner that also talks to them must not hand them
the token. Standard library only.
"""
from __future__ import annotations

import hmac
import http.cookiejar
import ipaddress
import json
import os
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

from . import local_config, secret_files

HEADER = "X-Media-Lab-Local"
FILE_NAME = "local-token.txt"
DEFAULT_PORT = 7863                 # the studio's port unless install.sh chose another
INSTALL_CONFIG = "install.json"     # where install.sh / `media-lab config` record it


def token_path(root: Path | None = None) -> Path:
    return Path(root if root is not None else local_config.home()) / FILE_NAME


def ensure(root: Path | None = None) -> str:
    """Create the token (0600) if missing, tighten its mode, and return it."""
    path = token_path(root)
    secret_files.ensure(path, lambda: secrets.token_hex(32) + "\n")
    secret_files.tighten([path])
    return path.read_text(encoding="utf-8").strip()


def read(root: Path | None = None) -> str:
    """The token, or "" when this process cannot read it (not the studio box)."""
    try:
        return token_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time check; an empty expected token never matches anything."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8", "replace"),
                               expected.encode("utf-8", "replace"))


def own_host(host: str) -> bool:
    """Is ``host`` one of THIS machine's studio addresses?"""
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    return host in local_config.own_addresses()


def studio_ports(root: Path | None = None) -> set[int]:
    """The port(s) this machine's studio answers on: 7863, the port install.sh
    recorded in install.json, and ``MEDIA_LAB_PORT`` when it is set."""
    ports = {DEFAULT_PORT}
    try:
        cfg = json.loads((token_path(root).parent / INSTALL_CONFIG).read_text(encoding="utf-8"))
        port = str(cfg.get("port", "")) if isinstance(cfg, dict) else ""
        if port.isdigit():
            ports.add(int(port))
    except (OSError, ValueError):
        pass
    env = os.environ.get("MEDIA_LAB_PORT", "").strip()
    if env.isdigit():
        ports.add(int(env))
    return ports


def headers_for(url: str, root: Path | None = None) -> dict[str, str]:
    """``{HEADER: token}`` when ``url`` is this machine's studio, else ``{}``.

    This machine's studio means: plain http(s), one of this machine's own
    addresses, the studio's port, and no user-info in the address (URL parsers
    disagree about ``a\\@b``; this one refuses to guess)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return {}
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or "\\" in parts.netloc:
        return {}
    if not own_host(parts.hostname or "") or port not in studio_ports(root):
        return {}
    token = read(root)
    return {HEADER: token} if token else {}


def authorize(request: urllib.request.Request, root: Path | None = None) -> urllib.request.Request:
    """Attach the token to a urllib Request aimed at this machine's studio."""
    for key, value in headers_for(request.full_url, root).items():
        request.add_unredirected_header(key, value)
    return request


class StudioAuthHandler(urllib.request.BaseHandler):
    """urllib handler: every request to this machine's studio carries the token."""

    handler_order = 400

    def __init__(self, root: Path | None = None):
        self.root = root

    def http_request(self, request):
        return authorize(request, self.root)

    https_request = http_request


def install(root: Path | None = None) -> None:
    """Make plain ``urllib.request.urlopen`` calls in a runner script carry the
    token to the local studio (and to nothing else)."""
    urllib.request.install_opener(urllib.request.build_opener(StudioAuthHandler(root)))


def studio_opener(base_url: str, code: str | None = None, root: Path | None = None):
    """An opener for the studio API from a tool: on the studio machine it carries
    the local token; anywhere else it signs in once with ``code`` (the family
    code, e.g. from MEDIA_LAB_CODE) and keeps the session cookie."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         StudioAuthHandler(root))
    if code:
        request = urllib.request.Request(
            base_url.rstrip("/") + "/api/gate", method="POST",
            data=json.dumps({"code": code}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=20) as response:
            if json.loads(response.read().decode("utf-8") or "{}").get("ok") is not True:
                raise PermissionError("the studio refused that code")
    return opener
