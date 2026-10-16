"""Sharing one editor between sessions: who is who, what they ran, the editor lease, and who started PIE.

Everything here is advisory and lives in Saved/Tether next to the beacon: sessions that use `ue` see each other;
nothing stops a person or a script that goes around it. A session is a Claude Code session
(CLAUDE_CODE_SESSION_ID) or, outside Claude Code, "user"; UE_SESSION overrides it.
"""
import json
import os
import re
import time

from .core import CliError, read_json, run_git, state_dir, write_json

LEASE_MINUTES = 15      # default length of `ue lease take`
ACTIVE_MINUTES = 5      # other sessions' commands this recent make a restart or close ask for the lease
ACTIVITY_KEEP = 3600    # seconds of activity kept in activity.jsonl
LEASED = 5              # exit code: another session holds the editor lease


def session_id():
    """This process's session: UE_SESSION, else the Claude Code session (short id), else "user"."""
    explicit = os.environ.get("UE_SESSION")
    if explicit:
        return explicit
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return claude[:8] if claude else "user"


def session_names(project):
    return read_json(os.path.join(state_dir(project), "sessions.json")) or {}


def set_session_name(project, name, sid=None):
    names = session_names(project)
    names[sid or session_id()] = name
    write_json(os.path.join(state_dir(project), "sessions.json"), names)


def session_label(project, sid, names=None):
    """'spire (3f2a9c1d)' when the session named itself, else the id; "user" stays "user"."""
    name = (names if names is not None else session_names(project)).get(sid)
    return f"{name} ({sid})" if name and name != sid else sid


def activity_path(project):
    return os.path.join(state_dir(project), "activity.jsonl")


def record_activity(project, argv, now=None):
    """Append this command to the activity log (one JSON line; trimmed to the last hour now and then)."""
    now = time.time() if now is None else now
    path = activity_path(project)
    line = json.dumps({"session": session_id(), "cmd": " ".join(argv)[:160], "time": round(now, 1)})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(path) > 256 * 1024:  # an hour of a busy editor is far less; keep only that
            rows = [r for r in read_activity(project) if r["time"] > now - ACTIVITY_KEEP]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
    except OSError:
        pass  # a log of who did what must never fail the command itself


def read_activity(project):
    rows = []
    try:
        with open(activity_path(project), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a line half-written by a concurrent append
                if isinstance(row, dict) and "session" in row and "time" in row:
                    rows.append(row)
    except OSError:
        pass
    return rows


def recent_sessions(project, minutes, exclude=None, now=None):
    """{session: its last row} for the sessions that ran a command in the last `minutes`, newest first."""
    now = time.time() if now is None else now
    last = {}
    for row in read_activity(project):
        if row["time"] >= now - minutes * 60 and row["session"] != exclude:
            last[row["session"]] = row
    return dict(sorted(last.items(), key=lambda kv: -kv[1]["time"]))


def duration(seconds):
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} min"


def ago(seconds):
    return f"{duration(seconds)} ago"


# ---------------------------------------------------------------- the lease

def lease_path(project):
    return os.path.join(state_dir(project), "lease.json")


def active_lease(project, now=None):
    """The editor lease if one is held and not expired, else None."""
    lease = read_json(lease_path(project))
    now = time.time() if now is None else now
    return lease if isinstance(lease, dict) and lease.get("until", 0) > now else None


def describe_lease(project, lease, now=None):
    now = time.time() if now is None else now
    return (f"leased by {session_label(project, lease['session'])}: {lease.get('what') or 'no reason given'}, "
            f"{max(1, round((lease['until'] - now) / 60))} min left (since "
            f"{time.strftime('%H:%M', time.localtime(lease['since']))})")


def take_lease(project, what, minutes, now=None):
    """Take (or renew) the lease for this session; refuse while another session holds it."""
    now = time.time() if now is None else now
    held = active_lease(project, now)
    me = session_id()
    if held and held["session"] != me:
        raise CliError(f"the editor is {describe_lease(project, held, now)}; wait, or ask that session to run "
                       "`ue lease release`", LEASED)
    lease = {"session": me, "what": what, "since": held["since"] if held else now, "until": now + minutes * 60}
    write_json(lease_path(project), lease)
    return lease, bool(held)


