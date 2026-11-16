"""The job loop: queued requests become telerouter agent sessions; their progress comes back over ADM's MCP
server (the agent's tools) and telerouter's session state (turn over, failed, closed).

    queued -> starting (session requested) -> working (task typed in) -> done | failed | cancelled

A job ends when the agent calls complete_request / fail_request, when the owner cancels it, when it runs past
its timeout, or when the agent stops (its turn ended) without finishing even after `max_nudges` reminders.
Its MCP token is revoked, its staged uploads deleted, and its tmux session closed (unless keep_sessions).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import security
from .remote import RemoteError, TelerouterClient, jellyfin_refresh
from .store import ACTIVE, FINISHED

if TYPE_CHECKING:
    from .app_state import State

log = logging.getLogger("adm.jobs")

TICK = 10.0
RETRY_START = 60.0
KIND_WORD = {"movies": "movie", "shows": "TV show", "books": "book"}

PROMPT = """You are ADM's download agent (ADM = the owner's agentic download manager). Fulfil this media request \
end to end and report through the `adm` MCP tools. Nobody is watching live; ADM tracks the job.

REQUEST {rid}: {kind_word} "{title}"{year}{details}
{notes}Target library: "{library}" ({kind}).
{guidance}
How to work:
1. Call adm get_request first: it has the full request and the rules for uploads.
2. Work only in your current folder (the job folder); keep downloads and temporary files here.
3. Find a copy of the requested {kind_word} on the internet and download it here. Follow the owner's guidance \
above on sources, quality and language. Call report_progress when you move to a new stage \
(searching, downloading, verifying, uploading, importing).
4. Check what you got: the right title, year{check_extra}; complete and readable (ffprobe/file if installed); no \
samples, trailers, executables or password-protected archives. Extract archives here.
5. Look at the library before naming anything: list_library (and find_in_library to see whether it is already \
there). Name the new folders and files exactly the way the existing entries are named and nested.
6. For every file: start_upload, run the curl command it returns, then import_media with the path inside the \
library. Subtitles that belong to a video may be imported next to it.
7. Finish with complete_request (a short summary: what you imported and from where). If you can't get a usable \
copy, call fail_request with the reason instead of importing something wrong.
8. Delete the downloads in the job folder once they are imported.
Don't phone, message or announce anything about this job: ADM reports it to the owner."""

NUDGE = ("[ADM] Your turn ended but request {rid} is still open. If the work is done, call adm complete_request; if "
         "it can't be done, call adm fail_request with the reason; otherwise continue with the next step.")


def build_prompt(job: dict, library_name: str, guidance: str) -> str:
    kind = job["kind"]
    details = f" ({job['details']})" if job.get("details") else ""
    return PROMPT.format(
        rid=job["id"], kind_word=KIND_WORD[kind], title=job["title"],
        year=f" ({job['year']})" if job.get("year") else "", details=details,
        notes=f"Notes from the owner: {job['notes']}\n" if job.get("notes") else "",
        library=library_name, kind=kind,
        guidance=f"Owner's standing guidance for every job:\n{guidance.strip()}\n" if guidance.strip() else "",
        check_extra=", season and episodes" if kind == "shows" else (", author and edition" if kind == "books" else ""))


