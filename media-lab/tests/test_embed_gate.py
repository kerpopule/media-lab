"""Media Lab inside the VibeX Studio app: framing headers and the embed handshake.

Unit cases for media_lab_core/embed_gate.py, then the whole door over HTTP
against app.py under a disposable HOME, reached as a public HTTPS host
(studio.example) so nothing is trusted by Host and the partitioned cookie
path is the one exercised.
"""

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_lab_core import embed_gate, studio_jobs

SECRET = "s" * 64
CODES = {"user": "USERCODE", "admin": "1234"}
role_code = CODES.__getitem__
APP = "https://app.example"
DEVICE = "0" * 32


def _req(headers=None, cookies=None, method="GET"):
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(headers=h, cookies=cookies or {}, method=method)


# ---------------------------------------------------------------------------
# unit
# ---------------------------------------------------------------------------

def test_origins_are_plain_and_cannot_split_a_csp_header():
    assert embed_gate.normalize_origin("https://App.Example/") == "https://app.example"
    assert embed_gate.normalize_origin("tauri://localhost") == "tauri://localhost"
    assert embed_gate.normalize_origin("http://localhost:8098") == "http://localhost:8098"
    for bad in ("https://a.example/path", "https://a.example; script-src *", "https://a.example 'unsafe-inline'",
                "javascript:alert(1)", "", "*", "https://*.example", "https://a.example,https://b.example"):
        assert embed_gate.normalize_origin(bad) == "", bad


def test_frame_ancestors_lists_only_configured_apps_unless_reached_over_loopback():
    origins = ["tauri://localhost", "http://tauri.localhost", APP, "https://bad.example; frame-ancestors *"]
    public = embed_gate.frame_headers("studio.example", origins)
    assert public["Content-Security-Policy"] == \
        "frame-ancestors 'self' tauri://localhost http://tauri.localhost https://app.example"
    assert public["X-Frame-Options"] == "SAMEORIGIN"
    local = embed_gate.frame_headers("127.0.0.1:7863", origins)["Content-Security-Policy"]
    assert local.endswith("http://localhost:* http://127.0.0.1:*")
    assert "localhost:*" not in embed_gate.frame_headers("100.64.0.1:7863", origins)["Content-Security-Policy"]


def test_parent_allowed():
    origins = ["tauri://localhost", APP]
    assert embed_gate.parent_allowed("tauri://localhost", "studio.example", origins)
    assert embed_gate.parent_allowed("https://app.example/", "studio.example", origins)
    assert not embed_gate.parent_allowed("https://evil.example", "studio.example", origins)
    assert not embed_gate.parent_allowed("http://localhost:8081", "studio.example", origins)
    assert embed_gate.parent_allowed("http://localhost:8081", "localhost:7863", origins)
    assert not embed_gate.parent_allowed("http://localhost.evil.example", "localhost:7863", origins)
    assert not embed_gate.parent_allowed(embed_gate.NATIVE_PARENT, "localhost:7863", origins)


def test_next_path_stays_on_this_studio():
    assert embed_gate.safe_next("/cut?project=p1&embed=1") == "/cut?project=p1&embed=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:x", "",
                "/embed?next=/embed", "/a\r\nSet-Cookie: x", "/" + "a" * 3000):
        assert embed_gate.safe_next(bad) == embed_gate.DEFAULT_NEXT, bad


def test_ticket_round_trip_is_single_use_and_bound_to_its_app():
    now = 1_790_000_000
    book = embed_gate.TicketBook()
    kw = dict(secret=SECRET, role_code=role_code, book=book, request_host="studio.example", origins=[APP])
    t = embed_gate.mint_ticket(SECRET, "admin", role_code, APP, now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="different app"):
        embed_gate.redeem_ticket(t, "https://evil.example", now=now, **kw)
    assert embed_gate.redeem_ticket(t, APP, now=now + 5, **kw) == "admin"
    with pytest.raises(embed_gate.EmbedRefused, match="already used"):
        embed_gate.redeem_ticket(t, APP, now=now + 6, **kw)
    late = embed_gate.mint_ticket(SECRET, "user", role_code, APP, now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="expired"):
        embed_gate.redeem_ticket(late, APP, now=now + embed_gate.TICKET_TTL + 1, **kw)
    forged = t.replace(".admin.", ".user.")
    with pytest.raises(embed_gate.EmbedRefused, match="not valid"):
        embed_gate.redeem_ticket(forged, APP, now=now, **kw)
    # rotating the code kills a ticket already minted
    other = embed_gate.mint_ticket(SECRET, "user", role_code, APP, now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="not valid"):
        embed_gate.redeem_ticket(other, APP, now=now, **(kw | {"role_code": lambda r: "ROTATED"}))
    # an app that was allowed when the ticket was minted but no longer is
    gone = embed_gate.mint_ticket(SECRET, "user", role_code, APP, now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="not allowed"):
        embed_gate.redeem_ticket(gone, APP, now=now, **(kw | {"origins": []}))


