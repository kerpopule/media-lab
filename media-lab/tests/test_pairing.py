from urllib.parse import unquote

import pytest

from media_lab_core import pairing as p


def test_classify_kinds():
    assert p.classify_address("127.0.0.1", "lo") == "loopback"
    assert p.classify_address("100.101.102.103", "utun4") == "tailscale"
    assert p.classify_address("100.66.1.2", "tailscale0") == "tailscale"
    assert p.classify_address("192.168.1.71", "en0") == "lan"
    assert p.classify_address("10.0.0.5", "eth0") == "lan"
    assert p.classify_address("172.17.0.1", "docker0") == "virtual"
    assert p.classify_address("192.168.64.1", "bridge100") == "virtual"
    assert p.classify_address("8.8.4.4", "eth0") == "other"
    assert p.classify_address("not-an-ip", "eth0") == "other"


def test_pick_best_prefers_tailnet_then_lan():
    addrs = [p.make_address("127.0.0.1", "lo"), p.make_address("172.17.0.1", "docker0"),
             p.make_address("192.168.1.71", "en0"), p.make_address("100.101.1.1", "utun4")]
    assert p.pick_best_address(addrs).ip == "100.101.1.1"
    assert p.pick_best_address(addrs[:3]).ip == "192.168.1.71"
    assert p.pick_best_address(addrs[:2]).ip == "172.17.0.1"
    assert p.pick_best_address(addrs[:1]).ip == "127.0.0.1"
    assert p.pick_best_address([]) is None


def test_rank_is_stable_within_kind():
    a = p.make_address("192.168.1.10", "en0")
    b = p.make_address("192.168.1.11", "en5")
    assert [x.ip for x in p.rank_addresses([a, b])] == [a.ip, b.ip]
    assert [x.ip for x in p.rank_addresses([b, a])] == [b.ip, a.ip]


def test_server_url():
    assert p.server_url("192.168.1.2", 7863) == "http://192.168.1.2:7863"
    assert p.server_url("fe80::1", 80) == "http://[fe80::1]:80"


def test_build_pair_link_matches_phone_contract():
    link = p.build_pair_link("http://192.168.1.20:7863/")
    assert link.startswith("vibex://pair?medialab=")
    enc = link.split("medialab=", 1)[1]
    assert "/" not in enc and ":" not in enc            # fully percent-encoded
    assert unquote(enc) == "http://192.168.1.20:7863"   # what decodeURIComponent yields


def test_build_pair_link_workbench_half_needs_token():
    assert "workbench" not in p.build_pair_link("http://h:1", "http://h:2", None)
    assert "workbench" not in p.build_pair_link("http://h:1", "http://h:2", "has space")
    link = p.build_pair_link("http://h:1", "http://h:2", "tok123")
    assert "&workbench=http%3A%2F%2Fh%3A2&wbt=tok123" in link


def test_build_pair_link_rejects_bare_host():
    with pytest.raises(ValueError):
        p.build_pair_link("192.168.1.2:7863")


def test_pairing_summary_shape_and_loopback_fallback():
    addrs = [p.make_address("127.0.0.1", "lo"), p.make_address("192.168.1.71", "en0")]
    s = p.pairing_summary(7863, addrs, "ABCD2345", "1234")
    assert s["best"]["url"] == "http://192.168.1.71:7863"
    assert s["link"] == p.build_pair_link("http://192.168.1.71:7863")
    assert s["urls"][0]["url"] == "http://127.0.0.1:7863"
    assert [u["url"] for u in s["urls"]][1:] == ["http://192.168.1.71:7863"]
    assert s["access_code"] == "ABCD2345" and s["admin_code"] == "1234"
    lonely = p.pairing_summary(7891, [p.make_address("127.0.0.1", "lo")], None)
    assert lonely["best"]["url"] == "http://127.0.0.1:7891"
    assert lonely["link"].endswith("127.0.0.1%3A7891")


def test_render_qr_text_is_half_block_and_fits_a_terminal():
    qrcode = pytest.importorskip("qrcode")
    del qrcode
    text = p.render_qr_text("vibex://pair?medialab=http%3A%2F%2F192.168.1.20%3A7863")
    lines = text.splitlines()
    assert 15 <= len(lines) <= 30
    assert all(len(line) <= 60 for line in lines)
    assert set("".join(lines)) <= set("█▀▄ \xa0")   # qrcode pads with nbsp
    assert p.render_qr_text("x", invert=False) != p.render_qr_text("x", invert=True)


def test_format_summary_mentions_everything():
    addrs = [p.make_address("127.0.0.1", "lo"), p.make_address("100.101.1.1", "utun4")]
    s = p.pairing_summary(7863, addrs, "ABCD2345", "1234")
    out = p.format_summary(s, "QRQR", "/x/access-code.txt")
    for needle in ("http://127.0.0.1:7863", "http://100.101.1.1:7863", "ABCD2345", "1234",
                   "QRQR", s["link"], "More options", "/x/access-code.txt"):
        assert needle in out
    assert "qrcode" in p.format_summary(s, None)      # graceful without the package


def test_parse_ip_addr_output():
    text = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
            "2: eth0    inet 10.1.2.3/24 brd 10.1.2.255 scope global eth0\n"
            "5: tailscale0    inet 100.90.1.2/32 scope global tailscale0\n"
            "7: veth1a@if8    inet 172.19.0.1/16 scope global veth1a\n")
    assert p.parse_ip_addr_output(text) == [("lo", "127.0.0.1"), ("eth0", "10.1.2.3"),
                                            ("tailscale0", "100.90.1.2"), ("veth1a", "172.19.0.1")]


def test_parse_ifconfig_output():
    text = ("lo0: flags=8049<UP,LOOPBACK> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP> mtu 1500\n\tether aa:bb\n\tinet 192.168.1.71 netmask 0xffffff00\n"
            "utun4: flags=8051<UP> mtu 1280\n\tinet 100.90.1.2 --> 100.90.1.2 netmask 0xffffffff\n")
    assert p.parse_ifconfig_output(text) == [("lo0", "127.0.0.1"), ("en0", "192.168.1.71"),
                                             ("utun4", "100.90.1.2")]


def test_list_ipv4_addresses_with_fake_runner(monkeypatch):
    import subprocess

    class R:
        def __init__(self, stdout):
            self.stdout, self.returncode = stdout, 0

    def runner(cmd, **kw):
        if cmd[0] == "ip":
            return R("2: eth0    inet 10.1.2.3/24 scope global eth0\n")
        if cmd[-2:] == ["ip", "-4"]:
            return R("100.90.1.2\n")
        return R("")

    monkeypatch.setattr(p.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(p.shutil.os.path, "exists", lambda path: True)
    addrs = p.list_ipv4_addresses(runner)
    kinds = {a.ip: a.kind for a in addrs}
    assert kinds == {"10.1.2.3": "lan", "100.90.1.2": "tailscale"}
    assert p.pick_best_address(addrs).ip == "100.90.1.2"
    del subprocess
