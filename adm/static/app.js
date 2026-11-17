"use strict";
// ADM web UI: vanilla JS, no build step, no inline script/style (strict CSP), DOM built with el() only.

let S = { csrf: "", version: "" };
let timer = null;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (k === "value") e.value = v;
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-CSRF-Token"] = S.csrf;
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (r.status === 401 && !path.startsWith("/api/login") && path !== "/api/session") { boot(); throw new Error("please log in again"); }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : (Array.isArray(d) ? d.map(x => x.msg).join("; ") : `HTTP ${r.status}`));
  }
  return data;
}

function toast(msg, bad) {
  const t = el("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 2800);
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message, true); throw e; }
}

function ago(ts) {
  if (!ts) return "";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return `${Math.round(d)} s ago`;
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function size(n) { return n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : n >= 1048576 ? `${(n / 1048576).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`; }
const KIND = { movies: ["Movie", "M"], shows: ["TV show", "TV"], books: ["Book", "B"] };

function logo() { return el("img", { class: "brand-logo", src: "/static/icon.svg", alt: "" }); }
function field(label, input, hint, wide) {
  return el("label", { class: "field" + (wide ? " wide" : "") }, el("span", { class: "label" }, label), input, hint ? el("span", { class: "hint" }, hint) : null);
}
function formData(root) {
  const d = {};
  root.querySelectorAll("[name]").forEach(i => {
    if (i.type === "checkbox") d[i.name] = i.checked;
    else if (i.type === "number") d[i.name] = i.value === "" ? null : Number(i.value);
    else d[i.name] = i.value;
  });
  return d;
}

// -- auth ------------------------------------------------------------------------------------------
async function boot() {
  clearInterval(timer);
  const s = await fetch("/api/session", { credentials: "same-origin" }).then(r => r.json());
  S.version = s.version;
  if (s.logged_in) { S.csrf = s.csrf; window.onhashchange = route; shell(); route(); return; }
  authScreen(s);
}

function authScreen(s) {
  const app = document.getElementById("app");
  const msg = el("p", { class: "hint" });
  let form;
  if (s.setup_needed) {
    form = el("form", {},
      el("p", { class: "muted" }, "Create the admin account. The setup code is in the container log (docker logs adm)."),
      field("Setup code", el("input", { name: "setup_code", autocomplete: "off", required: true })),
      field("User name", el("input", { name: "username", value: "admin", autocomplete: "username", required: true })),
      field("Password (12+ characters)", el("input", { name: "password", type: "password", autocomplete: "new-password", required: true })),
      el("button", { class: "primary", type: "submit" }, "Create account"), msg);
    form.onsubmit = async e => { e.preventDefault(); try { await api("POST", "/api/setup", formData(form)); boot(); } catch (x) { msg.textContent = x.message; } };
  } else if (s.totp_needed) {
    form = el("form", {},
      field("Code from your authenticator app", el("input", { name: "code", inputmode: "numeric", autocomplete: "one-time-code", required: true })),
      el("button", { class: "primary", type: "submit" }, "Verify"), msg);
    form.onsubmit = async e => { e.preventDefault(); try { await api("POST", "/api/login/totp", formData(form)); boot(); } catch (x) { msg.textContent = x.message; } };
  } else {
    form = el("form", {},
      field("User name", el("input", { name: "username", autocomplete: "username", required: true })),
      field("Password", el("input", { name: "password", type: "password", autocomplete: "current-password", required: true })),
      el("button", { class: "primary", type: "submit" }, "Sign in"), msg);
    form.onsubmit = async e => { e.preventDefault(); try { await api("POST", "/api/login", formData(form)); boot(); } catch (x) { msg.textContent = x.message; } };
  }
  app.replaceChildren(el("div", { class: "auth" }, el("div", { class: "panel" }, el("div", { class: "brand" }, logo(), "ADM"), form)));
}

// -- shell -----------------------------------------------------------------------------------------
const PAGES = [["requests", "Requests"], ["libraries", "Libraries"], ["settings", "Settings"], ["account", "Account"]];

function shell() {
  const side = el("nav", { class: "side" },
    el("div", { class: "brand" }, logo(), el("div", {}, "ADM", el("small", {}, "agentic download manager"))),
    el("div", { class: "nav" }, PAGES.map(([id, label]) => el("a", { href: `#/${id}`, "data-page": id, onclick: () => side.classList.remove("open") }, label))),
    el("div", { class: "foot" }, el("a", { href: "#", onclick: async e => { e.preventDefault(); await api("POST", "/api/logout"); boot(); } }, "Sign out"), ` · v${S.version}`));
  const top = el("div", { class: "topbar" }, el("button", { onclick: () => side.classList.toggle("open") }, "☰"), logo(), el("b", { id: "pagename" }, "ADM"));
  document.getElementById("app").replaceChildren(top, el("div", { class: "shell" }, side, el("main", { id: "main" })));
}

function route() {
  clearInterval(timer);
  const [, page, arg] = (location.hash || "#/requests").split("/");
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.page === (page === "request" ? "requests" : page)));
  const name = (PAGES.find(p => p[0] === page) || ["", page === "request" ? "Request" : "ADM"])[1];
  const pn = document.getElementById("pagename"); if (pn) pn.textContent = name;
  const main = document.getElementById("main");
  const view = { requests: pageRequests, request: m => pageRequestImpl(m, arg || ""), libraries: pageLibraries, settings: pageSettings, account: pageAccount }[page] || pageRequests;
  view(main).catch(e => main.replaceChildren(el("div", { class: "panel" }, e.message)));
}

