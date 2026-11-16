import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from adm import tls
from adm.jobs import Jobs
from adm.settings import Settings
from adm.web import create_app

PW = "correct horse battery"


class FakeTelerouter:
    """telerouter's Sessions API and Jellyfin, as an httpx transport."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.created: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.refreshed = 0
        self.down = False
        self.auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        if self.down:
            raise httpx.ConnectError("down")
        path = request.url.path
        if path == "/Library/Refresh":
            self.refreshed += 1
            return httpx.Response(204)
        if path == "/System/Info":
            return httpx.Response(200, json={"ServerName": "jf", "Version": "12.4"})
        if path == "/v1/machines":
            return httpx.Response(200, json={"machines": [{"name": "agents", "agent_kind": "claude", "state": "ready"}]})
        if path == "/v1/sessions" and request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            sid = f"s_{len(self.created):012x}"
            self.sessions[sid] = {"id": sid, "name": body["name"], "machine": "agents", "folder": body["folder"],
                                  "state": "working", "turns": 0, "reply": ""}
            return httpx.Response(202, json=self.sessions[sid])
        m = re.match(r"^/v1/sessions/(s_[0-9a-f]+)(/screen|/messages)?$", path)
        if m and m.group(1) in self.sessions:
            s = self.sessions[m.group(1)]
            if m.group(2) == "/screen":
                return httpx.Response(200, json={"screen": "agent screen"})
            if m.group(2) == "/messages":
                self.sent.append((s["id"], json.loads(request.content)["text"]))
                s["state"] = "working"
                return httpx.Response(202, json=s)
            if request.method == "DELETE":
                self.closed.append(s["id"])
                s["state"] = "closed"
            return httpx.Response(200, json=s)
        return httpx.Response(404, json={"detail": "no such session"})


@pytest.fixture()
def settings(tmp_path):
    (tmp_path / "libraries" / "movies").mkdir(parents=True)
    (tmp_path / "libraries" / "books").mkdir(parents=True)
    return Settings(data_dir=str(tmp_path / "data"), setup_code="ABCD-EFGH",
                    library_roots=[str(tmp_path / "libraries")], staging_dir=str(tmp_path / "staging"))


@pytest.fixture()
def fake():
    return FakeTelerouter()


@pytest.fixture()
def app(settings, fake, monkeypatch):
    monkeypatch.setattr(Jobs, "start", lambda self: None)      # tests drive tick() themselves
    files = tls.ensure(settings.data_dir, [])
    a = create_app(settings, files)
    a.state.st.jobs.transport = httpx.MockTransport(fake.handler)
    return a


@pytest.fixture()
def c(app):
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def setup_admin(c) -> dict:
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": c.get("/api/session").json()["csrf"]}
