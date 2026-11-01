"""One family login: the family code, the admin code, and nothing else.

Covers the 2026-09-25 security change end to end on a disposable data root:
  * no Host-header trust (``Host: localhost`` / the bind or tailnet address is
    not a pass) and no tailnet trust;
  * the local tool token (0600 file) is the only code-free way in, and only for
    the family permission set;
  * the family code is one permission set (make, edit, Library, queue) and the
    admin code alone opens server settings;
  * forgiving typing, one-year passes, rotation that signs every device out at
    once without a restart;
  * secret files born 0600 and re-tightened at start, codes never logged;
  * an existing install's legacy codes keep working until rotated;
  * the throttling identity cannot be forged from the tailnet.
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_lab_core import family_code, local_token, secret_files, studio_library

REPO = Path(__file__).parents[1]
LEGACY_FAMILY = "ABCD2345"      # the old 8-letter access-code shape
LEGACY_ADMIN = "0042"           # the old 4-digit admin default


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _load_app(tmp_path_factory, name, seed=None):
    """Import app.py under a fresh HOME; returns (module, captured stdout)."""
    home = tmp_path_factory.mktemp(name)
    root = home / "media-lab-simple"
    root.mkdir()
    for item in ("static", "config"):
        (root / item).symlink_to(REPO / item)
    if seed:
        seed(root)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS",
                                          "MEDIA_LAB_HOME")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    os.environ.pop("MEDIA_LAB_HOME", None)
    module_name = f"family_login_app_{name}"
    spec = importlib.util.spec_from_file_location(module_name, REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            spec.loader.exec_module(module)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module, out.getvalue()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    """A brand-new install: the app mints its own codes."""
    return _load_app(tmp_path_factory, "fresh")


@pytest.fixture(scope="module")
def legacy(tmp_path_factory):
    """An install from before word codes: 8-letter family code, 4-digit admin
    code, secret files left world-readable by an older build."""
    def seed(root):
        for name, text in (("access-code.txt", LEGACY_FAMILY + "\n"),
                           ("admin-pin.txt", LEGACY_ADMIN + "\n"),
                           ("access-secret.txt", "a" * 64),
                           ("providers.json", "{}"),
                           ("vapid_private.pem", "not a real key\n")):
            path = root / name
            path.write_text(text)
            os.chmod(path, 0o664)
    return _load_app(tmp_path_factory, "legacy", seed)


@pytest.fixture(autouse=True)
def _open_door(request):
    """The module-scoped studios share one client identity ("anon": a test
    client has no address), and 3 wrong codes lock it for an hour. Every case
    starts with the door open."""
    yield
    for name in ("fresh", "legacy"):
        if name in request.fixturenames:
            app, _ = request.getfixturevalue(name)
            with app._attempt_lock:
                for table in app._lockouts.values():
                    table.clear()


def _family(app):
    return app.ACCESS_CODE_FILE.read_text().strip()


def _admin(app):
    return app.PIN_FILE.read_text().strip()


def _client(app, base="https://studio.example", code=None):
    """A browser-like client: it loads a public file first, as a browser does.
    (The lockout keys on the client's network, not on any cookie: see
    tests/test_door_lockout.py.)"""
    client = TestClient(app.app, base_url=base)
    assert client.get("/manifest.json").status_code == 200
    if code is not None:
        signed = client.post("/api/gate", json={"code": code})
        assert signed.status_code == 200, signed.text
    return client


# ---------------------------------------------------------------- the codes

def test_word_list_is_big_clean_and_unique():
    assert len(family_code.WORDS) >= 1024
    assert len(set(family_code.WORDS)) == len(family_code.WORDS)
    assert all(w.isascii() and w.isalpha() and w.islower() and 3 <= len(w) <= 8
               for w in family_code.WORDS)
    assert family_code.FAMILY_WORDS * family_code.bits_per_word() >= 40
    assert family_code.ADMIN_WORDS * family_code.bits_per_word() >= 60


def test_minted_codes_are_words_not_digits():
    fam, adm = family_code.mint_family(), family_code.mint_admin()
    assert len(fam.split("-")) == 4 and all(w in family_code.WORDS for w in fam.split("-"))
    assert len(adm.split("-")) == 6 and all(w in family_code.WORDS for w in adm.split("-"))
    assert not family_code.is_weak(fam) and not family_code.is_weak(adm)
    assert family_code.is_weak("0042") and family_code.is_weak("ABCD2345")
    assert len({family_code.mint_family() for _ in range(50)}) == 50


def test_typing_is_forgiving_but_legacy_codes_compare_exactly():
    norm = family_code.normalize
    assert norm("maple-otter-lantern-comet") == norm("Maple otter  LANTERN.comet") \
        == norm(" maple_otter-lantern, comet ") == "MAPLEOTTERLANTERNCOMET"
    assert norm("abcd2345") == "ABCD2345" and norm("0042") == "0042"


def test_secret_files_are_born_private_and_tightened(tmp_path):
    target = tmp_path / "secret.txt"
    secret_files.write_private(target, "x")
    assert _mode(target) == 0o600 and target.read_text() == "x"
    loose = tmp_path / "loose.txt"
    loose.write_text("y")
    os.chmod(loose, 0o664)
    assert secret_files.tighten([loose, tmp_path / "missing"]) == [loose]
    assert _mode(loose) == 0o600
    assert secret_files.ensure(target, lambda: "new") is False and target.read_text() == "x"
    assert secret_files.ensure(tmp_path / "made.txt", lambda: "new") is True
    assert _mode(tmp_path / "made.txt") == 0o600


# ---------------------------------------------------------------- fresh install

def test_fresh_install_mints_word_codes_all_private_and_never_logs_them(fresh):
    app, log = fresh
    fam, adm = _family(app), _admin(app)
    assert len(fam.split("-")) == 4 and len(adm.split("-")) == 6 and fam != adm
    for path in (app.ACCESS_CODE_FILE, app.PIN_FILE, app.ACCESS_SECRET_FILE, app.LOCAL_TOKEN_FILE):
        assert _mode(path) == 0o600, path.name
    assert fam not in log and adm not in log and family_code.normalize(fam) not in log
    assert "access-code.txt" in log and "admin-pin.txt" in log     # where, not what
    assert "short and guessable" not in log


def test_host_header_and_network_grant_nothing(fresh):
    app, _ = fresh
    for base in ("http://127.0.0.1", "http://localhost", "http://127.0.0.1:7863",
                 "http://studio.example"):
        client = TestClient(app.app, base_url=base)
        assert client.get("/api/queue").status_code == 401, base
        assert client.get("/api/gallery").status_code == 401, base
        assert client.get("/", headers={"Accept": "text/html"}).status_code == 401, base
    # a tailnet address configured as this machine's own is no pass either
    tailnet = TestClient(app.app, base_url="http://100.101.1.1:7863",
                         headers={"Host": "100.101.1.1"})
    assert tailnet.get("/api/queue").status_code == 401
    assert app.request_role(SimpleNamespace(cookies={}, headers={"host": "localhost"})) == ""


def test_local_token_is_the_only_code_free_way_in_and_only_family(fresh):
    app, _ = fresh
    token = app.LOCAL_TOKEN_FILE.read_text().strip()
    assert token and token == app.LOCAL_TOKEN and len(token) >= 32
    client = TestClient(app.app, base_url="http://127.0.0.1")
    assert client.get("/api/queue", headers={local_token.HEADER: "wrong"}).status_code == 401
    assert client.get("/api/queue", headers={local_token.HEADER: token}).status_code == 200
    me = client.get("/api/me", headers={local_token.HEADER: token}).json()
    assert me["owner"] is False
    refused = client.post("/api/providers", json={"provider": "fal", "enabled": False},
                          headers={local_token.HEADER: token})
    assert refused.status_code == 403 and refused.json()["owner_only"] is True


def test_gate_page_speaks_of_the_family_code(fresh):
    app, _ = fresh
    page = TestClient(app.app, base_url="https://studio.example").get(
        "/", headers={"Accept": "text/html"})
    assert page.status_code == 401
    assert "family code" in page.text and 'maxlength="80"' in page.text
    assert "Spaces, dashes and capitals don't matter" in page.text


def test_family_code_is_one_permission_set_without_server_settings(fresh):
    app, _ = fresh
    client = _client(app, code=_family(app))
    me = client.get("/api/me").json()
    assert me == {"role": "admin", "owner": False}      # queue controls, not settings
    assert client.get("/api/queue").status_code == 200
    assert client.get("/api/gallery").status_code == 200
    assert client.get("/api/admin/check").status_code == 200     # studio manager
    for method, path, body in (
            ("post", "/api/providers", {"provider": "fal", "api_key": "fal-test-key-123456"}),
            ("post", "/api/setup/install", {"engines": ["image"]}),
            ("post", "/api/residency/plan", {"profile": "qwen-only"}),
            ("post", "/api/residency/apply", {"profile": "qwen-only"}),
            ("post", "/api/admin/family-code", None)):
        response = getattr(client, method)(path, json=body) if body is not None \
            else getattr(client, method)(path)
        assert response.status_code == 403, path
        assert response.json()["owner_only"] is True, path
        assert "admin code" in response.json()["error"], path
    # the family code in X-Lab-Pin is not the admin code either
    assert client.post("/api/providers", json={"provider": "fal", "enabled": False},
                       headers={"X-Lab-Pin": _family(app)}).status_code == 403


def test_admin_code_opens_server_settings_and_writes_them_private(fresh):
    app, _ = fresh
    client = _client(app, code=_admin(app).upper().replace("-", " "))   # forgiving typing
    assert client.get("/api/me").json() == {"role": "admin", "owner": True}
    saved = client.post("/api/providers", json={"provider": "fal", "api_key": "fal-test-key-123456"})
    assert saved.status_code == 200, saved.text
    assert _mode(app.PROVIDERS_FILE) == 0o600
    assert json.loads(app.PROVIDERS_FILE.read_text())["fal"]["api_key"] == "fal-test-key-123456"
    assert client.post("/api/providers", json={"provider": "fal", "api_key": ""}).status_code == 200


def test_owner_script_can_use_the_admin_code_header_without_a_cookie(fresh):
    app, _ = fresh
    client = _client(app)
    ok = client.post("/api/providers", json={"provider": "fal", "enabled": False},
                     headers={"X-Lab-Pin": _admin(app)})
    assert ok.status_code == 200, ok.text
    assert client.get("/api/queue", headers={"X-Lab-Pin": "not-the-code"}).status_code == 401


def test_forgiving_typing_and_one_year_passes(fresh):
    app, _ = fresh
    typed = "  " + _family(app).upper().replace("-", "  ") + " "
    client = _client(app)
    signed = client.post("/api/gate", json={"code": typed})
    assert signed.status_code == 200 and signed.json()["role"] == "user"
    cookie = signed.headers["set-cookie"]
    assert f"mlab_access=" in cookie and f"Max-Age={365 * 24 * 3600}" in cookie
    passes = client.post("/api/gate", json={"code": typed, "studio_library": True,
                                            "studio_render": True, "studio_device": "b" * 32}).json()
    assert passes["expiresIn"] == passes["renderExpiresIn"] == 365 * 24 * 3600
    # a pass issued 200 days ago still reads the Library; one from 400 days ago does not
    old = studio_library.ticket(app.ACCESS_SECRET, "user", app.ACCESS_CODE,
                                now=time.time() - 200 * 86400)
    ancient = studio_library.ticket(app.ACCESS_SECRET, "user", app.ACCESS_CODE,
                                    now=time.time() - 400 * 86400)
    bare = _client(app)
    assert bare.get("/api/studio/library", headers={"Authorization": f"Bearer {old}"}).status_code == 200
    assert bare.get("/api/studio/library", headers={"Authorization": f"Bearer {ancient}"}).status_code == 401


def test_rotating_the_family_code_signs_every_device_out_at_once(fresh):
    app, _ = fresh
    old_code = _family(app)
    phone = _client(app, code=old_code)
    paired = _client(app).post(
        "/api/gate", json={"code": old_code, "studio_library": True}).json()["token"]
    owner = _client(app, code=_admin(app))
    rotated = owner.post("/api/admin/family-code")
    assert rotated.status_code == 200
    new_code = rotated.json()["family_code"]
    assert new_code != old_code and len(new_code.split("-")) == 4
    assert _family(app) == new_code and _mode(app.ACCESS_CODE_FILE) == 0o600
    # every old family device is out: the browser cookie and the app's pass
    assert phone.get("/api/queue").status_code == 401
    bare = _client(app)
    assert bare.get("/api/studio/library", headers={"Authorization": f"Bearer {paired}"}).status_code == 401
    assert bare.post("/api/gate", json={"code": old_code}).status_code == 403
    # the owner's own admin session is untouched, and the new code works
    assert owner.get("/api/me").json()["owner"] is True
    assert _client(app, code=new_code).get("/api/queue").status_code == 200


def test_a_code_file_rewritten_by_the_cli_is_live_without_a_restart(fresh):
    app, _ = fresh
    signed_in = _client(app, code=_family(app))
    new_code = family_code.mint_family()
    secret_files.write_private(app.ACCESS_CODE_FILE, new_code + "\n")
    app._codes_seen["at"] = 0.0          # skip the one-second throttle
    assert signed_in.get("/api/queue").status_code == 401
    assert _client(app, code=new_code).get("/api/queue").status_code == 200
    # an emptied code file never opens the door: the last code stays in force
    app.ACCESS_CODE_FILE.write_text("")
    app._codes_seen["at"] = 0.0
    assert _client(app).post("/api/gate", json={"code": ""}).status_code in (403, 422)
    assert _client(app, code=new_code).get("/api/queue").status_code == 200
    secret_files.write_private(app.ACCESS_CODE_FILE, new_code + "\n")


def test_throttle_identity_cannot_be_forged_from_the_tailnet(fresh, monkeypatch):
    app, _ = fresh
    monkeypatch.setattr(app, "PUBLIC_HOSTS", {"studio.example.com", "second.example.org"})

    def request(peer, host, cf=None):
        headers = {"host": host}
        if cf:
            headers["cf-connecting-ip"] = cf
        return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None,
                               headers=headers)

    # through our own proxy (cloudflared / tailscale serve) on a public host:
    # the edge's CF-Connecting-IP — every public hostname, not just the first
    assert app._client_ip(request("127.0.0.1", "studio.example.com", "203.0.113.9")) == "203.0.113.9"
    assert app._client_ip(request("127.0.0.1", "second.example.org", "203.0.113.7")) == "203.0.113.7"
    # a direct tailnet/LAN client forging the public Host + CF header is keyed by
    # its own address, so it cannot mint a fresh backoff identity per guess
    assert app._client_ip(request("100.101.1.1", "studio.example.com", "198.51.100.1")) == "100.101.1.1"
    assert app._client_ip(request("192.168.1.20", "studio.example.com", "198.51.100.2")) == "192.168.1.20"
    # our proxy on a non-public host: no identity (shared key), never a CF header
    assert app._client_ip(request("127.0.0.1", "spark.internal", "198.51.100.3")) == ""
    assert app._client_ip(request(None, "studio.example.com", "203.0.113.5")) == "203.0.113.5"


def test_an_ipv6_visitor_cannot_rotate_through_its_own_64(fresh, monkeypatch):
    """Home broadband and phones get a whole IPv6 /64 and may use any address
    in it. Keyed per address, every guess could come from a new one and start
    with two free tries; keyed per /64 they all share one backoff."""
    app, _ = fresh
    monkeypatch.setattr(app, "PUBLIC_HOSTS", {"studio.example.com"})

    def request(peer, cf):
        return SimpleNamespace(client=SimpleNamespace(host=peer),
                               headers={"host": "studio.example.com", "cf-connecting-ip": cf})

    one = app._client_ip(request("127.0.0.1", "2a01:4f8:c0c:1a2b::1"))
    assert one == "2a01:4f8:c0c:1a2b::/64"
    assert app._client_ip(request("127.0.0.1", "2a01:4f8:c0c:1a2b:dead:beef:1:2")) == one
    assert app._client_ip(request("127.0.0.1", "2A01:4F8:C0C:1A2B::9")) == one
    assert app._client_ip(request("127.0.0.1", "2a01:4f8:c0c:1a2c::1")) != one     # a neighbour
    assert app._client_ip(request("127.0.0.1", "::ffff:203.0.113.9")) == "203.0.113.9"
    assert app._client_ip(request("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    # tailnet and other private IPv6 peers keep a key of their own
    assert app._client_ip(request("fd7a:115c:a1e0::1", "")) == "fd7a:115c:a1e0::1"
    assert app._client_ip(request("fd7a:115c:a1e0::2", "")) == "fd7a:115c:a1e0::2"
    assert app._client_ip(request("127.0.0.1", "not-an-address")) == "not-an-address"

    # end to end: a new address in the same /64 for every wrong guess backs off
    codes = []
    for i in range(5):
        client = TestClient(app.app, base_url="https://studio.example.com")   # no device cookie
        codes.append(client.post("/api/gate", json={"code": f"wrong-guess-v6-{i}"},
                                 headers={"cf-connecting-ip": f"2a01:4f8:c0c:7777::{i + 1:x}"}).status_code)
    assert codes == [403, 403, 429, 429, 429], codes


def test_the_lockout_still_guards_the_family_code(fresh):
    app, _ = fresh
    client = _client(app)
    codes = [client.post("/api/gate", json={"code": f"wrong-guess-{i}"}).status_code
             for i in range(4)]
    assert codes == [403, 403, 429, 429]                  # 3 wrong in 10 min: shut for 1 h
    assert client.post("/api/gate", json={"code": _family(app)}).status_code == 429


# ---------------------------------------------------------------- existing installs

def test_legacy_codes_keep_working_until_rotated_and_files_are_tightened(legacy):
    app, log = legacy
    assert _family(app) == LEGACY_FAMILY and _admin(app) == LEGACY_ADMIN   # not rewritten
    assert _client(app, code=LEGACY_FAMILY.lower()).get("/api/me").json()["owner"] is False
    assert _client(app, code=LEGACY_ADMIN).get("/api/me").json()["owner"] is True
    for name in ("access-code.txt", "admin-pin.txt", "access-secret.txt", "providers.json",
                 "vapid_private.pem", "local-token.txt"):
        assert _mode(app.ROOT / name) == 0o600, name
    # the log says the codes are weak and how to fix it, without the codes
    assert "the family code is short and guessable" in log
    assert "the admin code is short and guessable" in log
    assert "media-lab code --rotate --admin" in log
    assert LEGACY_FAMILY not in log and f" {LEGACY_ADMIN}" not in log


def test_old_role_cookies_survive_the_upgrade(legacy):
    app, _ = legacy
    # a cookie minted by the previous build (same secret, same stored code)
    iat = str(int(time.time()) - 86400)
    sig = app.hmac.new(app.ACCESS_SECRET.encode(),
                       f"mlab2:user:{iat}:{LEGACY_FAMILY}".encode(),
                       app.hashlib.sha256).hexdigest()[:32]
    client = _client(app)
    client.cookies.set(app.SESSION_COOKIE, f"user.{iat}.{sig}")
    assert client.get("/api/queue").status_code == 200


# ---------------------------------------------------------------- local tools

def test_local_token_file_is_private_and_only_sent_to_this_machine(tmp_path, monkeypatch):
    import urllib.request
    from media_lab_core import local_config
    monkeypatch.setattr(local_config, "own_addresses", lambda: {"127.0.0.1", "localhost", "10.9.8.7"})
    token = local_token.ensure(tmp_path)
    assert len(token) == 64 and _mode(tmp_path / "local-token.txt") == 0o600
    assert local_token.ensure(tmp_path) == token                    # stable across starts
    for url in ("http://127.0.0.1:7863/api/queue", "http://localhost:7863/x",
                "http://10.9.8.7:7863/api/queue", "http://[::1]:7863/"):
        assert local_token.headers_for(url, tmp_path) == {local_token.HEADER: token}, url
    for url in ("https://studio.example.com/api/queue", "http://10.9.8.8:7863/",
                "http://127.0.0.1.evil.example/"):
        assert local_token.headers_for(url, tmp_path) == {}, url
    req = local_token.authorize(urllib.request.Request("http://127.0.0.1:7863/api/queue"), tmp_path)
    assert req.unredirected_hdrs["X-media-lab-local"] == token
    assert local_token.matches(token, token) and not local_token.matches(token, "")
    assert not local_token.matches("", "") and not local_token.matches("x", token)
    assert local_token.read(tmp_path / "nowhere") == ""


def test_local_token_goes_to_the_studio_port_only(tmp_path, monkeypatch):
    """Another service on this box is not the studio. The text-model port on a
    Spark is a bridge that forwards every request header to a second machine,
    and runners that install the token handler also call it."""
    import urllib.request
    from media_lab_core import local_config
    monkeypatch.setattr(local_config, "own_addresses", lambda: {"127.0.0.1", "localhost", "10.9.8.7"})
    monkeypatch.delenv("MEDIA_LAB_PORT", raising=False)
    token = local_token.ensure(tmp_path)
    for url in ("http://127.0.0.1:8004/v1/chat/completions", "http://10.9.8.7:8290/health",
                "http://localhost/api/queue", "https://127.0.0.1/api/queue",
                "http://evil.example\\@127.0.0.1:7863/", "http://user@127.0.0.1:7863/",
                "ftp://127.0.0.1:7863/", "http://127.0.0.1:notaport/"):
        assert local_token.headers_for(url, tmp_path) == {}, url
    req = local_token.authorize(urllib.request.Request("http://127.0.0.1:8004/v1/models"), tmp_path)
    assert local_token.HEADER.capitalize() not in req.unredirected_hdrs
    # a studio that install.sh put on another port still gets it
    (tmp_path / "install.json").write_text(json.dumps({"port": 7870}))
    assert local_token.headers_for("http://127.0.0.1:7870/api/queue", tmp_path) == {local_token.HEADER: token}
    monkeypatch.setenv("MEDIA_LAB_PORT", "7999")
    assert local_token.headers_for("http://localhost:7999/api/queue", tmp_path) == {local_token.HEADER: token}
    assert local_token.headers_for("http://127.0.0.1:8004/v1/models", tmp_path) == {}


def test_runners_that_call_the_studio_carry_the_token_not_a_host_header():
    for rel in ("runner/queue_watchdog.py", "runner/verify_companion_snapshot.py",
                "runner/verify_pplx_ltx_parallel.py",
                "runner/queue_storyboard_assembly.py", "media_lab_core/cut_cli.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "local_token" in text, rel
        # no code path sends a forged Host header any more (prose may mention it)
        assert '{"Host": "localhost"}' not in text and "-H 'Host: localhost'" not in text, rel


def test_deploy_probe_uses_the_token_and_never_reads_a_refusal_as_idle():
    text = (REPO / "tools" / "deploy-spark.sh").read_text()
    assert "local-token.txt" in text and "-H @-" in text           # token via stdin, not argv
    # the legacy header survives only as the fallback for a pre-token app
    legacy = [line for line in text.splitlines()
              if "Host: localhost" in line and not line.lstrip().startswith("#")]
    assert len(legacy) == 1 and line_is_else_branch(legacy[0])
    assert "'/local-token.txt'" in text                             # the box owns it
    assert 'print(-2)' in text and '"-2"' in text and "not deploying blind" in text


def line_is_else_branch(line: str) -> bool:
    return line.strip().startswith("else curl")


def test_the_dev_proxy_never_lends_its_session_to_the_network(monkeypatch):
    import importlib
    monkeypatch.delenv("HOST", raising=False)
    sys.path.insert(0, str(REPO))
    try:
        local_studio = importlib.import_module("local_studio")
        local_studio = importlib.reload(local_studio)
    finally:
        sys.path.remove(str(REPO))
    assert local_studio.HOST == "127.0.0.1" and local_studio._lend_session()
    for host in ("0.0.0.0", "100.101.1.1", "192.168.1.20"):
        monkeypatch.setattr(local_studio, "HOST", host)
        assert not local_studio._lend_session(), host
