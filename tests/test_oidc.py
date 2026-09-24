"""Sign-in through Authentik (OIDC), tested against a fake provider that behaves like Authentik's token endpoint."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from clothing_advisor import oidc as oidc_module
from clothing_advisor.config import Config
from clothing_advisor.oidc import Oidc, OidcDenied, OidcError
from clothing_advisor.web import create_app

from .conftest import TOKEN

ISSUER = "https://auth.example/application/o/clothing-advisor/"
CALLBACK = "https://ca.example/app/oidc/callback"
CALLBACK_TESTSERVER = "https://testserver/app/oidc/callback"
ADMIN, VIEWER = "clothing-advisor-admin", "clothing-advisor-viewer"
NOW = 1_800_000_000.0


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(claims: dict) -> str:
    return ".".join([b64(b'{"alg":"RS256","typ":"JWT"}'), b64(json.dumps(claims).encode()), b64(b"not-checked")])


class FakeProvider:
    """Just enough of Authentik: discovery, token and userinfo endpoints."""

    def __init__(self, groups=(ADMIN,), name="Joren", email="joren@example.org", groups_in_token=True):
        self.groups, self.name, self.email, self.groups_in_token = list(groups), name, email, groups_in_token
        self.nonce = ""
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.token_status = 200
        self.override: dict = {}
        self.issuer_in_discovery = ISSUER
        self.down = False

    def id_claims(self) -> dict:
        claims = {"iss": ISSUER, "aud": "clothing-advisor", "sub": "user-uuid-1", "exp": NOW + 600, "iat": NOW,
                  "nonce": self.nonce, "name": self.name, "email": self.email}
        if self.groups_in_token:
            claims["groups"] = self.groups
        claims.update(self.override)
        return claims

    def fetch(self, method, url, headers, body):
        if self.down:
            raise OSError("connection refused")
        parsed = urlparse(url)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()} if body else {}
        self.calls.append((method, url, headers, form))
        if parsed.path.endswith("/.well-known/openid-configuration"):
            return 200, json.dumps({
                "issuer": self.issuer_in_discovery, "authorization_endpoint": "https://auth.example/application/o/authorize/",
                "token_endpoint": "https://auth.example/application/o/token/",
                "userinfo_endpoint": "https://auth.example/application/o/userinfo/"}).encode()
        if parsed.path == "/application/o/token/":
            if self.token_status != 200:
                return self.token_status, b'{"error":"invalid_grant"}'
            return 200, json.dumps({"access_token": "at-123", "id_token": jwt(self.id_claims())}).encode()
        if parsed.path == "/application/o/userinfo/":
            return 200, json.dumps({"sub": "user-uuid-1", "groups": self.groups}).encode()
        return 404, b"{}"

    def token_form(self) -> dict:
        return next(c[3] for c in self.calls if c[1].endswith("/token/"))


@pytest.fixture
def oidc_env(env, monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "clothing-advisor")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "s3cret-value-for-tests")
    monkeypatch.setenv("OIDC_REDIRECT_URIS", f"{CALLBACK} {CALLBACK_TESTSERVER}")
    return env


def make(oidc_env, provider=None, clock=lambda: NOW):
    provider = provider or FakeProvider()
    return Oidc(Config.from_env(), fetch=provider.fetch, clock=clock), provider


def begin(o: Oidc, provider: FakeProvider, host="ca.example", nxt="/app/wardrobe"):
    """Start a sign-in like the browser would, and return what comes back from Authentik's side."""
    url, cookie, secure = o.start(host, nxt)
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    provider.nonce = query["nonce"]
    return url, cookie, secure, query


# ------------------------------------------------------------------ the redirect to Authentik
def test_start_builds_an_authorization_request_with_pkce_state_and_nonce(oidc_env):
    o, provider = make(oidc_env)
    url, cookie, secure, q = begin(o, provider)

    assert url.startswith("https://auth.example/application/o/authorize/?")
    assert q["response_type"] == "code" and q["client_id"] == "clothing-advisor" and q["scope"] == "openid profile email"
    assert q["redirect_uri"] == CALLBACK and secure is True
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 20 and len(q["nonce"]) >= 20
    assert "client_secret" not in url and cookie and "." in cookie