// -- requests --------------------------------------------------------------------------------------
async function pageRequests(main) {
  const [data, libs, ms] = await Promise.all([api("GET", "/api/requests"), api("GET", "/api/libraries"), api("GET", "/api/machines")]);
  let kind = "movies";
  const agents = [];
  if (ms.telerouter_ready || ms.default_runner === "telerouter") agents.push({ id: "telerouter", label: "telerouter (tmux session)", models: ms.telerouter_models, def: "" });
  for (const m of ms.machines.filter(x => x.enabled)) agents.push({ id: m.id, label: `${m.name} · ${m.kind === "hermes" ? "Hermes" : "Claude Code"} over SSH`, models: m.models, def: m.default_model });
  const runnerSel = el("select", { name: "runner" }, agents.map(a => el("option", { value: a.id, selected: a.id === ms.default_runner }, a.label)));
  const modelList = el("datalist", { id: "model-list" });
  const modelIn = el("input", { name: "model", list: "model-list", autocomplete: "off", spellcheck: "false" });
  function setRunner() {
    const a = agents.find(x => x.id === runnerSel.value) || { models: [], def: "" };
    const opts = [...new Set([a.def, ...(a.models || [])].filter(Boolean))];
    modelList.replaceChildren(...opts.map(m => el("option", { value: m })));
    modelIn.placeholder = a.def ? `default: ${a.def}` : "agent's default";
  }
  runnerSel.addEventListener("change", setRunner);
  const libSel = el("select", { name: "library_id" });
  const details = el("input", { name: "details" });
  const detailsLabel = el("span", { class: "label" });
  const seg = el("div", { class: "seg" });
  function setKind(k) {
    kind = k;
    seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
    const ls = libs.libraries.filter(l => l.kind === k && l.enabled);
    libSel.replaceChildren(...(ls.length ? ls.map(l => el("option", { value: l.id }, l.name)) : [el("option", { value: "" }, `no ${k} library yet`)]));
    detailsLabel.textContent = { movies: "Details (optional)", shows: "Seasons / episodes", books: "Author" }[k];
    details.placeholder = { movies: "e.g. director's cut, 4K", shows: "e.g. season 2, or S01E01-E05", books: "e.g. Yaşar Kemal" }[k];
  }
  for (const k of ["movies", "shows", "books"]) seg.append(el("button", { type: "button", "data-k": k, onclick: () => setKind(k) }, KIND[k][0]));
  const form = el("form", { class: "form" },
    field("Title", el("input", { name: "title", required: true, placeholder: "What should the agent find?" })),
    field("Year", el("input", { name: "year", inputmode: "numeric", maxlength: "4", placeholder: "optional" })),
    el("label", { class: "field" }, detailsLabel, details),
    field("Library", libSel),
    field("Agent", agents.length ? runnerSel : el("span", { class: "hint" }, "Set up telerouter or add an agent machine under Settings.")),
    field("Model", el("span", { class: "field" }, modelIn, modelList), "Pick a suggestion or type any model name; empty = the default."),
    field("Notes for the agent", el("textarea", { name: "notes", placeholder: "language, quality, edition, sources to prefer…" }), null, true),
    el("div", { class: "row field wide" }, el("button", { class: "primary", type: "submit" }, "Request"), el("span", { class: "hint" }, "An agent session searches, downloads and files it into the library.")));
  form.onsubmit = async e => {
    e.preventDefault();
    const d = formData(form); d.kind = kind;
    const r = await act(() => api("POST", "/api/requests", d), "Requested");
    location.hash = `#/request/${r.id}`;
  };
  setKind(kind);
  setRunner();
  const c = data.counts;
  const list = el("div", { class: "reqs" }, data.requests.length ? data.requests.map(reqRow) : el("p", { class: "muted" }, "No requests yet."));
  main.replaceChildren(el("h1", {}, "Requests"),
    el("div", { class: "stats" },
      [["Queued", c.queued], ["Working", (c.starting || 0) + (c.working || 0)], ["Done", c.done], ["Failed", c.failed]].map(([l, n]) => el("div", { class: "stat" }, el("span", { class: "label" }, l), el("b", {}, n || 0)))),
    el("div", { class: "panel" }, el("div", { class: "row" }, el("h2", {}, "New request"), el("span", { class: "spacer" }), seg), form),
    list);
  timer = setInterval(async () => {
    try { const d = await api("GET", "/api/requests"); list.replaceChildren(...(d.requests.length ? d.requests.map(reqRow) : [el("p", { class: "muted" }, "No requests yet.")])); } catch (_) { /* next tick */ }
  }, 5000);
}

