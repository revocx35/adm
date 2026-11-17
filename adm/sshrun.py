"""Agents ADM runs itself over SSH: one detached process per job turn, no tmux.

Per machine: one asyncssh connection (key in /data/ssh, the machine's host key pinned) that also holds a reverse
tunnel `127.0.0.1:<tunnel_port>` on the machine -> ADM's loopback-only HTTP listener (MCP + uploads). The agent
therefore reaches ADM at `http://127.0.0.1:<tunnel_port>` on its own host: no route to ADM, no certificate trust,
encrypted inside SSH.

Per job: `<folder>/<request id>/` is the agent's working folder; `.adm/` in it (0700) holds the prompt of each turn,
the MCP config (job token, 0600), the launch script, the agent's JSON event stream (`out-N.jsonl`), stderr and its
exit code. A turn runs as `setsid nohup sh .adm/run-N.sh`, so it survives ADM restarts and lost connections; ADM
reads the stream from a stored offset. A follow-up turn (a reminder, the owner's message) resumes the agent's own
session (`claude --resume <id>`, `hermes chat --resume <id>`).

Claude Code: `claude -p --output-format stream-json --verbose --strict-mcp-config --mcp-config ... --settings ...`
(only ADM's MCP server, its tools pre-allowed). Hermes keeps MCP servers in its global config.yaml, so each job gets
its own HERMES_HOME: links to everything in ~/.hermes (credentials, sessions, skills) except config.yaml, which is a
copy with `mcp_servers` replaced by ADM's server and `hooks` dropped - the owner's own Hermes is never edited.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import base64
import json
import logging
import re
import shlex
import time
import uuid
from pathlib import Path

import asyncssh

from .config import Machine

log = logging.getLogger("adm.ssh")

KEEPALIVE_S = 20
KEEPALIVE_MAX = 9                 # a Proxmox backup in suspend mode can freeze a container for over a minute
SCREEN_LINES = 400
PATHS = 'PATH="$HOME/.local/bin:$HOME/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"; export PATH'

# Runs on the machine (stdin: JSON {src, dst, url, token}): the job's own Hermes home.
# Hermes 0.22's `hermes chat` is a client of a gateway: a home whose gateway.lock is a symlink is served by the gateway
# that lock names - linking all of ~/.hermes handed ADM's jobs to the user's running gateway, with the user's config
# (no ADM tools, the user's MCP servers; 2026-11-17). So the home links only an allowlist (credentials, the install, its
# runtime, skills, the model catalogue) and gets its own config: the client then starts a separate, short-lived gateway
# for this home (`gateway run --idle-exit`), with only ADM's MCP server, and its own sessions/cron/memories in the home.
# Verified on Hermes 0.22.4: a run in such a home is served by a new gateway with HERMES_HOME = the job home and sees
# no MCP server of the user's; the gateway exits once idle.
HERMES_CONFIG_PY = r'''import json, os, sys
import yaml
a = json.load(sys.stdin)
src, dst = a["src"], a["dst"]
os.makedirs(dst, mode=0o700, exist_ok=True)
LINK = [".env", "auth.json", "auth.lock", "hermes-agent", "tools", "skills", "models_dev_cache.json",
        "models_dev_cache.etag", "SOUL.md"]
for name in LINK:
    s, d = os.path.join(src, name), os.path.join(dst, name)
    if os.path.lexists(s) and not os.path.lexists(d):
        os.symlink(s, d)
if os.path.lexists(os.path.join(dst, "gateway.lock")):
    raise SystemExit("the job home must not contain a gateway lock")
cfg = {}
if os.path.exists(os.path.join(src, "config.yaml")):
    with open(os.path.join(src, "config.yaml")) as f:
        cfg = yaml.safe_load(f) or {}
for k in ("hooks", "kanban", "cron", "mcp_servers"):
    cfg.pop(k, None)
cfg["mcp_servers"] = {"adm": {"url": a["url"], "headers": {"Authorization": "Bearer " + a["token"]}, "timeout": 900}}
gw = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
gw["unmanaged_idle_exit_seconds"] = 60
cfg["gateway"] = gw
fd = os.open(os.path.join(dst, "config.yaml"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print("ok")
'''

# Finds a Python that has PyYAML: Hermes' own interpreter (its launcher chain ends in `exec <python3> ...`), else python3.
FIND_PY = r'''f=$(command -v hermes) || exit 3
py=""
for i in 1 2 3 4; do
  [ -f "$f" ] || break
  c=$(grep -o '^exec [^ ]*' "$f" | head -1 | cut -d' ' -f2)
  case "$c" in *python*) py=$c; break;; "") break;; *) f=$c;; esac
done
for p in "$py" python3; do [ -n "$p" ] && "$p" -c "import yaml" 2>/dev/null && { echo "$p"; exit 0; }; done
exit 4'''


# Claude Code: the model names the installed CLI knows (it is one binary or one JS bundle), and the user's own default.
CLAUDE_MODELS_SH = r'''b=$(command -v claude) || exit 9
b=$(readlink -f "$b")
grep -aoE 'claude-(opus|sonnet|haiku|fable)-[0-9]+(-[0-9]+)?(\[1m\])?' "$b" | sort -u
echo "ADM-DEFAULT $(sed -n 's/^[[:space:]]*"model"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$HOME/.claude/settings.json" 2>/dev/null | head -1)"'''

# Hermes: its own picker code (providers the user has credentials for, their models, the current default), run in
# Hermes' runtime through `hermes --run-module code` (the interactive console reading this one line from stdin).
HERMES_MODELS_PY = r'''
import json, io, contextlib
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from hermes_cli.config import load_config
    from hermes_cli.model_switch import list_authenticated_providers
    cfg = load_config() or {}
    m = cfg.get("model") or {}
    m = m if isinstance(m, dict) else {"default": str(m)}
    provs = list_authenticated_providers(current_provider=m.get("provider") or "", current_base_url=m.get("base_url") or "",
                                         user_providers=cfg.get("providers"), custom_providers=cfg.get("custom_providers"),
                                         max_models=500, current_model=m.get("default") or "", non_blocking_catalogs=True,
                                         probe_custom_providers=False)
print("ADM-MODELS " + json.dumps({"provider": m.get("provider") or "", "model": m.get("default") or "",
      "providers": [{"slug": p.get("slug"), "current": bool(p.get("is_current")),
                     "models": [x if isinstance(x, str) else (x.get("id") or x.get("name") or "") for x in (p.get("models") or [])]}
                    for p in provs]}))
'''


def claude_model_key(name: str):
    """Newest first: fable > opus > sonnet > haiku within a version; dated ids and 1M variants after their base."""
    mt = re.match(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(\[1m\])?$", name)
    if not mt:
        return (1, 0, 0, 9, name)
    fam, major, minor, big = mt.group(1), int(mt.group(2)), int(mt.group(3) or 0), bool(mt.group(4))
    dated = minor > 99
    order = {"fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}[fam]
    return (0, -major, (0 if dated else -minor), order * 2 + big + (10 if dated else 0), name)


class SshError(Exception):
    pass


def q(s: str) -> str:
    return shlex.quote(s)


def fingerprint(public_key: str) -> str:
    try:
        blob = base64.b64decode(public_key.split()[1])
    except (IndexError, ValueError):
        return "?"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def short(e: BaseException) -> str:
    return (str(e) or e.__class__.__name__).splitlines()[0][:200]


class Keys:
    """ADM's own SSH key (ed25519, /data/ssh/id_ed25519, 0600); the public half goes into authorized_keys."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "ssh" / "id_ed25519"
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = asyncssh.generate_private_key("ssh-ed25519", comment="adm")
            key.write_private_key(str(self.path))
            self.path.chmod(0o600)
        self.key = asyncssh.read_private_key(str(self.path))

    @property
    def public(self) -> str:
        return self.key.export_public_key().decode().strip()


