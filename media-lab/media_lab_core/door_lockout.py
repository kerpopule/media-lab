"""The code prompt's lockout: three wrong codes, then the door stays shut a while.

The owner's rule (2026-09-26):

* **3 wrong codes within 10 minutes** from one client locks the code prompt
  for that client for **1 hour**;
* every later lockout of the same client **doubles**: 2 h, 4 h, 8 h, ...
  capped at **7 days**;
* a client that stays clean (no wrong code, no lockout running) for a whole
  **24 hours** steps back down one level, so a family member's bad evening is
  forgotten in a day while a patient guesser keeps climbing;
* the owner can lift any lockout at once: ``media-lab code --unlock``.

"One client" is one network, not one browser: an IPv4 address, or an IPv6 /64
(see :func:`throttle_key`). A guesser cannot clear a lockout by throwing away
cookies, and a phone cannot walk through the 2^64 addresses of its own /64.
The price is that a house behind one IPv4 address shares one counter.

The lockout guards the code PROMPT only. A device that is already signed in
never passes through here -- its cookie or studio pass is checked without it --
so it keeps working while the door is shut for new sign-ins.

Two separate counters ("namespaces") use the same rule:

* ``gate`` -- the family/admin code prompt (``POST /api/gate``);
* ``admin`` -- the admin code alone: owner scripts sending it in
  ``X-Lab-Pin``, and the code prompt WHILE ``gate`` is locked (only the admin
  code is checked then, so family typos never lock the owner out, and a
  guesser learns nothing more about the family code during a lockout).

Nothing global ever refuses: only a client's own wrong answers lock that
client, so a stranger cannot shut the door on anybody else's network.

State is one JSON-able dict per namespace, ``{key: entry}``, with entries
``{"strikes": [t, ...], "level": n, "until": t, "clean": t}``. Every function
takes ``now`` (epoch seconds) so the rule is testable without waiting a week.

The owner's unlock reaches a running server through a request file
(``auth-unlock.json`` under the data root, mode 0600): the server applies it
within a few seconds and deletes it. The file route works while the server is
running (it holds the live table in memory and would overwrite a direct edit)
and while it is down (it applies the request at start).

Standard library only: the ``media-lab`` CLI imports it without the venv.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import time
from pathlib import Path

STRIKES = 3                       # wrong codes ...
STRIKE_WINDOW = 10 * 60           # ... within this many seconds lock the door
FIRST_LOCK = 60 * 60              # the first lockout: 1 hour
MAX_LOCK = 7 * 24 * 3600          # doubling stops at a week
DECAY_AFTER = 24 * 3600           # a clean day steps the level back down by one
KEY_MAX = 5000                    # tracked clients per namespace (a few KB each at most)
NAMESPACES = ("gate", "admin")
UNLOCK_REQUEST = "auth-unlock.json"


# ---------------------------------------------------------------- identity

def throttle_key(raw: str | None) -> str:
    """The lockout key for one client address.

    An IPv6 visitor normally holds a whole /64 -- home broadband and phones get
    one each -- and can pick a new address in it for every request, so a public
    IPv6 address is keyed by its /64 and an IPv4-mapped one by its IPv4
    address. IPv4, tailnet (fd7a:...) and other private addresses are keyed
    exactly as they are. Anything that is not an address is returned as-is."""
    raw = (raw or "").strip()[:45]
    try:
        ip = ipaddress.ip_address(raw.strip("[]"))
    except ValueError:
        return raw
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        if ip.is_global:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_key(ip: str | None) -> str:
    """The table key for a client whose address is ``ip`` (already passed
    through :func:`throttle_key`). No address at all -- a request from this
    machine, or through one of its own proxies on a host that is not listed
    as public -- is the one shared key ``anon``."""
    return f"ip:{ip}" if ip else "anon"


def target_key(raw: str | None) -> str:
    """What the owner typed after ``--unlock``: ``all``, ``anon`` (this
    machine's own scripts), or an address (any address in an IPv6 /64 names
    the whole /64). Raises ValueError for anything else."""
    raw = (raw or "all").strip()
    if raw.lower() in ("all", "anon"):
        return raw.lower()
    raw = raw.removeprefix("ip:")
    candidate = raw.split("/", 1)[0]
    try:
        ipaddress.ip_address(candidate.strip("[]"))
    except ValueError:
        raise ValueError(f"not an address: {raw!r} (use an IP address, 'anon' or 'all')") from None
    return client_key(throttle_key(candidate))


# ---------------------------------------------------------------- the rule

def lock_seconds(level: int) -> int:
    """How long lockout number ``level`` lasts: 1 h, 2 h, 4 h, ... <= 7 days."""
    if level <= 0:
        return 0
    return min(FIRST_LOCK * 2 ** min(level - 1, 30), MAX_LOCK)


def _entry(e) -> dict:
    """A well-formed entry from whatever was stored (a hand-edited or older
    file must never crash the door, and must never read as "unlocked" by
    accident either -- a bad number keeps its lock)."""
    if not isinstance(e, dict):
        e = {}
    def num(v, default=0.0):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if math.isfinite(v) else default
    strikes = e.get("strikes")
    return {"strikes": [num(t) for t in strikes] if isinstance(strikes, list) else [],
            "level": max(0, int(num(e.get("level")))),
            "until": num(e.get("until")),
            "clean": num(e.get("clean"))}


def _settle(e: dict, now: float) -> dict:
    """Forget strikes older than the window and apply the daily step-down."""
    e["strikes"] = [t for t in e["strikes"] if 0 <= now - t < STRIKE_WINDOW]
    if e["level"] > 0 and now >= e["until"]:
        clean = max(e["clean"], e["until"])
        steps = int((now - clean) // DECAY_AFTER)
        if steps > 0:
            e["level"] = max(0, e["level"] - steps)
            e["clean"] = clean + steps * DECAY_AFTER
    return e


def remaining(table: dict, key: str, now: float) -> int:
    """Seconds this client's door stays shut (0 = open)."""
    e = table.get(key)
    if not e:
        return 0
    left = _entry(e)["until"] - now
    return math.ceil(left) if left > 0 else 0


