"""FastAPI: admin auth, the requests/settings/libraries API, the agents' MCP endpoint and upload endpoint, the UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import ipaddress
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect

from . import library, security, tls
from .app_state import State
from .config import (AgentSettings, Admin, Jellyfin, Library, SecuritySettings, Telerouter, merge_secrets, redact)
from .jobs import Jobs
from .mcp import McpServer
from .remote import RemoteError, TelerouterClient, jellyfin_check
from .settings import Settings
from .store import ACTIVE, FINISHED, ConfigStore, Db, write_private

log = logging.getLogger("adm.web")
STATIC = Path(__file__).parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
PUBLIC = {"/api/health", "/api/session", "/api/setup", "/api/login", "/api/login/totp", "/api/logout"}
CHUNK = 1024 * 1024


class SetupBody(BaseModel):
    setup_code: str = Field(max_length=64)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    username: str = Field(default="", max_length=64)
    password: str = Field(max_length=256)


class CodeBody(BaseModel):
    code: str = Field(max_length=12)


class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class NewRequest(BaseModel):
    kind: Literal["movies", "shows", "books"]
    title: str = Field(min_length=1, max_length=200)
    year: str = Field(default="", max_length=4, pattern=r"^(\d{4})?$")
    details: str = Field(default="", max_length=300)     # season/episodes, author, edition, language...
    notes: str = Field(default="", max_length=2000)
    library_id: str = Field(default="", max_length=16)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SettingsBody(BaseModel):
    telerouter: dict | None = None
    jellyfin: dict | None = None
    agent: dict | None = None
    security: dict | None = None


def cross_site(headers) -> bool:
    return headers.get("sec-fetch-site", "") in ("cross-site", "same-site")


def _validate(model_cls, data: dict):
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise HTTPException(422, msg) from e


def _qr_data_url(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def public_request(j: dict) -> dict:
    return {k: v for k, v in j.items() if k not in ("token_hash",)}


def create_app(settings: Settings, files: tls.TlsFiles) -> FastAPI:
    store = ConfigStore(settings.data_dir)
    cfg = store.config
    db = Db(Path(settings.data_dir) / "adm.db")
    st = State(settings=settings, store=store, db=db, tls_ca_pem=files.ca_pem.decode(),
               own_cert=bool(settings.tls_cert), spki_pin=files.server_spki)
    jobs = Jobs(st)
    st.jobs = jobs
    mcp = McpServer(st)
    throttle = security.LoginThrottle()
    totp_replay = security.TotpReplay()
    sessions = security.SessionStore(cfg.security.session_idle_minutes * 60, cfg.security.session_absolute_hours * 3600)
    hash_slots = asyncio.Semaphore(2)
    upload_locks: dict[str, asyncio.Lock] = {}
    trusted = []
    for p in settings.trusted_proxies:
        with contextlib.suppress(ValueError):
            trusted.append(ipaddress.ip_network(p, strict=False))
    setup_code_path = Path(settings.data_dir) / "setup-code"

    def setup_code() -> str:
        if settings.setup_code:
            return settings.setup_code
        if not setup_code_path.exists():
            write_private(setup_code_path, security.new_enrollment_code())
        return setup_code_path.read_text().strip()

    if cfg.admin is None:
        log.warning("first start: open the web UI and create the admin account with setup code %s", setup_code())

    # runs that were going on when ADM stopped: their sessions may still run, the loop checks them again

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("ADM %s starting (https :%d)", settings.version, settings.https_port)
        os.makedirs(settings.staging, mode=0o700, exist_ok=True)
        jobs.start()
        yield
        await jobs.stop()

    app = FastAPI(title="ADM", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.st = st

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not any(ip in n for n in trusted):
            return peer
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            try:
                if not any(ipaddress.ip_address(hop) in n for n in trusted):
                    return hop
            except ValueError:
                return peer
        return peer

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/"):
                if path not in PUBLIC:
                    s = sessions.get(request.cookies.get(security.COOKIE))
                    if not s or s.pending_totp:
                        return JSONResponse({"detail": "login required"}, status_code=401)
                    request.state.session = s
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        sent = request.headers.get("x-csrf-token", "")
                        if cross_site(request.headers) or not hmac.compare_digest(sent, s.csrf):
                            return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
                elif request.method == "POST" and cross_site(request.headers):
                    return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            resp = await call_next(request)
            h = resp.headers
            h["Content-Security-Policy"] = CSP
            h["X-Frame-Options"] = "DENY"
            h["X-Content-Type-Options"] = "nosniff"
            h["Referrer-Policy"] = "no-referrer"
            h["Strict-Transport-Security"] = "max-age=31536000"
            h["Cross-Origin-Opener-Policy"] = "same-origin"
            h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            if path.startswith(("/api/", "/mcp", "/upload/")) or path == "/":
                h["Cache-Control"] = "no-store"
            elif path.startswith("/static/"):
                h["Cache-Control"] = "no-cache"
            return resp

    app.add_middleware(Guard)

    async def check_password(password: str, stored: str | None) -> bool:
        async with hash_slots:
            return await asyncio.to_thread(security.verify_password, password, stored)

    def charge(*keys: str) -> None:
        try:
            throttle.begin(*keys)
        except security.Locked as e:
            raise HTTPException(429, "too many attempts, try again later",
                                headers={"Retry-After": str(int(e.retry_after) + 1)}) from e

    def start_session(request: Request, response: Response, username: str, pending: bool = False):
        sessions.delete(request.cookies.get(security.COOKIE))
        sid, s = sessions.create(username, pending_totp=pending)
        response.set_cookie(security.COOKIE, sid, httponly=True, secure=True, samesite="strict", path="/")
        return s

    # -- auth -------------------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        s = sessions.get(request.cookies.get(security.COOKIE))
        return {"setup_needed": cfg.admin is None, "logged_in": bool(s and not s.pending_totp),
                "totp_needed": bool(s and s.pending_totp), "username": s.username if s else "",
                "csrf": s.csrf if s and not s.pending_totp else "", "version": settings.version}

    @app.post("/api/setup")
    async def setup(body: SetupBody, request: Request, response: Response):
        if cfg.admin is not None:
            raise HTTPException(409, "already set up")
        ip = client_ip(request)
        charge(f"ip:{ip}", "setup")
        if not hmac.compare_digest(body.setup_code.strip().upper().encode(), setup_code().upper().encode()):
            log.warning("wrong setup code from %s", ip)
            raise HTTPException(401, "wrong setup code")
        throttle.success(f"ip:{ip}", "setup")
        problem = security.password_problem(body.password, body.username)
        if problem:
            raise HTTPException(422, problem)
        cfg.admin = Admin(username=body.username,
                          password_hash=await asyncio.to_thread(security.hash_password, body.password))
        store.save()
        setup_code_path.unlink(missing_ok=True)
        log.info("admin account '%s' created from %s", body.username, ip)
        start_session(request, response, body.username)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        ip = client_ip(request)
        user = body.username.strip().lower()
        charge(f"ip:{ip}", f"user:{user}")
        admin = cfg.admin
        ok = await check_password(body.password, admin.password_hash if admin else None)
        if not ok or not admin or not hmac.compare_digest(user.encode(), admin.username.lower().encode()):
            log.warning("failed login for '%s' from %s", body.username[:64], ip)
            raise HTTPException(401, "wrong user name or password")
        throttle.success(f"ip:{ip}", f"user:{user}")
        start_session(request, response, admin.username, pending=admin.totp_enabled)
        if not admin.totp_enabled:
            log.info("admin login from %s", ip)
        return {"ok": True, "totp_needed": admin.totp_enabled}

    @app.post("/api/login/totp")
    async def login_totp(body: CodeBody, request: Request, response: Response):
        s = sessions.get(request.cookies.get(security.COOKIE))
        if not s or not s.pending_totp or not cfg.admin:
            raise HTTPException(401, "log in first")
        ip = client_ip(request)
        charge(f"ip:{ip}", "totp")
        if not totp_replay.verify(cfg.admin.totp_secret, body.code):
            log.warning("wrong 2FA code from %s", ip)
            raise HTTPException(401, "wrong code")
        throttle.success(f"ip:{ip}", "totp")
        start_session(request, response, s.username)
        log.info("admin login (2FA) from %s", ip)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.delete(request.cookies.get(security.COOKIE))
        response.delete_cookie(security.COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/account/password")
    async def change_password(body: PasswordChange, request: Request, response: Response):
        admin = cfg.admin
        assert admin
        ip = client_ip(request)
        charge(f"ip:{ip}", f"user:{admin.username.lower()}")
        if not await check_password(body.current, admin.password_hash):
            raise HTTPException(401, "current password is wrong")
        throttle.success(f"ip:{ip}", f"user:{admin.username.lower()}")
        problem = security.password_problem(body.new, admin.username)
        if problem:
            raise HTTPException(422, problem)
        admin.password_hash = await asyncio.to_thread(security.hash_password, body.new)
        store.save()
        sessions.clear()
        start_session(request, response, admin.username)
        log.info("admin password changed from %s", ip)
        return {"ok": True}

    @app.get("/api/account")
    async def account():
        return {"username": cfg.admin.username if cfg.admin else "", "totp_enabled": bool(cfg.admin and cfg.admin.totp_enabled)}

    @app.post("/api/account/totp/setup")
    async def totp_setup(request: Request):
        admin = cfg.admin
        assert admin
        if admin.totp_enabled:
            raise HTTPException(409, "2FA is already on")
        secret = security.new_totp_secret()
        request.state.session.totp_candidate = secret     # type: ignore[attr-defined]
        uri = security.totp_uri(secret, admin.username)
        return {"secret": secret, "uri": uri, "qr": _qr_data_url(uri)}

    @app.post("/api/account/totp/enable")
    async def totp_enable(body: CodeBody, request: Request):
        admin = cfg.admin
        assert admin
        secret = getattr(request.state.session, "totp_candidate", "")
        if not secret or not totp_replay.verify(secret, body.code):
            raise HTTPException(401, "wrong code - check the time on your phone")
        admin.totp_secret, admin.totp_enabled = secret, True
        store.save()
        log.info("2FA enabled for %s", admin.username)
        return {"ok": True}

    @app.post("/api/account/totp/disable")
    async def totp_disable(body: LoginBody, request: Request):
        admin = cfg.admin
        assert admin
        ip = client_ip(request)
        charge(f"ip:{ip}", f"user:{admin.username.lower()}")
        if not await check_password(body.password, admin.password_hash):
            raise HTTPException(401, "wrong password")
        throttle.success(f"ip:{ip}", f"user:{admin.username.lower()}")
        admin.totp_secret, admin.totp_enabled = "", False
        store.save()
        log.info("2FA disabled for %s", admin.username)
        return {"ok": True}

    # -- requests ---------------------------------------------------------------------------------
    def find_req(rid: str) -> dict:
        j = db.request(rid) if re.match(r"^r_[0-9a-f]{10}$", rid) else None
        if not j:
            raise HTTPException(404, "no such request")
        return j

    @app.get("/api/requests")
    async def list_requests(state: str = ""):
        rows = db.requests(500, state if state in ("queued",) + ACTIVE + FINISHED else "")
        counts = {s: 0 for s in ("queued",) + ACTIVE + FINISHED}
        for r in db.q("SELECT state, COUNT(*) AS n FROM requests GROUP BY state"):
            counts[r["state"]] = r["n"]
        libs = {x.id: x.name for x in cfg.libraries}
        return {"requests": [{**public_request(r), "library": libs.get(r["library_id"], "?")} for r in rows],
                "counts": counts}

    @app.post("/api/requests")
    async def create_request(body: NewRequest):
        libs = [x for x in cfg.libraries if x.enabled and x.kind == body.kind]
        lib = next((x for x in libs if x.id == body.library_id), None) if body.library_id else (libs[0] if libs else None)
        if not lib:
            raise HTTPException(422, f"add a {body.kind} library first (Libraries)")
        title = " ".join(body.title.split())
        if any(ord(c) < 32 for c in title + body.details):
            raise HTTPException(422, "no control characters")
        j = db.new_request(body.kind, title, body.year, " ".join(body.details.split()), body.notes.strip(), lib.id)
        log.info("request %s: %s '%s'", j["id"], body.kind, title)
        jobs.wake()
        return public_request(j)

    @app.get("/api/requests/{rid}")
    async def get_request(rid: str, after: int = 0):
        j = find_req(rid)
        lib = store.library(j["library_id"])
        return {"request": {**public_request(j), "library": lib.name if lib else "?"},
                "events": db.events(rid, after), "imports": db.imports(rid),
                "uploads": [{k: u[k] for k in ("id", "filename", "size", "received", "state")} for u in db.uploads(rid)]}

    @app.post("/api/requests/{rid}/cancel")
    async def cancel_request(rid: str):
        j = find_req(rid)
        if j["state"] in FINISHED:
            raise HTTPException(409, f"already {j['state']}")
        if j["state"] == "queued":
            db.update(rid, state="cancelled", finished=time.time())
            db.event(rid, "state", "cancelled")
        else:
            await jobs.finish(rid, "cancelled", error="cancelled by the owner")
        return public_request(db.request(rid))

    @app.post("/api/requests/{rid}/retry")
    async def retry_request(rid: str):
        j = find_req(rid)
        if j["state"] not in ("failed", "cancelled"):
            raise HTTPException(409, "only failed or cancelled requests can be retried")
        db.update(rid, state="queued", error="", stage="", message="", progress=None, session_id="",
                  session_name="", session_turns=0, nudges=0, finished=None, started=None)
        db.event(rid, "state", "queued again")
        jobs.wake()
        return public_request(db.request(rid))

    @app.delete("/api/requests/{rid}")
    async def delete_request(rid: str):
        j = find_req(rid)
        if j["state"] not in FINISHED:
            raise HTTPException(409, "cancel it first")
        db.delete_request(rid)
        return {"ok": True}

    @app.post("/api/requests/{rid}/message")
    async def message_agent(rid: str, body: MessageBody):
        j = find_req(rid)
        if j["state"] not in ACTIVE or not j["session_id"]:
            raise HTTPException(409, "no agent is working on this request")
        try:
            async with jobs.client() as tr:
                await tr.send(j["session_id"], f"[ADM, from the owner about request {rid}] {body.text.strip()}",
                              timeout=int(min(cfg.agent.job_timeout_hours * 3600, 86_400)))
        except RemoteError as e:
            raise HTTPException(409 if "wait for its answer" in str(e) else 502, str(e)) from e
        db.event(rid, "owner", body.text.strip())
        return {"ok": True}

    @app.get("/api/requests/{rid}/screen")
    async def request_screen(rid: str):
        j = find_req(rid)
        if not j["session_id"]:
            raise HTTPException(409, "no agent session")
        try:
            async with jobs.client() as tr:
                text = await tr.screen(j["session_id"], 300)
        except RemoteError as e:
            raise HTTPException(502, str(e)) from e
        return {"screen": text[-120_000:]}

    # -- libraries --------------------------------------------------------------------------------
    def lib_info(x: Library) -> dict:
        ok, why = True, ""
        try:
            library.root_of(x.path, settings.library_roots)
        except library.LibraryError as e:
            ok, why = False, str(e)
        return {**x.model_dump(), "available": ok, "problem": why}

    @app.get("/api/libraries")
    async def list_libraries():
        return {"libraries": [lib_info(x) for x in cfg.libraries], "roots": settings.library_roots}

    def check_lib(x: Library) -> None:
        try:
            library.root_of(x.path, settings.library_roots)
        except library.LibraryError as e:
            raise HTTPException(422, str(e)) from e
        if any(o.id != x.id and o.path == x.path for o in cfg.libraries):
            raise HTTPException(422, "another library uses that folder")

    @app.post("/api/libraries")
    async def add_library(payload: dict):
        x = _validate(Library, {k: v for k, v in payload.items() if k != "id"})
        check_lib(x)
        cfg.libraries.append(x)
        store.save()
        return lib_info(x)

    @app.put("/api/libraries/{lid}")
    async def edit_library(lid: str, payload: dict):
        old = store.library(lid)
        if not old:
            raise HTTPException(404, "no such library")
        x = _validate(Library, {**old.model_dump(), **payload, "id": lid})
        check_lib(x)
        cfg.libraries[cfg.libraries.index(old)] = x
        store.save()
        return lib_info(x)

    @app.delete("/api/libraries/{lid}")
    async def delete_library(lid: str):
        old = store.library(lid)
        if not old:
            raise HTTPException(404, "no such library")
        if db.one("SELECT id FROM requests WHERE library_id=? AND state IN ('queued','starting','working')", (lid,)):
            raise HTTPException(409, "requests are still using it")
        cfg.libraries.remove(old)
        store.save()
        return {"ok": True}

    @app.get("/api/libraries/{lid}/tree")
    async def library_tree(lid: str, path: str = ""):
        x = store.library(lid)
        if not x:
            raise HTTPException(404, "no such library")
        try:
            root = library.root_of(x.path, settings.library_roots)
            return await asyncio.to_thread(library.tree, root, path, 1, 1000)
        except library.LibraryError as e:
            raise HTTPException(422, str(e)) from e

    # -- settings ---------------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings(request: Request):
        return {"telerouter": redact(cfg.telerouter), "jellyfin": redact(cfg.jellyfin), "agent": cfg.agent.model_dump(),
                "security": cfg.security.model_dump(), "ca_sha256": files.ca_sha256,
                "suggested_adm_url": f"https://{request.headers.get('host', '')}"}

    @app.put("/api/settings")
    async def put_settings(body: SettingsBody):
        new = {}
        for name, cls in (("telerouter", Telerouter), ("jellyfin", Jellyfin), ("agent", AgentSettings),
                          ("security", SecuritySettings)):
            part = getattr(body, name)
            if part is None:
                continue
            old = getattr(cfg, name).model_dump()
            new[name] = _validate(cls, merge_secrets({**old, **part}, old))
        for name, value in new.items():
            setattr(cfg, name, value)
        store.save()
        jobs.wake()
        return await get_settings_plain()

    async def get_settings_plain():
        return {"telerouter": redact(cfg.telerouter), "jellyfin": redact(cfg.jellyfin),
                "agent": cfg.agent.model_dump(), "security": cfg.security.model_dump()}

    @app.post("/api/settings/test/{what}")
    async def test_settings(what: str):
        try:
            if what == "telerouter":
                async with TelerouterClient(cfg.telerouter, transport=jobs.transport) as tr:
                    ms = await tr.machines()
                if not ms:
                    return {"ok": False, "message": "connected, but no machine allows API sessions"}
                names = ", ".join(f"{m['name']} ({m['agent_kind']}, {m['state']})" for m in ms)
                return {"ok": True, "message": f"connected; machines: {names}"}
            if what == "jellyfin":
                return {"ok": True, "message": "connected to " + await jellyfin_check(cfg.jellyfin, jobs.transport)}
        except RemoteError as e:
            return {"ok": False, "message": str(e)}
        raise HTTPException(404, "unknown test")

    # -- agents: MCP and uploads ------------------------------------------------------------------
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp_endpoint(request: Request):
        return await mcp.handle(request, client_ip(request), throttle)

    def upload_auth(request: Request, uid: str) -> tuple[dict, dict]:
        ip = client_ip(request)
        try:
            throttle.check(f"up:{ip}")
        except security.Locked as e:
            raise HTTPException(429, "too many attempts", headers={"Retry-After": str(int(e.retry_after) + 1)}) from e
        tok = security.bearer(request.headers)
        u = db.upload(uid) if re.match(r"^u_[0-9a-f]{16}$", uid) else None
        j = db.request(u["request_id"]) if u else None
        if not (u and j and tok.startswith("adu_") and security.token_matches(tok, u["token_hash"])
                and j["state"] in ACTIVE):
            with contextlib.suppress(security.Locked):
                throttle.begin(f"up:{ip}")
            raise HTTPException(401, "unauthorized")
        return u, j

    @app.get("/upload/{uid}")
    async def upload_status(uid: str, request: Request):
        u, _j = upload_auth(request, uid)
        return {"upload_id": u["id"], "size": u["size"], "received_bytes": u["received"], "state": u["state"]}

    @app.put("/upload/{uid}")
    async def upload_put(uid: str, request: Request):
        u, j = upload_auth(request, uid)
        if u["state"] != "open":
            raise HTTPException(409, f"upload is {u['state']}")
        lock = upload_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "this upload is already running")
        async with lock:
            path = jobs.staged(uid)
            have = path.stat().st_size if path.exists() else 0
            start = 0
            cr = request.headers.get("content-range", "")
            if cr:
                m = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", cr.strip())
                if not m or (m.group(3) != "*" and int(m.group(3)) != u["size"]):
                    raise HTTPException(400, "bad Content-Range")
                start = int(m.group(1))
                if start != have:
                    return JSONResponse({"detail": "resume from received_bytes", "received_bytes": have},
                                        status_code=416)
            received = start
            mode = "r+b" if start and path.exists() else "wb"
            os.makedirs(path.parent, mode=0o700, exist_ok=True)
            f = await asyncio.to_thread(open, path, mode)
            try:
                if start:
                    f.seek(start)
                else:
                    f.truncate(0)
                buf = bytearray()
                last_save = time.monotonic()
                try:
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > u["size"]:
                            raise HTTPException(413, f"more data than the announced {u['size']} bytes")
                        buf += chunk
                        if len(buf) >= CHUNK:
                            await asyncio.to_thread(f.write, bytes(buf))
                            buf.clear()
                            if time.monotonic() - last_save > 5:
                                db.set_upload(uid, received=received - len(buf))
                                last_save = time.monotonic()
                except ClientDisconnect:
                    pass
                if buf:
                    await asyncio.to_thread(f.write, bytes(buf))
            finally:
                await asyncio.to_thread(f.close)
            size_now = path.stat().st_size
            done = size_now == u["size"]
            db.set_upload(uid, received=size_now, state="complete" if done else "open")
            if done:
                db.event(j["id"], "progress", f"received {u['filename']}")
            return {"upload_id": uid, "received_bytes": size_now, "size": u["size"],
                    "state": "complete" if done else "open"}

    # -- UI ---------------------------------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/ca.pem")
    async def ca_pem():
        return Response(files.ca_pem, media_type="application/x-pem-file")

    @app.get("/static/{name}")
    async def static(name: str):
        p = STATIC / name
        if not re.match(r"^[a-z0-9_-]+\.(js|css|svg|png)$", name) or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    return app
