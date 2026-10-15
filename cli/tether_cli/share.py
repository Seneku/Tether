"""Sharing one editor between sessions: who is who, what they ran, the editor lease, and who started PIE.

Everything here is advisory and lives in Saved/Tether next to the beacon: sessions that use `ue` see each other;
nothing stops a person or a script that goes around it. A session is a Claude Code session
(CLAUDE_CODE_SESSION_ID) or, outside Claude Code, "user"; UE_SESSION overrides it.
"""
import json
import os
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
    """The post-edit hook notes which session last wrote a project source file (Claude Code's Edit/Write)."""
    edits = read_json(edits_path(project)) or {}
    now = time.time() if now is None else now
    edits[edit_key(project, path)] = {"session": sid, "time": round(now, 1)}
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
