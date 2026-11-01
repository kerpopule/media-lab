"""Media Lab inside VibeX Studio: the door for the embedded studio.

The app shows this studio inside itself: an iframe on web and desktop, a
WebView on phones. Two rules shape how that frame signs in:

* A cross-site frame is sent no SameSite=Lax cookie, so the studio's normal
  sign-in cookie does not reach it.
* A long-lived pass must never travel in a URL (history, logs, Referer).

So the frame signs in through a handshake, and nothing secret is in any URL:

1. The app loads ``/embed?next=/...`` in the frame. ``/embed`` is a small page
   that holds no secret.
2. If the frame is not signed in yet, ``/embed`` asks its parent for a ticket:
   ``postMessage`` to ``window.parent`` (web/desktop) or the native bridge
   (phone app).
3. The app mints the ticket with the device pass it got when it was paired:
   ``POST /api/embed/ticket`` with ``Authorization: Bearer <render pass>``. The
   ticket works once, lives 60 seconds and is bound to the app origin the
   browser reported in its ``Origin`` header.
4. The app posts the ticket into the frame, addressed to the studio's exact
   origin. The frame checks the message came from its own parent and from an
   allowed app origin, then redeems it same-origin: ``POST /api/embed/redeem``.
5. The server checks the ticket against that parent origin and sets a short
   embed cookie. Over HTTPS it is ``SameSite=None; Secure; Partitioned`` (a
   CHIPS cookie that exists only under the app's own top-level site); over
   plain HTTP it can only be ``SameSite=Lax``, which works when the app and the
   studio share a site (for example two localhost ports) or in the phone
   WebView, where the studio is the top-level page.
6. The embed cookie counts only on requests the studio's own page made
   (``Sec-Fetch-Site: same-origin`` or a user-initiated ``none``), so no other
   site can ride on it.

Framing itself is limited by ``Content-Security-Policy: frame-ancestors``:
``'self'``, the desktop app origins, ``MEDIA_LAB_BROWSER_ORIGINS`` from
config/local.env, and loopback dev servers only when the studio itself is being
reached over loopback. ``X-Frame-Options: SAMEORIGIN`` is the fallback for
browsers that predate frame-ancestors (modern browsers ignore it when
frame-ancestors is present).

Nothing here trusts a client IP, and nothing here names a machine: allowed app
origins come from config/local.env.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Callable, Iterable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from media_lab_core import studio_jobs

COOKIE = "mlab_embed"
TICKET_TTL = 60                    # seconds a ticket may wait before redeem
COOKIE_MAX_AGE = 24 * 3600         # the embed session; the app re-handshakes
RENEW_AFTER = 12 * 3600            # past this age /embed asks for a fresh ticket
NATIVE_PARENT = "vibex-native-app"  # the phone app's WebView bridge (no web origin)
TICKET_PATH = "/api/embed/ticket"
BRIDGE_PATHS = frozenset({TICKET_PATH})
GATE_EXEMPT_PATHS = frozenset({"/embed", "/api/embed/redeem", "/api/embed/status"})
DEFAULT_NEXT = "/?embed=1"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
# CSP host-sources cannot spell an IPv6 literal, so [::1] dev servers are not listed.
LOOPBACK_ANCESTORS = ("http://localhost:*", "http://127.0.0.1:*")
_LOOPBACK_PARENT = re.compile(r"http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?")
# An origin is scheme://host[:port] with nothing a CSP header could be split on.
_ORIGIN = re.compile(r"[a-z][a-z0-9+.-]{0,31}://[a-z0-9._~-]{1,253}(?::\d{1,5})?|"
                     r"[a-z][a-z0-9+.-]{0,31}://\[[0-9a-f:.]{2,45}\](?::\d{1,5})?")
_TICKET = re.compile(r"(mlab-embed-ticket-v1\.(user|admin)\.(\d{10,12})\.([0-9a-f]{32})\."
                     r"(n|o[A-Za-z0-9_-]{4,700}))\.([0-9a-f]{64})")
_SESSION = re.compile(r"(e1\.(user|admin)\.(\d{10,12}))\.([0-9a-f]{64})")
_RENDER_PASS = re.compile(r"Bearer (mlab-render-v1\.(user|admin)\.\d{10,12}\.[a-f0-9]{32}\.[a-f0-9]{64})")

RoleCode = Callable[[str], str]


class EmbedRefused(Exception):
    """A ticket or parent the embed door will not accept (the message is safe to show)."""


# ---------------------------------------------------------------------------
# origins and framing
# ---------------------------------------------------------------------------

def host_of(value: str) -> str:
    """Lower-cased host of a Host header or origin authority, without the port."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):
        end = v.find("]")
        return v[:end + 1] if end > 0 else ""
    return v.split(":", 1)[0]


