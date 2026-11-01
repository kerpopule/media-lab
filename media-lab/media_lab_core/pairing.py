"""Pairing helpers: which address to advertise, the deep link, the terminal QR.

Everything the phone needs to find this Media Lab is one link:

    vibex://pair?medialab=<url-encoded http://host:port>

VibeXStudio parses it in ``src/lib/media-pairing.ts`` (``parsePairDeepLinkV2``),
then probes ``<url>/manifest.json`` — the one gate-exempt, CORS-open endpoint —
before it shows the Media Lab tab. Optional ``workbench``/``wbt`` halves ride on
the same link for the desktop app's Workbench; this module can carry them but
never invents them.

The functions here are pure (addresses in, text out) so they are unit-testable;
the two ``list_*``/``tailscale_*`` helpers at the bottom are the only ones that
touch the machine, and they only *read* it.
"""
from __future__ import annotations

import io
import ipaddress
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence
from urllib.parse import quote

DEFAULT_PORT = 7863
PAIR_SCHEME = "vibex://pair"

# Tailscale hands every node an address from the CGNAT block.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
# Interface names that are almost always a VM/container bridge, never the
# address a phone on the same Wi-Fi can reach.
_VIRTUAL_IFACE = re.compile(
    r"^(docker|br-|virbr|vmnet|vboxnet|veth|lxc|lxd|cni|flannel|podman|bridge\d|"
    r"utun|tun|tap|wg|zt|tailscale)", re.I)
# On macOS the Tailscale address sits on a utun interface; the CGNAT test above
# still classifies it as tailnet, so the utun prefix only demotes non-tailnet
# tunnel addresses.

# Ordering of address kinds, best first. A tailnet address works from anywhere
# the phone is signed into the same tailnet (cellular included); a LAN address
# only works on the same Wi-Fi but needs no Tailscale on the phone.
_KIND_RANK = {"tailscale": 0, "lan": 1, "other": 2, "virtual": 3, "loopback": 4}


@dataclass(frozen=True)
class Address:
    ip: str
    iface: str = ""
    kind: str = "other"

    def as_dict(self) -> dict:
        return asdict(self)


def classify_address(ip: str, iface: str = "", tailscale_ips: Iterable[str] = ()) -> str:
    """One of tailscale / lan / virtual / loopback / other for an IPv4 string."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "other"
    if addr.is_loopback:
        return "loopback"
    if ip in set(tailscale_ips) or addr in _TAILNET:
        return "tailscale"
    if iface and _VIRTUAL_IFACE.match(iface):
        return "virtual"
    if addr.is_private:
        return "lan"
    return "other"


def make_address(ip: str, iface: str = "", tailscale_ips: Iterable[str] = ()) -> Address:
    return Address(ip=ip, iface=iface, kind=classify_address(ip, iface, tailscale_ips))


def rank_addresses(addresses: Sequence[Address]) -> list[Address]:
    """Best first. Stable for equal kinds so interface order (en0 before en5,
    eth0 before eth1) breaks ties the way people expect."""
    return sorted(addresses, key=lambda a: _KIND_RANK.get(a.kind, 9))


def pick_best_address(addresses: Sequence[Address]) -> Address | None:
    """The address to put in the QR: tailnet if present, else the first LAN
    address, else anything routable. Loopback only when nothing else exists."""
    ranked = rank_addresses(addresses)
    return ranked[0] if ranked else None


def server_url(host: str, port: int = DEFAULT_PORT, scheme: str = "http") -> str:
    """``http://host:port`` — the bare origin the phone stores."""
    if ":" in host and not host.startswith("["):      # bare IPv6
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def build_pair_link(medialab_url: str, workbench_url: str | None = None,
                    workbench_token: str | None = None) -> str:
    """``vibex://pair?medialab=<enc>[&workbench=<enc>&wbt=<token>]``.

    Percent-encodes with ``safe=""`` so ``://`` and ``:port`` survive the
    phone's ``[?&]medialab=([^&#]*)`` regex + ``decodeURIComponent``. A
    workbench half without its token is dropped, matching the app, which
    refuses a token-less workbench rather than pairing something broken.
    """
    url = medialab_url.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(f"medialab_url must be http(s)://host:port, got {url!r}")
    params = [f"medialab={quote(url, safe='')}"]
    if workbench_url and workbench_token and re.match(r"^\S+$", workbench_token):
        params.append(f"workbench={quote(workbench_url.strip().rstrip('/'), safe='')}")
        params.append(f"wbt={quote(workbench_token, safe='')}")
    return f"{PAIR_SCHEME}?{'&'.join(params)}"


def render_qr_text(data: str, invert: bool = True, border: int = 1) -> str:
    """The QR as unicode half-blocks (two modules per row) so a version-3/4 code
    fits in ~20 lines of an ordinary terminal.

    ``invert=True`` draws light modules as █ — right for the usual light-text-
    on-dark terminal, where an un-inverted code comes out with a dark quiet
    zone that some phone cameras refuse. Pass ``invert=False`` for a light
    terminal theme.
    """
    try:
        import qrcode  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only on a broken venv
        raise RuntimeError(
            "the 'qrcode' package is missing — run ./install.sh (or "
            "`.venv/bin/pip install qrcode`) and try again") from exc
    qr = qrcode.QRCode(border=border)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=invert)
    return buf.getvalue().rstrip("\n")