def test_the_callback_matches_the_address_the_browser_is_on(oidc_env):
    o, _ = make(oidc_env)
    assert o.redirect_uri_for("ca.example") == CALLBACK
    assert o.redirect_uri_for("TESTSERVER") == CALLBACK_TESTSERVER
    assert o.redirect_uri_for("unknown.example") == CALLBACK          # falls back to the first registered one


# ------------------------------------------------------------------ finishing the sign-in
def test_a_valid_sign_in_gives_an_admin_and_proves_possession_of_the_pkce_verifier(oidc_env):
    o, provider = make(oidc_env)
    _, cookie, _, q = begin(o, provider)

    identity, nxt, redirect_uri = o.finish("the-code", q["state"], cookie)

    assert (identity.sub, identity.name, identity.email, identity.role) == ("user-uuid-1", "Joren", "joren@example.org", "admin")
    assert nxt == "/app/wardrobe" and redirect_uri == CALLBACK
    form = provider.token_form()
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code" and form["redirect_uri"] == CALLBACK
    assert form["client_id"] == "clothing-advisor" and form["client_secret"] == "s3cret-value-for-tests"
    assert b64(hashlib.sha256(form["code_verifier"].encode()).digest()) == q["code_challenge"]


def test_the_viewer_group_gives_a_read_only_role(oidc_env):
    o, provider = make(oidc_env, FakeProvider(groups=[VIEWER]))
    _, cookie, _, q = begin(o, provider)
    assert o.finish("c", q["state"], cookie)[0].role == "viewer"


def test_admin_wins_when_someone_is_in_both_groups(oidc_env):
    o, provider = make(oidc_env, FakeProvider(groups=[VIEWER, ADMIN, "other"]))
    _, cookie, _, q = begin(o, provider)
    assert o.finish("c", q["state"], cookie)[0].role == "admin"


def test_someone_in_neither_group_is_denied(oidc_env):
    o, provider = make(oidc_env, FakeProvider(groups=["unrelated"]))
    _, cookie, _, q = begin(o, provider)
    with pytest.raises(OidcDenied):
        o.finish("c", q["state"], cookie)


def test_groups_are_read_from_userinfo_when_the_id_token_lacks_them(oidc_env):
    o, provider = make(oidc_env, FakeProvider(groups=[VIEWER], groups_in_token=False))
    _, cookie, _, q = begin(o, provider)

    identity, *_ = o.finish("c", q["state"], cookie)

    assert identity.role == "viewer" and any(c[1].endswith("/userinfo/") for c in provider.calls)


@pytest.mark.parametrize("override, why", [
    ({"nonce": "someone-elses"}, "nonce"),
    ({"aud": "another-client"}, "not for this client"),
    ({"aud": ["x", "y"]}, "not for this client"),
    ({"iss": "https://evil.example/application/o/clothing-advisor/"}, "issuer"),
    ({"exp": NOW - 3600}, "expired"),
    ({"sub": ""}, "no sub"),
])
def test_a_bad_id_token_is_refused(oidc_env, override, why):
    o, provider = make(oidc_env)
    _, cookie, _, q = begin(o, provider)
    provider.override = override
    with pytest.raises(OidcError, match=why):
        o.finish("c", q["state"], cookie)


def test_a_list_audience_containing_our_client_is_fine(oidc_env):
    o, provider = make(oidc_env)
    _, cookie, _, q = begin(o, provider)
    provider.override = {"aud": ["other", "clothing-advisor"]}
    assert o.finish("c", q["state"], cookie)[0].role == "admin"