def next_lock(table: dict, key: str, now: float) -> int:
    """How long the NEXT lockout of this client would last."""
    e = table.get(key)
    level = _settle(_entry(e), now)["level"] if e else 0
    return lock_seconds(level + 1)


def strike(table: dict, key: str, now: float) -> dict:
    """Book one wrong code for ``key``.

    Returns ``{"locked", "retry_after", "tries_left", "level", "next_lock"}``.
    A wrong code sent while the door is already shut is not a strike: the code
    was never checked (the caller refuses before it compares anything)."""
    e = table[key] = _settle(_entry(table.get(key)), now)
    left = remaining(table, key, now)
    if left:
        return {"locked": True, "retry_after": left, "tries_left": 0,
                "level": e["level"], "next_lock": lock_seconds(e["level"] + 1)}
    e["strikes"].append(now)
    e["clean"] = now
    if len(e["strikes"]) >= STRIKES:
        e["level"] += 1
        secs = lock_seconds(e["level"])
        e["until"] = now + secs
        e["clean"] = e["until"]
        e["strikes"] = []
        return {"locked": True, "retry_after": secs, "tries_left": 0,
                "level": e["level"], "next_lock": lock_seconds(e["level"] + 1)}
    return {"locked": False, "retry_after": 0, "tries_left": STRIKES - len(e["strikes"]),
            "level": e["level"], "next_lock": lock_seconds(e["level"] + 1)}