def normalize_origin(value: str) -> str:
    """The origin in canonical form, or "" when it is not a plain origin."""
    v = (value or "").strip().rstrip("/").lower()
    return v if _ORIGIN.fullmatch(v) else ""


def is_loopback(request_host: str) -> bool:
    return host_of(request_host) in LOOPBACK_HOSTS


def allowed_parents(request_host: str, origins: Iterable[str]) -> list[str]:
    """App origins that may frame this studio, as CSP source expressions.

    Configured origins always count. Loopback dev servers (any port) count only
    when the studio itself is being reached over loopback: a laptop running the
    app's dev server next to its own studio."""
    out: list[str] = []
    for origin in origins:
        o = normalize_origin(origin)
        if o and o not in out:
            out.append(o)
    if is_loopback(request_host):
        out.extend(p for p in LOOPBACK_ANCESTORS if p not in out)
    return out


def parent_allowed(parent: str, request_host: str, origins: Iterable[str]) -> bool:
    p = normalize_origin(parent)
    if not p:
        return False
    configured = {normalize_origin(o) for o in origins} - {""}
    if p in configured:
        return True
    return is_loopback(request_host) and bool(_LOOPBACK_PARENT.fullmatch(p))


def frame_headers(request_host: str, origins: Iterable[str]) -> dict[str, str]:
    """The anti-clickjacking headers for one response."""
    sources = " ".join(["'self'", *allowed_parents(request_host, origins)])
    return {"Content-Security-Policy": f"frame-ancestors {sources}",
            "X-Frame-Options": "SAMEORIGIN"}


def apply_frame_headers(response: Response, request_host: str, origins: Iterable[str]) -> None:
    for name, value in frame_headers(request_host, origins).items():
        if name == "Content-Security-Policy" and response.headers.get(name):
            # Several CSP headers are all enforced; never replace a page's own.
            response.headers.append(name, value)
        elif not response.headers.get(name):
            response.headers[name] = value


def is_frame_navigation(request: Request) -> bool:
    """A browser loading a page into an iframe (Fetch Metadata)."""
    return (request.method == "GET"
            and (request.headers.get("sec-fetch-dest") or "").lower() in ("iframe", "frame"))


def safe_next(raw: Optional[str]) -> str:
    """A same-origin path to open after sign-in; anything else is the studio home."""
    v = (raw or "").strip()
    if (not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or len(v) > 2000
            or any(ch in v for ch in "\\\r\n\t\0") or v.startswith("/embed")):
        return DEFAULT_NEXT
    return v