function reqRow(r) {
  const sub = r.state === "done" ? (r.summary || "done") : r.state === "failed" ? (r.error || "failed") : (r.message || r.stage || r.state);
  return el("div", { class: "req", onclick: () => { location.hash = `#/request/${r.id}`; } },
    el("div", { class: "kind " + r.kind }, KIND[r.kind][1]),
    el("div", {},
      el("div", { class: "title" }, r.title, r.year ? el("span", { class: "muted" }, ` (${r.year})`) : null, r.details ? el("span", { class: "muted" }, ` · ${r.details}`) : null),
      el("div", { class: "sub" }, `${r.library} · ${ago(r.created)} · ${sub}`),
      r.progress !== null && r.progress !== undefined && ["starting", "working"].includes(r.state) ? el("div", { class: "bar" }, barFill(r.progress)) : null),
    el("span", { class: "badge " + r.state }, r.state === "working" && r.stage ? r.stage : r.state));
}
function barFill(p) { const i = el("i"); i.style.width = `${Math.max(2, Math.min(100, p))}%`; return i; }

async function pageRequestImpl(main, rid) {
  const d = await api("GET", `/api/requests/${rid}`);
  const r = d.request;
  const active = ["starting", "working"].includes(r.state);
  const head = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("div", {}, el("h1", {}, r.title, r.year ? ` (${r.year})` : ""),
        el("div", { class: "muted" }, `${KIND[r.kind][0]} → ${r.library}`, r.details ? ` · ${r.details}` : "")),
      el("span", { class: "spacer" }), el("span", { class: "badge " + r.state }, r.state)),
    r.message ? el("p", {}, r.stage ? el("span", { class: "label" }, r.stage + " · ") : null, r.message) : null,
    r.progress !== null && r.progress !== undefined && active ? el("div", { class: "bar" }, barFill(r.progress)) : null,
    r.summary ? el("p", {}, el("span", { class: "label" }, "Summary · "), r.summary) : null,
    r.error ? el("p", { class: "muted" }, el("span", { class: "label" }, "Error · "), r.error) : null,
    r.notes ? el("p", { class: "hint" }, "Notes: " + r.notes) : null,
    el("div", { class: "row" },
      active || r.state === "queued" ? el("button", { class: "danger", onclick: async () => { if (confirm("Cancel this request? The agent session is closed.")) { await act(() => api("POST", `/api/requests/${rid}/cancel`), "Cancelled"); route(); } } }, "Cancel") : null,
      ["failed", "cancelled"].includes(r.state) ? el("button", { onclick: async () => { await act(() => api("POST", `/api/requests/${rid}/retry`), "Queued again"); route(); } }, "Retry") : null,
      ["done", "failed", "cancelled"].includes(r.state) ? el("button", { class: "danger", onclick: async () => { if (confirm("Delete this request from the list? Imported files stay in the library.")) { await act(() => api("DELETE", `/api/requests/${rid}`)); location.hash = "#/requests"; } } }, "Delete") : null,
      el("span", { class: "spacer" }),
      el("span", { class: "hint" }, r.agent || "")));

  const evs = el("div", { class: "timeline" });
  let lastEv = 0;
  function addEvents(list) {
    for (const e of list) {
      lastEv = Math.max(lastEv, e.id);
      evs.append(el("div", { class: "ev " + e.kind }, el("span", { class: "t" }, clock(e.at)), el("span", { class: "k" }, e.kind), el("span", { class: "x" }, e.text)));
    }
    evs.scrollTop = evs.scrollHeight;
  }
  addEvents(d.events);

  const imports = el("div", {}, d.imports.length ? el("table", {}, el("tr", {}, el("th", {}, "File in the library"), el("th", {}, "Size")),
    d.imports.map(i => el("tr", {}, el("td", { class: "mono" }, i.path), el("td", {}, size(i.size))))) : el("p", { class: "muted" }, "Nothing imported yet."));

  const term = el("pre", { class: "term" }, "…");
  const msgIn = el("input", { placeholder: "Tell the agent something (e.g. prefer the 1080p release)" });
  const agentPanel = (r.session_id || (r.runner !== "telerouter" && r.started)) ? el("div", { class: "panel" },
    el("div", { class: "row" }, el("h2", {}, "Agent session"), el("span", { class: "spacer" }), el("span", { class: "hint" }, active ? "live, refreshes every 4 s" : "last screen")),
    term,
    active ? el("form", { class: "row", onsubmit: async e => { e.preventDefault(); if (!msgIn.value.trim()) return; const res = await act(() => api("POST", `/api/requests/${rid}/message`, { text: msgIn.value })); toast(res.how === "sent" ? "Sent" : "Queued: the agent gets it when its current turn ends"); msgIn.value = ""; } },
      el("div", { class: "spacer" }, msgIn), el("button", { type: "submit" }, "Send")) : null) : null;

  async function loadScreen() {
    if (!agentPanel) return;
    try { const s = await api("GET", `/api/requests/${rid}/screen`); const atEnd = term.scrollTop + term.clientHeight >= term.scrollHeight - 30; term.textContent = s.screen; if (atEnd) term.scrollTop = term.scrollHeight; }
    catch (e) { term.textContent = e.message; }
  }
  main.replaceChildren(el("a", { class: "back", href: "#/requests" }, "← Requests"), head,
    el("div", { class: "grid2" }, el("div", { class: "panel" }, el("h2", {}, "Timeline"), evs), el("div", { class: "panel" }, el("h2", {}, "Imported"), imports)),
    ...(agentPanel ? [agentPanel] : []));
  loadScreen();
  if (active || r.state === "queued") {
    let tick = 0;
    timer = setInterval(async () => {
      tick++;
      try {
        const n = await api("GET", `/api/requests/${rid}?after=${lastEv}`);
        addEvents(n.events);
        if (n.request.state !== r.state || n.imports.length !== d.imports.length || n.request.message !== r.message) { route(); return; }
        if (tick % 2 === 0) loadScreen();
      } catch (_) { /* next tick */ }
    }, 2000);
  }
}

