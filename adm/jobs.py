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
from .sshrun import SshError, SshRunner
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
        self.ssh: SshRunner = SshRunner(state.settings.data_dir, state.settings.internal_port)

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
        await self.ssh.close_all()

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
    def machine(self, runner: str):
        return next((m for m in self.st.store.config.machines if m.id == runner), None)

    async def tick(self) -> None:
        db = self.st.db
        rows = db.requests(1000)
        active = [j for j in rows if j["state"] in ACTIVE]
        queued = sorted((j for j in rows if j["state"] == "queued"), key=lambda j: j["created"])
        tr_jobs = [j for j in active if j["runner"] == "telerouter"]
        if tr_jobs:
            try:
                async with self.client() as tr:
                    for j in tr_jobs:
                        await self._check(tr, j)
            except RemoteError as e:
                log.info("telerouter: %s", e)
        for j in active:
            if j["runner"] != "telerouter":
                await self._check_ssh(j)
        for j in rows:                       # finished SSH jobs whose agent may still be wrapping up
            if j["state"] in FINISHED and j["runner"] != "telerouter" and '"cleanup_after"' in (j["runtime"] or ""):
                await self._cleanup_ssh(j)
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

    def _requeue(self, j: dict, why: str) -> None:
        self.st.db.update(j["id"], state="queued", token_hash="", message=f"waiting: {why}", stage="")
        self.st.db.event(j["id"], "system", f"could not start the agent: {why}")
        self._next_try[j["id"]] = time.time() + RETRY_START

    async def _start(self, j: dict) -> None:
        cfg = self.st.store.config
        db = self.st.db
        lib = self.st.store.library(j["library_id"])
        m = self.machine(j["runner"]) if j["runner"] != "telerouter" else None
        if j["runner"] == "telerouter":
            problem = ("ADM's address for agents is not set (Settings -> Agent)" if not cfg.agent.adm_url else
                       "telerouter is not set up (Settings -> telerouter)"
                       if not (cfg.telerouter.url and cfg.telerouter.token) else "")
        else:
            problem = ("its agent machine was removed" if not m else f"{m.name} is disabled" if not m.enabled else "")
            if m and m.kind == "hermes":
                # Hermes 0.22's `hermes chat` hands the conversation to the user's running gateway, which uses its own
                # config: the job would run without ADM's tools and with the user's MCP servers (seen 2026-11-17).
                # Refused until a job can run isolated from that gateway.
                db.update(j["id"], state="failed", finished=time.time(), stage="failed",
                          error="Hermes jobs are switched off for now: Hermes hands each job to your running Hermes "
                                "gateway, which uses its own settings instead of the job's, so the agent would not get "
                                "ADM's tools. Use a Claude Code machine meanwhile.")
                db.event(j["id"], "state", "failed: Hermes jobs are switched off for now")
                return
        problem = "its library was removed" if not lib else problem
        if problem:
            db.update(j["id"], message=f"waiting: {problem}")
            self._next_try[j["id"]] = time.time() + RETRY_START
            return
        token = security.new_token("adj_")
        model = j["model"] or (m.default_model if m else "")
        db.update(j["id"], state="starting", token_hash=security.token_hash(token), message="", stage="starting",
                  error="", started=time.time(), attempts=j["attempts"] + 1, nudges=0, session_turns=0,
                  session_id="", session_name="", runtime="{}")
        db.event(j["id"], "state", "starting the agent" + (f" ({model})" if model else ""))
        prompt = build_prompt(j, lib.name, cfg.agent.guidance)
        if m:
            await self._start_ssh(j, m, prompt, token, model)
            return
        name = "adm-" + j["id"][2:]
        server = {"name": "adm", "url": f"{cfg.agent.adm_url}/mcp", "token": token,
                  "description": "ADM: the media request you are fulfilling"}
        if self.st.tls_ca_pem and not self.st.own_cert:
            server["tls_ca"] = self.st.tls_ca_pem
        body = {"name": name, "folder": f"{cfg.telerouter.folder.rstrip('/')}/{j['id']}", "prompt": prompt,
                "timeout": int(min(cfg.agent.job_timeout_hours * 3600, 86_400)), "mcp_servers": [server]}
        if cfg.telerouter.machine:
            body["machine"] = cfg.telerouter.machine
        if model:
            body["model"] = model
        try:
            async with self.client() as tr:
                s = await tr.create(**body)
        except RemoteError as e:
            self._requeue(j, str(e))        # telerouter down or refusing: back to the queue, retry in a minute
            return
        self._next_try.pop(j["id"], None)
        db.update(j["id"], session_id=str(s.get("id", "")), session_name=str(s.get("name") or name),
                  message=f"session {s.get('name')} on {s.get('machine')}")
        db.event(j["id"], "system", f"agent session {s.get('name')} on {s.get('machine')} (folder {s.get('folder')})")
        log.info("job %s: session %s on %s", j["id"], s.get("name"), s.get("machine"))

    async def _start_ssh(self, j: dict, m, prompt: str, token: str, model: str) -> None:
        db = self.st.db
        try:
            rt = await self.ssh.start_turn(m, j["id"], {"machine": m.id, "model": model}, prompt, token, model)
        except SshError as e:
            self._requeue(j, str(e))
            return
        self._next_try.pop(j["id"], None)
        self._save_turn(j["id"], rt)
        db.event(j["id"], "system", f"agent started on {m.name} over SSH (folder {rt['dir']})")
        if db.request(j["id"])["state"] in FINISHED:
            return                       # a fast agent already finished the job while the turn was being started
        db.update(j["id"], state="working", stage="searching", session_name=f"{m.name} pid {rt['pid']}",
                  message=f"{'Claude Code' if m.kind == 'claude' else 'Hermes'} on {m.name}")

    def _save_turn(self, rid: str, rt: dict) -> None:
        """Store a just-started turn's runtime without losing what arrived meanwhile: the agent runs from the
        moment it is launched, so it may already have ended the job (cleanup_after) or the owner written to it."""
        cur = self.st.db.runtime(rid)
        for k in ("cleanup_after", "owner_messages"):
            if k in cur:
                rt[k] = cur[k]
        self.st.db.set_runtime(rid, rt)

    def _timed_out(self, j: dict) -> bool:
        cfg = self.st.store.config
        return bool(j["started"] and time.time() - j["started"] > cfg.agent.job_timeout_hours * 3600)

    async def _turn_over(self, j: dict, reply: str, send) -> None:
        """A turn ended while the request is still open: the owner's queued messages, a reminder, or give up."""
        db, cfg = self.st.db, self.st.store.config
        if reply:
            db.event(j["id"], "agent", reply)
        j = db.request(j["id"])
        if not j or j["state"] in FINISHED:
            return
        if j["nudges"] < cfg.agent.max_nudges:
            db.update(j["id"], nudges=j["nudges"] + 1, state="working")
            db.event(j["id"], "system", "the agent stopped without finishing; reminding it")
            await send(NUDGE.format(rid=j["id"]))
        else:
            await self.finish(j["id"], "failed", error="the agent stopped without completing the request" +
                                                       (f": {reply[:500]}" if reply else ""))

    async def _check(self, tr: TelerouterClient, j: dict) -> None:
        db, cfg = self.st.db, self.st.store.config
        if self._timed_out(j):
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
            db.update(j["id"], session_turns=int(s.get("turns") or 0))

            async def send(text: str) -> None:
                with contextlib.suppress(RemoteError):
                    await tr.send(j["session_id"], text, timeout=int(min(cfg.agent.job_timeout_hours * 3600, 86_400)))
            await self._turn_over(j, str(s.get("reply") or "").strip(), send)

    async def _check_ssh(self, j: dict) -> None:
        db, cfg = self.st.db, self.st.store.config
        m = self.machine(j["runner"])
        if not m:
            await self.finish(j["id"], "failed", error="its agent machine was removed")
            return
        if self._timed_out(j):
            await self.finish(j["id"], "failed", error=f"timed out after {cfg.agent.job_timeout_hours:g} h")
            return
        if m.kind == "hermes":                       # see _start: Hermes jobs are switched off for now
            await self.finish(j["id"], "failed", error="Hermes jobs are switched off for now: Hermes hands each job to "
                              "your running Hermes gateway, which uses its own settings instead of the job's, so the "
                              "agent would not get ADM's tools. Use a Claude Code machine meanwhile.")
            return
        rt = db.runtime(j["id"])
        if not rt.get("pid"):
            return
        try:
            rt, result = await self.ssh.poll(m, rt)
        except SshError as e:
            db.update(j["id"], message=f"can't reach {m.name}: {e}")    # the agent keeps working; look again later
            return
        db.set_runtime(j["id"], rt)
        if result is None:
            return
        db.update(j["id"], session_turns=int(rt.get("turn") or 0))
        if result["error"] and not result["text"]:
            db.event(j["id"], "system", f"turn {rt.get('turn')} ended: {result['error']}")
            if not rt.get("session") or result["exit"] not in (0, 1):
                await self.finish(j["id"], "failed", error=f"the agent failed: {result['error']}")
                return
        pending = list(rt.get("owner_messages") or [])

        async def send(text: str) -> None:
            await self._next_turn(j, m, text)
        if pending and db.request(j["id"])["state"] not in FINISHED:
            rt["owner_messages"] = []
            db.set_runtime(j["id"], rt)
            if result["text"]:
                db.event(j["id"], "agent", result["text"])
            await self._next_turn(j, m, "\n\n".join(f"[ADM, from the owner about request {j['id']}] {t}"
                                                     for t in pending))
            return
        await self._turn_over(j, result["text"].strip(), send)

    async def _next_turn(self, j: dict, m, text: str) -> None:
        db = self.st.db
        rt = db.runtime(j["id"])
        token = ""                                   # the job's MCP config from turn 1 is reused
        try:
            rt = await self.ssh.start_turn(m, j["id"], rt, text, token, rt.get("model", ""))
        except SshError as e:
            await self.finish(j["id"], "failed", error=f"could not continue the agent: {e}")
            return
        self._save_turn(j["id"], rt)
        db.update(j["id"], session_name=f"{m.name} pid {rt['pid']}")

    async def message(self, j: dict, text: str) -> str:
        """The owner writes to the agent: telerouter types it in; over SSH it is queued for the next turn."""
        cfg = self.st.store.config
        if j["runner"] == "telerouter":
            async with self.client() as tr:
                await tr.send(j["session_id"], f"[ADM, from the owner about request {j['id']}] {text}",
                              timeout=int(min(cfg.agent.job_timeout_hours * 3600, 86_400)))
            return "sent"
        rt = self.st.db.runtime(j["id"])
        rt["owner_messages"] = (list(rt.get("owner_messages") or []) + [text])[-10:]
        self.st.db.set_runtime(j["id"], rt)
        return "queued: the agent gets it when its current turn ends"

    def agent_label(self, j: dict) -> str:
        """Where and with what a request runs, for the request page."""
        if j["runner"] == "telerouter":
            return " · ".join(x for x in ("telerouter", j["session_name"] and f"tmux {j['session_name']}", j["model"]) if x)
        m = self.machine(j["runner"])
        model = j["model"] or self.st.db.runtime(j["id"]).get("model") or (m.default_model if m else "")
        kind = ("Hermes" if m.kind == "hermes" else "Claude Code") if m else "?"
        return " · ".join(x for x in ((m.name if m else "removed machine"), f"{kind} over SSH", model) if x)

    def screen(self, j: dict) -> str | None:
        """SSH jobs: the agent's recent output; None for telerouter jobs (ask telerouter)."""
        if j["runner"] == "telerouter":
            return None
        return "\n".join(self.st.db.runtime(j["id"]).get("screen") or []) or "(no output yet)"

    # -- ending a job -----------------------------------------------------------------------------
    async def finish(self, rid: str, state: str, error: str = "", summary: str = "", by_agent: bool = False) -> None:
        db, cfg = self.st.db, self.st.store.config
        j = db.request(rid)
        if not j or j["state"] in FINISHED:
            return
        db.update(rid, state=state, token_hash="", finished=time.time(), error=error[:2000],
                  summary=summary[:4000] or j["summary"], stage=state, message="")
        db.event(rid, "state", state + (f": {error}" if error else (f": {summary}" if summary else "")))
        log.info("job %s %s%s", rid, state, f" ({error})" if error else "")
        self.discard_uploads(rid)
        if j["runner"] != "telerouter":
            rt = db.runtime(rid)
            # the agent that ended the job is still finishing its turn: let it, then clean up (max 3 min)
            rt["cleanup_after"] = time.time() + (180 if by_agent else 0)
            db.set_runtime(rid, rt)
            if not by_agent:
                await self._cleanup_ssh(db.request(rid))
        elif j["session_id"] and not cfg.agent.keep_sessions:
            try:
                async with self.client() as tr:
                    await tr.close(j["session_id"])
            except RemoteError as e:
                db.event(rid, "system", f"could not close the agent session: {e}")
        self.wake()

    async def _cleanup_ssh(self, j: dict) -> None:
        db, cfg = self.st.db, self.st.store.config
        rt = db.runtime(j["id"])
        m = self.machine(j["runner"])
        if not m or not rt.get("dir"):
            rt.pop("cleanup_after", None)
            db.set_runtime(j["id"], rt)
            return
        try:
            if rt.get("pid") and rt.get("turn"):
                with contextlib.suppress(SshError, KeyError):
                    rt, _r = await self.ssh.poll(m, rt)        # the output the agent wrote since the last look
                    keep_after = db.runtime(j["id"]).get("cleanup_after")
                    rt["cleanup_after"] = keep_after if keep_after is not None else rt.get("cleanup_after", 0)
                    db.set_runtime(j["id"], rt)
            if time.time() < float(rt.get("cleanup_after") or 0):
                code, out, _e = await self.ssh.sh(m, f"kill -0 {int(rt.get('pid') or 0)} 2>/dev/null && echo ALIVE")
                if "ALIVE" in out:
                    return                                    # still wrapping up; next tick
            await self.ssh.stop(m, rt)
            await self.ssh.cleanup(m, rt, keep=cfg.agent.keep_sessions)
        except SshError as e:
            log.info("job %s: cleanup on %s postponed: %s", j["id"], m.name, e)
            return
        rt.pop("cleanup_after", None)
        db.set_runtime(j["id"], rt)
        db.event(j["id"], "system", "agent stopped and job folder " + ("secrets removed" if cfg.agent.keep_sessions
                                                                       else "removed") + f" on {m.name}")

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
