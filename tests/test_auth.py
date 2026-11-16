"""Admin auth paths: setup code, generic errors + lockout, CSRF, cookies/headers, expiry, logout, 2FA, secrets."""

import time

import pyotp

from adm import security
from tests.conftest import PW, setup_admin


def test_setup_needs_the_code_and_a_strong_password(c):
    assert c.get("/api/session").json()["setup_needed"] is True
    assert c.get("/api/requests").status_code == 401
    assert c.post("/api/setup", json={"setup_code": "WRONG", "username": "admin", "password": PW}).status_code == 401
    assert c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": "short"}).status_code == 422
    setup_admin(c)
    assert c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "x", "password": PW}).status_code == 409


def test_cookie_and_security_headers(c):
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("__host-adm=")
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in cookie
    h = c.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and "max-age" in h["strict-transport-security"]
    assert c.get("/api/requests").headers["cache-control"] == "no-store"


def test_wrong_password_is_generic_and_locks_out(c):
    setup_admin(c)
    c.post("/api/logout", headers={"X-CSRF-Token": ""})
    r1 = c.post("/api/login", json={"username": "admin", "password": "wrong password 1"})
    r2 = c.post("/api/login", json={"username": "nobody", "password": PW})
    assert r1.status_code == r2.status_code == 401 and r1.json() == r2.json()
    codes = [c.post("/api/login", json={"username": "admin", "password": f"wrong {i}"}).status_code for i in range(8)]
    assert 429 in codes
    r = c.post("/api/login", json={"username": "admin", "password": PW})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_state_changes_need_csrf_and_same_site(c):
    h = setup_admin(c)
    body = {"kind": "movies", "title": "x"}
    assert c.post("/api/requests", json=body).status_code == 403
    assert c.post("/api/requests", json=body, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert c.post("/api/requests", json=body, headers={**h, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/requests", json=body, headers=h).status_code == 422      # passes the guard (no library yet)


def _session_store(app) -> security.SessionStore:
    for r in app.router.routes:
        for cell in (getattr(getattr(r, "endpoint", None), "__closure__", None) or []):
            try:
                v = cell.cell_contents
            except ValueError:
                continue
            if isinstance(v, security.SessionStore):
                return v
    raise AssertionError("no session store")


def test_logout_and_idle_expiry_end_the_session(c, app):
    h = setup_admin(c)
    old = c.cookies.get(security.COOKIE)
    c.post("/api/logout", headers=h)
    c.cookies.set(security.COOKIE, old)
    assert c.get("/api/requests").status_code == 401          # really invalidated server-side
    c.cookies.clear()
    c.post("/api/login", json={"username": "admin", "password": PW})
    assert c.get("/api/requests").status_code == 200
    for s in _session_store(app)._sessions.values():
        s.last_seen = time.time() - 10 * 3600
    assert c.get("/api/requests").status_code == 401


def test_totp_login_and_replay(c):
    h = setup_admin(c)
    secret = c.post("/api/account/totp/setup", headers=h).json()["secret"]
    assert c.post("/api/account/totp/enable", json={"code": "000000"}, headers=h).status_code == 401
    code = pyotp.TOTP(secret).now()
    assert c.post("/api/account/totp/enable", json={"code": code}, headers=h).status_code == 200
    c.post("/api/logout", headers=h)
    r = c.post("/api/login", json={"username": "admin", "password": PW})
    assert r.json()["totp_needed"] is True
    assert c.get("/api/requests").status_code == 401                       # half-logged-in is not logged in
    assert c.post("/api/login/totp", json={"code": code}).status_code == 401   # replayed code
    assert c.post("/api/login/totp", json={"code": pyotp.TOTP(secret).at(time.time() + 30)}).status_code == 200
    assert c.get("/api/requests").status_code == 200


def test_secrets_are_write_only(c):
    h = setup_admin(c)
    tok = "trm_" + "a" * 43
    r = c.put("/api/settings", json={"telerouter": {"url": "https://tr.lan:8443", "token": tok},
                                     "jellyfin": {"url": "https://jf.lan", "api_key": "abcdef0123456789abcd"}}, headers=h)
    assert r.status_code == 200, r.text
    s = c.get("/api/settings")
    assert tok not in s.text and "abcdef0123456789abcd" not in s.text
    assert s.json()["telerouter"]["token_set"] is True and s.json()["jellyfin"]["api_key_set"] is True
    c.put("/api/settings", json={"telerouter": {"url": "https://tr.lan:8443", "token": ""}}, headers=h)
    assert c.app.state.st.store.config.telerouter.token == tok          # empty keeps the stored secret
    assert c.put("/api/settings", json={"telerouter": {"url": "http://tr.lan"}}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"telerouter": {"token": "abc"}}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"agent": {"adm_url": "http://x"}}, headers=h).status_code == 422