def test_native_tickets_only_redeem_through_the_phone_bridge():
    now = 1_790_000_000
    kw = dict(secret=SECRET, role_code=role_code, book=embed_gate.TicketBook(), request_host="studio.example",
              origins=[APP], now=now)
    native = embed_gate.mint_ticket(SECRET, "user", role_code, "", now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="phone app"):
        embed_gate.redeem_ticket(native, APP, **kw)
    assert embed_gate.redeem_ticket(native, embed_gate.NATIVE_PARENT, **kw) == "user"
    browser = embed_gate.mint_ticket(SECRET, "user", role_code, APP, now=now)
    with pytest.raises(embed_gate.EmbedRefused, match="different app"):
        embed_gate.redeem_ticket(browser, embed_gate.NATIVE_PARENT, **kw)


def test_ticket_book_is_bounded():
    book = embed_gate.TicketBook(limit=2)
    assert book.claim("a", 100, 0) and book.claim("b", 100, 0)
    assert not book.claim("c", 100, 0)
    assert book.claim("c", 300, 200)      # expired entries were dropped


def test_session_cookie_value():
    now = 1_790_000_000
    token = embed_gate.session_token(SECRET, "user", role_code, now=now)
    assert embed_gate.session_age(token, SECRET, role_code, now=now + 10) == ("user", 10)
    assert embed_gate.session_age(token, SECRET, role_code, now=now + embed_gate.COOKIE_MAX_AGE + 1)[0] == ""
    assert embed_gate.session_age(token.replace(".user.", ".admin."), SECRET, role_code, now=now)[0] == ""
    assert embed_gate.session_age(token, SECRET, lambda r: "ROTATED", now=now)[0] == ""
    # not interchangeable with the studio's own session cookie format
    assert embed_gate.session_age(f"user.{now}.{'a' * 32}", SECRET, role_code, now=now)[0] == ""


def test_cookie_counts_only_on_the_studio_pages_own_requests():
    now = time.time()
    raw = embed_gate.session_token(SECRET, "user", role_code, now=now)
    jar = {embed_gate.COOKIE: raw}
    ok = lambda h, m="GET": embed_gate.cookie_role(_req(h, jar, m), SECRET, role_code)  # noqa: E731
    assert ok({"Sec-Fetch-Site": "same-origin"}, "POST") == "user"
    assert ok({"Sec-Fetch-Site": "none"}) == "user"
    assert ok({"Sec-Fetch-Site": "cross-site"}) == ""
    assert ok({"Sec-Fetch-Site": "same-site"}, "POST") == ""
    # browsers without Fetch Metadata: Origin must be this host on a write
    assert ok({"Host": "studio.example", "Origin": "https://studio.example"}, "POST") == "user"
    assert ok({"Host": "studio.example", "Origin": "https://evil.example"}, "POST") == ""
    assert ok({"Host": "studio.example", "Origin": "null"}, "POST") == ""
    # no browser metadata at all: the phone app's own fetch sharing its WebView jar
    assert ok({"Host": "studio.example"}, "POST") == "user"
    # but redeeming a ticket always needs the studio page's own request
    assert not embed_gate.same_origin_post(_req({"Host": "studio.example"}, method="POST"))
    assert not embed_gate.same_origin_post(_req({"Sec-Fetch-Site": "none"}, method="POST"))
    assert embed_gate.same_origin_post(_req({"Sec-Fetch-Site": "same-origin"}, method="POST"))


def test_cookie_header():
    secure = embed_gate.cookie_header("v", True)
    assert secure.startswith("mlab_embed=v; Path=/; Max-Age=86400; HttpOnly")
    assert "Secure" in secure and "SameSite=None" in secure and "Partitioned" in secure
    plain = embed_gate.cookie_header("v", False)
    assert "SameSite=Lax" in plain and "Secure" not in plain and "Partitioned" not in plain