def test_the_state_must_match_and_the_cookie_must_be_ours_and_fresh(oidc_env):
    now = [NOW]
    o, provider = make(oidc_env, clock=lambda: now[0])
    _, cookie, _, q = begin(o, provider)

    with pytest.raises(OidcError, match="state"):
        o.finish("c", "wrong-state", cookie)
    with pytest.raises(OidcError, match="state"):
        o.finish("c", "", cookie)
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("c", q["state"], "")
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("c", q["state"], cookie[:-3] + "AAA")           # tampered signature
    now[0] += oidc_module.STATE_TTL + 1
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("c", q["state"], cookie)                        # the sign-in took too long


def test_a_rejected_code_and_an_unreachable_provider_are_sign_in_errors(oidc_env):
    o, provider = make(oidc_env)
    _, cookie, _, q = begin(o, provider)
    provider.token_status = 400
    with pytest.raises(OidcError, match="token endpoint answered 400"):
        o.finish("c", q["state"], cookie)

    provider.down = True
    with pytest.raises(OidcError, match="cannot reach"):
        make(oidc_env, provider)[0].start("ca.example", "/app")


def test_discovery_with_another_issuer_is_reported_clearly(oidc_env):
    o, provider = make(oidc_env)
    provider.issuer_in_discovery = "http://internal:9000/application/o/clothing-advisor/"
    with pytest.raises(OidcError, match="issuer mismatch"):
        o.start("ca.example", "/app")


def test_metadata_is_cached(oidc_env):
    o, provider = make(oidc_env)
    begin(o, provider)
    begin(o, provider)
    assert sum(1 for c in provider.calls if c[1].endswith("openid-configuration")) == 1


def test_with_an_internal_url_requests_go_there_but_keep_the_public_host(oidc_env, monkeypatch):
    monkeypatch.setenv("OIDC_INTERNAL_URL", "http://authentik-server:9000")
    o, provider = make(oidc_env)
    provider.issuer_in_discovery = ISSUER
    _, cookie, _, q = begin(o, provider)
    o.finish("c", q["state"], cookie)

    for method, url, headers, _ in provider.calls:
        assert url.startswith("http://authentik-server:9000/"), url
        assert headers["Host"] == "auth.example" and headers["X-Forwarded-Proto"] == "https"


# ------------------------------------------------------------------ our own session cookie
def test_a_session_round_trips_expires_and_cannot_be_forged(oidc_env):
    now = [NOW]
    o, provider = make(oidc_env, clock=lambda: now[0])
    _, cookie, _, q = begin(o, provider)
    identity = o.finish("c", q["state"], cookie)[0]

    session = o.new_session(identity)
    assert o.read_session(session) == identity
    assert o.read_session(session[:-2] + "AA") is None
    assert o.read_session("garbage") is None and o.read_session("") is None
    now[0] += 7 * 86400 + 1
    assert o.read_session(session) is None                       # OIDC_SESSION_DAYS (default 7) is over


def test_the_two_cookie_kinds_and_two_deployments_cannot_be_mixed_up(oidc_env, monkeypatch):
    o, provider = make(oidc_env)
    _, sign_in_cookie, _, q = begin(o, provider)
    identity = o.finish("c", q["state"], sign_in_cookie)[0]
    session = o.new_session(identity)

    assert o.read_session(sign_in_cookie) is None                 # a sign-in-in-progress cookie is no session
    with pytest.raises(OidcError):
        o.finish("c", q["state"], session)                        # and a session is no sign-in-in-progress cookie

    monkeypatch.setenv("CA_ACCESS_TOKEN", "a-completely-different-token-1234567890")
    other, _ = make(oidc_env)
    assert other.read_session(session) is None                    # rotating the access token signs everybody out


def test_a_session_with_an_unknown_role_is_refused(oidc_env):
    o, _ = make(oidc_env)
    forged = oidc_module._sign({"typ": "session", "sub": "x", "name": "x", "email": "", "role": "root", "exp": NOW + 99}, o._key)
    assert o.read_session(forged) is None


# ------------------------------------------------------------------ configuration
def test_oidc_is_off_without_an_issuer(env):
    assert Config.from_env().oidc_issuer == "" and Oidc(Config.from_env()).enabled is False


