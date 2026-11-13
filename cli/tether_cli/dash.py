"""`ue dash`: a local status page for every Tether project on this machine: its editors, the lease and the queue
for it, the `ue` commands running now, recent activity and background jobs. The user can reorder the queue, drop a
request, hand the editor to a request or end the lease from it."""
import glob
import json
import re
import os
import secrets
import subprocess
import sys
import threading
import time

from .core import CliError, UE_SCRIPT, editor_beacons, editor_process, find_project, read_json, state_dir, write_json
from .share import (
    active_lease, current_lease, drop_request, end_lease, hand_over, lease_requests, move_request, pie_owner,
    read_activity, running_commands, session_label, session_names,
)
from .jobs import job_line, jobs_dir, kill_tree
from .setup import tether_version

DASH_PORT = 8765
DASH_IDLE = 30 * 60   # the server exits after this long with no page open
ACTIVITY_MINUTES = 30  # the activity feed's window
WAITING = re.compile(r"\bwait\b.*\blease\b")  # `ue wait lease`, `ue --timeout 3000 wait lease`


# ---------------------------------------------------------------- which projects

def registry_path():
    if os.environ.get("TETHER_REGISTRY"):  # tests
        return os.environ["TETHER_REGISTRY"]
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    base = base or os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "Tether", "projects.json")


def known_projects():
    """The .uproject paths `ue` has run on (the ones still there), most recently used first."""
    rows = read_json(registry_path()) or {}
    rows = rows if isinstance(rows, dict) else {}
    return [p for p, _ in sorted(rows.items(), key=lambda kv: -kv[1]) if os.path.isfile(p)]


def register_project(project, now=None):
    """Note that `ue` ran on this project (for `ue dash`). At most one write a minute per project."""
    now = time.time() if now is None else now
    path, project = registry_path(), os.path.abspath(project)
    try:
        rows = read_json(path) or {}
        rows = rows if isinstance(rows, dict) else {}
        same = [k for k in rows if os.path.normcase(k) == os.path.normcase(project)]
        if same and now - rows[same[0]] < 60:
            return
        for k in same + [k for k in rows if not os.path.isfile(k)]:  # (and the projects since deleted)
            rows.pop(k, None)
        rows[project] = round(now)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, rows)
    except (OSError, ValueError):
        pass  # never fail a command over the dashboard's list


def merge_projects(extra):
    """`extra` then the known projects, each once (paths compare without case on Windows), the missing left out."""
    seen, out = set(), []
    for p in list(extra) + known_projects():
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and os.path.isfile(p):
            seen.add(key)
            out.append(os.path.abspath(p))
    return out


def projects_in(paths):
    """.uproject files named by `paths`: a .uproject, a project folder, or a folder of project folders."""
    found = []
    for p in paths:
        p = os.path.abspath(p)
        if p.lower().endswith(".uproject") and os.path.isfile(p):
            found.append(p)
        elif os.path.isdir(p):
            found += glob.glob(os.path.join(p, "*.uproject")) or glob.glob(os.path.join(p, "*", "*.uproject"))
        else:
            raise CliError(f"no project at {p}")
    return found


# ---------------------------------------------------------------- what is going on

def editor_rows(project, now, running=()):
    rows = []
    working = any(not WAITING.search(r.get("cmd") or "") for r in running)
    for b in editor_beacons(project):
        alive = editor_process(b.get("pid"))
        age = now - b.get("heartbeat", 0)
        # a long `ue py` stage holds the game thread: busy, not hung (as `ue status` says)
        state = ("not running" if not alive else ("busy" if working else "no heartbeat") if age > 10
                 else "pie" if b.get("pie") else "ready")
        rows.append({"pid": b.get("pid"), "state": state, "alive": bool(alive), "heartbeat_age": round(age, 1),
                     "map": b.get("map"), "headless": bool(b.get("headless")), "dirty": b.get("dirty") or 0,
                     "engine": (b.get("engine") or "").split("+")[0], "by_tether": b.get("launched_by_tether"),
                     "owner": b.get("owner"), "pie_owner": pie_owner(project, b) if alive else None,
                     "port": b.get("mcp_port")})
    return rows


