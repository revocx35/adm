#!/usr/bin/env python3
"""Stand-in for `claude -p` / `hermes chat --oneshot` speaking stream-json: it does what a real download agent
does through ADM's MCP tools (get_request, list_library, start_upload + the returned curl, import_media,
complete_request), with a fake file instead of a real download. Its argv goes to fake-argv.json in the job folder."""
import json
import os
import subprocess
import sys
import urllib.request
import uuid

prog = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if "--version" in args:
    print(f"{prog} fake 1.0")
    sys.exit(0)
json.dump({"prog": prog, "argv": args, "hermes_home": os.environ.get("HERMES_HOME", "")}, open("fake-argv.json", "a"))


def opt(name, default=""):
    return args[args.index(name) + 1] if name in args else default


def emit(o):
    print(json.dumps(o), flush=True)


if prog == "claude":
    srv = json.load(open(opt("--mcp-config")))["mcpServers"]["adm"]
    prompt = sys.stdin.read()
    session = opt("--session-id") or opt("--resume")
else:
    import yaml
    srv = yaml.safe_load(open(os.environ["HERMES_HOME"] + "/config.yaml"))["mcp_servers"]["adm"]
    prompt = open(opt("--query-file")).read()
    session = opt("--resume") or "hermes-" + uuid.uuid4().hex[:8]
url, auth = srv["url"], srv["headers"]["Authorization"]
emit({"type": "system", "subtype": "init", "session_id": session, "model": opt("--model")})


def call(name, **a):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}})
    req = urllib.request.Request(url, data=body.encode(), headers={"Authorization": auth, "Content-Type": "application/json"})
    res = json.load(urllib.request.urlopen(req, timeout=60))["result"]
    emit({"type": "tool_use", "name": name, "preview": json.dumps(a)[:80]})
    if res.get("isError"):
        raise SystemExit(f"{name} failed: {res['content'][0]['text']}")
    return res["structuredContent"]


if "STOP EARLY" in prompt:            # a request whose notes say so: end the turn without finishing (reminder test)
    emit({"type": "text", "text": "I stopped early on purpose."})
    emit({"type": "result", "session_id": session, "result": "stopped early", "text": "stopped early", "exit_code": 0})
    sys.exit(0)
r = call("get_request")
call("report_progress", stage="downloading", message="fake download", percent=50)
call("list_library")
data = (r["title"] + " ").encode() * 2000
name = "fake.mkv" if r["kind"] != "books" else "fake.epub"
open(name, "wb").write(data)
up = call("start_upload", filename=name, size_bytes=len(data))
subprocess.run(up["command"], shell=True, check=True, stdout=subprocess.DEVNULL)
ext = name.rsplit(".", 1)[1]
folder = f"{r['title']} ({r['year']})" if r["year"] else r["title"]
call("import_media", upload_id=up["upload_id"], path=f"{folder}/{folder}.{ext}")
call("complete_request", summary=f"fake {prog} imported {folder}")
os.remove(name)
emit({"type": "result", "session_id": session, "result": "done", "text": "done", "exit_code": 0})