@pytest.mark.parametrize("name, value, message", [
    ("OIDC_ISSUER", "http://auth.example/application/o/x/", "https"),
    ("OIDC_REDIRECT_URIS", "http://ca.example/app/oidc/callback", "https"),
    ("OIDC_REDIRECT_URIS", "https://ca.example/app/login", "callback"),
    ("OIDC_CLIENT_SECRET", "", "OIDC_CLIENT_SECRET"),
    ("OIDC_CLIENT_ID", "", "OIDC_CLIENT_ID"),
    ("OIDC_SESSION_DAYS", "400", "between 1 and 90"),
    ("OIDC_INTERNAL_URL", "ftp://x", "http"),
])
def test_bad_oidc_configuration_is_refused_at_startup(oidc_env, monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Config.from_env()


def test_plain_http_is_only_accepted_for_localhost(oidc_env, monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "http://localhost:19000/application/o/clothing-advisor/")
    monkeypatch.setenv("OIDC_REDIRECT_URIS", "http://localhost:18130/app/oidc/callback")
    assert Config.from_env().oidc_issuer.startswith("http://localhost")


# ------------------------------------------------------------------ in the web app
@pytest.fixture
def web(oidc_env, fake, monkeypatch):
    from datetime import date
    from clothing_advisor import stylist
    monkeypatch.setattr(stylist, "today_date", lambda cfg: date(2026, 7, 15))
    provider = FakeProvider()
    app = create_app(Config.from_env(), fake, start_worker=False)
    app.state.ctx.oidc._fetch = provider.fetch
    app.state.ctx.oidc._now = lambda: NOW
    with TestClient(app, base_url="https://testserver") as client:
        client.provider, client.ctx = provider, app.state.ctx
        yield client


def sign_in(web, nxt="/app", groups=None):
    """Walk the whole flow through the app; returns the callback response."""
    if groups is not None:
        web.provider.groups = groups
    start = web.get(f"/app/oidc/start?next={nxt}", follow_redirects=False)
    assert start.status_code == 303, start.text
    q = {k: v[0] for k, v in parse_qs(urlparse(start.headers["location"]).query).items()}
    web.provider.nonce = q["nonce"]
    return web.get(f"/app/oidc/callback?code=abc&state={q['state']}", follow_redirects=False)


def test_the_login_page_offers_the_sign_in_button_and_keeps_the_token_form_as_emergency_access(web):
    page = web.get("/app/login").text
    assert 'href="/app/oidc/start?next=/app"' in page
    assert "Emergency access" in page and 'type="password"' in page and 'action="/app/login"' in page


def test_without_oidc_the_login_page_is_just_the_token_form(env, fake):
    with TestClient(create_app(Config.from_env(), fake, start_worker=False), base_url="https://testserver") as c:
        page = c.get("/app/login").text
        assert "/app/oidc/start" not in page and 'type="password"' in page
        assert c.get("/app/oidc/start", follow_redirects=False).status_code == 404
        assert c.get("/app/oidc/callback?code=x&state=y", follow_redirects=False).status_code == 404


def test_signing_in_as_an_admin_opens_the_whole_app_and_signing_out_closes_it(web):
    start = web.get("/app/oidc/start?next=/app/wardrobe", follow_redirects=False)
    assert start.headers["location"].startswith("https://auth.example/application/o/authorize/")
    cookie = start.headers["set-cookie"]
    assert "ca_oidc=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "path=/app/oidc" in cookie.lower()

    response = sign_in(web, "/app/wardrobe")
    assert response.status_code == 303 and response.headers["location"] == "/app/wardrobe"
    session = [c for c in response.headers.get_list("set-cookie") if c.startswith("ca_session=")][0]
    assert "HttpOnly" in session and "Secure" in session and "samesite=lax" in session.lower()

    assert web.get("/app/wardrobe").status_code == 200
    assert web.get("/app/settings").status_code == 200
    page = web.get("/app").text
    assert "Sign out" in page and "Joren" in page and TOKEN not in page
    assert web.get("/api/queue").status_code == 200

    out = web.post("/app/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/app/login"
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303


def test_a_viewer_can_look_at_the_suggestions_and_nothing_else(web):
    response = sign_in(web, "/app/wardrobe", groups=[VIEWER])
    assert response.headers["location"] == "/app/tile"            # not the page he asked for: he may not see it

    tile = web.get("/app/tile")
    assert tile.status_code == 200 and "readonly: true" in tile.text     # no buttons: read-only like the tv
    assert TOKEN not in tile.text and 'k: ""' in tile.text and 'v: ""' in tile.text   # and no token handed to a browser
    assert web.get("/api/advice/current").status_code == 200
    assert web.get("/app/wardrobe").status_code == 403
    assert web.get("/app/settings").status_code == 403
    assert web.post("/api/advice", json={"message": "x"}).status_code == 403
    assert web.post("/api/outfits/1/choose").status_code == 403
    assert web.get("/api/queue").status_code == 403
    assert web.post("/app/logout", follow_redirects=False).status_code == 303


def test_someone_without_a_group_gets_no_session(web):
    response = sign_in(web, groups=["unrelated"])
    assert response.status_code == 403 and "no access to Clothing Advisor" in response.text
    assert not [c for c in response.headers.get_list("set-cookie") if c.startswith("ca_session=") and "Max-Age=0" not in c]
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303


def test_a_failed_or_forged_callback_shows_the_login_page_again(web):
    assert web.get("/app/oidc/callback?error=access_denied", follow_redirects=False).status_code == 400
    forged = web.get("/app/oidc/callback?code=abc&state=nope", follow_redirects=False)     # no cookie at all
    assert forged.status_code == 400 and "Sign-in failed" in forged.text
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303


def test_when_authentik_is_down_the_token_still_gets_you_in(web):
    web.provider.down = True
    page = web.get("/app/oidc/start", follow_redirects=False)
    assert page.status_code == 502 and "emergency token" in page.text
    assert web.get("/app/wardrobe", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert web.post("/app/login", data={"token": TOKEN, "next": "/app"}, follow_redirects=False).status_code == 303


def test_next_can_never_point_outside_the_app(web):
    for evil in ("https://evil.example/", "//evil.example", "/other"):
        response = sign_in(web, evil)
        assert response.headers["location"] == "/app", evil
        web.cookies.clear()


def test_a_forged_or_expired_session_cookie_is_ignored(web):
    web.cookies.set("ca_session", "forged.value")
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303
    web.cookies.clear()
    sign_in(web)
    web.ctx.oidc._now = lambda: NOW + 8 * 86400
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303


def test_the_session_cookie_is_not_the_access_token_and_vice_versa(web):
    sign_in(web)
    session = web.cookies["ca_session"]
    assert session != TOKEN and TOKEN not in session
    web.cookies.clear()
    web.cookies.set("ca_session", TOKEN)
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303
    web.cookies.clear()
    web.cookies.set("ca_auth", session)
    assert web.get("/app/wardrobe", follow_redirects=False).status_code == 303


def test_tile_and_view_tokens_are_untouched_by_the_sign_in(oidc_env, fake, monkeypatch):
    monkeypatch.setenv("CA_TILE_TOKEN", "tile-token-0123456789abcdef")
    monkeypatch.setenv("CA_VIEW_TOKEN", "view-token-0123456789abcdefgh")
    with TestClient(create_app(Config.from_env(), fake, start_worker=False), base_url="https://testserver") as c:
        assert c.get("/app/tile?k=tile-token-0123456789abcdef").status_code == 200
        assert c.get("/app/tile?v=view-token-0123456789abcdefgh").status_code == 200
        assert c.get("/app/wardrobe?k=tile-token-0123456789abcdef").status_code == 403
        assert c.get("/api/tv", headers={"Authorization": "Bearer view-token-0123456789abcdefgh"}).status_code == 200