def release_lease(project, force=False):
    """Release this session's lease (any session's with force). The lease released, or None if none was held."""
    held = active_lease(project)
    if not held:
        return None
    if held["session"] != session_id() and not force:
        raise CliError(f"the lease is not yours: the editor is {describe_lease(project, held)} "
                       "(--force releases it anyway)", LEASED)
    try:
        os.remove(lease_path(project))
    except OSError:
        pass
    return held


def check_shared(project, action, destructive, ignore=False, now=None):
    """Before `action` on the shared editor: refuse while another session holds the lease. A destructive action
    (restart, close) also refuses when other sessions used the editor in the last few minutes and nobody holds
    the lease: those sessions would lose the editor mid-task. `ignore` (--ignore-lease) skips both."""
    if ignore or os.environ.get("UE_IGNORE_LEASE") == "1":  # --ignore-lease anywhere on the command line
        return
    now = time.time() if now is None else now
    held = active_lease(project, now)
    me = session_id()
    if held and held["session"] != me:
        raise CliError(f"{action} refused: the editor is {describe_lease(project, held, now)}. Wait for "
                       "`ue lease release`, or pass --ignore-lease", LEASED)
    if destructive and not held:
        others = recent_sessions(project, ACTIVE_MINUTES, exclude=me, now=now)
        if others:
            names = session_names(project)
            who = "; ".join(f"{session_label(project, s, names)} {ago(now - r['time'])}: {r['cmd'][:60]}"
                            for s, r in list(others.items())[:3])
            raise CliError(f"{action} refused: other sessions used this editor in the last {ACTIVE_MINUTES} min "
                           f"({who}). Take the lease first (`ue lease take --for \"...\"`) so they see it, or pass "
                           "--ignore-lease", LEASED)


def lease_note(project):
    """A warning line when another session holds the lease (for commands that only read or edit), else None."""
    held = active_lease(project)
    if held and held["session"] != session_id():
        return f"note: the editor is {describe_lease(project, held)}"
    return None


# ---------------------------------------------------------------- who wrote a source file

def edits_path(project):
    return os.path.join(state_dir(project), "edits.json")


def edit_key(project, path):
    return os.path.relpath(os.path.abspath(path), os.path.dirname(project)).replace("\\", "/").lower()


def record_edit(project, path, sid, now=None):
    """The post-edit hook notes which session last wrote a project file (Claude Code's Edit/Write), and when each
    session that wrote it last did ("by")."""
    edits = read_json(edits_path(project)) or {}
    now = time.time() if now is None else now
    key = edit_key(project, path)
    by = {s: t for s, t in ((edits.get(key) or {}).get("by") or {}).items() if t > now - 7 * 86400}
    by[sid] = round(now, 1)
    edits[key] = {"session": sid, "time": round(now, 1), "by": by}
    edits = {k: v for k, v in edits.items() if v.get("time", 0) > now - 7 * 86400}  # a week is plenty
    write_json(edits_path(project), edits)


def others_edits(project, paths, minutes=10, now=None):
    """[(path, session, edit time)] of the files another session wrote (through Claude Code's Edit/Write, as the
    post-edit hook saw) in the last `minutes`, and that nothing wrote since."""
    now = time.time() if now is None else now
    edits, me, found = read_json(edits_path(project)) or {}, session_id(), []
    for path in paths:
        record = edits.get(edit_key(project, path))
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if record and record["session"] != me and record["time"] >= mtime - 2 and now - record["time"] <= minutes * 60:
            found.append((path, record["session"], record["time"]))
    return found


def changed_files(project):
    """[(status, absolute path)] of the uncommitted files under the project folder (git status), or None outside git."""
    top = (run_git(project, "rev-parse", "--show-toplevel", check=False) or "").strip()
    if not top:
        return None
    out = run_git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".") or ""
    entries, rows = out.split("\0"), []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if status[0] in "RC":  # a rename: the old name follows
            i += 1
        rows.append((status, os.path.normpath(os.path.join(top, rel))))
    return rows