def succeed(table: dict, key: str, now: float) -> bool:
    """A right code: this client's recent wrong ones no longer count. Its
    lockout level is left to step down on its own (a correct code from a
    shared network is not a pardon for everybody else on it). Returns True
    when the table changed."""
    if key not in table:
        return False
    e = table[key] = _settle(_entry(table[key]), now)
    changed = bool(e["strikes"])
    e["strikes"] = []
    if e["level"] == 0 and e["until"] <= now:
        table.pop(key, None)
        changed = True
    return changed


def prune(table: dict, now: float) -> None:
    """Drop clients that can no longer matter and cap the table. When the cap
    bites, a running lockout is the last thing evicted: a flood of fresh
    addresses must not be a way to push one's own lockout out of the table."""
    for key in list(table):
        e = table[key] = _settle(_entry(table[key]), now)
        if not e["strikes"] and e["level"] == 0 and e["until"] <= now:
            table.pop(key, None)
    if len(table) > KEY_MAX:
        def danger(kv):
            e = kv[1]
            return (e["until"] > now, e["level"], max([e["clean"], *e["strikes"]]))
        for key, _e in sorted(table.items(), key=danger)[:len(table) - KEY_MAX]:
            table.pop(key, None)


def unlock(tables: dict, target: str) -> int:
    """Owner: lift lockouts. ``target`` comes from :func:`target_key`: ``all``
    clears both counters for every client; a key clears that client in both
    counters, level and all. Returns how many entries were removed."""
    n = 0
    for ns in NAMESPACES:
        table = tables.get(ns)
        if not isinstance(table, dict):
            continue
        if target == "all":
            n += len(table)
            table.clear()
        elif table.pop(target, None) is not None:
            n += 1
    return n


def describe(tables: dict, now: float) -> list[dict]:
    """Every client with a running lockout or recent wrong codes, for the
    owner's ``media-lab code --locks``."""
    out = []
    for ns in NAMESPACES:
        for key, raw in sorted((tables.get(ns) or {}).items()):
            e = _settle(_entry(raw), now)
            left = max(0, math.ceil(e["until"] - now))
            if left or e["strikes"] or e["level"]:
                out.append({"counter": ns, "client": key, "locked_for": left,
                            "level": e["level"], "recent_wrong": len(e["strikes"]),
                            "next_lock": lock_seconds(e["level"] + 1)})
    return out


def human(seconds: int) -> str:
    """ "59 min", "2 h", "3 d 4 h" -- for messages and the CLI."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, math.ceil(seconds / 60))} min"
    if seconds < 86400:
        h, m = divmod(round(seconds / 60), 60)
        return f"{h} h" + (f" {m} min" if m else "")
    d, h = divmod(round(seconds / 3600), 24)
    return f"{d} d" + (f" {h} h" if h else "")


# ---------------------------------------------------------------- persistence

def load_tables(state: dict) -> dict:
    """The per-namespace tables inside ``auth-attempts.json`` (created if
    missing; anything malformed is replaced by an empty table)."""
    tables = state.get("lockouts")
    if not isinstance(tables, dict):
        tables = state["lockouts"] = {}
    for ns in NAMESPACES:
        if not isinstance(tables.get(ns), dict):
            tables[ns] = {}
    return tables


def request_unlock(root: Path, target: str) -> Path:
    """Ask the (running or future) server to lift lockouts: write the request
    file, 0600. A second request before the first is applied widens to
    ``all`` rather than silently dropping one."""
    path = Path(root) / UNLOCK_REQUEST
    try:
        prev = json.loads(path.read_text())
        if isinstance(prev, dict) and prev.get("target") not in (None, target):
            target = "all"
    except (OSError, ValueError):
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"target": target, "requested": time.time()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def take_unlock_request(root: Path) -> str | None:
    """The pending unlock target, removing the request file; None if there is
    no (valid) request. Called by the server."""
    path = Path(root) / UNLOCK_REQUEST
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = None
    try:
        path.unlink()
    except OSError:
        pass
    target = raw.get("target") if isinstance(raw, dict) else None
    if target in ("all", "anon") or (isinstance(target, str) and target.startswith("ip:")):
        return target
    return None