def lease_row(project, lease, now, label):
    if not lease:
        return None
    stack, outer = [], current_lease(lease.get("outer"), now)
    while outer:
        stack.append({"what": outer.get("what"), "until": outer.get("until")})
        outer = current_lease(outer.get("outer"), now)
    busy = lease.get("busy")
    return {"session": lease["session"], "label": label(lease["session"]), "what": lease.get("what"),
            "since": lease.get("since"), "until": lease.get("until"), "hold": bool(lease.get("hold")),
            "granted_by": lease.get("granted_by"), "busy": busy and {"cmd": busy.get("cmd"), "since": busy.get("since")},
            "outer": stack}


def job_rows(project, now, keep=5):
    folder = jobs_dir(project)
    ids = sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json")) if os.path.isdir(folder) else []
    rows = []
    for job_id in ids[-keep * 4:]:
        line = job_line(folder, job_id)
        meta = read_json(os.path.join(folder, f"{job_id}.json")) or {}
        running = "exit" not in meta and "  running  " in line
        if running or now - (meta.get("ended") or meta.get("started") or 0) < 3600:
            rows.append({"id": job_id, "name": meta.get("name"), "running": running, "line": line})
    return rows[-keep:]


def project_snapshot(project, now=None):
    now = time.time() if now is None else now
    names = session_names(project)

    def label(sid):
        return session_label(project, sid, names) if sid else "?"
    lease = active_lease(project, now)
    activity = [r for r in read_activity(project) if r["time"] >= now - ACTIVITY_MINUTES * 60]
    running = running_commands(project)
    waits = {r.get("session"): r for r in running if WAITING.search(r.get("cmd") or "")}
    editors = editor_rows(project, now, running)
    return {
        "project": project,
        "name": os.path.splitext(os.path.basename(project))[0],
        "editors": editors,
        "lease": lease_row(project, lease, now, label),
        "queue": [{"session": r["session"], "label": label(r["session"]), "what": r.get("what"),
                   "minutes": r.get("minutes"), "time": r.get("time"),
                   "waiting": r["session"] in waits} for r in lease_requests(project, now)],
        # a queued session's `ue wait lease` shows on its queue row, not here
        "running": [{"session": r.get("session"), "label": label(r.get("actor") or r.get("session")),
                     "cmd": r.get("cmd"), "since": r.get("since"), "pid": r.get("pid")} for r in running
                    if not (WAITING.search(r.get("cmd") or "") and any(q["session"] == r.get("session")
                                                                       for q in lease_requests(project, now)))],
        "activity": [{"label": label(r.get("actor") or r["session"]), "cmd": r.get("cmd"), "time": r["time"]}
                     for r in reversed(activity[-60:])],
        "jobs": job_rows(project, now),
    }


def snapshot(projects, now=None):
    now = time.time() if now is None else now
    rows = []
    for p in projects:
        try:
            rows.append(project_snapshot(p, now))
        except Exception as exc:  # one project's broken state file must not blank the page
            rows.append({"project": p, "name": os.path.splitext(os.path.basename(p))[0], "error": str(exc)})

    def busy(r):
        return bool(r.get("lease") or r.get("queue") or r.get("running") or any(e["alive"] for e in r.get("editors", [])))
    rows.sort(key=lambda r: (not busy(r), -(r.get("activity") or [{"time": 0}])[0]["time"]))
    return {"time": now, "projects": rows}


def describe_snapshot(snap):
    """The page as text, for `ue dash --once`."""
    now, out = snap["time"], []
    span = lambda t: f"{(now - t) / 60:.0f} min" if now - t >= 90 else f"{now - t:.0f}s"
    for r in snap["projects"]:
        if r.get("error"):
            out.append(f"{r['name']}: error reading its state: {r['error']}")
            continue
        live = [e for e in r["editors"] if e["alive"]]
        quiet = not (live or r["lease"] or r["queue"] or r["running"] or r["activity"])
        eds = ", ".join(f"pid {e['pid']} {e['state']}" + (f" ({e['pie_owner']})" if e["pie_owner"] else "")
                        + (f" {e['map']}" if e["map"] else "") + (f", {e['dirty']} unsaved" if e["dirty"] else "")
                        for e in live) or "no editor"
        out.append(f"{r['name']}: {eds}" + ("  (quiet)" if quiet else ""))
        if quiet:
            continue
        lease = r["lease"]
        if lease:
            left = (f"busy: `{lease['busy']['cmd']}` for {span(lease['busy']['since'])}" if lease["busy"]
                    else f"{max(1, round((lease['until'] - now) / 60))} min left")
            out.append(f"  lease: {lease['label']}: {lease['what']} ({left}, since {span(lease['since'])} ago)"
                       + "".join(f"; inside its lease for {o['what']}" for o in lease["outer"]))
        else:
            out.append("  lease: free")
        for n, q in enumerate(r["queue"], 1):
            out.append(f"  queue {n}. {q['label']}: {q['what']} (waiting {span(q['time'])}"
                       + (", in `ue wait lease`)" if q.get("waiting") else ")"))
        for c in r["running"]:
            out.append(f"  running: {c['label']} `{c['cmd']}` for {span(c['since'])}")
        for j in r["jobs"]:
            if j["running"]:
                out.append(f"  job: {j['line']}")
        for a in r["activity"][:5]:
            out.append(f"  {span(a['time'])} ago  {a['label']}: {a['cmd'][:100]}")
    return out