// -- libraries -------------------------------------------------------------------------------------
async function pageLibraries(main) {
  const data = await api("GET", "/api/libraries");
  const rows = data.libraries.map(l => el("div", { class: "panel" },
    el("div", { class: "row" }, el("h2", {}, l.name), el("span", { class: "badge" }, KIND[l.kind][0]), l.available ? null : el("span", { class: "badge failed" }, "unavailable"),
      el("span", { class: "spacer" }), el("button", { onclick: () => browse(l) }, "Browse"), el("button", { onclick: () => libForm(l, data.roots) }, "Edit")),
    el("div", { class: "mono muted" }, l.path, l.refresh === "jellyfin" ? " · refreshes Jellyfin" : ""),
    l.problem ? el("p", { class: "hint" }, l.problem) : null));
  main.replaceChildren(el("h1", {}, "Libraries"),
    el("p", { class: "muted" }, `Folders the agent files media into. They must be mounted into the container below ${data.roots.join(", ")}. The agent looks at the existing folders and names new media the same way.`),
    ...rows, el("div", { id: "libform" }), el("button", { class: "primary", onclick: () => libForm(null, data.roots) }, "Add library"),
    el("div", { id: "browser" }));
}

function libForm(l, roots) {
  const isNew = !l;
  l = l || { name: "", kind: "movies", path: roots[0] + "/", refresh: "none", enabled: true };
  const kindSel = el("select", { name: "kind" }, ["movies", "shows", "books"].map(k => el("option", { value: k, selected: k === l.kind }, KIND[k][0] + "s")));
  const ref = el("select", { name: "refresh" }, [["none", "nothing"], ["jellyfin", "Jellyfin library scan"]].map(([v, t]) => el("option", { value: v, selected: v === l.refresh }, t)));
  const f = el("div", { class: "panel" }, el("h2", {}, isNew ? "Add library" : `Edit ${l.name}`),
    el("div", { class: "form" },
      field("Name", el("input", { name: "name", value: l.name, placeholder: "e.g. Jellyfin movies" })),
      field("Kind", kindSel),
      field("Folder in the container", folderInput(l.path), "Start typing: matching folders are listed (only below " + roots.join(", ") + ")."),
      field("After an import, start", ref),
      el("label", { class: "check field" }, el("input", { type: "checkbox", name: "enabled", checked: l.enabled }), "Enabled")),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: async () => { const d = formData(f); await act(() => isNew ? api("POST", "/api/libraries", d) : api("PUT", `/api/libraries/${l.id}`, d), "Saved"); route(); } }, "Save"),
      isNew ? null : el("button", { class: "danger", onclick: async () => { if (confirm(`Remove ${l.name}? Files stay on disk.`)) { await act(() => api("DELETE", `/api/libraries/${l.id}`), "Removed"); route(); } } }, "Remove"),
      el("button", { onclick: () => f.remove() }, "Close")));
  document.getElementById("libform").replaceChildren(f);
  f.scrollIntoView({ behavior: "smooth" });
}

