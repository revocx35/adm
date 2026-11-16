# AGENTS.md

Notes for AI agents (and humans) working on ADM, the agentic download manager. Read
[ARCHITECTURE.md](ARCHITECTURE.md) before changing the job loop, the MCP tools, uploads or library code.
Environment-specific details (hosts, IPs, mounts) belong in the git-ignored `CLAUDE.local.md` / `AGENTS.local.md`.

## What this is

A FastAPI app (Python 3.13, Docker) that turns media requests (movie / TV show / book) into tasks for a Claude Code
agent. Agent sessions come from telerouter's Sessions API (`/v1`, telerouter ≥ 0.17); the agent calls back through
ADM's MCP server (`/mcp`) and upload endpoint (`/upload/{id}`) and ADM files the media into mounted library folders.

## Layout

```
adm/
  __main__.py   uvicorn HTTPS            settings.py  ADM_* env          config.py  pydantic config, redact/merge_secrets
  store.py      config.json + SQLite     security.py  argon2/TOTP/throttle/sessions (from telerouter)
  tls.py        local CA (from telerouter)  web.py    FastAPI app + routes       jobs.py    job loop + prompt
  mcp.py        agents' MCP tools        library.py   path checks, tree, place   remote.py  telerouter + Jellyfin clients
  static/       index.html, app.js, app.css, icon.svg (vanilla JS)
tests/          pytest: test_auth.py, test_library.py, test_jobs.py (fake telerouter via httpx.MockTransport)
assets/icon.svg brand mark (same as adm/static/icon.svg)
```

The checkout folder may be called `agm` (an early typo of the name); the product, package and env vars are ADM.

## Commands

```bash
docker build -t adm-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/src -w /src adm-dev python -m pytest -q        # ~3 s, 31 tests
docker build -t adm:dev .                                                # the runtime image
docker run -d --name adm-test --network host --user 1000:1000 --read-only --tmpfs /tmp \
  -e ADM_HTTPS_PORT=18450 -e ADM_BIND=127.0.0.1 -e ADM_SETUP_CODE=TEST-CODE1 -v $PWD/scratch/data:/data adm:dev
# UI screenshots: mcr.microsoft.com/playwright/python:v1.55.0-noble (pip install playwright==1.55.0)
```

Remove test containers when done. Host-specific rules (resource limits, hosts, mounts) are in the git-ignored `CLAUDE.local.md`.

## Conventions

- One asyncio loop. SQLite calls are short and run on it (`Db` has a lock); file copies and tree walks go to
  `asyncio.to_thread`.
- Every `/api/` route gets session + CSRF from the `Guard` middleware; public ones are listed in `PUBLIC`. `/mcp` and
  `/upload/` are outside `/api/` and authenticate with their own bearer tokens (and throttle bad ones per IP).
- Tokens: `security.new_token(prefix)`; store only `token_hash`. Prefixes: `adj_` job (MCP), `adu_` upload.
- Config secrets (`token`, `api_key`) are write-only: add new secret field names to `config.SECRET_FIELDS`.
- UI: no inline script/style (CSP), DOM via `el()` only, never `innerHTML` with data. Keep pages usable at 360 px.
- MCP tools: add them in `McpServer._define()`; raise `ToolError` for messages the agent should read. Every tool acts
  on the token's own request and library only.
- Library writes only through `library.place` (path + type checks, no overwrite).
- When behaviour, API, env vars or the prompt change: update README.md, ARCHITECTURE.md, this file and `.env.example`
  in the same commit. Commit as the GitHub noreply identity with the Co-Authored-By trailer.

## Never

- Disable TLS verification on outbound calls (pin a CA instead), follow redirects blindly, or put a token in a URL or log.
- Give the agent a tool that runs commands on the ADM host or reads/writes outside the request's library.
- Let a library path point outside `ADM_LIBRARY_ROOTS` (that would expose `/data`).
- Overwrite an existing library file, or import a type outside the library kind's allow-list.
- Commit hosts, IPs, domains, credentials or mount paths of the owner's setup.
- Remove "Don't phone, message or announce anything" from the prompt: the agent host's own instructions may tell the
  agent to phone the owner when work is done.

## Gotchas

- The Debian base image already has a group called `adm`: the container user is `admapp` (uid 1000).
- A global `sed s/AGM/ADM/` once turned `PRAGMA` into `PRADMA`: grep for collateral when renaming.
- The upload `curl` uses `--pinnedpubkey … -k`: the pin is checked on every connection; `-k` only skips the public-CA
  chain check that a self-signed certificate can't pass. With `ADM_TLS_CERT` the pin is dropped and normal
  verification applies.
- telerouter reports a turn as over (`idle`, `turns` + 1) when Claude Code's turn ends; a finished job is closed by
  `complete_request` inside that turn, so `_check` re-reads the request before nudging.
- `TestClient.portal.call(jobs.tick)` runs a tick on the app's loop; tests patch `Jobs.start` so no background loop races.

## Status (0.1.0, 2026-11-16)

First version (owner: "make an new app named agentic download manager … the arr stack but agentic … connects to
telerouter via an api … the agent accesses the media folder of the selected service (jellyfin for movie/shows and
bookorbit for book) looks at the folder structure and adds the downloaded media with the same format").
31 tests (auth paths, library confinement, the job flow with a fake telerouter, uploads with resume, nudges,
cancel/retry, throttling). Verified end to end with a real Claude Code 2.1.325 (Sonnet 5.5, auto mode) against a
running ADM container: it fetched *Alice's Adventures in Wonderland* (Project Gutenberg), read the book library,
uploaded via the pinned curl command and imported it as `Lewis Carroll/Alice's Adventures in Wonderland (1865)/…epub`
matching the existing `Author/Title (Year)/Title (Year).ext` layout; the job token was refused right after
completion. That run launched Claude Code directly with telerouter's per-session files (`claude -p`), not yet through a
real telerouter tmux session. Not deployed anywhere yet.
