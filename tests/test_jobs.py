"""A request end to end with a fake telerouter: session start, the agent's MCP tools, upload (with resume),
import, completion, token revocation, Jellyfin scan; plus nudges, cancel, retry, throttling."""

import hashlib
import json

from tests.conftest import setup_admin


def configure(c, h, tmp_path):
    r = c.put("/api/settings", headers=h, json={
        "telerouter": {"url": "https://tr.lan:8443", "token": "trm_" + "a" * 43},
        "jellyfin": {"url": "https://jf.lan", "api_key": "abcdef0123456789abcd"},
        "agent": {"adm_url": "https://adm.lan:8450", "guidance": "Prefer EPUB.", "max_nudges": 1}})
    assert r.status_code == 200, r.text
    lib = c.post("/api/libraries", headers=h, json={"name": "Movies", "kind": "movies",
                                                    "path": str(tmp_path / "libraries" / "movies"),
                                                    "refresh": "jellyfin"})
    assert lib.status_code == 200, lib.text
    (tmp_path / "libraries" / "movies" / "Fury (2014)").mkdir()
    (tmp_path / "libraries" / "movies" / "Fury (2014)" / "Fury.2014.mkv").write_bytes(b"x")
    return lib.json()


def tick(c):
    c.portal.call(c.app.state.st.jobs.tick)


def mcp(c, token, name, **args):
    r = c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res.get("structuredContent") if not res["isError"] else {"error": res["content"][0]["text"]}


def new_job(c, h, fake):
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune", "year": "2021"}).json()["id"]
    tick(c)
    body = fake.created[-1]
    return rid, body["mcp_servers"][0]["token"], body