// A path field that lists matching folders as you type (an <input> + <datalist>, no inline script).
let folderListSeq = 0;
function folderInput(value) {
  const id = `folders-${++folderListSeq}`;
  const list = el("datalist", { id });
  const input = el("input", { name: "path", value, class: "mono", list: id, autocomplete: "off", spellcheck: "false" });
  let timer = null, asked = "";
  async function load() {
    const typed = input.value;
    if (typed === asked) return;
    asked = typed;
    try {
      const r = await api("GET", `/api/folders?path=${encodeURIComponent(typed)}`);
      if (input.value !== typed) return;              // an older answer: the user typed on
      list.replaceChildren(...r.folders.map(f => el("option", { value: f })));
    } catch (_) { /* suggestions are optional */ }
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 150); });
  input.addEventListener("focus", load);
  return el("span", { class: "field" }, input, list);
}

async function browse(l, path) {
  path = path || "";
  const box = document.getElementById("browser");
  try {
    const t = await api("GET", `/api/libraries/${l.id}/tree?path=${encodeURIComponent(path)}`);
    const up = path ? path.split("/").slice(0, -1).join("/") : null;
    box.replaceChildren(el("div", { class: "panel" },
      el("div", { class: "row" }, el("h2", {}, `${l.name} / ${path || ""}`), el("span", { class: "spacer" }), el("button", { onclick: () => box.replaceChildren() }, "Close")),
      el("div", { class: "tree" },
        up !== null ? el("div", { class: "dir", onclick: () => browse(l, up) }, "../") : null,
        t.entries.map(e => e.name.endsWith("/")
          ? el("div", { class: "dir", onclick: () => browse(l, (path ? path + "/" : "") + e.name.slice(0, -1)) }, e.name)
          : el("div", {}, e.name, el("span", { class: "muted" }, `  ${size(e.size || 0)}`)))),
      t.truncated ? el("p", { class: "hint" }, t.note) : null));
    box.scrollIntoView({ behavior: "smooth" });
  } catch (e) { toast(e.message, true); }
}