# ---------------------------------------------------------------- who wrote a file, from session transcripts

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Tools that cannot write project files: their calls say nothing about who wrote what
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch", "Skill", "TodoWrite",
                   "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "AskUserQuestion", "ExitPlanMode",
                   "EnterPlanMode", "SendMessage", "ListAgents", "ReadNotifications", "SendUserFile"}
STAMP = re.compile(r'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z?"')


def transcript_files(project, since):
    """{short session id: transcript path} of Claude Code sessions that worked in this project folder (or below
    it) and wrote their transcript after `since`."""
    root = os.path.normcase(os.path.abspath(os.path.dirname(project)))
    slugify = lambda folder: re.sub(r"[^A-Za-z0-9]", "-", folder)
    slug = slugify(os.path.abspath(os.path.dirname(project)))
    # sessions working in a parent folder inside the same repository write here too (a repo with the project in it)
    top = os.path.normcase(os.path.abspath((run_git(project, "rev-parse", "--show-toplevel", check=False) or "").strip()
                                           or os.path.dirname(project)))
    parents, here = set(), os.path.abspath(os.path.dirname(project))
    while os.path.dirname(here) != here and os.path.normcase(os.path.dirname(here)).startswith(top):
        here = os.path.dirname(here)
        parents.add(slugify(here))
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"),
                        "projects")
    found = {}
    for folder in os.listdir(base) if os.path.isdir(base) else []:
        if folder != slug and folder not in parents and not folder.startswith(slug + "-"):
            continue
        for name in os.listdir(os.path.join(base, folder)):
            path = os.path.join(base, folder, name)
            if not name.endswith(".jsonl") or os.path.getmtime(path) < since:
                continue
            if folder != slug and folder not in parents:  # Tether-Sandbox is under Tether, Tether-dev is not
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(65536)
                cwd = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', head)
                cwd = os.path.normcase(json.loads(f'"{cwd.group(1)}"')) if cwd else ""
                if not (cwd + os.sep).startswith(root + os.sep):
                    continue
            found[name[:8]] = path
    return found


