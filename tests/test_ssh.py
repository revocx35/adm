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

    async def models(self, m):
        if m.kind == "hermes":
            return {"models": ["deepseek:deepseek-flash", "copilot:gpt-5.4", "bad model name!"],
                    "providers": ["deepseek", "copilot"], "agent_default": "deepseek:deepseek-flash"}
        return {"models": ["opus", "claude-opus-5-5"], "providers": [], "agent_default": ""}

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


def test_hermes_jobs_run_again(c, tmp_path):
    h, fake, m = setup(c, tmp_path, kind="hermes")
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    assert c.get(f"/api/requests/{rid}").json()["request"]["state"] == "working" and fake.turns[0]["kind"] == "hermes"


def test_default_model_reminders_and_owner_messages(c, tmp_path):
    h, fake, m = setup(c, tmp_path)
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "runner": m["id"]}).json()["id"]
    tick(c)
    assert fake.turns[0]["model"] == "claude-sonnet-5-5" and fake.turns[0]["kind"] == "claude"
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


def test_load_models_and_adm_only_default(c, tmp_path):
    h, fake, m = setup(c, tmp_path, kind="hermes")
    r = c.post(f"/api/machines/{m['id']}/models", headers=h).json()
    assert r["available_models"] == ["deepseek:deepseek-flash", "copilot:gpt-5.4"]       # the odd name is dropped
    assert r["hermes_providers"] == ["deepseek", "copilot"] and r["agent_default"] == "deepseek:deepseek-flash"
    assert r["models_checked"] > 0
    # ADM's default for its jobs; the request form gets favourites + everything available
    assert c.put(f"/api/machines/{m['id']}", json={"default_model": "copilot:gpt-5.4"}, headers=h).status_code == 200
    lst = c.get("/api/machines").json()["machines"][0]
    assert lst["default_model"] == "copilot:gpt-5.4" and len(lst["available_models"]) == 2
    # switching the agent type forgets the other agent's list
    r = c.put(f"/api/machines/{m['id']}", json={"kind": "claude", "default_model": ""}, headers=h).json()
    assert r["available_models"] == [] and r["agent_default"] == ""


def test_hermes_provider_model_split():
    m = Machine(name="x", host="h", username="root", kind="hermes", hermes_provider="deepseek",
                hermes_providers=["deepseek", "copilot"])
    assert m.split_model("copilot:gpt-5.4") == ("copilot", "gpt-5.4")
    assert m.split_model("gpt-5.4") == ("deepseek", "gpt-5.4")              # the machine's provider setting
    assert m.split_model("ollama:qwen3:8b") == ("deepseek", "ollama:qwen3:8b")   # unknown prefix: a model name
    assert m.split_model("") == ("deepseek", "")
    c = Machine(name="x", host="h", username="root", kind="claude", hermes_providers=["copilot"])
    assert c.split_model("copilot:x") == ("", "copilot:x")


def test_claude_model_order():
    names = ["claude-opus-4-20250514", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2",
             "claude-haiku-5-5", "claude-opus-4-8", "claude-sonnet-5-5[1m]"]
    out = sorted(names, key=sshrun.claude_model_key)
    assert out[:3] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5[1m]"]
    assert out.index("claude-opus-4-8") < out.index("claude-opus-4-20250514")


