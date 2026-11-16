<img src="assets/icon.svg" width="64" height="64" alt="">

# ADM — agentic download manager

Like the *arr stack, but instead of indexers and a torrent client, an **AI agent** does the finding. You request a
movie, a TV show or a book; ADM asks [telerouter](https://github.com/revocx35/telerouter) for a fresh agent session
(Claude Code in tmux on one of your machines), the agent searches the web, downloads a copy, looks at how your
library is organised and files the new media the same way — then Jellyfin (or your book server) picks it up.

```
 you ──request──▶ ADM ──Sessions API──▶ telerouter ──SSH/tmux──▶ Claude Code on an agent machine
                   ▲                                                   │ searches, downloads, checks
                   └──── MCP tools + HTTPS uploads ◀────────────────────┘ list_library → upload → import_media
                   │
                   └──▶ library folders (Jellyfin movies/shows, books) ──▶ Jellyfin library scan
```

- **Requests page**: movie / TV show / book, title, year, seasons or author, notes for the agent. A live timeline
  (searching, downloading, uploading, importing), the agent's terminal as text, and a box to tell it something.
- **The agent never gets your file system.** It reaches ADM through ADM's own MCP server with a token that is valid
  for one request and one library: `list_library` / `find_in_library` to read the folder structure,
  `start_upload` (a one-time upload token and a ready `curl` command), `import_media` (path checked, file type
  checked, never overwrites), `complete_request` / `fail_request`.
- **It copies your naming**: it is told to look at existing entries first (`Title (Year)/Title (Year).mkv`,
  `Show/Season 01/…`, `Author/Title (Year)/Title (Year).epub` — whatever your library already uses).
- **Your rules**: *Settings → Agent → Guidance for every job* is added to every task (which sources are fine,
  quality, languages, EPUB over PDF…). The agent is told to follow it.
- Jobs that run too long, or agents that stop without finishing (after a reminder), fail cleanly; their session is
  closed and their tokens die. Failed requests can be retried.

## Install

```bash
git clone https://github.com/revocx35/adm && cd adm
cp .env.example .env          # set ADM_TLS_NAMES and the library folders
mkdir -p data staging && sudo chown 1000:1000 data staging
docker compose up -d --build
docker logs adm 2>&1 | grep 'setup code'
```

Open `https://<host>:8450`, create the admin with the setup code, then:

1. **An agent** - either telerouter or ADM's own SSH (see *Two ways to run the agent* below). For **telerouter** (0.17 or newer;
   0.18 for a model per request): *Agents & machines → edit the machine → Apps may start agent sessions here*, and
   *MCP access → New token* with **start agent sessions**. In ADM *Settings → telerouter*: its URL, the token and
   telerouter's CA (`https://<telerouter>:8443/ca.pem`). *Save & test* lists the machines.
2. **Agent**: *ADM address for agents* — the `https://` address the agent machine uses to reach ADM. That name or
   IP must be in `ADM_TLS_NAMES` (ADM's self-signed certificate); the agent's Claude Code trusts ADM's CA
   automatically, and uploads pin ADM's key.
3. **Libraries**: add a folder per library (kind movies / shows / books); the folder field lists matching folders as
   you type. Folders must be mounted into the container
   below `/libraries` and writable by uid 1000. Tick *Jellyfin library scan* for Jellyfin libraries and fill in
   *Settings → Jellyfin* (an API key from Jellyfin's dashboard).

The agent machine needs Claude Code logged in, `curl`, and whatever the agent should use to download (it decides;
`ffprobe` helps it check videos). Downloads land in the job folder (`~/adm-jobs/<request>` by default) and are
deleted after the import.

## Two ways to run the agent

| | telerouter | ADM's own SSH ("agent machines") |
|---|---|---|
| Session | a tmux session telerouter starts (watch it on telerouter's tmux page) | a one-off background process per job, no tmux |
| Agents | Claude Code | Claude Code **or Hermes Agent** |
| How the agent reaches ADM | ADM's address (`https://…`, ADM's CA trusted by the agent) | an SSH reverse tunnel: `http://127.0.0.1:<port>` on the agent's machine, no route or certificate needed |
| Model | optional per request (telerouter ≥ 0.18), else the machine's new-session model | per request, else the machine's default model |

**Agent machines** (*Settings → Agent machines → Add machine*): host, SSH user, Claude Code or Hermes, a default model
and model suggestions, permission mode (Claude Code) or provider/`--yolo` (Hermes), job folder and tunnel port. Add
ADM's public SSH key (shown there) to that user's `~/.ssh/authorized_keys`, then *Fetch & confirm host key* (compare the
fingerprint) and *Test*. The agent must be logged in on that machine for that user.

Each job runs in `<job folders>/<request id>/`: Claude Code as `claude -p --output-format stream-json` with only ADM's
MCP server (`--strict-mcp-config`); Hermes as `hermes chat --oneshot --format stream-json` with its own temporary
`HERMES_HOME` (your `~/.hermes` linked in, `config.yaml` copied with only ADM's MCP server and without hooks; your own
Hermes config is never changed). Reminders and your messages continue the same agent session. The job folder is deleted
when the job ends (*Keep finished jobs' sessions and job folders* keeps it, minus the job's secrets).

In the request form pick the **Agent** and type or pick a **Model**; *Settings → Agent → Default agent* sets the default.

## Behind a reverse proxy / single sign-on

ADM can sit behind a reverse proxy with forward auth (e.g. Authentik) for people. Agents must **not** go through it:
give them the LAN address in *Settings → Agent → ADM address for agents*, and on the public name block the agent
endpoints, e.g. in nginx:

```nginx
location /mcp     { return 404; }
location /upload/ { return 404; }
```

Set `ADM_TRUSTED_PROXIES` to the proxy's address so login throttling sees the real client IPs. ADM's own login
stays on as a second factor behind the proxy.

## Configuration (.env)

| Variable | Default | |
|---|---|---|
| `ADM_TLS_NAMES` | — | extra names/IPs for the certificate (the address agents use) |
| `ADM_HTTPS_PORT` | 8450 | |
| `ADM_INTERNAL_PORT` | 8451 | loopback-only HTTP inside the container that ADM's SSH tunnels lead to (MCP + uploads) |
| `ADM_SETUP_CODE` | random, logged | first-run code |
| `ADM_TRUSTED_PROXIES` | — | reverse proxies (CIDRs) whose `X-Forwarded-For` is trusted |
| `ADM_TLS_CERT` / `ADM_TLS_KEY` | — | your own certificate instead of the self-signed one |
| `ADM_LIBRARY_ROOTS` | `/libraries` | library folders must be below one of these |
| `ADM_STAGING_DIR` | `/data/staging` | uploads in progress (compose: `/staging`) |
| `ADM_MAX_UPLOAD_GB` | 200 | per file |

## Development

```bash
docker build -t adm-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/src -w /src adm-dev python -m pytest -q
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and [AGENTS.md](AGENTS.md). MIT licensed.
