"""OpenID Connect sign-in against Authentik: authorization-code flow with PKCE, groups -> role, signed session cookie.

Only this server talks to Authentik's token endpoint (through `OIDC_INTERNAL_URL` when Authentik is not reachable by
its public name from inside this container); the browser is always sent to the public address. The id_token comes
straight from the token endpoint over TLS, which OpenID Connect Core 3.1.3.7 accepts instead of checking its signature;
issuer, audience, expiry and nonce are checked here.

The sign-in only decides *who* you are. What you may do follows from your Authentik groups (admin / viewer). The tile
and view tokens never go through this; `CA_ACCESS_TOKEN` stays as the emergency way in when Authentik is down.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from .config import Config

log = logging.getLogger("clothing_advisor.oidc")

ROLE_ADMIN, ROLE_VIEWER = "admin", "viewer"
STATE_TTL = 600            # seconds to finish a sign-in once started
META_TTL = 3600
MAX_BODY = 1_000_000


class OidcError(Exception):
    """The sign-in failed (bad state, bad token, provider unreachable...). The message is for the log, not the user."""


class OidcDenied(OidcError):
    """Signed in fine, but this person is in none of the groups that may use the app."""


@dataclass(frozen=True)
class Identity:
    sub: str
    name: str
    email: str
    role: str


Fetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _urllib_fetch(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict[str, Any], key: bytes) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(key, body.encode(), hashlib.sha256).digest())}"


def _unsign(value: str, key: bytes, typ: str, now: float) -> dict[str, Any] | None:
    """The payload if the signature, the type and the expiry are all right; otherwise None."""
    try:
        body, sig = value.split(".", 1)
        if not hmac.compare_digest(_b64(hmac.new(key, body.encode(), hashlib.sha256).digest()), sig):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("typ") != typ or float(payload.get("exp", 0)) <= now:
        return None
    return payload


class Oidc:
    def __init__(self, cfg: Config, fetch: Fetch | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.enabled = bool(cfg.oidc_issuer)
        self._fetch = fetch or _urllib_fetch
        self._now = clock
        self._meta: dict[str, Any] | None = None
        self._meta_at = 0.0
        # Domain-separated from the raw access token, so a session cookie can never be replayed as the token itself.
        self._key = hashlib.sha256(b"clothing-advisor session v1|" + cfg.access_token.encode()).digest()

    # ------------------------------------------------------------------ talking to the provider
    def _request(self, method: str, url: str, data: dict[str, str] | None = None, bearer: str = "") -> tuple[int, bytes]:
        """One call to the provider. With OIDC_INTERNAL_URL the request goes to that address but still says which public
        host it is for (Host + X-Forwarded-Proto), so the provider builds the same issuer and URLs as for the browser."""
        headers = {"Accept": "application/json", "User-Agent": "clothing-advisor"}
        if self.cfg.oidc_internal_url:
            public, internal = urlparse(url), urlparse(self.cfg.oidc_internal_url)
            url = public._replace(scheme=internal.scheme, netloc=internal.netloc).geturl()
            headers.update({"Host": public.netloc, "X-Forwarded-Proto": public.scheme})
        body = None
        if data is not None:
            body = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            return self._fetch(method, url, headers, body)
        except OSError as exc:  # URLError, timeouts, refused connections
            raise OidcError(f"cannot reach the sign-in service: {exc}") from exc

    def _metadata(self) -> dict[str, Any]:
        if self._meta and self._now() - self._meta_at < META_TTL:
            return self._meta
        issuer = self.cfg.oidc_issuer
        status, body = self._request("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
        if status != 200:
            raise OidcError(f"discovery answered {status}")
        try:
            meta = json.loads(body)
        except ValueError as exc:
            raise OidcError("discovery did not return JSON") from exc
        if str(meta.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise OidcError(f"issuer mismatch: OIDC_ISSUER is {issuer!r} but the provider says {meta.get('issuer')!r} "
                            "(if Authentik is reached through OIDC_INTERNAL_URL, check it forwards the public host)")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not meta.get(key):
                raise OidcError(f"discovery has no {key}")
        self._meta, self._meta_at = meta, self._now()
        return meta

    # ------------------------------------------------------------------ the sign-in flow
    def redirect_uri_for(self, host: str) -> str:
        """The registered callback on the address the browser is using (it must come back to the same host)."""
        for uri in self.cfg.oidc_redirect_uris:
            if urlparse(uri).netloc.lower() == host.lower():
                return uri
        return self.cfg.oidc_redirect_uris[0]

    def start(self, host: str, next_path: str) -> tuple[str, str, bool]:
        """(where to send the browser, the signed short-lived cookie to set, whether that cookie can be Secure)."""
        meta = self._metadata()
        redirect_uri = self.redirect_uri_for(host)
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = urlencode({
            "response_type": "code", "client_id": self.cfg.oidc_client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile email", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        endpoint = meta["authorization_endpoint"]
        cookie = _sign({"typ": "oidc", "state": state, "nonce": nonce, "verifier": verifier, "next": next_path,
                        "redirect_uri": redirect_uri, "exp": self._now() + STATE_TTL}, self._key)
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", cookie, redirect_uri.startswith("https")

    def finish(self, code: str, state: str, cookie: str) -> tuple[Identity, str, str]:
        """Exchange the code and decide who this is: (identity, where to go next, the redirect uri that was used)."""
        pending = _unsign(cookie, self._key, "oidc", self._now()) if cookie else None
        if not pending:
            raise OidcError("no sign-in in progress (cookie missing, expired or tampered with)")
        if not code or not state or not hmac.compare_digest(str(pending["state"]), state):
            raise OidcError("state mismatch")

        meta = self._metadata()
        status, body = self._request("POST", meta["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "client_id": self.cfg.oidc_client_id, "client_secret": self.cfg.oidc_client_secret,
            "code_verifier": pending["verifier"],
        })
        if status != 200:
            raise OidcError(f"token endpoint answered {status}: {body[:200]!r}")
        try:
            tokens = json.loads(body)
            claims = self._claims(tokens["id_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OidcError(f"unusable token response: {exc}") from exc

        now = self._now()
        if str(claims.get("iss", "")).rstrip("/") != self.cfg.oidc_issuer.rstrip("/"):
            raise OidcError(f"wrong issuer in id_token: {claims.get('iss')!r}")
        aud = claims.get("aud")
        if self.cfg.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
            raise OidcError(f"id_token is not for this client: aud={aud!r}")
        if float(claims.get("exp", 0)) <= now - 60:
            raise OidcError("id_token has expired")
        if not hmac.compare_digest(str(claims.get("nonce", "")), str(pending["nonce"])):
            raise OidcError("nonce mismatch")
        sub = str(claims.get("sub", ""))
        if not sub:
            raise OidcError("id_token has no sub")

        if "groups" not in claims and meta.get("userinfo_endpoint") and tokens.get("access_token"):
            status, body = self._request("GET", meta["userinfo_endpoint"], bearer=str(tokens["access_token"]))
            if status == 200:
                try:
                    claims = {**json.loads(body), **{k: v for k, v in claims.items() if k != "groups"}}
                except ValueError:
                    pass
        groups = claims.get("groups")
        groups = {str(g) for g in groups} if isinstance(groups, list) else set()
        if self.cfg.oidc_admin_group in groups:
            role = ROLE_ADMIN
        elif self.cfg.oidc_viewer_group and self.cfg.oidc_viewer_group in groups:
            role = ROLE_VIEWER
        else:
            raise OidcDenied(f"{sub} is in none of {self.cfg.oidc_admin_group!r}, {self.cfg.oidc_viewer_group!r}")
        name = str(claims.get("name") or claims.get("preferred_username") or claims.get("email") or sub)
        return Identity(sub=sub, name=name, email=str(claims.get("email", "")), role=role), str(pending["next"]), str(pending["redirect_uri"])

    @staticmethod
    def _claims(id_token: str) -> dict[str, Any]:
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("id_token is not a JWT")
        claims = json.loads(_unb64(parts[1]))
        if not isinstance(claims, dict):
            raise ValueError("id_token payload is not an object")
        return claims

    # ------------------------------------------------------------------ our own session
    @property
    def session_seconds(self) -> int:
        return self.cfg.oidc_session_days * 86400

    def new_session(self, identity: Identity) -> str:
        now = self._now()
        return _sign({"typ": "session", "sub": identity.sub, "name": identity.name, "email": identity.email,
                      "role": identity.role, "iat": int(now), "exp": now + self.session_seconds}, self._key)

    def read_session(self, cookie: str) -> Identity | None:
        payload = _unsign(cookie, self._key, "session", self._now())
        if not payload or payload.get("role") not in (ROLE_ADMIN, ROLE_VIEWER):
            return None
        return Identity(sub=str(payload.get("sub", "")), name=str(payload.get("name", "")),
                        email=str(payload.get("email", "")), role=payload["role"])
