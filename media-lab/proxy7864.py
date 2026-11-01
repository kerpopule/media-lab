#!/usr/bin/env python3
"""Media Lab chat-injecting reverse proxy for the Maestro GUI (Advanced mode).

Proxies EVERYTHING (HTTP + WebSockets — Gradio needs WS) from :7864 to the
Maestro GUI origin, injecting the Media Lab chat widget <script> into text/html
responses only. Maestro itself is never modified.

Env:
  MAESTRO_ORIGIN  upstream origin (default http://<MEDIA_LAB_BIND_HOST>:7862 — the
                  GUI binds only to the studio's bind address)
  MEDIALAB_API    API origin the widget calls (default the studio's own :7863 URL;
                  /api/chat there has CORS enabled)
Both defaults come from config/local.env via media_lab_core.local_config.
Run (unit): config/media-lab-adv-mode.service
"""
import asyncio, os, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

from media_lab_core import local_config

_BIND = local_config.bind_host() if local_config.bind_host() not in ("0.0.0.0", "::") else "127.0.0.1"
ORIGIN = os.environ.get("MAESTRO_ORIGIN", f"http://{_BIND}:7862").rstrip("/")
API = os.environ.get("MEDIALAB_API", local_config.studio_url()).rstrip("/")
WIDGET = local_config.home() / "static/medialab-chat.js"
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-authenticate",
       "proxy-authorization", "te", "trailers", "content-length", "content-encoding",
       "accept-encoding", "host"}
INJECT = (f'<script>window.MEDIALAB_API="{API}";window.MEDIALAB_CHAT_BOTTOM=18;</script>'
          f'<script src="/--medialab-chat.js"></script>').encode()

app = FastAPI()


@app.get("/--medialab-chat.js")
def widget_js():
    return Response(WIDGET.read_bytes(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket, path: str):
    import websockets
    await ws.accept()
    qs = ws.scope.get("query_string", b"").decode()
    host = urlsplit(ORIGIN).netloc
    uri = f"ws://{host}/{path}" + (f"?{qs}" if qs else "")
    try:
        async with websockets.connect(uri, max_size=None) as up:
            async def c2s():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        await up.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
            async def s2c():
                async for m in up:
                    if isinstance(m, (bytes, bytearray)):
                        await ws.send_bytes(bytes(m))
                    else:
                        await ws.send_text(m)
            done, pending = await asyncio.wait(
                [asyncio.create_task(c2s()), asyncio.create_task(s2c())],
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _fetch(url, body, headers, method):
    """Blocking upstream fetch (runs in a thread)."""
    req = urllib.request.Request(url, data=body if body else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as up:
            return up.status, dict(up.headers.items()), up.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def http_proxy(path: str, request: Request):
    qs = request.url.query
    url = f"{ORIGIN}/{path}" + (f"?{qs}" if qs else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["Accept-Encoding"] = "identity"     # keep HTML plaintext for injection
    body = await request.body()
    try:
        status, uheaders, payload = await asyncio.to_thread(
            _fetch, url, body, headers, request.method)
    except Exception:
        return Response("Advanced mode is not running right now — start Maestro and reload.",
                        status_code=502, media_type="text/plain; charset=utf-8")
    ctype = next((v for k, v in uheaders.items() if k.lower() == "content-type"), "")
    rheaders = {k: v for k, v in uheaders.items()
                if k.lower() not in HOP and k.lower() != "content-type"}
    if "text/html" in ctype.lower():
        idx = payload.lower().rfind(b"</body>")
        payload = (payload[:idx] + INJECT + payload[idx:]) if idx != -1 else payload + INJECT
    return Response(payload, status_code=status, headers=rheaders,
                    media_type=ctype or None)