def test_render_pass_is_the_only_pass_that_mints():
    render = studio_jobs.ticket(SECRET, "user", CODES["user"], DEVICE)
    assert embed_gate.pass_role(f"Bearer {render}", SECRET, role_code) == "user"
    assert embed_gate.pass_role(f"bearer {render}", SECRET, role_code) == ""
    assert embed_gate.pass_role("Bearer mlab-library-v1.user.1790000000." + "a" * 64, SECRET, role_code) == ""
    assert embed_gate.pass_role(f"Bearer {render}", SECRET, lambda r: "ROTATED") == ""
    # a pass older than the default 30 days still mints on a host that keeps passes longer
    old = studio_jobs.ticket(SECRET, "user", CODES["user"], DEVICE, now=time.time() - 40 * 86400)
    assert embed_gate.pass_role(f"Bearer {old}", SECRET, role_code) == ""
    assert embed_gate.pass_role(f"Bearer {old}", SECRET, role_code, max_age=365 * 86400) == "user"


def test_bootstrap_page_cannot_be_broken_out_of():
    page = embed_gate.bootstrap_page([APP, "http://localhost:*"], False, "/?embed=1").body.decode()
    config = json.loads(re.search(r"var CFG=(\{.*?\});", page).group(1))
    assert config == {"parents": [APP], "loopback": False, "next": "/?embed=1",
                      "nativeParent": embed_gate.NATIVE_PARENT}
    hostile = embed_gate.bootstrap_page([APP], False, "/</script><script>alert(1)</script>").body.decode()
    assert "</script><script>alert(1)" not in hostile
    assert hostile.count("</script>") == 1