def bootstrap_redirect(path: str, query: str) -> RedirectResponse:
    """Send a signed-out frame to the handshake page instead of the code screen."""
    target = safe_next(path + (("?" + query) if query else ""))
    return RedirectResponse("/embed?next=" + quote(target, safe=""), status_code=303,
                            headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# tickets
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64(text: str) -> str:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def _mac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def pass_role(authorization: Optional[str], secret: str, role_code: RoleCode,
              max_age: Optional[int] = None) -> str:
    """The role of a valid generation (render) pass in an Authorization header.
    ``max_age`` is the host's pass lifetime (studio_jobs.identity)."""
    m = _RENDER_PASS.fullmatch((authorization or "").strip())
    if not m:
        return ""
    return m.group(2) if studio_jobs.identity(m.group(1), secret, role_code, max_age=max_age) else ""


def mint_ticket(secret: str, role: str, role_code: RoleCode, bound_origin: str,
                now: Optional[float] = None) -> str:
    if role not in ("user", "admin"):
        raise ValueError("unknown role")
    exp = int(time.time() if now is None else now) + TICKET_TTL
    origin = normalize_origin(bound_origin)
    bound = ("o" + _b64(origin)) if origin else "n"
    payload = f"mlab-embed-ticket-v1.{role}.{exp}.{secrets.token_hex(16)}.{bound}"
    return f"{payload}.{_mac(secret, 'embed-ticket:' + payload + ':' + role_code(role))}"


class TicketBook:
    """Each ticket redeems once. Process-local: one uvicorn worker serves the studio."""

    def __init__(self, limit: int = 20_000):
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()
        self._limit = limit

    def claim(self, nonce: str, exp: int, now: float) -> bool:
        with self._lock:
            for key in [k for k, v in self._used.items() if v < now]:
                self._used.pop(key, None)
            if nonce in self._used or len(self._used) >= self._limit:
                return False
            self._used[nonce] = exp
            return True


def redeem_ticket(raw: str, parent: str, *, secret: str, role_code: RoleCode, book: TicketBook,
                  request_host: str, origins: Iterable[str], now: Optional[float] = None) -> str:
    """The ticket's role, once, or EmbedRefused."""
    now = time.time() if now is None else now
    m = _TICKET.fullmatch((raw or "").strip())
    if not m:
        raise EmbedRefused("That sign-in ticket is not valid.")
    payload, role, exp, nonce, bound, signature = m.groups()
    if not hmac.compare_digest(signature, _mac(secret, "embed-ticket:" + payload + ":" + role_code(role))):
        raise EmbedRefused("That sign-in ticket is not valid.")
    if int(exp) < now or int(exp) > now + TICKET_TTL + 300:
        raise EmbedRefused("That sign-in ticket has expired. Reload Media Lab.")
    bound_origin = _unb64(bound[1:]) if bound != "n" else ""
    if bound_origin:
        # A browser minted it: the frame's parent must be that same app origin,
        # and that origin must still be one this studio lets frame it.
        if normalize_origin(parent) != bound_origin:
            raise EmbedRefused("This ticket was made for a different app.")
        if not parent_allowed(bound_origin, request_host, origins):
            raise EmbedRefused("This app is not allowed to show Media Lab.")
    elif parent != NATIVE_PARENT:
        # Minted without an Origin header: only the phone app's bridge uses those.
        raise EmbedRefused("This ticket was made for the phone app.")
    if not book.claim(nonce, int(exp), now):
        raise EmbedRefused("That sign-in ticket was already used. Reload Media Lab.")
    return role


# ---------------------------------------------------------------------------
# the embed session cookie
# ---------------------------------------------------------------------------

def session_token(secret: str, role: str, role_code: RoleCode, now: Optional[float] = None) -> str:
    prefix = f"e1.{role}.{int(time.time() if now is None else now)}"
    return f"{prefix}.{_mac(secret, 'embed-session:' + prefix + ':' + role_code(role))}"


def session_age(raw: str, secret: str, role_code: RoleCode, now: Optional[float] = None) -> tuple[str, float]:
    """(role, age in seconds) of a valid embed session value, else ("", 0)."""
    m = _SESSION.fullmatch(raw or "")
    if not m:
        return "", 0.0
    prefix, role, iat, signature = m.groups()
    if not hmac.compare_digest(signature, _mac(secret, "embed-session:" + prefix + ":" + role_code(role))):
        return "", 0.0
    age = (time.time() if now is None else now) - int(iat)
    return (role, age) if -300 <= age <= COOKIE_MAX_AGE else ("", 0.0)


def same_origin_request(request: Request) -> bool:
    """Did the studio's own page (or the user directly) make this request?

    Fetch Metadata answers it on every current browser. A browser without it
    still sends Origin on any cross-site write; compare that host with the Host
    header. A request with neither is not a browser request at all (the phone
    app's own fetch sharing its WebView's cookie jar, a script), so cross-site
    request forgery - the only thing this check is for - does not apply."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return bool(host_of(origin)) and host_of(origin) == host_of(request.headers.get("host") or "")


def same_origin_post(request: Request) -> bool:
    """Stricter than same_origin_request: a state change must come from the page itself."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() == "same-origin"
    origin = request.headers.get("origin")
    return bool(origin) and bool(host_of(origin)) and \
        host_of(origin) == host_of(request.headers.get("host") or "")


def cookie_role(request: Request, secret: str, role_code: RoleCode) -> str:
    """The role an embed cookie grants on this request, or ""."""
    raw = request.cookies.get(COOKIE, "")
    if not raw or not same_origin_request(request):
        return ""
    return session_age(raw, secret, role_code)[0]


def cookie_header(value: str, secure: bool, max_age: int = COOKIE_MAX_AGE) -> str:
    parts = [f"{COOKIE}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly"]
    # Over HTTPS: a partitioned third-party cookie, readable only under the
    # app's own top-level site. Over plain HTTP a cross-site frame cannot hold
    # a cookie at all, so Lax is the honest setting (same-site or top-level).
    parts += ["Secure", "SameSite=None", "Partitioned"] if secure else ["SameSite=Lax"]
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# the handshake page
# ---------------------------------------------------------------------------

_BOOTSTRAP = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>Media Lab</title>
<style>*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#0B0806;color:rgba(255,255,255,.92);
 font-family:'Barlow',-apple-system,system-ui,sans-serif;font-weight:300}