def test_full_request(c, fake, tmp_path):
    h = setup_admin(c)
    configure(c, h, tmp_path)
    rid, token, body = new_job(c, h, fake)
    # the session request: job folder, prompt, an MCP server with a job token and ADM's CA, nothing secret on disk
    assert body["name"] == "adm-" + rid[2:] and body["folder"] == f"~/adm-jobs/{rid}"
    assert 'movie "Dune" (2021)' in body["prompt"] and "Prefer EPUB." in body["prompt"]
    assert "Don't phone" in body["prompt"]
    srv = body["mcp_servers"][0]
    assert srv["name"] == "adm" and srv["url"] == "https://adm.lan:8450/mcp" and token.startswith("adj_")
    assert "BEGIN CERTIFICATE" in srv["tls_ca"]
    assert fake.auth[-1] == "Bearer trm_" + "a" * 43
    j = c.app.state.st.db.request(rid)
    assert j["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in c.get(f"/api/requests/{rid}").text

    # MCP protocol basics
    r = c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert {t["name"] for t in r["result"]["tools"]} >= {"get_request", "list_library", "start_upload", "import_media",
                                                         "complete_request", "fail_request"}
    info = mcp(c, token, "get_request")
    assert info["title"] == "Dune" and info["library"]["kind"] == "movies"
    assert mcp(c, token, "report_progress", stage="downloading", message="1080p", percent=40) == {"ok": True}
    assert c.get(f"/api/requests/{rid}").json()["request"]["state"] == "working"
    tree = mcp(c, token, "list_library", depth=2)
    assert tree["entries"][0]["name"] == "Fury (2014)/"
    assert mcp(c, token, "find_in_library", query="fury")["matches"] == ["Fury (2014)/", "Fury (2014)/Fury.2014.mkv"]
    assert "error" in mcp(c, token, "list_library", path="../..")
    assert "error" in mcp(c, token, "complete_request", summary="x")            # nothing imported yet

    # upload in two parts (resume), then import
    data = b"0123456789" * 1000
    up = mcp(c, token, "start_upload", filename="Dune.2021.1080p.mkv", size_bytes=len(data))
    assert "--pinnedpubkey" in up["command"] and "adu_" in up["command"]
    assert "error" in mcp(c, token, "start_upload", filename="evil.sh", size_bytes=5)
    utok = up["command"].split("Bearer ")[1].split("'")[0]
    assert utok.startswith("adu_") and utok != token
    url = f"/upload/{up['upload_id']}"
    auth = {"Authorization": f"Bearer {utok}"}
    assert c.put(url, content=data[:4000], headers={"Authorization": f"Bearer {token}"}).status_code == 401
    r = c.put(url, content=data[:4000], headers=auth)
    assert r.json() == {"upload_id": up["upload_id"], "received_bytes": 4000, "size": 10000, "state": "open"}
    assert "error" in mcp(c, token, "import_media", upload_id=up["upload_id"], path="Dune (2021)/Dune (2021).mkv")
    r = c.put(url, content=data[5000:], headers={**auth, "Content-Range": "bytes 5000-9999/10000"})
    assert r.status_code == 416 and r.json()["received_bytes"] == 4000
    r = c.put(url, content=data[4000:], headers={**auth, "Content-Range": "bytes 4000-9999/10000"})
    assert r.json()["state"] == "complete"
    assert c.put(url, content=b"x", headers=auth).status_code == 409
    assert "error" in mcp(c, token, "import_media", upload_id=up["upload_id"], path="../escape.mkv")
    assert "error" in mcp(c, token, "import_media", upload_id=up["upload_id"], path="Fury (2014)/Fury.2014.mkv")
    out = mcp(c, token, "import_media", upload_id=up["upload_id"], path="Dune (2021)/Dune (2021).mkv")
    assert out["imported"] == "Dune (2021)/Dune (2021).mkv"
    assert (tmp_path / "libraries" / "movies" / "Dune (2021)" / "Dune (2021).mkv").read_bytes() == data

    # finish: job done, session closed, token dead, Jellyfin asked to scan
    assert mcp(c, token, "complete_request", summary="Dune 1080p WEB-DL")["ok"] is True
    d = c.get(f"/api/requests/{rid}").json()
    assert d["request"]["state"] == "done" and d["imports"][0]["path"] == "Dune (2021)/Dune (2021).mkv"
    assert fake.closed == ["s_000000000001"]
    r = c.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    st = c.app.state.st
    st.jobs._refresh_due = 1
    tick(c)
    assert fake.refreshed == 1


def test_agent_that_stops_early_is_nudged_then_failed(c, fake, tmp_path):
    h = setup_admin(c)
    configure(c, h, tmp_path)
    rid, token, _ = new_job(c, h, fake)
    s = fake.sessions["s_000000000001"]
    s.update(state="idle", turns=1, reply="I could not decide")
    tick(c)
    assert fake.sent and rid in fake.sent[0][1] and "complete_request" in fake.sent[0][1]
    assert c.get(f"/api/requests/{rid}").json()["request"]["state"] == "working"
    s.update(state="idle", turns=2, reply="still nothing")
    tick(c)
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "failed" and "still nothing" in j["error"]
    assert fake.closed == ["s_000000000001"]
    # retry queues it again with a fresh session and token
    assert c.post(f"/api/requests/{rid}/retry", headers=h).json()["state"] == "queued"
    tick(c)
    assert len(fake.created) == 2 and fake.created[1]["mcp_servers"][0]["token"] != token


def test_cancel_closes_the_session_and_revokes_the_token(c, fake, tmp_path):
    h = setup_admin(c)
    configure(c, h, tmp_path)
    rid, token, _ = new_job(c, h, fake)
    assert c.post(f"/api/requests/{rid}/cancel", headers=h).json()["state"] == "cancelled"
    assert fake.closed == ["s_000000000001"]
    assert c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_telerouter_down_keeps_the_request_queued(c, fake, tmp_path):
    h = setup_admin(c)
    configure(c, h, tmp_path)
    fake.down = True
    rid = c.post("/api/requests", headers=h, json={"kind": "movies", "title": "Dune"}).json()["id"]
    tick(c)
    j = c.get(f"/api/requests/{rid}").json()["request"]
    assert j["state"] == "queued" and "unreachable" in j["message"]
    assert c.app.state.st.db.request(rid)["token_hash"] == ""


def test_unconfigured_request_waits(c, fake, tmp_path):
    h = setup_admin(c)
    c.post("/api/libraries", headers=h, json={"name": "Books", "kind": "books", "path": str(tmp_path / "libraries" / "books")})
    rid = c.post("/api/requests", headers=h, json={"kind": "books", "title": "Ince Memed"}).json()["id"]
    tick(c)
    assert "waiting" in c.get(f"/api/requests/{rid}").json()["request"]["message"] and not fake.created


def test_library_outside_the_roots_is_refused(c, tmp_path):
    h = setup_admin(c)
    r = c.post("/api/libraries", headers=h, json={"name": "x", "kind": "movies", "path": str(tmp_path / "data")})
    assert r.status_code == 422 and "outside" in r.json()["detail"]


def test_bad_agent_tokens_are_throttled(c):
    codes = [c.post("/mcp", headers={"Authorization": f"Bearer adj_bad{i}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code for i in range(9)]
    assert codes[0] == 401 and 429 in codes
    codes = [c.put(f"/upload/u_{i:016x}", content=b"x", headers={"Authorization": "Bearer adu_x"}).status_code
             for i in range(9)]
    assert codes[0] == 401 and 429 in codes
    assert c.post("/mcp", headers={"Origin": "https://evil.example"}, content=json.dumps({})).status_code == 403