class Jobs:
    def __init__(self, state: "State"):
        self.st = state
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._next_try: dict[str, float] = {}
        self._refresh_due: float = 0.0
        self.transport = None          # tests: an httpx transport for telerouter/Jellyfin

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job loop")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK)

    def client(self) -> TelerouterClient:
        return TelerouterClient(self.st.store.config.telerouter, transport=self.transport)

    # -- one pass ---------------------------------------------------------------------------------
    async def tick(self) -> None:
        db = self.st.db
        active = [j for j in db.requests(1000) if j["state"] in ACTIVE]
        queued = sorted(db.requests(1000, "queued"), key=lambda j: j["created"])
        if active:
            try:
                async with self.client() as tr:
                    for j in active:
                        await self._check(tr, j)
            except RemoteError as e:
                log.info("telerouter: %s", e)
        free = self.st.store.config.agent.max_concurrent - len([j for j in db.requests(1000) if j["state"] in ACTIVE])
        now = time.time()
        for j in queued:
            if free <= 0:
                break
            if self._next_try.get(j["id"], 0) > now:
                continue
            free -= 1
            await self._start(j)
        if self._refresh_due and now >= self._refresh_due:
            self._refresh_due = 0.0
            await self._refresh()

    async def _start(self, j: dict) -> None:
        cfg = self.st.store.config
        db = self.st.db
        lib = self.st.store.library(j["library_id"])
        problem = ("its library was removed" if not lib else
                   "ADM's address for agents is not set (Settings -> Agent)" if not cfg.agent.adm_url else
                   "telerouter is not set up (Settings -> telerouter)" if not (cfg.telerouter.url and cfg.telerouter.token)
                   else "")
        if problem:
            db.update(j["id"], message=f"waiting: {problem}")
            self._next_try[j["id"]] = time.time() + RETRY_START
            return
        token = security.new_token("adj_")
        name = "adm-" + j["id"][2:]
        db.update(j["id"], state="starting", token_hash=security.token_hash(token), session_name=name, message="",
                  stage="starting", error="", started=time.time(), attempts=j["attempts"] + 1, nudges=0,
                  session_turns=0, session_id="")
        db.event(j["id"], "state", "starting an agent session")
        server = {"name": "adm", "url": f"{cfg.agent.adm_url}/mcp", "token": token,
                  "description": "ADM: the media request you are fulfilling"}
        if self.st.tls_ca_pem and not self.st.own_cert:
            server["tls_ca"] = self.st.tls_ca_pem
        body = {"name": name, "folder": f"{cfg.telerouter.folder.rstrip('/')}/{j['id']}",
                "prompt": build_prompt(j, lib.name, cfg.agent.guidance),
                "timeout": int(min(cfg.agent.job_timeout_hours * 3600, 86_400)), "mcp_servers": [server]}
        if cfg.telerouter.machine:
            body["machine"] = cfg.telerouter.machine
        try:
            async with self.client() as tr:
                s = await tr.create(**body)
        except RemoteError as e:
            # telerouter down or refusing: back to the queue, try again in a minute
            db.update(j["id"], state="queued", token_hash="", message=f"waiting: {e}", stage="")
            db.event(j["id"], "system", f"could not start a session: {e}")
            self._next_try[j["id"]] = time.time() + RETRY_START
            return
        self._next_try.pop(j["id"], None)
        db.update(j["id"], session_id=str(s.get("id", "")), session_name=str(s.get("name") or name),
                  message=f"session {s.get('name')} on {s.get('machine')}")
        db.event(j["id"], "system", f"agent session {s.get('name')} on {s.get('machine')} (folder {s.get('folder')})")
        log.info("job %s: session %s on %s", j["id"], s.get("name"), s.get("machine"))

    async def _check(self, tr: TelerouterClient, j: dict) -> None:
        db, cfg = self.st.db, self.st.store.config
        if j["started"] and time.time() - j["started"] > cfg.agent.job_timeout_hours * 3600:
            await self.finish(j["id"], "failed", error=f"timed out after {cfg.agent.job_timeout_hours:g} h")
            return
        if not j["session_id"]:
            return
        try:
            s = await tr.get(j["session_id"])
        except RemoteError as e:
            if "no such session" in str(e):
                await self.finish(j["id"], "failed", error="telerouter no longer knows the agent session")
            return
        state = s.get("state")
        if state == "working" and j["state"] == "starting":
            db.update(j["id"], state="working", stage=j["stage"] if j["stage"] != "starting" else "searching")
            db.event(j["id"], "state", "the agent is working on it")
        elif state == "failed":
            await self.finish(j["id"], "failed", error=f"agent session failed: {s.get('error') or 'unknown'}")
        elif state == "closed":
            await self.finish(j["id"], "failed", error="the agent session was closed")
        elif state == "idle" and int(s.get("turns") or 0) > j["session_turns"]:
            reply = str(s.get("reply") or "").strip()
            db.update(j["id"], session_turns=int(s.get("turns") or 0))
            if reply:
                db.event(j["id"], "agent", reply)
            j = db.request(j["id"])
            if not j or j["state"] in FINISHED:
                return
            if j["nudges"] < cfg.agent.max_nudges:
                db.update(j["id"], nudges=j["nudges"] + 1, state="working")
                db.event(j["id"], "system", "the agent stopped without finishing; reminding it")
                with contextlib.suppress(RemoteError):
                    await tr.send(j["session_id"], NUDGE.format(rid=j["id"]),
                                  timeout=int(min(cfg.agent.job_timeout_hours * 3600, 86_400)))
            else:
                await self.finish(j["id"], "failed",
                                  error="the agent stopped without completing the request" +
                                        (f": {reply[:500]}" if reply else ""))

    # -- ending a job -----------------------------------------------------------------------------
    async def finish(self, rid: str, state: str, error: str = "", summary: str = "") -> None:
        db, cfg = self.st.db, self.st.store.config
        j = db.request(rid)
        if not j or j["state"] in FINISHED:
            return
        db.update(rid, state=state, token_hash="", finished=time.time(), error=error[:2000],
                  summary=summary[:4000] or j["summary"], stage=state, message="")
        db.event(rid, "state", state + (f": {error}" if error else (f": {summary}" if summary else "")))
        log.info("job %s %s%s", rid, state, f" ({error})" if error else "")
        self.discard_uploads(rid)
        if j["session_id"] and not cfg.agent.keep_sessions:
            try:
                async with self.client() as tr:
                    await tr.close(j["session_id"])
            except RemoteError as e:
                db.event(rid, "system", f"could not close the agent session: {e}")
        self.wake()

    def discard_uploads(self, rid: str) -> None:
        for u in self.st.db.uploads(rid):
            if u["state"] != "imported":
                self.st.db.set_upload(u["id"], state="discarded")
            with contextlib.suppress(OSError):
                self.staged(u["id"]).unlink(missing_ok=True)

    def staged(self, uid: str) -> Path:
        return Path(self.st.settings.staging) / f"{uid}.part"

    # -- library refresh --------------------------------------------------------------------------
    def schedule_refresh(self) -> None:
        self._refresh_due = time.time() + 30          # several files in a row: one scan

    async def _refresh(self) -> None:
        cfg = self.st.store.config
        if not any(lib.refresh == "jellyfin" for lib in cfg.libraries):
            return
        try:
            await jellyfin_refresh(cfg.jellyfin, transport=self.transport)
            log.info("asked Jellyfin to scan its libraries")
        except RemoteError as e:
            log.warning("Jellyfin refresh: %s", e)