async def fetch_host_key(host: str, port: int) -> str:
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port), 15)
    if key is None:
        raise SshError("the server sent no host key")
    return key.export_public_key().decode().strip()


class Conn:
    """One machine's connection + reverse tunnel, re-made on demand."""

    def __init__(self, keys: Keys, internal_port: int):
        self.keys = keys
        self.internal_port = internal_port
        self.conn: asyncssh.SSHClientConnection | None = None
        self.listener = None
        self.sig: tuple = ()
        self.home = ""
        self.lock = asyncio.Lock()
        self.error = ""

    async def get(self, m: Machine) -> asyncssh.SSHClientConnection:
        sig = (m.host, m.port, m.username, m.host_key, m.tunnel_port)
        async with self.lock:
            if self.conn is not None and (self.sig != sig or self.conn.is_closed()):
                await self.close()
            if self.conn is None:
                if not m.host_key:
                    raise SshError(f"{m.name}: confirm its host key first (Settings -> Agent machines)")
                pattern = m.host if m.port == 22 else f"[{m.host}]:{m.port}"
                known = asyncssh.import_known_hosts(f"{pattern} {m.host_key}\n")
                try:
                    conn = await asyncio.wait_for(asyncssh.connect(
                        m.host, m.port, username=m.username, client_keys=[self.keys.key], known_hosts=known,
                        agent_path=None, keepalive_interval=KEEPALIVE_S, keepalive_count_max=KEEPALIVE_MAX), 20)
                except asyncssh.HostKeyNotVerifiable as e:
                    self.error = ("its host key does not match the one ADM confirmed - Edit the machine, "
                                  "Fetch & confirm host key again (compare the fingerprint), Save")
                    raise SshError(f"{m.name}: {self.error}") from e
                except asyncssh.PermissionDenied as e:
                    self.error = "ADM's SSH key is not in authorized_keys"
                    raise SshError(f"{m.name}: {self.error}") from e
                except (OSError, asyncssh.Error, TimeoutError) as e:
                    self.error = short(e)
                    raise SshError(f"{m.name}: {self.error}") from e
                try:
                    self.listener = await conn.forward_remote_port("127.0.0.1", m.tunnel_port, "127.0.0.1",
                                                                   self.internal_port)
                except (OSError, asyncssh.Error) as e:
                    conn.close()
                    self.error = f"port {m.tunnel_port} on {m.name} is taken ({short(e)}); pick another tunnel port"
                    raise SshError(self.error) from e
                res = await conn.run('printf %s "$HOME"', check=False)
                self.home = str(res.stdout or "").strip()
                self.conn, self.sig, self.error = conn, sig, ""
                log.info("connected to %s (%s@%s), tunnel 127.0.0.1:%d", m.name, m.username, m.host, m.tunnel_port)
            return self.conn

    async def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.wait_closed(), 5)
        self.conn, self.listener = None, None