# ---------------------------------------------------------------------------
# over HTTP, against the real app
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    repo = Path(__file__).parents[1]
    home = tmp_path_factory.mktemp("embed-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config", "prompt-templates"):
        (root / name).symlink_to(repo / name)
    env = {"HOME": str(home), "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS": "1",
           "MEDIA_LAB_PUBLIC_HOSTS": "studio.example", "MEDIA_LAB_BROWSER_ORIGINS": APP,
           "MEDIA_LAB_TAILNET_HOST": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("embed_gate_test_app", repo / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["embed_gate_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


def _client(studio, base="https://studio.example"):
    return TestClient(studio.app, base_url=base)


def _render_pass(client, studio):
    r = client.post("/api/gate", json={"code": studio.ACCESS_CODE, "studio_render": True, "studio_device": DEVICE},
                    headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


SAME = {"Sec-Fetch-Site": "same-origin", "Origin": "https://studio.example"}


def test_every_page_carries_the_framing_rules(studio):
    c = _client(studio)
    for path in ("/", "/embed", "/manifest.json", "/api/embed/status", "/gate"):
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.headers["content-security-policy"] == \
            "frame-ancestors 'self' tauri://localhost http://tauri.localhost https://app.example", path
        assert r.headers["x-frame-options"] == "SAMEORIGIN", path
    local = _client(studio, "http://127.0.0.1:7863").get("/embed")
    assert "http://localhost:*" in local.headers["content-security-policy"]


def test_manifest_advertises_the_in_app_studio(studio):
    assert _client(studio).get("/manifest.json").json()["vibexEmbed"] == 1


def test_a_signed_out_frame_is_sent_to_the_handshake_not_the_code_screen(studio):
    c = _client(studio)
    framed = c.get("/?embed=1&job=j1", headers={"Accept": "text/html", "Sec-Fetch-Dest": "iframe"},
                   follow_redirects=False)
    assert framed.status_code == 303
    assert framed.headers["location"] == "/embed?next=%2F%3Fembed%3D1%26job%3Dj1"
    top = c.get("/", headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"}, follow_redirects=False)
    assert top.status_code == 401 and "/api/gate" in top.text      # a normal tab keeps the code screen
    page = c.get("/embed?next=%2F%2Fevil.example")
    assert page.status_code == 200 and '"next": "/?embed=1"' in page.text
    assert "private" in page.headers["cache-control"]


def test_ticket_needs_a_generation_pass_and_a_listed_app(studio):
    c = _client(studio)
    assert c.post("/api/embed/ticket", headers={"Origin": APP}).status_code == 401
    assert c.post("/api/embed/ticket", headers={"Origin": APP, "Authorization": "Bearer nope"}).status_code == 401
    token = _render_pass(c, studio)
    refused = c.post("/api/embed/ticket", headers={"Origin": "https://evil.example", "Authorization": f"Bearer {token}"})
    assert refused.status_code == 403 and refused.json()["error"] == "origin-not-allowed"
    ok = c.post("/api/embed/ticket", headers={"Origin": APP, "Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.json()["expiresIn"] == embed_gate.TICKET_TTL
    assert ok.headers["access-control-allow-origin"] == "*"
    assert "Authorization" in ok.headers["access-control-allow-headers"]
    pre = c.options("/api/embed/ticket", headers={"Origin": APP, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "authorization"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"


def test_the_full_handshake_signs_the_frame_in_with_a_partitioned_cookie(studio):
    app_side = _client(studio)
    token = _render_pass(app_side, studio)
    ticket = app_side.post("/api/embed/ticket", headers={"Origin": APP, "Authorization": f"Bearer {token}"}).json()["ticket"]

    frame = _client(studio)   # the frame's own cookie jar: nothing yet
    assert frame.get("/api/embed/status", headers=SAME).json() == {"signedIn": False, "renew": False}
    assert frame.get("/api/queue", headers=SAME).status_code == 401
    # a cross-site form post cannot redeem (login CSRF)
    csrf = frame.post("/api/embed/redeem", json={"ticket": ticket, "parent": APP},
                      headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
    assert csrf.status_code == 403
    wrong_parent = frame.post("/api/embed/redeem", json={"ticket": ticket, "parent": "https://evil.example"}, headers=SAME)
    assert wrong_parent.status_code == 403
    # the refusal above did not burn the ticket: parent is checked before it is claimed
    r = frame.post("/api/embed/redeem", json={"ticket": ticket, "parent": APP}, headers=SAME)
    assert r.status_code == 200 and r.json() == {"ok": True, "partitioned": True}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("mlab_embed=e1.user.")
    for attr in ("HttpOnly", "Secure", "SameSite=None", "Partitioned", "Path=/"):
        assert attr in cookie, attr
    raw = cookie.split(";", 1)[0].split("=", 1)[1]
    jar = {"Cookie": f"mlab_embed={raw}"}
    fresh = _client(studio)
    assert fresh.get("/api/embed/status", headers=SAME | jar).json() == {"signedIn": True, "renew": False}
    assert fresh.get("/api/me", headers=SAME | jar).json()["role"] == "admin"
    assert fresh.get("/api/queue", headers=SAME | jar).status_code == 200
    assert fresh.get("/", headers={"Sec-Fetch-Site": "same-origin", "Accept": "text/html"} | jar).status_code == 200
    # the same cookie, sent by any other site, opens nothing
    assert fresh.get("/api/queue", headers={"Sec-Fetch-Site": "cross-site"} | jar).status_code == 401
    assert fresh.post("/api/jobs/x/cancel", headers={"Sec-Fetch-Site": "cross-site"} | jar).status_code == 401
    # replay
    again = _client(studio).post("/api/embed/redeem", json={"ticket": ticket, "parent": APP}, headers=SAME)
    assert again.status_code == 403 and "already used" in again.json()["error"]


def test_an_old_embed_session_asks_for_a_fresh_ticket(studio):
    old = embed_gate.session_token(studio.ACCESS_SECRET, "user", studio._role_code,
                                   now=time.time() - embed_gate.RENEW_AFTER - 60)
    status = _client(studio).get("/api/embed/status", headers=SAME | {"Cookie": f"mlab_embed={old}"}).json()
    assert status == {"signedIn": True, "renew": True}


def test_plain_http_studio_gets_a_lax_cookie(studio):
    c = _client(studio, "http://studio.lan:7863")
    token = _render_pass(c, studio)
    ticket = c.post("/api/embed/ticket", headers={"Authorization": f"Bearer {token}"}).json()["ticket"]
    r = c.post("/api/embed/redeem", json={"ticket": ticket, "parent": embed_gate.NATIVE_PARENT},
               headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and r.json()["partitioned"] is False
    assert "SameSite=Lax" in r.headers["set-cookie"] and "Secure" not in r.headers["set-cookie"]


def test_loopback_is_not_trusted_but_can_still_embed_through_the_handshake(studio):
    # one family login: no Host/loopback trust, so even a local frame signs in
    local = _client(studio, "http://127.0.0.1:7863")
    assert local.get("/api/embed/status", headers=SAME).json()["signedIn"] is False
    framed = local.get("/", headers={"Accept": "text/html", "Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert framed.status_code == 303 and framed.headers["location"].startswith("/embed?next=")


def test_a_year_old_family_pass_still_mints_a_ticket(studio):
    old = studio_jobs.ticket(studio.ACCESS_SECRET, "user", studio._role_code("user"), DEVICE,
                             now=time.time() - 200 * 86400)
    r = _client(studio).post("/api/embed/ticket", headers={"Origin": APP, "Authorization": f"Bearer {old}"})
    assert r.status_code == 200, r.text


def test_the_studio_page_ships_embed_mode(studio):
    c = _client(studio, "http://127.0.0.1:7863")
    assert c.post("/api/gate", json={"code": studio.ACCESS_CODE}).status_code == 200
    html = c.get("/").text
    assert "window.MEDIALAB_EMBED" in html and "html.embed nav" in html
    assert "html.embed .mlc-fab" in html
    assert "!window.MEDIALAB_EMBED" in html      # no service worker inside the app