def pairing_summary(port: int, addresses: Sequence[Address], access_code: str | None,
                    admin_code: str | None = None, bind: str = "0.0.0.0") -> dict:
    """The machine-readable summary printed after every install/``pair``.

    ``urls`` is every reachable origin (loopback first, then best-first), and
    ``link`` is the deep link for the best non-loopback address — or the
    loopback one when the box has no network at all, so the desktop app on the
    same machine can still pair.
    """
    ranked = [a for a in rank_addresses(addresses) if a.kind != "loopback"]
    best = ranked[0] if ranked else Address("127.0.0.1", "lo", "loopback")
    best_url = server_url(best.ip, port)
    urls = [{"url": server_url("127.0.0.1", port), "iface": "lo", "kind": "loopback"}]
    urls += [{"url": server_url(a.ip, port), "iface": a.iface, "kind": a.kind} for a in ranked]
    return {
        "port": int(port),
        "bind": bind,
        "best": {**best.as_dict(), "url": best_url},
        "urls": urls,
        "link": build_pair_link(best_url),
        "access_code": access_code,
        "admin_code": admin_code,
    }


_KIND_LABEL = {"loopback": "This machine", "lan": "Same Wi-Fi / LAN",
               "tailscale": "Tailnet (anywhere)", "virtual": "Virtual bridge",
               "other": "Other"}


def format_summary(summary: dict, qr_text: str | None, access_code_path: str = "",
                   admin_code_path: str = "") -> str:
    """Human summary: URLs, codes, the QR, and the typed-in fallback."""
    lines = ["", "Media Lab is up.", ""]
    width = max(len(_KIND_LABEL.get(u["kind"], u["kind"])) for u in summary["urls"]) + 2
    for u in summary["urls"]:
        label = _KIND_LABEL.get(u["kind"], u["kind"])
        iface = f"  ({u['iface']})" if u.get("iface") and u["kind"] != "loopback" else ""
        lines.append(f"  {label:<{width}} {u['url']}{iface}")
    code = summary.get("access_code") or "(not minted yet — start the server once)"
    lines.append(f"  {'Family code':<{width}} {code}" + (f"   {access_code_path}" if access_code_path else ""))
    if summary.get("admin_code"):
        lines.append(f"  {'Admin code':<{width}} {summary['admin_code']}"
                     + (f"   {admin_code_path}" if admin_code_path else ""))
    lines += ["", "Pair the VibeXStudio phone app — point the phone camera at this:", ""]
    if qr_text:
        lines.append(qr_text)
    else:
        lines.append("  (QR unavailable — 'qrcode' package missing; the link below still works)")
    lines += ["", f"  {summary['link']}", "",
              "  No camera handy? In the app: Media Lab → More options → type",
              f"  {summary['best']['url']} and the family code (everyone in the house",
              "  uses the same one; each device enters it once).", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Machine readers (read-only)
# ---------------------------------------------------------------------------

_TAILSCALE_CANDIDATES = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                         "/usr/bin/tailscale", "/usr/local/bin/tailscale")


def tailscale_ipv4(runner=subprocess.run) -> str | None:
    """``tailscale ip -4`` when a CLI exists and the node is up; else None."""
    for cand in _TAILSCALE_CANDIDATES:
        exe = shutil.which(cand) if "/" not in cand else (cand if shutil.os.path.exists(cand) else None)
        if not exe:
            continue
        try:
            out = runner([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line and classify_address(line) == "tailscale":
                return line
    return None


def parse_ip_addr_output(text: str) -> list[tuple[str, str]]:
    """``ip -4 -o addr`` lines → [(iface, ip)]."""
    found = []
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line.strip())
        if m:
            found.append((m.group(1).split("@")[0], m.group(2)))
    return found


def parse_ifconfig_output(text: str) -> list[tuple[str, str]]:
    """BSD/macOS ``ifconfig`` output → [(iface, ip)]."""
    found, iface = [], ""
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+):", line)
        if m:
            iface = m.group(1)
            continue
        m = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and iface:
            found.append((iface, m.group(1)))
    return found


def list_ipv4_addresses(runner=subprocess.run) -> list[Address]:
    """Every IPv4 on this box, classified. Loopback is included so callers can
    always show a local URL; ``pick_best_address`` ranks it last."""
    pairs: list[tuple[str, str]] = []
    if shutil.which("ip"):
        try:
            out = runner(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5)
            pairs = parse_ip_addr_output(out.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            pairs = []
    if not pairs and shutil.which("ifconfig"):
        try:
            out = runner(["ifconfig"], capture_output=True, text=True, timeout=5)
            pairs = parse_ifconfig_output(out.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            pairs = []
    if not pairs:
        # Last resort: the address the default route would use.
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            pairs = [("default", s.getsockname()[0])]
            s.close()
        except OSError:
            pairs = [("lo", "127.0.0.1")]
    ts = tailscale_ipv4(runner)
    seen, out_addrs = set(), []
    for iface, ip in pairs:
        if ip in seen:
            continue
        seen.add(ip)
        out_addrs.append(make_address(ip, iface, [ts] if ts else ()))
    if ts and ts not in seen:
        out_addrs.append(Address(ts, "tailscale", "tailscale"))
    return out_addrs