async def test_claude_discovery_parses_and_filters(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    m = Machine(name="x", host="h", username="root", kind="claude")

    async def sh(mm, cmd, input=None, timeout=30):
        return 0, "claude-opus-5-5\nclaude-haiku-3-55\nclaude-opus-4-20250514\nclaude-sonnet-5-5\nADM-DEFAULT sonnet\n", ""
    r.sh = sh
    d = await r.models(m)
    assert d["models"] == ["opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-20250514"]
    assert d["agent_default"] == "sonnet"


async def test_hermes_discovery_parses_the_console_output(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    m = Machine(name="x", host="h", username="root", kind="hermes")
    payload = {"provider": "deepseek", "model": "deepseek-flash",
               "providers": [{"slug": "deepseek", "models": ["deepseek-flash"]}, {"slug": "copilot", "models": ["gpt-5.4"]}]}

    async def sh(mm, cmd, input=None, timeout=30):
        assert "--run-module code" in cmd and input.startswith("exec(")
        return 0, ">>> ADM-MODELS " + json.dumps(payload) + "\n>>> \n", ""
    r.sh = sh
    d = await r.models(m)
    assert d == {"models": ["deepseek:deepseek-flash", "copilot:gpt-5.4"], "providers": ["deepseek", "copilot"],
                 "agent_default": "deepseek:deepseek-flash"}


async def test_hermes_launch_line_uses_provider_and_model(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    m = Machine(name="x", host="h", username="root", kind="hermes", hermes_provider="deepseek",
                hermes_providers=["deepseek", "copilot"])
    written = {}

    class C:
        home = "/root"

        async def get(self, mm):
            return None
    r.conns[m.id] = C()

    async def sh(mm, cmd, input=None, timeout=30):
        if cmd.startswith("umask 077; cat >"):
            written[cmd.split("> ")[1].split(".tmp")[0].strip("'")] = input
            return 0, "", ""
        if "kill -0" in cmd:
            return 0, "", ""                       # no turn of this job running yet
        if "setsid -f" in cmd:
            assert ">/dev/null 2>&1 < /dev/null" in cmd and "&" not in cmd.replace("2>&1", "").replace("&&", "")
            return 0, "4242\n", ""
        if "import yaml" in cmd:
            return 0, "python3\n", ""
        if "HERMES_HOME:-" in cmd:
            return 0, "/root/.hermes", ""
        if ".adm-home.py" in cmd and "rm -f" not in cmd:
            payload = json.loads(input)
            assert payload["src"] == "/root/.hermes" and payload["dst"].endswith("/.adm/hermes-home")
            assert payload["url"] == "http://127.0.0.1:47900/mcp" and payload["token"] == "adj_x"
            return 0, "ok\n", ""
        return 0, "", ""
    r.sh = sh
    rt = await r.start_turn(m, "r_0123456789", {}, "find it", "adj_x", "copilot:gpt-5.4")
    script = written["/root/adm-jobs/r_0123456789/.adm/run-1.sh"]
    assert "--model gpt-5.4 --provider copilot" in script and rt["pid"] == 4242
    rt["session"] = "h-1"                       # reported by Hermes' result event, read by poll()
    await r.start_turn(m, "r_0123456789", rt, "go on", "", "gpt-5.4")
    assert "--model gpt-5.4 --provider deepseek" in written["/root/adm-jobs/r_0123456789/.adm/run-2.sh"]
    assert "--resume h-1" in written["/root/adm-jobs/r_0123456789/.adm/run-2.sh"]


async def test_a_turn_that_is_already_running_is_adopted(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    m = Machine(name="x", host="h", username="root", kind="claude")
    cmds = []

    class C:
        home = "/root"

        async def get(self, mm):
            return None
    r.conns[m.id] = C()

    async def sh(mm, cmd, input=None, timeout=30):
        cmds.append(cmd)
        if "kill -0" in cmd:
            return 0, "777\n", ""                 # the previous start's agent is alive
        return 0, "", ""
    r.sh = sh
    rt = await r.start_turn(m, "r_0123456789", {}, "find it", "adj_x", "")
    assert rt["pid"] == 777 and rt["turn"] == 1
    assert not any("setsid" in c for c in cmds)         # nothing new was started


async def test_launch_lost_answer_reads_the_pid_file(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    m = Machine(name="x", host="h", username="root", kind="claude")

    class C:
        home = "/root"

        async def get(self, mm):
            return None
    r.conns[m.id] = C()

    async def sh(mm, cmd, input=None, timeout=30):
        if "kill -0" in cmd:
            return 0, "", ""
        if "setsid -f" in cmd:
            raise sshrun.SshError("x: a command took over 20 s")
        if cmd.startswith("cat ") and "pid-1" in cmd:
            return 0, "4343\n", ""
        return 0, "", ""
    r.sh = sh
    rt = await r.start_turn(m, "r_0123456789", {}, "find it", "adj_x", "")
    assert rt["pid"] == 4343


def test_hermes_home_builder_isolates_the_job(tmp_path):
    """The script ADM runs on the machine: an allowlist of links, never the gateway lock, a private config."""
    import subprocess, sys, yaml
    src, dst = tmp_path / "hermes", tmp_path / "job" / "hermes-home"
    src.mkdir()
    for f in (".env", "auth.json", "gateway.lock", "gateway.sock", "state.db"):
        (src / f).write_text("x")
    for d in ("skills", "cron", "kanban", "memories", "sessions"):
        (src / d).mkdir()
    (src / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "deepseek-flash", "provider": "deepseek"},
        "mcp_servers": {"telerouter": {"url": "http://127.0.0.1:47801/mcp"}},
        "hooks": {"post_llm_call": [{"command": "/x"}]}, "cron": {"catch_up_missed": True},
        "kanban": {"review_dispatch": True}, "gateway": {"unmanaged_idle_exit_seconds": 600}}))
    out = subprocess.run([sys.executable, "-c", sshrun.HERMES_CONFIG_PY], input=json.dumps(
        {"src": str(src), "dst": str(dst), "url": "http://127.0.0.1:47900/mcp", "token": "adj_T"}),
        capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr
    names = sorted(p.name for p in dst.iterdir())
    assert names == [".env", "auth.json", "config.yaml", "skills"]          # no gateway.*, state, cron, kanban ...
    cfg = yaml.safe_load((dst / "config.yaml").read_text())
    assert cfg["mcp_servers"] == {"adm": {"url": "http://127.0.0.1:47900/mcp",
                                          "headers": {"Authorization": "Bearer adj_T"}, "timeout": 900}}
    assert "hooks" not in cfg and "cron" not in cfg and "kanban" not in cfg
    assert cfg["model"]["provider"] == "deepseek" and cfg["gateway"]["unmanaged_idle_exit_seconds"] == 60
    assert oct((dst / "config.yaml").stat().st_mode & 0o777) == "0o600"


async def test_stop_also_ends_the_job_gateway_and_leftovers(tmp_path):
    r = sshrun.SshRunner(str(tmp_path), 1)
    got = []

    async def sh(m, cmd, input=None, timeout=30):
        got.append(cmd)
        return 0, "", ""
    r.sh = sh
    await r.stop(Machine(name="x", host="h", username="root"), {"pid": 4242, "dir": "/root/adm-jobs/r_0123456789"})
    cmd = got[0]
    assert "kill -TERM -- -4242" in cmd
    assert "grep -qx HERMES_HOME=/root/adm-jobs/r_0123456789/.adm/hermes-home" in cmd    # only this job's gateway
    assert "/root/adm-jobs/r_0123456789/*" in cmd
    got.clear()
    await r.stop(Machine(name="x", host="h", username="root"), {"pid": 4242, "dir": "/etc"})    # never outside a job dir
    assert "HERMES_HOME" not in got[0] and "/etc" not in got[0]
