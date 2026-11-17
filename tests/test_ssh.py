"""ADM's own SSH runner: machines API, runner/model on requests, the job flow (turns, reminders, owner messages,
graceful cleanup), the tunnel address in upload commands, and rendering both agents' event streams."""

import json

from adm import sshrun
from adm.config import Machine
from tests.conftest import setup_admin
from tests.test_jobs import configure, mcp, tick

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


class FakeSsh:
    """Stands in for SshRunner: records turns; each poll returns what the test queued."""

    def __init__(self):
        self.turns: list[dict] = []
        self.results: list = []          # per poll: None (still running) or a result dict
        self.stopped = 0
        self.cleaned: list[bool] = []
        self.alive = False
        self.keys = type("K", (), {"public": "ssh-ed25519 AAAA adm"})()

    def status(self, m):
        return {"connected": True, "error": ""}

    def _conn(self, m):
        class C:
            async def close(self):
                pass
        return C()

    async def start_turn(self, m, rid, rt, prompt, token, model):
        rt = dict(rt)
        n = int(rt.get("turn") or 0) + 1
        rt.update(turn=n, pid=1000 + n, dir=f"/home/u/adm-jobs/{rid}", base=f"http://127.0.0.1:{m.tunnel_port}",
                  offset=0, session=rt.get("session") or "sess-1")
        self.turns.append({"n": n, "prompt": prompt, "token": token, "model": model, "kind": m.kind})
        return rt

    async def poll(self, m, rt):
        r = self.results.pop(0) if self.results else None
        rt = dict(rt)
        rt["screen"] = (rt.get("screen") or []) + ["→ list_library"]
        return rt, r

    async def sh(self, m, cmd, input=None, timeout=30):
        return 0, "ALIVE" if self.alive else "", ""

    async def stop(self, m, rt):
        self.stopped += 1

    async def cleanup(self, m, rt, keep):
        self.cleaned.append(keep)

    async def test(self, m):
        return "connected; fake"

    async def close_all(self):
        pass


def add_machine(c, h, **kw):
    body = {"name": "lxc", "host": "10.0.0.5", "username": "root", "host_key": KEY, "kind": "claude",
            "default_model": "claude-sonnet-5-5", "models": ["claude-opus-5-5"], **kw}
    r = c.post("/api/machines", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def setup(c, tmp_path, **kw):
    h = setup_admin(c)
    configure(c, h, tmp_path)
    fake = FakeSsh()
    c.app.state.st.jobs.ssh = fake
    m = add_machine(c, h, **kw)
    return h, fake, m


def test_machines_api_validates_and_needs_csrf(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    assert c.post("/api/machines", json={"name": "x", "host": "h", "username": "root"}).status_code == 403
    bad = [{"host": "a b"}, {"username": "Root!"}, {"host_key": "nope"}, {"folder": "/"}, {"folder": "~/a b"},
           {"default_model": "x;rm -rf"}, {"tunnel_port": 80}, {"kind": "bash"}]
    for b in bad:
        r = c.post("/api/machines", json={"name": "x", "host": "h", "username": "root", **b}, headers=h)
        assert r.status_code == 422, (b, r.text)
    lst = c.get("/api/machines").json()
    assert lst["public_key"].startswith("ssh-ed25519") and lst["machines"][0]["host_key_fingerprint"].startswith("SHA256:")
    # same host + tunnel port twice is refused
    r = c.post("/api/machines", json={"name": "y", "host": "10.0.0.5", "username": "root", "host_key": KEY}, headers=h)
    assert r.status_code == 422
    # default agent must exist
    assert c.put("/api/settings", json={"agent": {"default_runner": "nope"}}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"agent": {"default_runner": m["id"]}}, headers=h).status_code == 200


def test_request_runs_over_ssh_with_the_chosen_model(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    assert c.post("/api/requests", headers=h, json={"kind": "movies", "title": "x", "runner": "zzz"}).status_code == 422
    assert c.post("/api/requests", headers=h, json={"kind": "movies", "title": "x", "model": "a b"}).status_code == 422
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"],
                                                  "model": "claude-opus-5-5"}).json()["id"]
    tick(c)
    t = fake.turns[0]
    assert t["model"] == "claude-opus-5-5" and t["token"].startswith("adj_") and 'movie "Dune"' in t["prompt"]
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "working" and j["runner"] == m["id"] and "runtime" not in j
    # the agent's upload command goes through its SSH tunnel: plain loopback HTTP, no pin
    token = t["token"]
    up = mcp(c, token, "start_upload", filename="Dune.mkv", size_bytes=10)
    assert up["command"].endswith(f"'http://127.0.0.1:47900/upload/{up['upload_id']}'") or \
        up["command"].endswith(f"http://127.0.0.1:47900/upload/{up['upload_id']}")
    assert "--pinnedpubkey" not in up["command"]
    # the screen shows the agent's output
    tick(c)
    assert "list_library" in c.get(f"/api/requests/{rid}/screen").json()["screen"]


