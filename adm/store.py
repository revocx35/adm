"""Files and the database: config.json (0600, atomic) and SQLite adm.db (requests, events, uploads, imports)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .config import Config

log = logging.getLogger("adm.store")


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically write `data` to `path` with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigStore:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.dir / "config.json"
        self.config = self._load()

    def _load(self) -> Config:
        if not self.path.exists():
            return Config()
        try:
            return Config.model_validate_json(self.path.read_text())
        except ValueError:
            backup = self.path.with_suffix(f".broken-{int(time.time())}")
            os.replace(self.path, backup)
            log.error("config.json could not be read; moved to %s and starting empty", backup.name)
            return Config()

    def save(self) -> None:
        write_private(self.path, self.config.model_dump_json(indent=1))

    def library(self, lid: str):
        return next((x for x in self.config.libraries if x.id == lid), None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, year TEXT NOT NULL DEFAULT '',
  details TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', library_id TEXT NOT NULL,
  state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress REAL, message TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '', session_name TEXT NOT NULL DEFAULT '', session_turns INTEGER NOT NULL DEFAULT 0,
  token_hash TEXT NOT NULL DEFAULT '', nudges INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL, started REAL, finished REAL);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_req ON events(request_id, id);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
  received INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, token_hash TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, library_id TEXT NOT NULL, path TEXT NOT NULL,
  size INTEGER NOT NULL, at REAL NOT NULL);
"""

ACTIVE = ("starting", "working")
FINISHED = ("done", "failed", "cancelled")


class Db:
    """SQLite with one connection and a lock: every call is short (a few rows), so it runs on the event loop."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(requests)")}
        for col, ddl in (("runner", "TEXT NOT NULL DEFAULT 'telerouter'"), ("model", "TEXT NOT NULL DEFAULT ''"),
                         ("runtime", "TEXT NOT NULL DEFAULT '{}'")):
            if col not in cols:             # databases made by 0.1
                self.conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
        self.lock = threading.Lock()
        os.chmod(path, 0o600)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # -- requests ---------------------------------------------------------------------------------
    def new_request(self, kind: str, title: str, year: str, details: str, notes: str, library_id: str,
                    runner: str = "telerouter", model: str = "") -> dict:
        rid = "r_" + secrets.token_hex(5)
        now = time.time()
        self.x("INSERT INTO requests (id, kind, title, year, details, notes, library_id, state, created, updated, "
               "runner, model) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)",
               (rid, kind, title, year, details, notes, library_id, now, now, runner, model))
        self.event(rid, "state", "queued")
        return self.request(rid)

    def request(self, rid: str) -> dict | None:
        return self.one("SELECT * FROM requests WHERE id=?", (rid,))

    def requests(self, limit: int = 200, state: str = "") -> list[dict]:
        if state:
            return self.q("SELECT * FROM requests WHERE state=? ORDER BY created DESC LIMIT ?", (state, limit))
        return self.q("SELECT * FROM requests ORDER BY created DESC LIMIT ?", (limit,))

    def update(self, rid: str, **kw) -> None:
        if not kw:
            return
        kw["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE requests SET {cols} WHERE id=?", (*kw.values(), rid))

    def runtime(self, rid: str) -> dict:
        r = self.one("SELECT runtime FROM requests WHERE id=?", (rid,))
        try:
            return json.loads(r["runtime"]) if r else {}
        except ValueError:
            return {}

    def set_runtime(self, rid: str, rt: dict) -> None:
        self.update(rid, runtime=json.dumps(rt))

    def by_token(self, token_hash: str) -> dict | None:
        if not token_hash:
            return None
        return self.one("SELECT * FROM requests WHERE token_hash=?", (token_hash,))

    def delete_request(self, rid: str) -> None:
        for t in ("events", "uploads", "imports"):
            self.x(f"DELETE FROM {t} WHERE request_id=?", (rid,))
        self.x("DELETE FROM requests WHERE id=?", (rid,))

    # -- events -----------------------------------------------------------------------------------
    def event(self, rid: str, kind: str, text: str) -> None:
        self.x("INSERT INTO events (request_id, at, kind, text) VALUES (?,?,?,?)", (rid, time.time(), kind, text[:4000]))

    def events(self, rid: str, after: int = 0, limit: int = 500) -> list[dict]:
        return self.q("SELECT * FROM events WHERE request_id=? AND id>? ORDER BY id LIMIT ?", (rid, after, limit))

    # -- uploads / imports ------------------------------------------------------------------------
    def new_upload(self, rid: str, filename: str, size: int, token_hash: str) -> dict:
        uid = "u_" + secrets.token_hex(8)
        now = time.time()
        self.x("INSERT INTO uploads (id, request_id, filename, size, state, token_hash, created, updated) "
               "VALUES (?,?,?,?,?,?,?,?)", (uid, rid, filename, size, "open", token_hash, now, now))
        return self.upload(uid)

    def upload(self, uid: str) -> dict | None:
        return self.one("SELECT * FROM uploads WHERE id=?", (uid,))

    def uploads(self, rid: str) -> list[dict]:
        return self.q("SELECT * FROM uploads WHERE request_id=? ORDER BY created", (rid,))

    def set_upload(self, uid: str, **kw) -> None:
        kw["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE uploads SET {cols} WHERE id=?", (*kw.values(), uid))

    def add_import(self, rid: str, lid: str, path: str, size: int) -> None:
        self.x("INSERT INTO imports (request_id, library_id, path, size, at) VALUES (?,?,?,?,?)",
               (rid, lid, path, size, time.time()))

    def imports(self, rid: str) -> list[dict]:
        return self.q("SELECT * FROM imports WHERE request_id=? ORDER BY id", (rid,))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
