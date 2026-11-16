"""ADM's MCP server for download agents (Streamable HTTP, JSON responses, tools only, stateless).

Each job gets its own bearer token (`adj_...`, 256-bit, only its SHA-256 stored), handed to the agent's Claude
Code through telerouter's per-session MCP config. The token reaches exactly one request and one library, and
dies when the job ends. There is no tool that reads or writes outside that library, and no tool that runs
anything on the ADM host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__, library
from .security import bearer, new_token, token_hash
from .store import ACTIVE

log = logging.getLogger("adm.mcp")

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MAX_BODY = 128 * 1024
S = {"type": "string"}
STAGES = ["searching", "downloading", "verifying", "uploading", "importing", "other"]
FILENAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")
INSTRUCTIONS = ("ADM (agentic download manager): you are fulfilling one media request. get_request first; "
                "list_library before naming files; start_upload + curl + import_media per file; then "
                "complete_request or fail_request.")


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., Awaitable[Any]]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


class McpServer:
    def __init__(self, state):
        self.st = state
        self.tools: dict[str, Tool] = {}
        self._define()

    # -- auth ---------------------------------------------------------------------------------------
    def job_for(self, request_or_headers) -> dict | None:
        headers = getattr(request_or_headers, "headers", request_or_headers)
        tok = bearer(headers)
        if not tok.startswith("adj_"):
            return None
        j = self.st.db.by_token(token_hash(tok))
        return j if j and j["state"] in ACTIVE else None

    def _lib(self, job: dict):
        lib = self.st.store.library(job["library_id"])
        if not lib or not lib.enabled:
            raise ToolError("the request's library is gone or disabled; call fail_request")
        try:
            root = library.root_of(lib.path, self.st.settings.library_roots)
        except library.LibraryError as e:
            raise ToolError(f"the library is not available right now: {e}") from e
        return lib, root

    def _base(self, job: dict) -> str:
        """Where this job's agent reaches ADM: its SSH tunnel (http://127.0.0.1:port) or the configured address."""
        # from the machine itself, not the job's saved runtime: the agent's first calls can arrive before ADM
        # has stored the runtime of the turn it just started (that race once handed out an empty upload URL)
        runner = job.get("runner") or "telerouter"
        if runner != "telerouter":
            m = next((x for x in self.st.store.config.machines if x.id == runner), None)
            if m:
                return f"http://127.0.0.1:{m.tunnel_port}"
        return self.st.store.config.agent.adm_url

    def _curl(self, base: str, uid: str, token: str, local_name: str) -> str:
        """The upload command. Through an SSH tunnel: plain loopback HTTP (SSH encrypts it). With ADM's own
        certificate: curl pins its key (checked on every connection; -k only skips the public-CA chain check a
        private certificate can't pass). With an own certificate: normal verification."""
        url = f"{base}/upload/{uid}"
        tls = ""
        if url.startswith("https://") and self.st.spki_pin and not self.st.own_cert:
            tls = f"--pinnedpubkey {shlex.quote('sha256//' + self.st.spki_pin)} -k "
        return (f"curl --fail-with-body -sS {tls}-H {shlex.quote('Authorization: Bearer ' + token)} "
                f"-T {shlex.quote(local_name)} {shlex.quote(url)}")

    # -- tools --------------------------------------------------------------------------------------
    def _define(self) -> None:
        def tool(name, desc, schema):
            def deco(fn):
                self.tools[name] = Tool(name, desc, schema, fn)
                return fn
            return deco

        db = self.st.db

        @tool("get_request", "The media request you are fulfilling, its target library and the upload rules.", _obj({}))
        async def get_request(job):
            lib, _root = self._lib(job)
            return {"id": job["id"], "kind": job["kind"], "title": job["title"], "year": job["year"],
                    "details": job["details"], "notes": job["notes"],
                    "library": {"name": lib.name, "kind": lib.kind,
                                "allowed_file_types": sorted(library.ALLOWED[lib.kind])},
                    "owner_guidance": self.st.store.config.agent.guidance,
                    "uploads": "start_upload(filename, size_bytes) per file gives a curl command with a one-time "
                               "token for that file; then import_media(upload_id, path)",
                    "imported_so_far": [i["path"] for i in db.imports(job["id"])]}

        @tool("report_progress", "Tell the owner what you are doing now (shown in ADM).",
              _obj({"stage": {"type": "string", "enum": STAGES},
                    "message": {**S, "description": "one line, e.g. 'downloading 1080p WEB-DL, 4.2 GB'"},
                    "percent": {"type": "number", "minimum": 0, "maximum": 100}}, ["stage", "message"]))
        async def report_progress(job, stage: str, message: str, percent: float | None = None):
            if stage not in STAGES:
                raise ToolError(f"stage must be one of {', '.join(STAGES)}")
            msg = " ".join(str(message).split())[:300]
            db.update(job["id"], stage=stage, message=msg, progress=percent,
                      state="working" if job["state"] == "starting" else job["state"])
            db.event(job["id"], "progress", f"{stage}: {msg}" + (f" ({percent:.0f}%)" if percent is not None else ""))
            return {"ok": True}

        @tool("list_library", "Folders and files in the target library (relative paths). Start at the top, then "
              "look inside a few existing entries to copy their naming and nesting.",
              _obj({"path": {**S, "description": "folder inside the library; '' = the top"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1}}))
        async def list_library(job, path: str = "", depth: int = 1):
            _lib, root = self._lib(job)
            try:
                return await asyncio.to_thread(library.tree, root, path, int(depth))
            except library.LibraryError as e:
                raise ToolError(str(e)) from e

        @tool("find_in_library", "Search the library's folder and file names (all words must match), e.g. to see "
              "whether the title is already there.", _obj({"query": S}, ["query"]))
        async def find_in_library(job, query: str):
            _lib, root = self._lib(job)
            return {"matches": await asyncio.to_thread(library.find, root, str(query)[:200])}

        @tool("start_upload", "Announce one finished file you will import. Returns an upload id and the curl "
              "command that sends the file to ADM (run it from the folder the file is in).",
              _obj({"filename": {**S, "description": "the local file name, e.g. Movie.2024.1080p.mkv"},
                    "size_bytes": {"type": "integer", "minimum": 1}}, ["filename", "size_bytes"]))
        async def start_upload(job, filename: str, size_bytes: int):
            lib, _root = self._lib(job)
            name = os.path.basename(str(filename).strip())
            if not FILENAME_RE.match(name):
                raise ToolError("give the plain file name")
            library.check_ext(lib.kind, library.PurePosixPath(name))
            size = int(size_bytes)
            cap = self.st.settings.max_upload_gb * 1024 ** 3
            if size > cap:
                raise ToolError(f"files over {self.st.settings.max_upload_gb} GB are refused")
            staging = self.st.settings.staging
            os.makedirs(staging, mode=0o700, exist_ok=True)
            free = shutil.disk_usage(staging).free
            if size + 2 * 1024 ** 3 > free:
                raise ToolError(f"ADM has only {free / 1024 ** 3:.1f} GB free for uploads")
            if len([u for u in db.uploads(job["id"]) if u["state"] == "open"]) >= 50:
                raise ToolError("too many unfinished uploads for this request")
            token = new_token("adu_")
            u = db.new_upload(job["id"], name, size, token_hash(token))
            db.event(job["id"], "progress", f"uploading {name} ({human(size)})")
            base = self._base(job)
            return {"upload_id": u["id"], "url": f"{base}/upload/{u['id']}",
                    "command": self._curl(base, u["id"], token, name),
                    "resume": "if it breaks off: upload_status, then the same command with "
                              "-C <received_bytes> to continue",
                    "then": "import_media(upload_id, path inside the library)"}

        @tool("upload_status", "How many bytes of an upload arrived.", _obj({"upload_id": S}, ["upload_id"]))
        async def upload_status(job, upload_id: str):
            u = db.upload(str(upload_id))
            if not u or u["request_id"] != job["id"]:
                raise ToolError("no such upload")
            return {"upload_id": u["id"], "filename": u["filename"], "size": u["size"], "received_bytes": u["received"],
                    "state": u["state"]}

        @tool("import_media", "Put an uploaded file into the library at `path` (relative to the library root, "
              "folders are created). Never overwrites. Name it like the existing entries.",
              _obj({"upload_id": S,
                    "path": {**S, "description": "e.g. 'Dune (2021)/Dune (2021).mkv' or "
                                                  "'Show/Season 01/Show - S01E02 - Title.mkv'"}},
                   ["upload_id", "path"]))
        async def import_media(job, upload_id: str, path: str):
            lib, root = self._lib(job)
            u = db.upload(str(upload_id))
            if not u or u["request_id"] != job["id"]:
                raise ToolError("no such upload")
            if u["state"] != "complete":
                raise ToolError(f"the upload is {u['state']} ({u['received']} of {u['size']} bytes)")
            src = self.st.jobs.staged(u["id"])
            try:
                rel, size = await asyncio.to_thread(library.place, src, root, lib.kind, str(path))
            except library.LibraryError as e:
                raise ToolError(str(e)) from e
            except OSError as e:
                raise ToolError(f"could not write into the library ({e.__class__.__name__}: {e.strerror})") from e
            db.set_upload(u["id"], state="imported")
            db.add_import(job["id"], lib.id, rel, size)
            db.update(job["id"], stage="importing", message=f"imported {rel}")
            db.event(job["id"], "import", f"{lib.name}: {rel} ({human(size)})")
            if lib.refresh == "jellyfin":
                self.st.jobs.schedule_refresh()
            return {"imported": rel, "library": lib.name, "size": size}

        @tool("complete_request", "Finish the request after importing everything. Ends the job.",
              _obj({"summary": {**S, "description": "what you imported, the quality and where it came from"}},
                   ["summary"]))
        async def complete_request(job, summary: str):
            if not db.imports(job["id"]):
                raise ToolError("nothing was imported yet: import_media first (or fail_request)")
            await self.st.jobs.finish(job["id"], "done", summary=str(summary)[:4000], by_agent=True)
            return {"ok": True, "note": "thanks; delete the job folder's downloads now. ADM will close this session."}

        @tool("fail_request", "Give up on the request with the reason (nothing usable found, ...). Ends the job.",
              _obj({"reason": S}, ["reason"]))
        async def fail_request(job, reason: str):
            await self.st.jobs.finish(job["id"], "failed", error=str(reason)[:2000], by_agent=True)
            return {"ok": True}

    # -- protocol -----------------------------------------------------------------------------------
    async def handle(self, request: Request, ip: str, throttle) -> Response:
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        if request.headers.get("origin"):
            return JSONResponse({"error": "browsers may not call this"}, status_code=403)
        try:
            throttle.check(f"mcp:{ip}")
        except Exception:
            return JSONResponse({"error": "too many attempts"}, status_code=429)
        job = self.job_for(request)
        if not job:
            try:
                throttle.begin(f"mcp:{ip}")
            except Exception:
                pass
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="adm"'})
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            msg = json.loads(body)
        except ValueError:
            return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
        if isinstance(msg, list):
            results = [r for r in [await self._one(job, m) for m in msg[:20]] if r is not None]
            return JSONResponse(results) if results else Response(status_code=202)
        res = await self._one(job, msg)
        return JSONResponse(res) if res is not None else Response(status_code=202)

    async def _one(self, job: dict, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            return _ok(mid, {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "adm", "version": __version__}, "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                                       for t in self.tools.values()]})
        if method != "tools/call":
            return _err(mid, -32601, f"method not found: {method}")
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        t = self.tools.get(name)
        if not t:
            return _err(mid, -32602, f"unknown tool: {name}")
        if not isinstance(args, dict) or set(args) - set(t.schema["properties"]):
            return _err(mid, -32602, "invalid arguments")
        for k in t.schema.get("required", []):
            if k not in args:
                return _err(mid, -32602, f"missing argument: {k}")
        job = self.st.db.request(job["id"]) or job          # fresh state (an earlier call may have ended it)
        if job["state"] not in ACTIVE:
            return _ok(mid, {"content": [{"type": "text", "text": f"the request is {job['state']}"}], "isError": True})
        log.info("MCP %s for %s", name, job["id"])
        try:
            result = await asyncio.wait_for(t.fn(job, **args), 600)
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                             "structuredContent": result, "isError": False})
        except ToolError as ex:
            return _ok(mid, {"content": [{"type": "text", "text": str(ex)}], "isError": True})
        except library.LibraryError as ex:
            return _ok(mid, {"content": [{"type": "text", "text": str(ex)}], "isError": True})
        except TimeoutError:
            return _ok(mid, {"content": [{"type": "text", "text": "timed out"}], "isError": True})
        except (TypeError, ValueError) as ex:
            return _err(mid, -32602, f"invalid arguments: {ex}")
        except Exception as ex:
            log.exception("MCP tool %s failed", name)
            return _ok(mid, {"content": [{"type": "text", "text": f"failed: {ex.__class__.__name__}"}], "isError": True})


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def human(n: int) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"