def test_default_model_reminders_and_owner_messages(c, tmp_path):
    h, fake, m = setup(c, tmp_path, kind="hermes")
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    assert fake.turns[0]["model"] == "claude-sonnet-5-5" and fake.turns[0]["kind"] == "hermes"
    # the owner writes while the agent works: queued for the next turn
    r = c.post(f"/api/requests/{rid}/message", json={"text": "prefer 1080p"}, headers=h).json()
    assert r["how"].startswith("queued")
    fake.results = [{"text": "searching done", "session": "sess-1", "error": "", "exit": 0}]
    tick(c)
    assert len(fake.turns) == 2 and "prefer 1080p" in fake.turns[1]["prompt"]
    assert c.get(f"/api/requests/{rid}").json()["request"]["nudges"] == 0      # an owner turn is no reminder
    # turn ends without completing: one reminder (max_nudges=1 in configure), then failed
    fake.results = [{"text": "hmm", "session": "sess-1", "error": "", "exit": 0}]
    tick(c)
    assert len(fake.turns) == 3 and "complete_request" in fake.turns[2]["prompt"]
    fake.results = [{"text": "giving up", "session": "sess-1", "error": "", "exit": 0}]
    tick(c)
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "failed" and "giving up" in j["error"]
    assert fake.stopped == 1 and fake.cleaned == [False]


def test_agent_completion_waits_for_the_agent_then_cleans_up(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    token = fake.turns[0]["token"]
    assert mcp(c, token, "fail_request", reason="nothing legal found")["ok"] is True
    assert c.get(f"/api/requests/{rid}").json()["request"]["state"] == "failed"
    assert fake.cleaned == []                       # the agent is still finishing its turn
    fake.alive = True
    tick(c)
    assert fake.cleaned == []
    fake.alive = False                              # its process ended
    tick(c)
    assert fake.cleaned == [False] and fake.stopped == 1
    tick(c)
    assert fake.cleaned == [False]                  # only once


def test_cancel_stops_at_once_and_keep_sessions_keeps_the_folder(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    c.put("/api/settings", json={"agent": {"keep_sessions": True}}, headers=h)
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    c.post(f"/api/requests/{rid}/cancel", headers=h)
    assert fake.stopped == 1 and fake.cleaned == [True]     # .adm (secrets) removed, folder kept
    assert c.delete(f"/api/machines/{m['id']}", headers=h).status_code == 200


def test_agent_crash_fails_the_job(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    fake.results = [{"text": "", "session": "", "error": "the agent exited (status 127): claude: not found",
                     "exit": 127}]
    tick(c)
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "failed" and "not found" in j["error"]


def test_render_both_stream_formats():
    claude = [{"type": "system", "subtype": "init", "session_id": "abc", "model": "claude-sonnet-5-5"},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking for it."},
                                                            {"type": "tool_use", "name": "mcp__adm__list_library",
                                                             "input": {"path": ""}}]}},
              {"type": "result", "subtype": "success", "result": "Imported.", "session_id": "abc"}]
    hermes = [{"type": "text", "text": "Searching"}, {"type": "tool_use", "name": "terminal", "preview": "curl ..."},
              {"type": "result", "session_id": "h1", "text": "Done", "exit_code": 0}]
    lines = [x for ev in claude + hermes for x in sshrun.render(ev)]
    assert "Looking for it." in lines and any("mcp__adm__list_library" in x for x in lines)
    assert any(x.startswith("→ terminal") for x in lines) and "Done" in lines and "Imported." in lines
    assert sshrun.fingerprint(KEY).startswith("SHA256:")


def test_machine_model_validation():
    m = Machine(name="x", host="h", username="root", models=["a", "", "b"], folder="~/jobs/")
    assert m.models == ["a", "b"] and m.folder == "~/jobs"
    assert json.loads(m.model_dump_json())["kind"] == "claude"


def test_a_fast_agent_finishing_during_the_start_is_not_overwritten(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    jobs = c.app.state.st.jobs
    orig = fake.start_turn

    async def fast(mm, rid, rt, prompt, token, model):
        out = await orig(mm, rid, rt, prompt, token, model)
        # the agent's first MCP call already sees the tunnel address, and it ends the job at once
        assert c.app.state.st.db.request(rid)["runner"] == mm.id
        await jobs.finish(rid, "failed", error="nothing found", by_agent=True)
        return out
    fake.start_turn = fast
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "failed"
    assert "cleanup_after" in c.app.state.st.db.runtime(rid)


def test_changing_the_host_needs_a_new_host_key(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    r = c.put(f"/api/machines/{m['id']}", json={"host": "10.0.0.6"}, headers=h)
    assert r.status_code == 422 and "host key again" in r.json()["detail"]
    other = KEY.replace("AAAAIOMq", "AAAAIOMr")
    assert c.put(f"/api/machines/{m['id']}", json={"host": "10.0.0.6", "host_key": other}, headers=h).status_code == 200
    assert c.put(f"/api/machines/{m['id']}", json={"name": "renamed"}, headers=h).status_code == 200   # same host: fine