// -- settings --------------------------------------------------------------------------------------
async function pageSettings(main) {
  const [s, ms] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/machines")]);
  const tr = s.telerouter, jf = s.jellyfin, ag = s.agent;
  const trBox = el("div", { class: "form" },
    field("telerouter URL", el("input", { name: "url", value: tr.url, placeholder: "https://telerouter.lan:8443" })),
    field("Token (sessions scope)", el("input", { name: "token", type: "password", autocomplete: "off", placeholder: tr.token_set ? "saved - leave empty to keep" : "trm_…" }),
      "telerouter → MCP access → new token with \"start agent sessions\". Tick \"Apps may start agent sessions here\" on the machine."),
    field("Machine", el("input", { name: "machine", value: tr.machine, placeholder: "empty = the only one allowed" })),
    field("Job folders on that machine", el("input", { name: "folder", value: tr.folder })),
    field("telerouter's CA certificate (PEM)", el("textarea", { name: "ca_pem", class: "pem", placeholder: "paste https://<telerouter>/ca.pem - empty for a public certificate" }, tr.ca_pem), null, true));
  const jfBox = el("div", { class: "form" },
    field("Jellyfin URL", el("input", { name: "url", value: jf.url, placeholder: "https://jellyfin.example" })),
    field("API key", el("input", { name: "api_key", type: "password", autocomplete: "off", placeholder: jf.api_key_set ? "saved - leave empty to keep" : "Dashboard → API keys" })),
    field("Jellyfin's CA certificate (PEM)", el("textarea", { name: "ca_pem", class: "pem", placeholder: "only for a private certificate" }, jf.ca_pem), null, true));
  const runnerOpts = [["telerouter", "telerouter (tmux session)"], ...ms.machines.map(m => [m.id, `${m.name} (SSH)`])];
  const agBox = el("div", { class: "form" },
    field("Default agent for new requests", el("select", { name: "default_runner" }, runnerOpts.map(([v, t]) => el("option", { value: v, selected: v === ag.default_runner }, t)))),
    field("Model suggestions for telerouter jobs", el("input", { name: "telerouter_models", value: (ag.telerouter_models || []).join(", "), placeholder: "e.g. claude-sonnet-5-5, claude-opus-5-5" }),
      "Comma-separated. Empty model = the machine's new-session model in telerouter."),
    field("ADM address for agents", el("input", { name: "adm_url", value: ag.adm_url, placeholder: s.suggested_adm_url }),
      "How the agent's machine reaches this ADM (MCP + uploads). Its host name or IP must be in ADM's certificate (ADM_TLS_NAMES)."),
    field("Jobs at the same time", el("input", { name: "max_concurrent", type: "number", min: "1", max: "10", value: ag.max_concurrent })),
    field("Give up after (hours)", el("input", { name: "job_timeout_hours", type: "number", min: "0.25", max: "72", step: "0.25", value: ag.job_timeout_hours })),
    field("Reminders when the agent stops early", el("input", { name: "max_nudges", type: "number", min: "0", max: "5", value: ag.max_nudges })),
    field("Guidance for every job", el("textarea", { name: "guidance", placeholder: "Which sources to use, preferred quality/size, languages, subtitles, formats (e.g. EPUB over PDF)…" }, ag.guidance),
      "Added to every task the agent gets.", true),
    el("label", { class: "check field" }, el("input", { type: "checkbox", name: "keep_sessions", checked: ag.keep_sessions }), "Keep finished jobs' sessions and job folders (debugging)"));
  const out = el("p", { class: "hint" });
  async function save() {
    const agent = formData(agBox);
    agent.telerouter_models = splitList(agent.telerouter_models);
    const body = { telerouter: formData(trBox), jellyfin: formData(jfBox), agent };
    await act(() => api("PUT", "/api/settings", body), "Saved");
  }
  async function test(what) { await save(); const r = await act(() => api("POST", `/api/settings/test/${what}`)); out.textContent = r.message; toast(r.message, !r.ok); }
  main.replaceChildren(el("h1", {}, "Settings"),
    el("div", { class: "panel" }, el("h2", {}, "telerouter (agent sessions)"), trBox, el("div", { class: "row" }, el("button", { onclick: () => test("telerouter") }, "Save & test"))),
    machinesPanel(ms),
    el("div", { class: "panel" }, el("h2", {}, "Agent"), agBox),
    el("div", { class: "panel" }, el("h2", {}, "Jellyfin (library scans)"), jfBox, el("div", { class: "row" }, el("button", { onclick: () => test("jellyfin") }, "Save & test"))),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save"), out),
    el("p", { class: "hint mono" }, `ADM CA SHA-256: ${s.ca_sha256}`));
}

