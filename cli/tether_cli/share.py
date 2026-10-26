"""Sharing one editor between sessions: who is who, what they ran, the editor lease, and who started PIE.

Everything here is advisory and lives in Saved/Tether next to the beacon: sessions that use `ue` see each other;
nothing stops a person or a script that goes around it. A session is a Claude Code session
(CLAUDE_CODE_SESSION_ID) or, outside Claude Code, "user"; UE_SESSION overrides it.
"""
import json
import os
import re
import time

from .core import (CliError, actor_id, chosen_editor, same_session, command_lines, pid_alive, process_ancestry, process_name, read_json, run_git,
                   session_id, state_dir, write_json)

LEASE_MINUTES = 15      # default length of `ue lease take`
ACTIVE_MINUTES = 5      # other sessions' commands this recent make a restart or close ask for the lease
ACTIVITY_KEEP = 3600    # seconds of activity kept in activity.jsonl
LEASED = 5              # exit code: another session holds the editor lease


def session_names(project):
    return read_json(os.path.join(state_dir(project), "sessions.json")) or {}


def set_session_name(project, name, sid=None):
    names = session_names(project)
    names[sid or actor_id()] = name
    write_json(os.path.join(state_dir(project), "sessions.json"), names)


def session_label(project, sid, names=None):
    """'spire (3f2a9c1d)' when the session (or a sub-agent, '3f2a9c1d/a55cee02') named itself, else the id; "user"
    stays "user"."""
    name = (names if names is not None else session_names(project)).get(sid)
    return f"{name} ({sid})" if name and name != sid else sid


def activity_path(project):
    return os.path.join(state_dir(project), "activity.jsonl")


STATS_KEEP = 20000  # rows of stats.jsonl kept (months of use); it stays on this machine


def stats_path(project):
    return os.path.join(state_dir(project), "stats.jsonl")