def stamp_time(match):
    import calendar
    seconds = calendar.timegm(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S"))
    return seconds + (float("0." + match.group(2)) if match.group(2) else 0.0)


def transcript_events(path, since):
    """(writes [(time, normcased path)], spans [(start, end)]) from one transcript: the files its Edit/Write calls
    wrote, and when its other tool calls (Bash, `ue py`, MCP tools...) were running. Calls before `since` are
    skipped without parsing their JSON."""
    pending, writes, spans = {}, [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            stamp = STAMP.search(line)
            if not stamp:
                continue
            t = stamp_time(stamp)
            if t < since:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except ValueError:
                continue
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    pending[block.get("id")] = (t, block.get("name"), block.get("input") or {})
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    start, name, given = pending.pop(block["tool_use_id"])
                    if name in WRITE_TOOLS:
                        target = given.get("file_path") or given.get("notebook_path")
                        if target:
                            writes.append((t, os.path.normcase(os.path.abspath(target))))
                    elif name not in READ_ONLY_TOOLS:
                        spans.append((start, t))
    return writes, spans


def transcript_owners(path, mtime, sessions):
    """The sessions that wrote `path` at `mtime`: those whose Edit/Write named it then, else those with a
    command running then."""
    key = os.path.normcase(os.path.abspath(path))
    writers = {sid for sid, (writes, _) in sessions.items() if any(p == key and abs(t - mtime) <= 5 for t, p in writes)}
    if writers:
        return writers
    return {sid for sid, (_, spans) in sessions.items() if any(s - 1 <= mtime <= e + 2 for s, e in spans)}


def sort_changes(project, rows, now=None, committed=None, sessions=None):
    """Split changed files by who last wrote them: {"mine": [...], "shared": [(path, [other sessions])], "theirs":
    [(path, session)], "unsure": [(path, [sessions])], "unknown": [...], "via": {how: count}}. First the
    post-edit hook's record; for files it does not know, the Claude Code transcripts of the sessions working in
    this folder (`sessions`: {short id: (writes, spans)}): a file goes to the session whose Edit/Write named it,
    or whose command was running when it was written. Shared: mine, but another session also wrote it since its
    last commit (`committed(path)` gives that time; 0 when never committed)."""
    now = time.time() if now is None else now
    edits, me = read_json(edits_path(project)) or {}, session_id()
    claude = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "")[:8]
    if committed is None:
        def committed(path):
            out = run_git(project, "log", "-1", "--format=%ct", "--", path, check=False)
            return float(out.strip()) if out and out.strip() else 0.0
    mtimes = {}
    for _, path in rows:
        with __import__("contextlib").suppress(OSError):
            mtimes[path] = os.path.getmtime(path)
    if sessions is None:
        since = min(mtimes.values(), default=now) - 3600
        sessions = {sid: transcript_events(path, since) for sid, path in transcript_files(project, since).items()}
    label = lambda sid: me if sid == claude else sid
    out = {"mine": [], "shared": [], "theirs": [], "unsure": [], "unknown": [], "via": {}}
    for status, path in rows:
        record, mtime = edits.get(edit_key(project, path)), mtimes.get(path)
        others = set()
        if record and mtime is not None and record["time"] >= mtime - 2:
            owner, how = record["session"], "edit hook"
            others = {s for s, t in (record.get("by") or {}).items() if s != me}
        else:
            owners = {label(s) for s in transcript_owners(path, mtime, sessions)} if mtime is not None else set()
            if len(owners) != 1:
                out["unsure" if owners else "unknown"].append((path, sorted(owners)) if owners else path)
                continue
            owner, how = owners.pop(), "transcripts"
        if owner != me:
            out["theirs"].append((path, owner))
            continue
        since = committed(path)
        key = os.path.normcase(os.path.abspath(path))
        others = {s for s in others if (record.get("by") or {}).get(s, 0) > since} | {
            label(sid) for sid, (writes, _) in sessions.items() if label(sid) != me
            and any(p == key and t > since for t, p in writes)}
        if others:
            out["shared"].append((path, sorted(others)))
        else:
            out["mine"].append(path)
            out["via"][how] = out["via"].get(how, 0) + 1
    return out


def file_owner(project, path, now=None):
    """'edited by menu (aaea8357) 3 min ago (not you)' / '... (you)' for a file the hook saw, else the file's own
    state: 'uncommitted, written 07:12' from git and its modification time. None if it cannot tell."""
    now = time.time() if now is None else now
    full = path if os.path.isabs(path) else os.path.join(os.path.dirname(project), path)
    record = (read_json(edits_path(project)) or {}).get(edit_key(project, full))
    try:
        mtime = os.path.getmtime(full)
    except OSError:
        return None
    if record and record["time"] >= mtime - 2:  # written by that edit, not by something later
        mine = record["session"] == session_id()
        return (f"edited by {session_label(project, record['session'])} {ago(now - record['time'])}"
                + (" (you)" if mine else " (not you)"))
    prefix = ""
    if os.path.isdir(os.path.join(os.path.dirname(project), ".git")):
        try:
            changed = (run_git(project, "status", "--porcelain", "--", full, check=False) or "").strip()
            prefix = "uncommitted, " if changed else "committed, "
        except CliError:
            pass
    return f"{prefix}written {time.strftime('%H:%M', time.localtime(mtime))} ({ago(now - mtime)})"


# ---------------------------------------------------------------- PIE owner

def pie_owner_path(project):
    return os.path.join(state_dir(project), "pie-owner.json")


def record_pie_start(project, now=None):
    write_json(pie_owner_path(project), {"session": session_id(), "time": time.time() if now is None else now})


def clear_pie_start(project):
    """`pie stop`: the record belongs to the session that just ended, not to a later one started by hand."""
    try:
        os.remove(pie_owner_path(project))
    except OSError:
        pass


def pie_owner(project, beacon):
    """Who started the PIE session the beacon reports: "started by <session>", "started by hand in the editor"
    when no `ue pie start` led to it, or None when PIE is not running or the editor does not say when it began."""
    if not beacon or not beacon.get("pie") or not beacon.get("pie_since"):
        return None
    record = read_json(pie_owner_path(project)) or {}
    # `pie start` records itself just before asking for PIE, which then takes a moment (longer on a busy editor).
    if record.get("time") and -5 <= beacon["pie_since"] - record["time"] <= 60:
        return f"started by {session_label(project, record['session'])}"
    return "started by hand in the editor"