class SshRunner:
    def __init__(self, data_dir: str, internal_port: int):
        self.keys = Keys(data_dir)
        self.internal_port = internal_port
        self.conns: dict[str, Conn] = {}

    def _conn(self, m: Machine) -> Conn:
        return self.conns.setdefault(m.id, Conn(self.keys, self.internal_port))

    async def sh(self, m: Machine, cmd: str, input: str | None = None, timeout: float = 30) -> tuple[int, str, str]:
        conn = await self._conn(m).get(m)
        try:
            res = await asyncio.wait_for(conn.run(cmd, input=input, check=False), timeout)
        except TimeoutError as e:
            # a slow command is no broken connection: keep it (and the tunnel the agents use)
            raise SshError(f"{m.name}: a command took over {timeout:.0f} s") from e
        except (OSError, asyncssh.Error) as e:
            await self._conn(m).close()
            raise SshError(f"{m.name}: {short(e)}") from e
        return int(res.exit_status or 0), str(res.stdout or ""), str(res.stderr or "")

    async def close_all(self) -> None:
        for c in self.conns.values():
            await c.close()

    def status(self, m: Machine) -> dict:
        c = self.conns.get(m.id)
        return {"connected": bool(c and c.conn and not c.conn.is_closed()), "error": c.error if c else ""}

    # -- checks -----------------------------------------------------------------------------------
    async def test(self, m: Machine) -> str:
        binary = "claude" if m.kind == "claude" else "hermes"
        code, out, err = await self.sh(m, f"{PATHS}; command -v {binary} >/dev/null || exit 9; "
                                          f"{binary} --version 2>&1 | head -1; command -v curl >/dev/null || echo NO_CURL")
        if code == 9:
            raise SshError(f"connected, but `{binary}` is not installed for {m.username} on {m.name}")
        version = out.strip().splitlines()[0] if out.strip() else "?"
        if "NO_CURL" in out:
            raise SshError(f"connected ({version}), but curl is missing (the agent uploads with it)")
        if m.kind == "hermes":
            code, _o, _e = await self.sh(m, f"{PATHS}; {FIND_PY}")
            if code:
                raise SshError(f"connected ({version}), but no Python with PyYAML was found to write the job's "
                               "Hermes config")
        return f"connected; {version}; tunnel 127.0.0.1:{m.tunnel_port} -> ADM is up"

    async def models(self, m: Machine) -> dict:
        """What the agent on the machine offers: {"models": [...], "providers": [...], "agent_default": "..."}.
        Hermes models are "provider:model" (a Hermes model only works with its provider)."""
        if m.kind == "claude":
            code, out, err = await self.sh(m, f"{PATHS}; {CLAUDE_MODELS_SH}", timeout=60)
            if code == 9:
                raise SshError(f"`claude` is not installed for {m.username} on {m.name}")
            names, default = set(), ""
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("ADM-DEFAULT"):
                    default = line[len("ADM-DEFAULT"):].strip()
                elif re.match(r"^claude-[a-z]+-[0-9][0-9\-\[\]a-z]*$", line):
                    mt = re.match(r"^claude-[a-z]+-\d+-(\d+)", line)
                    if mt and 10 <= int(mt.group(1)) < 10_000_000:
                        continue                 # a fragment of a longer name inside the binary (e.g. "-3-55")
                    names.add(line)
            models = ["opus", "sonnet", "haiku"] + sorted(names, key=claude_model_key)
            return {"models": models, "providers": [], "agent_default": default}
        line = "exec(" + repr(HERMES_MODELS_PY) + ")\n"
        code, out, err = await self.sh(m, f"{PATHS}; cd /tmp && timeout 150 hermes --run-module code 2>/dev/null",
                                       input=line, timeout=180)
        hit = re.search(r"ADM-MODELS (\{.*\})", out)
        if not hit:
            raise SshError(f"could not read Hermes' model list on {m.name}" + (f": {err.strip()[-200:]}" if err.strip() else ""))
        d = json.loads(hit.group(1))
        models, providers = [], []
        for p in d.get("providers") or []:
            slug = str(p.get("slug") or "")
            if not slug:
                continue
            providers.append(slug)
            models += [f"{slug}:{x}" for x in p.get("models") or [] if x]
        cur = f"{d['provider']}:{d['model']}" if d.get("provider") and d.get("model") else str(d.get("model") or "")
        return {"models": models, "providers": providers, "agent_default": cur}

    # -- jobs -------------------------------------------------------------------------------------
    async def job_dir(self, m: Machine, rid: str) -> str:
        c = self._conn(m)
        await c.get(m)
        base = m.folder
        if base.startswith("~"):
            base = c.home.rstrip("/") + base[1:]
        d = f"{base.rstrip('/')}/{rid}"
        if not re.match(r"^/[A-Za-z0-9_./-]+/r_[0-9a-f]{10}$", d):
            raise SshError(f"unusable job folder {d!r}")
        return d

    async def _write(self, m: Machine, path: str, content: str, mode: int = 0o600) -> None:
        code, _o, err = await self.sh(m, f"umask 077; cat > {q(path)}.tmp && chmod {mode:o} {q(path)}.tmp && "
                                         f"mv -f {q(path)}.tmp {q(path)}", input=content)
        if code:
            raise SshError(f"could not write {path} on {m.name}: {err.strip()[:200]}")

    async def start_turn(self, m: Machine, rid: str, rt: dict, prompt: str, token: str, model: str) -> dict:
        """Start turn rt['turn'] + 1 of a job; returns the updated runtime (dir, turn, pid, offset, session)."""
        rt = dict(rt)
        if not rt.get("dir"):
            rt["dir"] = await self.job_dir(m, rid)
        d, n = rt["dir"], int(rt.get("turn") or 0) + 1
        a = f"{d}/.adm"
        code, _o, err = await self.sh(m, f"umask 077; mkdir -p {q(d)} && mkdir -p {q(a)} && chmod 700 {q(a)}")
        if code:
            raise SshError(f"could not create {d} on {m.name}: {err.strip()[:200]}")
        base = f"http://127.0.0.1:{m.tunnel_port}"
        rt["base"] = base
        # a turn of this job may already be running (a start whose answer got lost): adopt it, never start another
        _c, live, _e = await self.sh(m, f"p=$(cat {q(a)}/pid-{n} 2>/dev/null) && kill -0 \"$p\" 2>/dev/null && echo \"$p\"")
        if live.strip().isdigit():
            log.warning("job %s: turn %d is already running on %s (pid %s) - adopting it", rid, n, m.name, live.strip())
            rt.update(turn=n, pid=int(live.strip()), started=time.time())
            rt.setdefault("offset", 0)
            rt.setdefault("screen", [])
            return rt
        await self._write(m, f"{a}/prompt-{n}.txt", prompt)
        if m.kind == "claude":
            if n == 1:
                rt["session"] = str(uuid.uuid4())
                await self._write(m, f"{a}/mcp.json", json.dumps({"mcpServers": {"adm": {
                    "type": "http", "url": f"{base}/mcp", "headers": {"Authorization": f"Bearer {token}"}}}}))
                await self._write(m, f"{a}/settings.json", json.dumps({"permissions": {"allow": ["mcp__adm"]}}))
            args = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
                    "--mcp-config", ".adm/mcp.json", "--settings", ".adm/settings.json"]
            if model:
                args += ["--model", model]
            if m.permission_mode != "default":
                args += ["--permission-mode", m.permission_mode]
            args += (["--session-id", rt["session"]] if n == 1 else ["--resume", rt["session"]])
            env = ""
        else:
            home = f"{a}/hermes-home"
            if n == 1:
                await self._hermes_home(m, home, f"{base}/mcp", token)
            args = ["hermes", "chat", "--query-file", f".adm/prompt-{n}.txt", "--oneshot", "--format", "stream-json"]
            provider, hmodel = m.split_model(model)
            if hmodel:
                args += ["--model", hmodel]
            if provider:
                args += ["--provider", provider]
            if m.hermes_yolo:
                args += ["--yolo"]
            if n > 1 and rt.get("session"):
                args += ["--resume", rt["session"]]
            env = f"HERMES_HOME={q(home)}; export HERMES_HOME; "
        stdin = f" < .adm/prompt-{n}.txt" if m.kind == "claude" else " < /dev/null"
        script = (f"#!/bin/sh\n{PATHS}\ncd {q(d)} || exit 1\necho $$ > .adm/pid-{n}\n{env}\n"
                  f"{' '.join(q(x) for x in args)}{stdin} > .adm/out-{n}.jsonl 2> .adm/err-{n}.log\n"
                  f"echo $? > .adm/exit-{n}\n")
        await self._write(m, f"{a}/run-{n}.sh", script, 0o700)
        # Fully detached: `setsid -f` forks, the child starts a new session (its pid = the process group) and every
        # descriptor is redirected, so nothing holds the SSH channel open. (`cmd &` + `echo $!` left a subshell holding
        # the channel under bash: the call timed out, the job was re-queued and started again - 3 copies on 2026-11-17.)
        # The script writes its own pid; if this call times out anyway, the pid file decides - never start twice.
        launch = (f"cd {q(d)} && rm -f .adm/pid-{n} && setsid -f sh .adm/run-{n}.sh >/dev/null 2>&1 < /dev/null; "
                  f"for i in $(seq 1 50); do [ -s .adm/pid-{n} ] && break; sleep 0.1; done; cat .adm/pid-{n} 2>/dev/null")
        try:
            code, out, err = await self.sh(m, launch, timeout=20)
        except SshError as e:
            code, out, err = 1, "", str(e)
        pid = out.strip()
        if not pid.isdigit():
            await asyncio.sleep(2)
            _c, out2, _e = await self.sh(m, f"cat {q(d)}/.adm/pid-{n} 2>/dev/null")
            pid = out2.strip()
        if not pid.isdigit():
            raise SshError(f"could not start the agent on {m.name}: {err.strip()[:200] or 'no pid'}")
        rt.update(turn=n, pid=int(pid), offset=0, started=time.time())
        rt.setdefault("screen", [])
        rt["screen"] = (rt["screen"] + [f"── turn {n} ({'Claude Code' if m.kind == 'claude' else 'Hermes'}"
                                        f"{', ' + model if model else ''}) ──"])[-SCREEN_LINES:]
        log.info("job %s: turn %d started on %s (pid %s)", rid, n, m.name, pid)
        return rt

    async def _hermes_home(self, m: Machine, home: str, url: str, token: str) -> None:
        code, py, _e = await self.sh(m, f"{PATHS}; {FIND_PY}")
        if code or not py.strip():
            raise SshError(f"{m.name}: no Python with PyYAML for the job's Hermes config")
        _c, src, _e = await self.sh(m, 'printf %s "${HERMES_HOME:-$HOME/.hermes}"')
        src = src.strip()
        if not src.startswith("/"):
            raise SshError(f"{m.name}: can't find the Hermes home")
        payload = json.dumps({"src": src, "dst": home, "url": url, "token": token})
        tmp = f"{home}.adm-home.py"
        await self._write(m, tmp, HERMES_CONFIG_PY)
        code, out, err = await self.sh(m, f"{q(py.strip())} {q(tmp)}", input=payload)
        await self.sh(m, f"rm -f {q(tmp)}")
        if code or "ok" not in out:
            raise SshError(f"could not prepare the job's Hermes home on {m.name}: {err.strip()[-200:]}")

    async def poll(self, m: Machine, rt: dict) -> tuple[dict, dict | None]:
        """Read new output. Returns (runtime, turn_result or None while the turn runs).
        turn_result = {"text", "session", "error", "exit"}."""
        rt = dict(rt)
        d, n, off = rt["dir"], rt["turn"], int(rt.get("offset") or 0)
        cmd = (f"cd {q(d)} 2>/dev/null || {{ echo GONE; exit 0; }}; "
               f"if kill -0 {int(rt['pid'])} 2>/dev/null; then echo ALIVE; else echo DEAD; fi; "
               f"cat .adm/exit-{n} 2>/dev/null || echo -; "
               f"S=$(wc -c < .adm/out-{n}.jsonl 2>/dev/null || echo 0); echo $S; "
               f"tail -c +{off + 1} .adm/out-{n}.jsonl 2>/dev/null | head -c 2000000")
        _code, out, _err = await self.sh(m, cmd, timeout=40)
        head, _, rest = out.partition("\n")
        if head.strip() == "GONE":
            return rt, {"text": "", "session": rt.get("session", ""), "error": "the job folder is gone", "exit": -1}
        exitline, _, rest = rest.partition("\n")
        _size, _, data = rest.partition("\n")
        complete = data.rsplit("\n", 1)[0] + "\n" if "\n" in data else ""     # whole lines only
        rt["offset"] = off + len(complete.encode())
        result = None
        lines = list(rt.get("screen") or [])
        for line in complete.splitlines():
            ev = _json(line)
            if ev is None:
                continue
            lines.extend(render(ev))
            if ev.get("type") == "result":
                result = {"text": str(ev.get("result") or ev.get("text") or ""),
                          "session": str(ev.get("session_id") or rt.get("session") or ""),
                          "error": str(ev.get("error") or ("agent error" if ev.get("is_error") else "")),
                          "exit": int(ev.get("exit_code") or 0)}
            elif ev.get("type") == "system" and ev.get("session_id") and not rt.get("session"):
                rt["session"] = str(ev["session_id"])
        rt["screen"] = lines[-SCREEN_LINES:]
        if result and result["session"]:
            rt["session"] = result["session"]
        if result is None and head.strip() == "DEAD":
            # exited without a result event: crashed, killed, not logged in, bad flag ...
            _c, err, _e = await self.sh(m, f"tail -c 600 {q(d)}/.adm/err-{n}.log 2>/dev/null")
            result = {"text": "", "session": rt.get("session", ""), "exit": _int(exitline),
                      "error": f"the agent exited (status {exitline.strip() or '?'})"
                               + (f": {err.strip().splitlines()[-1][:300]}" if err.strip() else "")}
        return rt, result

    async def stop(self, m: Machine, rt: dict) -> None:
        """Stop the job's turn - and, for Hermes, the job's own gateway (killing the client only detaches: "accepted
        work continues at the gateway") - and anything still working in the job folder."""
        pid = int(rt.get("pid") or 0)
        d = rt.get("dir") or ""
        cmds = []
        if pid > 1:
            cmds.append(f"kill -TERM -- -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null")
        if re.match(r"^/[A-Za-z0-9_./-]+/r_[0-9a-f]{10}$", d):
            home = f"{d}/.adm/hermes-home"
            # the job gateway: its environment names the job's HERMES_HOME (never the user's gateway: other home)
            cmds.append(f"for p in $(pgrep -f 'gateway run' 2>/dev/null); do "
                        f"tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null | grep -qx {q('HERMES_HOME=' + home)} && "
                        f"kill -TERM $p; done")
            # helper shells the agent left working in the job folder (they outlive a killed agent)
            cmds.append(f"for p in $(ls /proc | grep -E '^[0-9]+$'); do c=$(readlink /proc/$p/cwd 2>/dev/null) || continue; "
                        f"case \"$c\" in {q(d)}|{q(d)}/*) [ $p != $$ ] && kill -TERM $p 2>/dev/null;; esac; done")
        if cmds:
            await self.sh(m, "; ".join(cmds) + "; true", timeout=60)

    async def cleanup(self, m: Machine, rt: dict, keep: bool) -> None:
        """Always remove .adm (job token, configs); the whole job folder too unless keep."""
        d = rt.get("dir") or ""
        if not re.match(r"^/[A-Za-z0-9_./-]+/r_[0-9a-f]{10}$", d):
            return
        target = f"{d}/.adm" if keep else d
        await self.sh(m, f"rm -rf -- {q(target)}", timeout=120)