main{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
 gap:12px;padding:24px;text-align:center}
.k{font-size:.6875rem;letter-spacing:.22em;text-transform:uppercase;color:#C99A6A;font-weight:500}
h1{font-family:'Space Grotesk',system-ui,sans-serif;font-weight:600;font-size:1.6rem}
p{color:rgba(255,255,255,.62);font-size:.92rem;max-width:340px;line-height:1.45}
.spin{width:22px;height:22px;border-radius:50%;border:2px solid rgba(255,255,255,.18);
 border-top-color:#E8C193;animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}
button{margin-top:6px;padding:12px 18px;border:0;border-radius:12px;cursor:pointer;font:inherit;
 font-weight:600;background:linear-gradient(140deg,#E8C193,#C99A6A 55%,#A97B4E);color:#160D06}
[hidden]{display:none!important}</style></head><body>
<main><div class="k">VibeX Studio</div><h1>Media Lab</h1><div class="spin" id="spin"></div>
<p id="say">Opening your studio&hellip;</p>
<button type="button" id="own" hidden>Open Media Lab in its own window</button></main>
<script>
(function(){"use strict";
var CFG=__CONFIG__;
var P="vibex-lab:";
var nativeApp=!!window.ReactNativeWebView;
var framed=(function(){try{return window.self!==window.top;}catch(e){return true;}})();
var parentOrigin=null,settled=false,asked=false;
function allowed(o){
  if(typeof o!=="string")return false;
  if(CFG.parents.indexOf(o)>=0)return true;
  return CFG.loopback&&/^http:\\/\\/(localhost|127\\.0\\.0\\.1)(:\\d{1,5})?$/.test(o);
}
function ancestor(){
  try{var a=location.ancestorOrigins;if(a&&a.length&&allowed(a[0]))return a[0];}catch(e){}
  return null;
}
function tell(kind,extra){
  var msg={type:P+kind};if(extra)for(var k in extra)msg[k]=extra[k];
  if(nativeApp){try{window.ReactNativeWebView.postMessage(JSON.stringify(msg));}catch(e){}return;}
  if(!framed)return;
  /* Nothing this page posts is secret; still address the verified parent when known. */
  var target=parentOrigin||ancestor()||"*";
  try{window.parent.postMessage(msg,target);}catch(e){}
}
function say(text,busy){document.getElementById("say").textContent=text;
  document.getElementById("spin").hidden=!busy;}
function go(){
  settled=true;
  try{sessionStorage.setItem("mlab-embed","1");
      if(parentOrigin)sessionStorage.setItem("mlab-embed-parent",parentOrigin);}catch(e){}
  tell("signed-in");
  location.replace(CFG.next);
}
function blocked(reason){
  settled=true;
  say("This window can't keep you signed in to Media Lab. Open it in its own window instead.",false);
  var b=document.getElementById("own");b.hidden=false;
  b.onclick=function(){window.open(CFG.next.replace(/([?&])embed=1(&|$)/,"$1").replace(/[?&]$/,""),"_blank","noopener");};
  tell("blocked",{reason:reason});
}
function status(){
  return fetch("/api/embed/status",{credentials:"same-origin",cache:"no-store"})
    .then(function(r){return r.ok?r.json():{signedIn:false};})
    .catch(function(){return {signedIn:false,offline:true};});
}
function redeem(ticket,parent){
  if(settled)return;
  say("Signing you in\\u2026",true);
  fetch("/api/embed/redeem",{method:"POST",credentials:"same-origin",cache:"no-store",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket:ticket,parent:parent})})
  .then(function(r){return r.json().catch(function(){return {};}).then(function(d){
    if(!r.ok)throw new Error(d.error||"Sign-in was refused.");
    return status();
  });})
  .then(function(s){if(s.signedIn)go();else blocked("cookies");})
  .catch(function(e){settled=true;say(String(e&&e.message||e),false);tell("failed");});
}
window.addEventListener("message",function(ev){
  if(nativeApp||settled||ev.source!==window.parent||!allowed(ev.origin))return;
  var d=ev.data;if(!d||typeof d!=="object")return;
  if(d.type===P+"ticket"&&typeof d.ticket==="string"&&d.ticket.length<1200){
    parentOrigin=ev.origin;redeem(d.ticket,ev.origin);
  }else if(d.type===P+"no-ticket"){
    parentOrigin=ev.origin;say("Sign in to Media Lab in the app to continue.",false);
  }
});
if(nativeApp){
  window.__vibexEmbedDeliver=function(d){
    if(settled||!d||typeof d!=="object")return;
    if(d.type==="ticket"&&typeof d.ticket==="string"&&d.ticket.length<1200)redeem(d.ticket,CFG.nativeParent);
    else if(d.type==="no-ticket")say("Sign in to Media Lab in the app to continue.",false);
  };
}
status().then(function(s){
  if(s.signedIn&&!s.renew)return go();
  if(!framed&&!nativeApp)return go();       /* a normal tab: the usual code screen takes over */
  if(s.offline){say("Media Lab is not answering. Check the server and try again.",false);tell("failed");return;}
  asked=true;tell("need-ticket");
  setTimeout(function(){
    if(settled)return;
    if(s.signedIn)return go();              /* renewal is best effort */
    say("Waiting for the app to sign you in\\u2026",true);
  },8000);
});
tell("hello");
})();
</script></body></html>"""


def bootstrap_page(parents: list[str], loopback: bool, next_path: str) -> HTMLResponse:
    config = {"parents": [p for p in parents if "*" not in p], "loopback": loopback,
              "next": safe_next(next_path), "nativeParent": NATIVE_PARENT}
    # JSON inside <script>: escape the characters that could close or confuse it.
    blob = (json.dumps(config).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return HTMLResponse(_BOOTSTRAP.replace("__CONFIG__", blob),
                        headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

class RedeemReq(BaseModel):
    ticket: str = Field(max_length=1200)
    parent: str = Field(max_length=300)


def router(*, secret: Callable[[], str], role_code: RoleCode, origins: Callable[[], list[str]],
           secure: Callable[[Request], bool], signed_in: Callable[[Request], bool],
           pass_max_age: Optional[int] = None, book: Optional[TicketBook] = None) -> APIRouter:
    """The embed door. ``signed_in`` is the studio's own sign-in check and
    ``pass_max_age`` how long this host keeps device passes."""
    api = APIRouter()
    tickets = book or TicketBook()

    @api.get("/embed")
    def embed_page(request: Request, next: str = DEFAULT_NEXT):  # noqa: A002 - query name
        host = request.headers.get("host") or ""
        return bootstrap_page(allowed_parents(host, origins()), is_loopback(host), next)

    @api.post(TICKET_PATH)
    def embed_ticket(request: Request):
        role = pass_role(request.headers.get("authorization"), secret(), role_code, pass_max_age)
        if not role:
            return JSONResponse({"error": "A Media Lab generation pass is required."}, status_code=401)
        origin = request.headers.get("origin") or ""
        host = request.headers.get("host") or ""
        if origin and not parent_allowed(origin, host, origins()):
            return JSONResponse({"error": "origin-not-allowed",
                                 "detail": "This app's address is not in the studio's allowed app list."},
                                status_code=403)
        return JSONResponse({"ticket": mint_ticket(secret(), role, role_code, origin),
                             "expiresIn": TICKET_TTL})

    @api.post("/api/embed/redeem")
    def embed_redeem(r: RedeemReq, request: Request):
        # Only the studio's own /embed page redeems; a cross-site form post
        # (login CSRF) is refused before the ticket is even read.
        if not same_origin_post(request):
            return JSONResponse({"error": "Sign-in must come from the studio page."}, status_code=403)
        host = request.headers.get("host") or ""
        try:
            role = redeem_ticket(r.ticket, r.parent, secret=secret(), role_code=role_code, book=tickets,
                                 request_host=host, origins=origins())
        except EmbedRefused as refused:
            return JSONResponse({"error": str(refused)}, status_code=403)
        is_secure = secure(request)
        resp = JSONResponse({"ok": True, "partitioned": is_secure})
        resp.headers.append("set-cookie", cookie_header(session_token(secret(), role, role_code), is_secure))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @api.get("/api/embed/status")
    def embed_status(request: Request):
        ok = bool(signed_in(request))
        age = session_age(request.cookies.get(COOKIE, ""), secret(), role_code)[1] \
            if ok and same_origin_request(request) else 0.0
        return JSONResponse({"signedIn": ok, "renew": bool(ok and age > RENEW_AFTER)},
                            headers={"Cache-Control": "private, no-store"})

    return api