function splitList(v) { return String(v || "").split(/[,\s]+/).map(x => x.trim()).filter(Boolean); }

function machinesPanel(ms) {
  const rows = ms.machines.map(m => el("div", { class: "row machine" },
    el("span", { class: "badge " + (m.connected ? "done" : m.error ? "failed" : "") }, m.connected ? "connected" : m.error ? "error" : "idle"),
    el("b", {}, m.name), el("span", { class: "muted" }, `${m.username}@${m.host}:${m.port} · ${m.kind === "hermes" ? "Hermes" : "Claude Code"}${m.default_model ? " · " + m.default_model : ""}${m.enabled ? "" : " · disabled"}`),
    el("span", { class: "spacer" }),
    el("button", { onclick: async () => { const r = await act(() => api("POST", `/api/machines/${m.id}/test`)); toast(r.message, !r.ok); } }, "Test"),
    el("button", { onclick: () => machineForm(m) }, "Edit"),
    m.error ? el("p", { class: "hint" }, m.error) : null));
  return el("div", { class: "panel" }, el("h2", {}, "Agent machines (ADM connects over SSH itself)"),
    el("p", { class: "muted" }, "Each job runs as a one-off agent process on the machine (no tmux), in its own job folder. The agent reaches ADM through ADM's SSH tunnel, so the machine needs no route or certificate for ADM."),
    el("div", { class: "field wide" }, el("span", { class: "label" }, "ADM's SSH key - add it to ~/.ssh/authorized_keys of the user on each machine"),
      el("pre", { class: "term" }, ms.public_key)),
    ...rows, el("div", { id: "machineform" }),
    el("div", { class: "row" }, el("button", { onclick: () => machineForm(null) }, "Add machine")));
}

