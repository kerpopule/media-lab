#!/usr/bin/env python3
"""Governed :8003 compatibility shim for the active Spark text slot.

The authoritative runtime is always :8004. The stable Director model name is
translated to the exact active checkpoint, while exact Hermes aliases remain
exact and therefore fail loudly when their requested runtime is not resident.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only

UP_HOST, UP_PORT = "127.0.0.1", 8004
ACTIVE_FILE = local_config.home() / "pool/text-runtime-active"
MODELS = {
    "pplx": "pplx-computer-qwen-3-8-27b-dflash2-20260824",
    "flash": "qwen3.8-flash-next-ud-iq1-m",
}


def active_mode():
    try:
        mode = ACTIVE_FILE.read_text().strip().lower()
    except Exception:
        mode = "pplx"
    return mode if mode in MODELS else "pplx"


async def read_headers(reader):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1)
        if not chunk:
            return None, b""
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def header_value(head_text, name):
    for line in head_text.split("\r\n")[1:]:
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return None


async def handle(client_reader, client_writer):
    try:
        head, rest = await read_headers(client_reader)
        if head is None:
            client_writer.close()
            return
        head_text = head.decode("latin-1")
        request_line = head_text.split("\r\n")[0]
        content_length = int(header_value(head_text, "content-length") or 0)
        body = rest
        while len(body) < content_length:
            chunk = await client_reader.read(content_length - len(body))
            if not chunk:
                break
            body += chunk

        if "/v1/chat/completions" in request_line and body:
            try:
                payload = json.loads(body)
                mode = active_mode()
                if payload.get("model") == "media-lab-text":
                    payload["model"] = MODELS[mode]
                if mode == "pplx":
                    kwargs = payload.setdefault("chat_template_kwargs", {})
                    kwargs.setdefault("enable_thinking", False)
                    # PPLX exposes OpenAI's high tier as xhigh.
                    if payload.get("reasoning_effort") == "high":
                        payload["reasoning_effort"] = "xhigh"
                if not payload.get("max_tokens"):
                    payload["max_tokens"] = 800
                body = json.dumps(payload).encode()
            except Exception:
                # Preserve the original request; the upstream runtime will emit
                # the real protocol error rather than a fabricated shim answer.
                pass

        lines = [line for line in head_text.split("\r\n")
                 if line and not line.lower().startswith(("content-length:", "connection:"))]
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        outgoing = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

        server_reader, server_writer = await asyncio.open_connection(UP_HOST, UP_PORT)
        server_writer.write(outgoing)
        await server_writer.drain()
        while True:
            chunk = await server_reader.read(65536)
            if not chunk:
                break
            client_writer.write(chunk)
            await client_writer.drain()
        server_writer.close()
    except Exception:
        pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


async def main():
    # Loopback always; the configured bind address too when it is a specific
    # non-loopback interface (MEDIA_LAB_BIND_HOST in config/local.env).
    bind = local_config.bind_host()
    if bind in ("0.0.0.0", "::"):
        servers = [await asyncio.start_server(handle, bind, 8003)]
    else:
        servers = [await asyncio.start_server(handle, "127.0.0.1", 8003)]
        if bind not in ("127.0.0.1", "localhost", ""):
            servers.append(await asyncio.start_server(handle, bind, 8003))
    for server in servers:
        await server.__aenter__()
    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            await server.__aexit__(None, None, None)


asyncio.run(main())