def _json(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def _int(s: str) -> int:
    try:
        return int(s.strip())
    except ValueError:
        return -1


def _clip(s: str, n: int = 300) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def render(ev: dict) -> list[str]:
    """One stream-json event (Claude Code or Hermes) as readable lines for the request page."""
    t = ev.get("type")
    if t == "assistant":                                    # Claude Code
        out = []
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and str(c.get("text", "")).strip():
                out += [x for x in str(c["text"]).strip().splitlines() if x.strip()][-20:]
            elif c.get("type") == "tool_use":
                out.append(f"→ {c.get('name')} {_clip(json.dumps(c.get('input') or {}, ensure_ascii=False), 200)}")
        return out
    if t == "user":                                         # Claude Code tool results
        out = []
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                out.append(f"  ✗ {_clip(c.get('content'), 200)}")
        return out
    if t == "text":                                         # Hermes text delta
        txt = str(ev.get("text") or "").strip()
        return [_clip(txt, 400)] if txt else []
    if t == "tool_use":                                     # Hermes
        return [f"→ {ev.get('name')} {_clip(ev.get('preview') or ev.get('args') or '', 200)}"]
    if t == "tool_result" and ev.get("is_error"):
        return [f"  ✗ {ev.get('name')} failed"]
    if t == "result":
        txt = ev.get("result") or ev.get("text") or ""
        err = ev.get("error") or ("error" if ev.get("is_error") else "")
        return [f"■ turn finished{' with an error: ' + _clip(err, 200) if err else ''}"] + \
            ([_clip(txt, 600)] if txt else [])
    if t == "system" and ev.get("subtype") == "init":
        return [f"· session {ev.get('session_id') or ''} model {ev.get('model') or ''}".strip()]
    return []