function machineForm(m) {
  const isNew = !m;
  m = m || { name: "", host: "", port: 22, username: "root", host_key: "", kind: "claude", default_model: "", models: [], permission_mode: "auto",
    hermes_provider: "", hermes_yolo: true, folder: "~/adm-jobs", tunnel_port: 47900, enabled: true };
  let hostKey = m.host_key;
  const fp = el("span", { class: "hint" }, m.host_key ? `Host key: ${m.host_key_fingerprint}` : "Host key not confirmed yet.");
  const resetKey = () => { if (hostKey) { hostKey = ""; fp.textContent = "Host or port changed: fetch & confirm the host key again."; } };
  const sel = (name, value, opts) => el("select", { name }, opts.map(([v, t]) => el("option", { value: v, selected: v === value }, t)));
  const f = el("div", { class: "panel" }, el("h2", {}, isNew ? "Add agent machine" : `Edit ${m.name}`),
    el("div", { class: "form" },
      field("Name", el("input", { name: "name", value: m.name, placeholder: "e.g. claude LXC" })),
      field("SSH host", el("input", { name: "host", value: m.host, placeholder: "192.168.1.20", oninput: resetKey })),
      field("SSH port", el("input", { name: "port", type: "number", value: m.port, oninput: resetKey })),
      field("SSH user", el("input", { name: "username", value: m.username })),
      field("Agent", sel("kind", m.kind, [["claude", "Claude Code"], ["hermes", "Hermes Agent"]])),
      field("Default model", el("input", { name: "default_model", value: m.default_model, placeholder: "empty = the agent's default" })),
      field("Model suggestions", el("input", { name: "models", value: (m.models || []).join(", "), placeholder: "claude-sonnet-5-5, claude-opus-5-5" }), "Comma-separated; shown in the request form."),
      field("Permission mode (Claude Code)", sel("permission_mode", m.permission_mode, [["auto", "auto"], ["acceptEdits", "acceptEdits"], ["bypassPermissions", "bypassPermissions"], ["dontAsk", "dontAsk"], ["default", "default (asks - nobody answers)"]])),
      field("Provider (Hermes)", el("input", { name: "hermes_provider", value: m.hermes_provider, placeholder: "empty = Hermes' default" })),
      field("Job folders", el("input", { name: "folder", value: m.folder })),
      field("Tunnel port on the machine", el("input", { name: "tunnel_port", type: "number", value: m.tunnel_port }), "A free loopback port there; the agent reaches ADM at http://127.0.0.1:<port>."),
      el("label", { class: "check field" }, el("input", { type: "checkbox", name: "hermes_yolo", checked: m.hermes_yolo }), "Hermes: run tools without asking (--yolo)"),
      el("label", { class: "check field" }, el("input", { type: "checkbox", name: "enabled", checked: m.enabled }), "Enabled")),
    el("div", { class: "row" }, el("button", { onclick: async () => {
      const d = formData(f);
      const r = await act(() => api("POST", "/api/machines/hostkey", { host: d.host, port: d.port }));
      if (confirm(`Trust this SSH host key for ${d.host}?\n\n${r.fingerprint}\n\nCompare it with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub (on that machine)`)) {
        hostKey = r.host_key; fp.textContent = `Host key: ${r.fingerprint} (saved when you press Save)`;
      }
    } }, "Fetch & confirm host key"), fp),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: async () => {
        const d = formData(f);
        d.models = splitList(d.models); d.host_key = hostKey;
        if (!d.host_key) { toast("Confirm the host key first", true); return; }
        await act(() => isNew ? api("POST", "/api/machines", d) : api("PUT", `/api/machines/${m.id}`, d), "Saved");
        route();
      } }, "Save"),
      isNew ? null : el("button", { class: "danger", onclick: async () => { if (confirm(`Remove ${m.name}?`)) { await act(() => api("DELETE", `/api/machines/${m.id}`), "Removed"); route(); } } }, "Remove"),
      el("button", { onclick: () => f.remove() }, "Close")));
  document.getElementById("machineform").replaceChildren(f);
  f.scrollIntoView({ behavior: "smooth" });
}

// -- account ---------------------------------------------------------------------------------------
async function pageAccount(main) {
  const a = await api("GET", "/api/account");
  const pw = el("form", { class: "form" },
    field("Current password", el("input", { name: "current", type: "password", autocomplete: "current-password" })),
    field("New password (12+)", el("input", { name: "new", type: "password", autocomplete: "new-password" })),
    el("div", { class: "row field wide" }, el("button", { type: "submit" }, "Change password")));
  pw.onsubmit = async e => { e.preventDefault(); await act(() => api("POST", "/api/account/password", formData(pw)), "Password changed"); boot(); };
  const twofa = el("div", {});
  if (a.totp_enabled) {
    const f = el("form", { class: "row" }, el("input", { name: "password", type: "password", placeholder: "password to turn it off", autocomplete: "current-password" }), el("button", { class: "danger", type: "submit" }, "Turn off 2FA"));
    f.onsubmit = async e => { e.preventDefault(); await act(() => api("POST", "/api/account/totp/disable", formData(f)), "2FA off"); route(); };
    twofa.append(el("p", {}, "Two-factor sign-in is on."), f);
  } else {
    twofa.append(el("p", { class: "muted" }, "Two-factor sign-in is off."), el("button", { onclick: async () => {
      const r = await act(() => api("POST", "/api/account/totp/setup"));
      const f = el("form", { class: "row" }, el("input", { name: "code", inputmode: "numeric", placeholder: "6-digit code" }), el("button", { class: "primary", type: "submit" }, "Turn on"));
      f.onsubmit = async e => { e.preventDefault(); await act(() => api("POST", "/api/account/totp/enable", formData(f)), "2FA on"); route(); };
      twofa.replaceChildren(r.qr ? el("img", { src: r.qr, alt: "QR code", width: "180", height: "180" }) : null, el("p", { class: "mono" }, r.secret), f);
    } }, "Set up 2FA"));
  }
  main.replaceChildren(el("h1", {}, "Account"), el("div", { class: "panel" }, el("h2", {}, `Signed in as ${a.username}`), pw), el("div", { class: "panel" }, el("h2", {}, "Two-factor sign-in"), twofa));
}

boot();