# ---------------------------------------------------------------- the server

def dash_url(port):
    return f"http://127.0.0.1:{port}/"


def dash_running(port):
    """The running dashboard's /api/ping answer on this port, or None."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) and data.get("tether_dash") else None
    except (OSError, ValueError):
        return None


def queue_action(projects, body):
    """Apply one page action. body: {project, action, session?}. Returns a short line for the page."""
    project = next((p for p in projects() if os.path.normcase(p) == os.path.normcase(body.get("project") or "")), None)
    if not project:
        raise CliError("not a project this dashboard shows")
    action, sid = body.get("action"), body.get("session")
    if action in ("up", "down", "top", "bottom"):
        move_request(project, sid, action)
        return f"moved {sid}'s request {action}"
    if action == "drop":
        if not any(r["session"] == sid for r in lease_requests(project)):
            raise CliError(f"no request from {sid}")
        drop_request(project, sid)
        return f"dropped {sid}'s request (that session is not told; its `ue wait lease` keeps waiting)"
    if action == "grant":
        lease = hand_over(project, sid)
        return f"the editor is {lease['session']}'s now; the previous holder is told on its next command"
    if action == "end":
        held = end_lease(project)
        return f"ended {held['session']}'s lease" if held else "no lease was held"
    raise CliError(f"unknown action {action!r}")


def serve(port, extra, idle=DASH_IDLE):
    """Run the dashboard until no page has asked for anything in `idle` seconds."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token, last = secrets.token_urlsafe(18), [time.time()]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def projects():
        return merge_projects(extra)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") not in hosts:  # a DNS-rebinding page names its own host
                return self.send(403, {"error": "bad host"})
            last[0] = time.time()
            if self.path == "/api/ping":
                return self.send(200, {"tether_dash": True, "version": tether_version(), "pid": os.getpid()})
            if self.path == "/api/state":
                return self.send(200, snapshot(projects()))
            if self.path in ("/", "/index.html"):
                return self.send(200, PAGE.replace("__TOKEN__", token).encode("utf-8"), "text/html")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            # Only the page itself knows the token, and a custom header can't be sent cross-origin without a
            # CORS preflight, which this server never answers.
            if self.headers.get("Host") not in hosts or self.headers.get("X-Tether-Dash") != token:
                return self.send(403, {"error": "forbidden"})
            last[0] = time.time()
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path != "/api/queue":
                    return self.send(404, {"error": "not found"})
                self.send(200, {"ok": True, "message": queue_action(projects, body)})
            except CliError as exc:
                self.send(400, {"ok": False, "error": str(exc)})
            except ValueError:
                self.send(400, {"ok": False, "error": "bad request"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def watch():
        while time.time() - last[0] < idle:
            time.sleep(15)
        server.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    server.serve_forever()


def cmd_dash(ctx, args):
    """`ue dash [PROJECTS...] [--once] [--listen PORT] [--no-open] [--stop]`."""
    extra = projects_in(args.projects) if args.projects else []
    try:
        extra.append(find_project(getattr(args, "project", None)))
    except CliError:
        pass
    for p in extra:
        register_project(p)
    if args.serve:
        serve(args.listen, extra)
        return 0
    if args.once:
        snap = snapshot(merge_projects(extra))
        ctx.data = snap
        for line in describe_snapshot(snap) or ["no Tether projects yet: `ue dash <folder>` adds the ones in it"]:
            print(line)
        return 0
    running = dash_running(args.listen)
    if args.stop:
        if not running:
            print("no dashboard running on port", args.listen)
            return 0
        kill_tree(running["pid"])
        print(f"stopped the dashboard (pid {running['pid']})")
        return 0
    if not running:
        cmd = [sys.executable, UE_SCRIPT, "dash", "--serve", "--listen", str(args.listen)] + extra
        detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                   | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True})
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, cwd=os.path.dirname(UE_SCRIPT), **detach)
        deadline = time.time() + 10
        while not running and time.time() < deadline:
            time.sleep(0.2)
            running = dash_running(args.listen)
        if not running:
            raise CliError(f"the dashboard did not start on port {args.listen} (another program on it? --listen N)")
    url = dash_url(args.listen)
    print(f"dashboard: {url} (pid {running['pid']}; it stops {DASH_IDLE // 60} min after the last page closes; "
          "`ue dash --stop`)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tether dashboard</title>
<style>
:root {
  --bg: #f6f6f4; --card: #ffffff; --ink: #1c1d1f; --muted: #6b6f76; --line: #e3e3df;
  --accent: #2f6fde; --ok: #1f8a4c; --warn: #b7791f; --bad: #c0392b; --chip: #eef0f3; --hl: #fff7e0;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #141518; --card: #1d1f23; --ink: #e8e9eb; --muted: #9096a0; --line: #2e3137;
          --accent: #6b9bff; --ok: #4cc27f; --warn: #e0a640; --bad: #ff6b5e; --chip: #2a2d33; --hl: #3a3420; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: baseline; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--bg); z-index: 2; }
header h1 { font-size: 16px; margin: 0; }
header .meta { color: var(--muted); font-size: 12px; }
header .spacer { flex: 1; }
label.toggle { color: var(--muted); font-size: 12px; user-select: none; }
main { padding: 16px 20px 40px; display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; min-width: 0; }
.card.quiet { padding: 10px 16px; opacity: .75; }
.card h2 { font-size: 15px; margin: 0 0 2px; display: flex; gap: 8px; align-items: center; }
.path { color: var(--muted); font-size: 11px; word-break: break-all; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 4px; }
.chip { background: var(--chip); border-radius: 999px; padding: 2px 9px; font-size: 12px; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; vertical-align: 1px; background: var(--muted); }
.dot.ready { background: var(--ok); } .dot.pie { background: var(--accent); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); }
h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 14px 0 6px; }
.lease { border-left: 3px solid var(--accent); padding: 6px 10px; background: var(--chip); border-radius: 0 6px 6px 0; }
.lease.free { border-left-color: var(--ok); color: var(--muted); }
.lease .who { font-weight: 600; }
.sub { color: var(--muted); font-size: 12px; }
ol.queue { list-style: none; margin: 0; padding: 0; }
ol.queue li { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-top: 1px solid var(--line); }
ol.queue li:first-child { border-top: 0; }
.n { width: 20px; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.grow { flex: 1; min-width: 0; }
.grow .what { overflow-wrap: anywhere; }
.btns { display: flex; gap: 4px; flex-shrink: 0; }
button { font: inherit; font-size: 12px; border: 1px solid var(--line); background: var(--card); color: var(--ink); border-radius: 6px; padding: 3px 8px; cursor: pointer; }
button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
button:disabled { opacity: .35; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.primary:hover { color: #fff; filter: brightness(1.1); }
button.danger:hover { border-color: var(--bad); color: var(--bad); }
table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }
td { padding: 3px 6px 3px 0; vertical-align: top; border-top: 1px solid var(--line); }
td.t { width: 64px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
td.s { width: 30%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
code, td.c { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.running li { margin: 3px 0; }
ul.plain { margin: 0; padding-left: 18px; }
.flash { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); background: var(--ink); color: var(--bg); padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: calc(100% - 32px); }
.flash.on { opacity: 1; }
.err { color: var(--bad); }
.more { color: var(--accent); cursor: pointer; font-size: 12px; margin-top: 4px; display: inline-block; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; grid-column: 1 / -1; }
</style>
</head>
<body>
<header>
  <h1>Tether</h1><span class="meta" id="meta">loading…</span><span class="spacer"></span>
  <label class="toggle"><input type="checkbox" id="showQuiet"> show quiet projects</label>
</header>
<main id="root"></main>
<div class="flash" id="flash"></div>
<script>
const TOKEN = "__TOKEN__";
let state = null, drawn = null, expanded = {}, busy = false;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
try { $("#showQuiet").checked = localStorage.getItem("tetherQuiet") === "1"; } catch (e) {}
$("#showQuiet").onchange = e => { try { localStorage.setItem("tetherQuiet", e.target.checked ? "1" : "0"); } catch (x) {} render(); };

function span(t) { const s = Math.max(0, state.time - t); return s < 90 ? Math.round(s) + "s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h"; }
function clock(t) { return new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
function flash(text, bad) { const f = $("#flash"); f.textContent = text; f.className = "flash on" + (bad ? " err" : ""); clearTimeout(flash.t); flash.t = setTimeout(() => f.className = "flash", 3500); }

// Buttons carry data-a (action), data-p (project index) and data-q (queue index) into the last state drawn.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-a]");
  if (!b || b.disabled || !drawn) return;
  const p = drawn.projects[+b.dataset.p], q = b.dataset.q != null ? p.queue[+b.dataset.q] : null, a = b.dataset.a;
  if (a === "more") { expanded[p.project] = !expanded[p.project]; return render(); }
  const L = p.lease;
  const ask = a === "end" ? `End ${L.label}'s lease now? It is told on its next ue command; a command of its still running is not stopped.`
            : a === "grant" ? `Give the editor to ${q.label} now?` + (L && L.session !== q.session ? ` ${L.label} loses its lease (told on its next command).` : "")
            : a === "drop" ? `Drop ${q.label}'s request? (That session is not told.)` : null;
  act(p.project, a, q ? q.session : null, ask);
});

async function act(project, action, session, ask) {
  if (ask && !confirm(ask)) return;
  busy = true;
  try {
    const r = await fetch("/api/queue", {method: "POST", headers: {"Content-Type": "application/json", "X-Tether-Dash": TOKEN},
                                         body: JSON.stringify({project, action, session})});
    const d = await r.json();
    flash(d.ok ? d.message : (d.error || "failed"), !d.ok);
  } catch (e) { flash("the dashboard server is gone (run ue dash again)", true); }
  busy = false; poll();
}

function editorChip(e) {
  const cls = !e.alive ? "bad" : e.state === "ready" ? "ready" : e.state === "pie" || e.state === "busy" ? "pie" : "warn";
  let s = `<span class="chip"><span class="dot ${cls}"></span>pid ${e.pid} · ${esc(e.state)}`;
  if (e.pie_owner) s += ` (${esc(e.pie_owner)})`;
  if (e.headless) s += " · headless";
  if (e.map) s += ` · ${esc(e.map.split("/").pop())}`;
  if (e.dirty) s += ` · ${e.dirty} unsaved`;
  if (e.alive && e.heartbeat_age > 10) s += ` ${Math.round(e.heartbeat_age)}s`;
  return s + "</span>";
}

function card(p, pi) {
  if (p.error) return `<section class="card"><h2>${esc(p.name)}</h2><div class="err">${esc(p.error)}</div></section>`;
  const live = p.editors.filter(e => e.alive);
  const quiet = !live.length && !p.lease && !p.queue.length && !p.running.length && !p.activity.length;
  let h = `<section class="card${quiet ? " quiet" : ""}"><h2>${esc(p.name)}</h2><div class="path">${esc(p.project)}</div>`;
  h += `<div class="chips">${live.length ? live.map(editorChip).join("") : '<span class="chip"><span class="dot"></span>no editor</span>'}</div>`;
  if (quiet) return h + "</section>";

  h += "<h3>Lease</h3>";
  const L = p.lease;
  if (L) {
    const left = L.busy ? `busy: <code>${esc(L.busy.cmd)}</code> for ${span(L.busy.since)}`
                        : `until ${clock(L.until)} (${Math.max(1, Math.round((L.until - state.time) / 60))} min left)`;
    h += `<div class="lease"><span class="who">${esc(L.label)}</span>: ${esc(L.what || "no reason given")}
      <div class="sub">${left} · held ${span(L.since)}${L.hold ? " · --hold" : ""}${L.granted_by ? " · granted by " + esc(L.granted_by) : ""}
      ${L.outer.map(o => " · inside its lease for " + esc(o.what)).join("")}</div>
      <div class="btns" style="margin-top:6px"><button class="danger" data-a="end" data-p="${pi}">End lease</button></div></div>`;
  } else {
    h += `<div class="lease free">free</div>`;
  }

  h += `<h3>Queue (${p.queue.length})</h3>`;
  if (p.queue.length) {
    h += '<ol class="queue">' + p.queue.map((q, i) => {
      const n = p.queue.length, d = `data-p="${pi}" data-q="${i}"`;
      return `<li><span class="n">${i + 1}</span><div class="grow"><b>${esc(q.label)}</b>: <span class="what">${esc(q.what)}</span>
        <div class="sub">waiting ${span(q.time)}${q.waiting ? " in <code>ue wait lease</code>" : " (not polling)"} · wants ${q.minutes || "?"} min${i ? "" : " · next in line"}</div></div><div class="btns">
        <button title="to the top" data-a="top" ${d} ${i ? "" : "disabled"}>⤒</button>
        <button title="up" data-a="up" ${d} ${i ? "" : "disabled"}>↑</button>
        <button title="down" data-a="down" ${d} ${i < n - 1 ? "" : "disabled"}>↓</button>
        <button class="primary" title="give it the editor now" data-a="grant" ${d}>Grant</button>
        <button class="danger" title="drop this request" data-a="drop" ${d}>✕</button>
        </div></li>`;
    }).join("") + "</ol>";
  } else {
    h += '<div class="sub">nobody waiting</div>';
  }

  if (p.running.length) {
    h += `<h3>Running now (${p.running.length})</h3><ul class="plain running">` + p.running.map(r =>
      `<li><b>${esc(r.label)}</b> <code>${esc(r.cmd)}</code> <span class="sub">for ${span(r.since)} · pid ${r.pid}</span></li>`).join("") + "</ul>";
  }
  const jobs = p.jobs.filter(j => j.running).concat(p.jobs.filter(j => !j.running).slice(-2));
  if (jobs.length) {
    h += "<h3>Jobs</h3><ul class=\"plain\">" + jobs.map(j => `<li><code>${esc(j.line)}</code></li>`).join("") + "</ul>";
  }
  if (p.activity.length) {
    const all = expanded[p.project], rows = all ? p.activity : p.activity.slice(0, 8);
    h += `<h3>Activity, last 30 min (${p.activity.length})</h3><table>` + rows.map(a =>
      `<tr><td class="t">${span(a.time)}</td><td class="s" title="${esc(a.label)}">${esc(a.label)}</td><td class="c">${esc(a.cmd)}</td></tr>`).join("") + "</table>";
    if (p.activity.length > 8) h += `<span class="more" data-a="more" data-p="${pi}">${all ? "fewer" : "all " + p.activity.length}</span>`;
  }
  return h + "</section>";
}

function render() {
  if (!state) return;
  drawn = state;  // the click handler reads the rows the buttons were drawn from
  const showQuiet = $("#showQuiet").checked;
  const shown = (p) => showQuiet || p.error || p.editors.some(e => e.alive) || p.lease || p.queue.length || p.running.length || p.activity.length;
  const cards = state.projects.map((p, i) => shown(p) ? card(p, i) : "").filter(Boolean);
  const hidden = state.projects.length - cards.length;
  $("#root").innerHTML = cards.join("") || `<div class="empty">nothing going on in ${state.projects.length} project(s)</div>`;
  const waiting = state.projects.reduce((n, p) => n + (p.queue ? p.queue.length : 0), 0);
  $("#meta").textContent = `${state.projects.length} project(s)` + (hidden ? `, ${hidden} quiet hidden` : "") + (waiting ? ` · ${waiting} waiting for an editor` : "") + ` · updated ${clock(state.time)}`;
  document.title = (waiting ? `(${waiting}) ` : "") + "Tether dashboard";
}

async function poll() {
  try { state = await (await fetch("/api/state", {cache: "no-store"})).json(); if (!busy) render(); }
  catch (e) { $("#meta").innerHTML = '<span class="err">the dashboard server stopped: run <code>ue dash</code> again</span>'; }
}
poll(); setInterval(() => { if (!document.hidden) poll(); }, 2000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
</script>
</body>
</html>
"""