def record_stat(project, command, code, seconds, error=None, now=None):
    """One row per finished `ue` command for `ue stats`: what ran, its exit code, how long, the error's first line."""
    now = time.time() if now is None else now
    row = {"cmd": command, "exit": code, "s": round(seconds, 2), "time": round(now)}
    if error:
        row["error"] = error.splitlines()[0][:200]
    path = stats_path(project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        if os.path.getsize(path) > 4 * 1024 * 1024:  # trim now and then, not on every command
            rows = read_stats(project)[-STATS_KEEP:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
    except OSError:
        pass  # usage stats must never fail the command


def read_stats(project, since=0):
    rows = []
    try:
        with open(stats_path(project), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("time", 0) >= since:
                    rows.append(row)
    except OSError:
        pass
    return rows


def error_shape(text):
    """An error message with its specifics (paths, numbers, names in quotes) blanked, so repeats group together."""
    text = re.sub(r"(/Game|/Script|[A-Za-z]:[\\/])[^\s,;)'\"]*", "<path>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "'...'", text)
    return re.sub(r"\d+(\.\d+)?", "N", text)


def cmd_stats(ctx, args):
    """`ue stats [--days N]`: which ue commands this project ran, how often they failed and how long they took, from
    Saved/Tether/stats.jsonl (local only), to see what needs work."""
    since = time.time() - args.days * 86400 if args.days else 0
    rows = read_stats(ctx.project, since)
    if not rows:
        print("no ue commands recorded yet" + (f" in the last {args.days:g} day(s)" if args.days else "")
              + " (Tether 0.30 and later record each command in Saved/Tether/stats.jsonl)")
        return 0
    by = {}
    for row in rows:
        by.setdefault(row["cmd"], []).append(row)
    first = min(r["time"] for r in rows)
    print(f"{len(rows)} ue command(s) since {time.strftime('%Y-%m-%d %H:%M', time.localtime(first))}, "
          f"{sum(1 for r in rows if r.get('exit'))} failed (exit != 0)")
    print(f"  {'command':<22} {'runs':>5} {'failed':>7} {'median':>8} {'slowest':>8}")
    order = sorted(by.items(), key=lambda kv: (-sum(1 for r in kv[1] if r.get("exit")), -len(kv[1])))
    for cmd, runs in order[: args.limit]:
        failed = sum(1 for r in runs if r.get("exit"))
        took = sorted(r.get("s", 0) for r in runs)
        print(f"  {cmd:<22} {len(runs):>5} {failed:>4} {failed * 100 // len(runs):>2}% {took[len(took) // 2]:>7.1f}s "
              f"{took[-1]:>7.1f}s")
    errors = {}
    for row in rows:
        if row.get("error"):
            key = (row["cmd"], error_shape(row["error"]))
            errors.setdefault(key, [0, row["error"]])[0] += 1
    if errors:
        print("most frequent errors:")
        for (cmd, _), (count, example) in sorted(errors.items(), key=lambda kv: -kv[1][0])[:10]:
            print(f"  {count:>4}x  ue {cmd}: {example[:140]}")
    ctx.data = {cmd: {"runs": len(r), "failed": sum(1 for x in r if x.get("exit"))} for cmd, r in by.items()}
    return 0


def record_activity(project, argv, now=None):
    """Append this command to the activity log (one JSON line; trimmed to the last hour now and then)."""
    now = time.time() if now is None else now
    path = activity_path(project)
    line = json.dumps({"session": session_id(), "actor": actor_id(), "cmd": " ".join(argv)[:160], "time": round(now, 1)})
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


def recent_sessions(project, minutes, exclude=None, now=None, by="session"):
    """{session: its last row} for the sessions that ran a command in the last `minutes`, newest first; by="actor"
    lists a session's sub-agents apart."""
    now = time.time() if now is None else now
    last = {}
    for row in read_activity(project):
        if row["time"] >= now - minutes * 60 and row["session"] != exclude:
            last[row.get(by) or row["session"]] = row
    return dict(sorted(last.items(), key=lambda kv: -kv[1]["time"]))


def duration(seconds):
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} min"


def ago(seconds):
    return f"{duration(seconds)} ago"


# ---------------------------------------------------------------- the lease

def lease_path(project):
    return os.path.join(state_dir(project), "lease.json")


def drop_lease_file(project):
    """Delete lease.json. On Windows a delete fails while any other process has the file open (Python opens files
    without delete sharing), and with several sessions polling it in `ue wait lease` one is often reading it: the
    release used to fail silently and the lease stayed held (UnrealRacing 2026-10-26). Retry, then overwrite it
    with an expired lease, which a reader does not block."""
    path = lease_path(project)
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    write_json(path, {"until": 0})


def current_lease(lease, now):
    """The innermost lease of a stack still running: a session's lease taken again for something else (a script it
    runs) sits on top of the first, under "outer", which comes back when the inner one is released or runs out."""
    while isinstance(lease, dict) and "until" in lease:
        if lease["until"] > now:
            return lease
        lease = lease.get("outer")
    return None


def holder_pie_running(project, sid):
    """Whether an editor of this project is in a PIE session that session `sid` started (`ue pie start`/`pie do`):
    a PIE test runs for minutes between its holder's `ue` commands (a script polling for the test's result file)."""
    record = read_json(pie_owner_path(project)) or {}
    if record.get("session") != sid or not record.get("time"):
        return False
    folder = os.path.join(state_dir(project), "editors")
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        beacon = read_json(os.path.join(folder, name)) if name.endswith(".json") else None
        if isinstance(beacon, dict) and beacon.get("pie") and beacon.get("pie_since") \
                and -5 <= beacon["pie_since"] - record["time"] <= 60 and pid_alive(beacon.get("pid")):
            return True
    return False


def lease_idle(project, lease, now):
    """Seconds the holder of `lease` (the file's top level) has left the editor idle while another session waits
    for it, or 0: no `ue` command of the holder's running and no PIE session it started, since its last command
    ended (or it took the lease). A lease taken with --hold never idles, nor one taken before 0.35 (no "lapse")."""
    if lease.get("hold") or not lease.get("lapse"):
        return 0
    sid = lease.get("session")
    if not any(r["session"] != sid for r in lease_requests(project, now)):
        return 0
    idle = now - max(lease.get("active", 0), lease.get("since", 0))
    if idle < LEASE_IDLE * 60:
        return 0
    if any(r.get("session") == sid for r in running_commands(project)) or holder_pie_running(project, sid):
        return 0
    return idle


def active_lease(project, now=None):
    """The editor lease if one is held and not expired, else None. A lease whose time ran out while its holder's
    `ue` command is still running (a long `ue py` stage) is still held: it lasts LEASE_KEEP min past that command.
    A lease whose holder has left the editor idle for LEASE_IDLE min while another session waits has lapsed
    (lease_idle): the editor goes to whoever waits, and the holder is told on its next command."""
    lease = read_json(lease_path(project))
    now = time.time() if now is None else now
    if not isinstance(lease, dict) or "until" not in lease:
        return None
    held = current_lease(lease, now)
    if held:
        return None if lease_idle(project, lease, now) else held
    busy = [r for r in running_commands(project) if r.get("session") == lease.get("session")]
    return dict(lease, until=now + LEASE_KEEP * 60, busy=busy[0]) if busy else None


def describe_lease(project, lease, now=None):
    now = time.time() if now is None else now
    busy = lease.get("busy") or next((r for r in running_commands(project) if r.get("session") == lease["session"]),
                                     None)
    outer = current_lease(lease.get("outer"), now)
    # The expiry first: after a renewal "since" is still the first take, and read alone it looked unextended
    return (f"leased by {session_label(project, lease['session'])}: {lease.get('what') or 'no reason given'}, "
            + (f"busy: `{busy['cmd']}` for {duration(now - busy.get('since', now))} (held until it ends, then "
               f"{LEASE_KEEP} min), " if busy else
               f"until {time.strftime('%H:%M', time.localtime(lease['until']))} "
               f"({max(1, round((lease['until'] - now) / 60))} min left), ")
            + f"held since {time.strftime('%H:%M', time.localtime(lease['since']))}"
            + (f" (inside its lease for {outer.get('what')}, until {time.strftime('%H:%M', time.localtime(outer['until']))})"
               if outer else ""))


def keep_lease(project, now=None):
    """This session's lease, if it would run out within LEASE_KEEP min, now lasts LEASE_KEEP min from now (at the
    start and at the end of each of its `ue` commands). True when it was about to run out."""
    now = time.time() if now is None else now
    held = active_lease(project, now)
    if not held or held["session"] != session_id() or held["until"] - now >= LEASE_KEEP * 60 - 30:
        return False  # (renewed within the last half minute, or longer to go anyway)
    held = {k: v for k, v in held.items() if k != "busy"}
    write_json(lease_path(project), dict(held, until=now + LEASE_KEEP * 60, kept=True))
    return True


# ---------------------------------------------------------------- commands running now

def running_dir(project):
    return os.path.join(state_dir(project), "running")


def start_running(project, argv, now=None):
    """Note this `ue` command while it runs (stop_running removes it): a long command keeps its session's lease,
    and `ue status`, `who` and `lease` say what the editor is busy with. The path, or None."""
    path = os.path.join(running_dir(project), f"{os.getpid()}.json")
    try:
        write_json(path, {"session": session_id(), "actor": actor_id(), "cmd": " ".join(argv)[:160], "pid": os.getpid(),
                          "since": round(time.time() if now is None else now, 1)})
    except OSError:
        return None
    return path


def stop_running(path):
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


def running_commands(project):
    """[{session, cmd, pid, since}] of the `ue` commands running now on this project (other than this one), oldest
    first. Records of processes that are gone (or whose pid now belongs to something that is not Python) go."""
    folder, rows = running_dir(project), []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        pid = name[:-5]
        if not name.endswith(".json") or not pid.isdigit() or int(pid) == os.getpid():
            continue
        alive = pid_alive(int(pid))
        image = process_name(int(pid)) if alive else None
        row = read_json(os.path.join(folder, name))
        if not alive or (image and not image.lower().startswith("py")) or not isinstance(row, dict):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("since", 0))


def describe_running(project, row, now=None, names=None):
    now = time.time() if now is None else now
    return (f"{session_label(project, row.get('session'), names)} `{row.get('cmd')}` for "
            f"{duration(now - row.get('since', now))} (pid {row.get('pid')})")


def take_lease(project, what, minutes, now=None, hold=False):
    """Take (or renew) the lease for this session; refuse while another session holds it. Taken again by this
    session for something else (a script inside a session's lease takes its own), the new lease goes on top and
    the one before comes back when it is released: a script's `lease release` no longer drops its caller's lease.
    hold: the lease never lapses while its holder is idle (lease_idle); a nested lease keeps its outer one's.
    Returns (lease, "taken" | "renewed" | "nested")."""
    now = time.time() if now is None else now
    held = active_lease(project, now)
    me = session_id()
    if held and held["session"] != me:
        raise CliError(f"the editor is {describe_lease(project, held, now)}; wait, or ask that session to run "
                       "`ue lease release`", LEASED)
    held = {k: v for k, v in held.items() if k != "busy"} if held else None
    if held and held.get("what") != what:
        lease, how = {"session": me, "what": what, "since": now, "until": now + minutes * 60, "outer": held}, "nested"
    else:
        lease = dict(held or {"session": me, "what": what, "since": now}, until=now + minutes * 60)
        how = "renewed" if held else "taken"
    lease["active"] = now
    if hold or (held and held.get("hold")):
        lease["hold"] = True
    else:
        lease["lapse"] = True
    write_json(lease_path(project), lease)
    drop_request(project, me)  # asked for it earlier: answered
    return lease, how


def release_lease(project, force=False):
    """Release this session's lease (any session's with force): the innermost one, so a lease it was taken inside
    of is held again. Returns (the lease released, the lease held again or None), or (None, None) if none was."""
    held = active_lease(project)
    if not held:
        return None, None
    if held["session"] != session_id() and not force:
        raise CliError(f"the lease is not yours: the editor is {describe_lease(project, held)} "
                       "(--force releases it anyway)", LEASED)
    back = current_lease(held.get("outer"), time.time()) if not force else None
    if back:
        write_json(lease_path(project), back)
        return held, back
    drop_lease_file(project)
    return held, None


def check_shared(project, action, destructive, ignore=False, now=None):
    """Before `action` on the shared editor: refuse while another session holds the lease. A destructive action
    (restart, close) also refuses when other sessions used the editor in the last few minutes and nobody holds
    the lease: those sessions would lose the editor mid-task. `ignore` (--ignore-lease) skips both."""
    if ignore or os.environ.get("UE_IGNORE_LEASE") == "1":  # --ignore-lease anywhere on the command line
        return
    if own_editor(project):  # `ue open --second`: this session's own editor, outside the shared one's lease
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


def own_editor(project):
    """True when the editor this session talks to is its own (`ue open --second`)."""
    try:
        return same_session((chosen_editor(project) or {}).get("owner")) and bool(session_id())
    except CliError:
        return False


def lease_note(project):
    """A warning line when another session holds the lease (for commands that only read or edit), else None."""
    held = active_lease(project)
    if held and held["session"] != session_id() and not own_editor(project):
        return f"note: the editor is {describe_lease(project, held)}"
    return None


# ---------------------------------------------------------------- asking for the lease

REQUEST_HOURS = 2  # a request nobody granted is dropped after this


def requests_path(project):
    return os.path.join(state_dir(project), "lease-requests.json")


def lease_requests(project, now=None):
    """Open requests for the editor, oldest first: [{session, what, minutes, time}]."""
    now = time.time() if now is None else now
    rows = read_json(requests_path(project)) or []
    return [r for r in rows if isinstance(r, dict) and now - r.get("time", 0) < REQUEST_HOURS * 3600]


def request_lease(project, what, minutes, now=None):
    """Ask the session holding the lease for the editor next (one request per session; asking again updates it).
    Returns the lease it is waiting on, or None when the editor is free (take it instead)."""
    now = time.time() if now is None else now
    me = session_id()
    rows = [r for r in lease_requests(project, now) if r["session"] != me]
    rows.append({"session": me, "what": what, "minutes": minutes, "time": now})
    write_json(requests_path(project), rows)
    return active_lease(project, now)


def drop_request(project, sid):
    rows = lease_requests(project)
    if any(r["session"] == sid for r in rows):
        write_json(requests_path(project), [r for r in rows if r["session"] != sid])


def grant_lease(project, to=None, now=None):
    """Hand this session's lease (or a free editor) to the oldest request, or to session `to`: the lease is
    written in its name for what it asked. Returns the new lease."""
    now = time.time() if now is None else now
    held, me = active_lease(project, now), session_id()
    if held and held["session"] != me:
        raise CliError(f"the lease is not yours to grant: the editor is {describe_lease(project, held, now)}", LEASED)
    rows = lease_requests(project, now)
    pick = next((r for r in rows if r["session"] == to or (to and session_names(project).get(r["session"]) == to)),
                None) if to else (rows[0] if rows else None)
    if not pick:
        raise CliError("no request for the editor" + (f" from {to}" if to else "")
                       + (" (open: " + ", ".join(session_label(project, r["session"]) for r in rows) + ")" if rows else ""))
    lease = {"session": pick["session"], "what": pick["what"], "since": now, "until": now + pick["minutes"] * 60,
             "granted_by": me}
    write_json(lease_path(project), lease)
    drop_request(project, pick["session"])
    return lease


REMIND_MINUTES = 10  # a request the lease holder was told about is said again after this
LEASE_KEEP = 5       # a lease whose holder keeps running `ue` commands lasts this long past the last one
LEASE_IDLE = 3       # ... but lapses after this long with the editor idle (no command, no PIE of its own) while
                     # another session waits: holders thinking between commands kept sessions queued (UnrealRacing)


def note_lease_active(project, now=None):
    """At the end of each of this session's `ue` commands: the lease it holds was in use until now (lease_idle)."""
    now = time.time() if now is None else now
    lease = read_json(lease_path(project))
    if isinstance(lease, dict) and lease.get("session") == session_id() and current_lease(lease, now):
        write_json(lease_path(project), dict(lease, active=now))


def lapsed_lease_note(project, now=None):
    """A line for this session if the lease file still names it but the lease lapsed while it was idle, else None.
    Said once: the file is then dropped (the editor is free)."""
    now = time.time() if now is None else now
    lease = read_json(lease_path(project))
    if not isinstance(lease, dict) or lease.get("session") != session_id() or not current_lease(lease, now):
        return None
    idle = lease_idle(project, lease, now)
    if not idle:
        return None
    waiting = [session_label(project, r["session"]) for r in lease_requests(project, now) if r["session"] != lease["session"]]
    drop_lease_file(project)
    return (f"note: your editor lease ({lease.get('what')}) lapsed: the editor sat idle {duration(idle)} while "
            f"{', '.join(waiting)} waited. `ue lease take` again (or `ue lease request` to queue); take it with "
            "--hold for work a hand-over would lose, and run long steps as one script")


def lease_messages(project, now=None):
    """Lines for this session at the start of any `ue` command: requests waiting on its lease, or a lease just
    granted to it (said once). Each request is said once, again when it changes, and again every REMIND_MINUTES
    while it waits (`ue status` and `ue who` list them all)."""
    now = time.time() if now is None else now
    me, held, out = session_id(), active_lease(project, now), []
    lapsed = None if held else lapsed_lease_note(project, now)
    if lapsed:
        out.append(lapsed)
    if held and held["session"] == me:
        if held.get("granted_by") and not held.get("told"):
            out.append(f"note: the editor lease is yours now, granted by {session_label(project, held['granted_by'])} "
                       f"({max(1, round((held['until'] - now) / 60))} min; `ue lease release` when done)")
            held = {k: v for k, v in dict(held, told=True).items() if k != "busy"}
            write_json(lease_path(project), held)
        if keep_lease(project, now) and not held.get("kept"):  # in use: LEASE_KEEP min past the last command
            out.append(f"note: your editor lease ({held.get('what')}) was about to run out; it now lasts "
                       f"{LEASE_KEEP} min past your last `ue` command (`ue lease release` when done)")
        rows, due = lease_requests(project, now), []
        for r in rows:
            if r["session"] != me and now - r.get("told", 0) >= REMIND_MINUTES * 60:
                due.append(r)
                r["told"] = now
        if due:
            out.append("note: waiting for the editor: " + "; ".join(
                f"{session_label(project, r['session'])} ({ago(now - r['time'])}): {r['what']}" for r in due)
                + ". `ue lease grant` hands it over when you are done")
            write_json(requests_path(project), rows)
    return out


# ---------------------------------------------------------------- who wrote a source file

def edits_path(project):
    return os.path.join(state_dir(project), "edits.json")


def edit_key(project, path):
    try:
        return os.path.relpath(os.path.abspath(path), os.path.dirname(project)).replace("\\", "/").lower()
    except ValueError:  # another drive than the project's (Windows): the absolute path is the key
        return os.path.abspath(path).replace("\\", "/").lower()


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
    """Note who is starting PIE: the session, this `ue` command and the processes above it (a shell loop that
    keeps starting PIE can then be found and stopped: `ue who`, `ue pie status`)."""
    import sys
    write_json(pie_owner_path(project), {"session": session_id(), "time": time.time() if now is None else now,
                                         "cmd": " ".join(["ue"] + sys.argv[1:])[:160], "pid": os.getpid(),
                                         "parents": process_ancestry(os.getpid())})


SHELL_HOSTS = ("node", "claude", "explorer", "windowsterminal", "openconsole", "conhost", "code", "services",
               "svchost", "wininit")  # long-lived hosts above any command line, not loops


def pie_starter(project, beacon):
    """For a running PIE session `ue` started: (the command that started it, [(pid, command line)] of the shells
    above it still running). A shell still running after its `ue pie ...` command ended is usually a loop: the
    processes to stop. None when PIE is not running or was not started through `ue`."""
    if pie_owner(project, beacon) in (None, "started by hand in the editor"):
        return None
    record = read_json(pie_owner_path(project)) or {}
    if pid_alive(record.get("pid")):  # the starting command still runs: nothing stray yet
        return record.get("cmd"), []
    shells, mine = [], {pid for pid, _ in process_ancestry(os.getpid())}  # the shell asking is not a stray loop
    for pid, image in record.get("parents") or []:
        if os.path.splitext(image)[0].lower() in SHELL_HOSTS:
            break  # the agent or terminal hosting the commands: above here is not a loop
        if pid not in mine and pid_alive(pid) and (process_name(pid) or image).lower() == image.lower():
            shells.append(pid)
    lines = command_lines(shells) if shells else {}
    return record.get("cmd"), [(pid, lines[pid]) for pid in shells if pid in lines]


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
