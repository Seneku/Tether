"""Editor lifecycle: status, open, close, build, crash; the shared editor's lease and `ue who`."""
import argparse
import os
import sys
import time

from .core import (
    CliError, actor_id, blocking_dialogs, crash_reports, crash_summary, describe_crash, editor_beacons, editor_executable, flush_deletes,
    forget_beacon,
    editor_live, editor_process,
    decline_package_restore, editor_label, engine_root, filter_log, live_coding_available, live_editors, logged_dialogs,
    log_file_lines, newest_log, parse_res, pick_port, pid_alive, port_open, read_json, read_new_log, recent_crash,
    remember_target,
    run_git, startup_dialogs, startup_failure,
    state_dir,
    subprocess, write_json,
)
from .share import (
    ACTIVE_MINUTES, active_lease, ago, changed_files, check_shared, describe_lease, describe_running, drop_request,
    duration, grant_lease, lease_note, lease_requests, others_edits, pie_owner, pie_starter, recent_sessions,
    release_lease, request_lease, running_commands, session_id, session_label, session_names, set_session_name,
    sort_changes, take_lease,
)
from .build import (
    build_timing, classify_changes, compile_check, compile_overlay, csharp_changes, csharp_layout, csharp_reload_build,
    editor_env,
    live_coding_build,
    describe_reflection, live_patches, reflection_changes, stale_code_note,
    uat_user_solution, ubt_build,
)


# ---------------------------------------------------------------- commands

def cmd_status(ctx, args):
    beacon = ctx.beacon
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    if not beacon:
        launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
        if launch.get("pid") and editor_process(launch["pid"]):  # started, but its beacon is not up yet
            dialogs = startup_dialogs(launch["pid"])
            print(f"{name}: editor starting (pid {launch['pid']}, {time.time() - launch.get('started', time.time()):.0f}s "
                  "ago; not loaded yet)" + (f"; waiting on a dialog: \"{dialogs[0][:80]}\"" if dialogs else ""))
            return 2
        print(f"{name}: editor not running (ue open starts one; an editor you open yourself works too, once it "
              "has loaded)")
        return 2
    alive = editor_process(beacon.get("pid"))
    age = time.time() - beacon.get("heartbeat", 0)
    port = beacon.get("mcp_port")
    if not alive:
        state = "not running"
        crash = recent_crash(ctx.project, beacon.get("heartbeat", 0) - 120)
        if crash:
            state += f" (crashed {time.strftime('%H:%M', time.localtime(crash['time']))}: {crash_summary(crash, 100)}; ue crash)"
    elif age > 10:
        dialogs = blocking_dialogs(beacon.get("pid"))
        busy = running_commands(ctx.project)
        if busy and not dialogs:  # a long `ue py` stage holds the game thread: busy, not hung
            state = f"busy: {describe_running(ctx.project, busy[0])} (no heartbeat for {age:.0f}s while it runs)"
        else:
            state = f"HUNG? (no heartbeat for {age:.0f}s" + (f"; blocked by modal: \"{dialogs[0][:80]}\")" if dialogs else ")")
    else:
        owner = pie_owner(ctx.project, beacon)
        state = ("pie" + (f" ({owner})" if owner else "")) if beacon.get("pie") else "ready"
    lease = active_lease(ctx.project)
    if ctx.transport == "remote":
        link = "via python remote execution"
    else:
        link = f"mcp :{port} " + ("up" if alive and port and port_open(port) else "down")
    dirty = beacon.get("dirty") if alive else None
    others = [b for b in live_editors(ctx.project) if b.get("pid") != beacon.get("pid")] if alive else []
    ctx.data = dict(beacon, state=state, alive=bool(alive), heartbeat_age=round(age, 1), lease=lease,
                    others=[{"pid": b.get("pid"), "headless": b.get("headless"),
                             "launched_by_tether": b.get("launched_by_tether")} for b in others])
    print(f"{name} | {state} | UE {beacon.get('engine', '?').split('+')[0]} | {editor_label(beacon)} | "
          f"map {beacon.get('map')} | {link} | heartbeat {age:.1f}s ago"
          + (f" | {dirty} unsaved (ue changes)" if dirty else "")
          + (f" | revision control {beacon['vcs']}" if alive and beacon.get("vcs") else "")
          + (f" | {describe_lease(ctx.project, lease)}" if lease else ""))
    for r in lease_requests(ctx.project) if lease else []:
        print(f"  lease requested by {session_label(ctx.project, r['session'])}: {r['what']} (ue lease grant)")
    if alive and not port and ctx.transport != "remote":
        print("  no MCP server in this editor: run `ue reload` (Tether starts one), or in the editor's console: "
              "ModelContextProtocol.StartServer")
    for other in others:
        print(f"  also open on this project: {editor_label(other)}, map {other.get('map')}. Saving the same assets "
              f"from two editors loses work; `ue --pid {other.get('pid')} ...` talks to that one"
              + (f", `ue close --pid {other.get('pid')}` closes it" if other.get("launched_by_tether") else ""))
    notes = {}  # editors in the same state share a line
    for b in ([beacon] if alive else []) + others:
        stale = stale_code_note(ctx.project, b)
        if stale:
            notes.setdefault(stale, []).append(str(b.get("pid")))
    for stale, pids in notes.items():
        print(f"  pid {pids[0]} {stale}" if len(pids) == 1 else
              f"  pids {', '.join(pids)} " + stale.replace("runs ", "run ", 1))
    return 0 if alive and age <= 10 else 2


OTHERS_MINUTES = 15  # another session's edits this recent may be half-written


def cmd_build(ctx, args):
    if getattr(args, "after_close", False):  # the job `ue build --when-closed` starts: wait, then a normal build
        os.environ.pop("UE_EDITOR_PID", None)  # a job queued before 0.29.3 inherited the --pid of an editor now gone
        print("waiting for every editor on this project to close")
        deadline = time.time() + 24 * 3600
        while live_editors(ctx.project):
            if time.time() > deadline:
                raise CliError("editors still open after 24 h; nothing built", 4)
            time.sleep(10)
        print(f"no editor open ({time.strftime('%H:%M')}): building")
    if getattr(args, "timing", False):
        return build_timing(ctx, args)
    if (getattr(args, "files", None) or getattr(args, "overlay", None)) and not args.check:
        raise CliError("--files and --overlay go with --check: ue build --check --files Source/X/A.cpp | "
                       "ue build --check --overlay")
    if args.check and getattr(args, "overlay", None):
        return compile_overlay(ctx, args.overlay, args.limit, session_id())
    if args.check:
        code = compile_check(ctx, args.limit, getattr(args, "files", None))
        if code is None:
            raise CliError("no game target (Source/<Project>.Target.cs) to check; `ue build` compiles the editor")
        print("  (the game target: editor-only modules and WITH_EDITOR code are not compiled; the editor is untouched)")
        return code
    beacon = ctx.beacon
    running = bool(beacon and editor_process(beacon.get("pid")))
    changes = classify_changes(ctx.project)
    has_csharp = csharp_layout(ctx.project) is not None
    pending_file = os.path.join(state_dir(ctx.project), "pending-reopen.json")
    if not running:
        code = ubt_build(ctx, args) or (uat_user_solution(ctx, args.limit) if has_csharp else 0)
        pending = read_json(pending_file)
        if pending and args.restart and not code:  # the editor a failed `--restart` closed: reopen it now
            os.remove(pending_file)
            return reopen_editor(ctx, args, pending)
        if pending and not code:
            print("note: the editor is still closed from a `ue build --restart` whose build failed: "
                  "ue build --restart (or ue open) reopens it")
        return code
    if not args.restart:
        note = lease_note(ctx.project)
        if note:  # a Live Coding patch changes the code another session's task is running on
            print(note)
        if changes and changes[1]:
            names = ", ".join(os.path.basename(p) for p in changes[1])
            print(f"restart required: module/project files changed ({names}); run `ue build --restart`")
            return 3
        did = False
        if (changes is None or changes[0]) and not live_coding_available():
            print("restart required: Live Coding is Windows-only, so C++ changes on this host need "
                  "`ue build --restart`")
            return 3
        if changes and changes[0] and not getattr(args, "include_others", False):
            # A Live Coding patch compiles every changed file, another session's half-written ones included
            theirs = others_edits(ctx.project, changes[0], minutes=OTHERS_MINUTES)
            if theirs:
                names = session_names(ctx.project)
                print("Live Coding would also compile files another session edited in the last "
                      f"{OTHERS_MINUTES} min and may still be writing:")
                for path, sid, when in theirs[:8]:
                    print(f"  {os.path.basename(path)}: {session_label(ctx.project, sid, names)}, {ago(time.time() - when)}")
                print("  ask that session (ue who), wait until it builds, or pass --include-others")
                return 5
        if changes and changes[2]:
            # New UFUNCTIONs/UPROPERTYs patched into a class with live instances: Live Coding reinstances the class,
            # and a world subsystem's next GetSubsystem<T> asserts (the next PIE crashed an editor)
            diffs = reflection_changes(ctx.project, changes[2])
            if diffs and not getattr(args, "live", False):
                print("restart required: reflected declarations changed (what UHT sees, not function bodies):")
                for line in describe_reflection(diffs):
                    print(f"  {line}")
                print("  Live Coding would patch them into classes with live instances (a new UFUNCTION on a world "
                      "subsystem crashed the next PIE). `ue build --restart`; `ue build --live` patches anyway "
                      "(then `pie start` refuses until a restart)")
                return 3
            args.reflection = describe_reflection(diffs or [], limit=2)
            args.reflection_known = diffs is not None
        if changes is None or changes[0]:  # C++ changed, or no baseline yet
            code = live_coding_build(ctx, args, changes)
            if code:
                return code
            did = True
        cs = csharp_changes(ctx.project)
        if has_csharp and (cs is None or cs):
            code = csharp_reload_build(ctx, args)
            if code:
                return code
            did = True
        if not did:
            print("nothing to build: no C++ or C# changes since the last green build")
        return 0
    check_shared(ctx.project, "build --restart", destructive=True, ignore=args.ignore_lease)
    launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
    reopen = {"map": beacon.get("map") if beacon.get("map") and not beacon["map"].startswith("/Temp") else None,
              "started": time.time()}
    if "nullrhi" in beacon:  # how this editor ran (a hand-opened one is windowed), from its beacon
        # The editor's own flags: launch.json describes the last editor `ue open` started, which may not be this one
        # (a hand-opened windowed editor must not come back -unattended).
        unattended = beacon["unattended"] if beacon.get("unattended") is not None else (
            launch.get("unattended") if beacon.get("headless") else False)
        reopen.update(headless=bool(beacon.get("headless")), render=not beacon.get("nullrhi"),
                      sound=bool(beacon.get("sound")), unattended=unattended)
    else:
        reopen.update(headless=launch.get("headless", False), render=launch.get("render", False),
                      sound=launch.get("sound", False), unattended=launch.get("unattended"))
    if launch.get("pid") == beacon.get("pid") and launch.get("size"):  # `ue open --size`: the same window again
        reopen["size"] = launch["size"]
    if changes is None or changes[0]:
        # Compile before closing: a build that fails after the close leaves every session sharing the editor without one.
        code = None if args.no_check else compile_check(ctx, args.limit)
        if code:
            print("  editor left open: the restart's build would fail (errors above). Fix them and run it again; "
                  "--no-check skips this check")
            return code
    cmd_close(ctx, argparse.Namespace(save=args.save, discard=args.discard, force=False, ignore_lease=True))
    os.environ.pop("UE_EDITOR_PID", None)  # --pid named the editor to close; it is gone now
    code = ubt_build(ctx, args) or (uat_user_solution(ctx, args.limit) if has_csharp else 0)
    if code:
        write_json(pending_file, reopen)
        print("  editor left closed because the build failed; fix the errors and run `ue build --restart` again "
              "(it reopens the editor)")
        return code
    return reopen_editor(ctx, args, reopen)


def reopen_editor(ctx, args, how):
    """Open the editor again after a restart build, as it ran before (map, headless or windowed, rendering)."""
    flags = (("headless" if how.get("headless") else "windowed", True),
             ("offscreen render", how.get("headless") and how.get("render")),
             ("sound", how.get("headless") and how.get("sound")), ("unattended", how.get("unattended")))
    print("reopening the editor as it ran: " + ", ".join(name for name, on in flags if on))
    try:
        code = cmd_open(ctx, argparse.Namespace(map=how.get("map"), headless=how.get("headless", False),
                                                render=how.get("render", False), sound=how.get("sound", False),
                                                size=how.get("size"), unattended=how.get("unattended"), no_build=True,
                                                limit=args.limit, wait=600, no_wait=False))
    except CliError as exc:  # the build went through; the editor did not start
        code = exc.code or 2
    # The last line says whether the editor is really back: a restart that ends on the build's line with no
    # editor running sends every session sharing it looking for what happened
    back = editor_live(ctx.project)
    total = time.time() - how.get("started", time.time())
    if code or not back:
        why = startup_failure(ctx.project, how.get("started", time.time()) - 5)
        log = newest_log(ctx.project, how.get("started", time.time()))
        errors = filter_log(log_file_lines(log), level="Error")[-3:] if log else []
        raise CliError(f"the build succeeded, but the editor did not come back ({total:.0f}s): "
                       + (why if why else "; ".join(errors) if errors else "no crash report and no errors in its log")
                       + ". `ue open` starts it again", 2)
    print(f"restart cycle: {total:.1f}s total; editor pid {back.get('pid')} ready on {back.get('map')}")
    return code


def cmd_open(ctx, args):
    beacon = ctx.beacon
    second = getattr(args, "second", False)
    if second and not live_editors(ctx.project):
        print("note: no editor is open on this project; starting an ordinary one (--second is for a private editor "
              "next to a shared one)")
        second = False
    elif beacon and editor_process(beacon.get("pid")) and not second:  # attach to the open editor, the user's own included
        print(f"already open ({editor_label(beacon)}); using it"
              + ("" if beacon.get("owner") in (None, actor_id()) else
                 " (another session's private editor: `ue open --second` starts your own)"))
        return cmd_status(ctx, args)
    if second:
        # While it runs, Windows keeps the files of every package it loaded open, so the shared editor cannot save
        # those packages ("Error moving file"): not while that editor holds someone's unsaved work
        dirty = 0
        try:
            dirty = int(str(ctx.tether("py", code="U = unreal.EditorLoadingAndSavingUtils\n"
                                                   "len(U.get_dirty_content_packages()) + len(U.get_dirty_map_packages())"))
                        .strip().splitlines()[-1])
        except (CliError, ValueError, IndexError):
            pass
        held = active_lease(ctx.project)
        busy = [f"{dirty} unsaved package(s)"] if dirty else []
        busy += [f"it is {describe_lease(ctx.project, held)}"] if held and held["session"] != session_id() else []
        if busy and not getattr(args, "force", False):
            raise CliError(f"the shared editor has {' and '.join(busy)}: while a second editor runs, the shared one "
                           "cannot save the packages the second one loaded (Windows keeps their files open), so its "
                           "saves fail. Wait until that work is saved, or pass --force")
        print("note: while this editor runs, the shared editor cannot save packages it has loaded (their files stay "
              "open); close it (`ue close`) as soon as you are done")
        # Its own MCP port, beacon and log; nobody else's default; outside the shared editor's lease. Headless:
        # a second window of the same project on the user's screen would only confuse.
        args.headless, args.no_build = True, True  # the binaries are the open editor's: nothing can be rebuilt
    has_csharp = csharp_layout(ctx.project) is not None
    if not live_editors(ctx.project):
        done, _ = flush_deletes(ctx.project)
        if done:
            print(f"deleted {len(done)} file(s) the last editor kept open (ue fab remove)")
    last = (beacon or {}).get("heartbeat", 0)
    crash = recent_crash(ctx.project, last - 120) if last else None
    if crash:  # the previous session ended in a crash: say so before starting a new one
        print(f"note: the last session crashed {time.strftime('%H:%M', time.localtime(crash['time']))}: "
              f"{crash_summary(crash)} (ue crash)")
    env = editor_env(ctx.project)  # fails fast, before the prebuild, when UnrealSharp could not start
    if not args.no_build:  # a stale module would raise the rebuild prompt (and quits headless editors)
        if os.path.isdir(os.path.join(os.path.dirname(ctx.project), "Source")):
            if ubt_build(ctx, argparse.Namespace(target=None, config="Development", limit=args.limit)):
                raise CliError("C++ build failed; not launching (fix the errors above, or pass --no-build)")
        if has_csharp and uat_user_solution(ctx, args.limit):
            raise CliError("C# build failed; not launching (fix the errors above, or pass --no-build)")
    exe = editor_executable(engine_root(ctx.project))
    port = pick_port(ctx.project)
    cmd = [exe, ctx.project] + ([args.map] if args.map else []) + [
        "-nosplash", "-ModelContextProtocolStartServer", f"-ModelContextProtocolPort={port}",
        "-TetherLaunched"]  # marks the editors the CLI started, so it never closes one the user opened
    shared_log = None
    if second:
        cmd += [f"-TetherOwner={actor_id()}"]
        shared_log = (beacon or {}).get("log")
        shared_log = (shared_log, os.path.getsize(shared_log)) if shared_log and os.path.isfile(shared_log) else None
    # UnrealSharp disables C# hot reload under -unattended, so headless UnrealSharp editors stay
    # attended; the bridge suppresses dialogs per operation instead.
    unattended = args.unattended if args.unattended is not None else bool(args.headless and not has_csharp)
    if args.headless:
        # Agent-only session: never block on the "restore unsaved packages?" prompt.
        # --render keeps a GPU renderer with no window (-RenderOffscreen), for `ue shot`; plain headless
        # skips rendering entirely (-nullrhi), which boots faster and leaves the viewport zero-sized.
        cmd += ["-RenderOffscreen" if args.render else "-nullrhi", "-AutoDeclinePackageRecovery"]
        if getattr(args, "sound", False):  # a real audio device (for ue audio) that stays silent: PIE at volume 0, editor at 0
            cmd += ["-ini:EditorPerProjectUserSettings:[/Script/UnrealEd.LevelEditorPlaySettings]:EnableGameSound=False",
                    "-ini:EditorPerProjectUserSettings:[/Script/UnrealEd.LevelEditorMiscSettings]:EditorVolumeLevel=0"]
        else:
            cmd += ["-nosound"]
        if not unattended:
            # Attended for UnrealSharp, but nobody can see a dialog: log each one and take its default.
            cmd += ["-RUNNINGUNATTENDEDSCRIPT"]
    if unattended:
        cmd += ["-unattended"]
    size = getattr(args, "size", None)
    if size:
        # Offscreen (-RenderOffscreen), the "display" is r.SetRes as the editor starts and the main window fills it;
        # the project's saved GameUserSettings resolution (often windowed fullscreen at the desktop's size) would
        # replace it, so that is set for this run too. Nothing is written to the project's config.
        width, height = parse_res(size)
        section = "-ini:GameUserSettings:[/Script/Engine.GameUserSettings]:"
        cmd += [f"-ini:Engine:[SystemSettings]:r.SetRes={width}x{height}", f"{section}ResolutionSizeX={width}",
                f"{section}ResolutionSizeY={height}", f"{section}LastUserConfirmedResolutionSizeX={width}",
                f"{section}LastUserConfirmedResolutionSizeY={height}", f"{section}FullscreenMode=2",
                f"{section}LastConfirmedFullscreenMode=2", f"{section}PreferredFullscreenMode=2"]
    remote = ctx.transport == "remote"
    if remote:  # no MCP server before 5.8: switch on the Python plugin's remote execution for this run
        cmd += ["-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=True"]
    write_json(os.path.join(state_dir(ctx.project), "launch.json"),
               {"headless": bool(args.headless), "render": bool(args.render or not args.headless),
                "sound": bool(getattr(args, "sound", False) or not args.headless), "unattended": unattended, "port": port,
                "size": getattr(args, "size", None),
                "pid": None})
    started = time.time()
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})  # outlive this CLI process either way
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **detach)
    launch_file = os.path.join(state_dir(ctx.project), "launch.json")
    write_json(launch_file, dict(read_json(launch_file) or {}, pid=proc.pid, started=started))  # `ue status` while it boots
    mode = ", ".join(m for m, on in (("headless", args.headless), ("offscreen render", args.headless and args.render),
                                     ("unattended", unattended)) if on)
    print(f"launched pid {proc.pid} " + ("(python remote execution)" if remote else f"on mcp :{port}")
          + (f" ({mode})" if mode else ""))
    if args.no_wait:
        return 0
    first_seen = None
    while time.time() - started < args.wait:
        beacon = next((b for b in editor_beacons(ctx.project) if b.get("pid") == proc.pid), None)
        if beacon and beacon.get("pid") == proc.pid and beacon.get("heartbeat", 0) > started and (
                remote or port_open(port)):
            # this editor's own log: a second editor on the project writes <Project>_2.log
            own = beacon.get("log") if beacon.get("log") and os.path.isfile(beacon["log"]) else newest_log(ctx.project, started)
            boot = log_file_lines(own) if own else read_new_log(ctx.project, from_start=True)
            errors = filter_log(boot, level="Error")
            print(f"ready in {time.time() - started:.1f}s | map {beacon.get('map')} | "
                  f"{len(errors)} errors at boot" + (" (ue log --since start --level Error)" if errors else ""))
            if second:
                remember_target(ctx.project, beacon)
                print(f"this session now uses pid {proc.pid} (its own: other sessions keep the shared editor, and "
                      "its work needs no lease); `ue close` closes it, `ue --pid N ...` reaches the other")
            for line in errors[:5]:
                print("  " + line)
            for title, text in logged_dialogs(boot):
                print(f"  dialog answered by default: {title}: {text.splitlines()[0] if text else ''}")
            if shared_log:  # what the shared editor ran into meanwhile (saves failing on files this one loaded)
                meanwhile = filter_log(log_file_lines(*shared_log), level="Error")
                if meanwhile:
                    print(f"meanwhile the shared editor logged {len(meanwhile)} error(s):")
                    for line in meanwhile[:3]:
                        print("  " + line)
            return 0
        if proc.poll() is not None:
            why = startup_failure(ctx.project, started - 5)
            own = newest_log(ctx.project, started)
            tail = [] if why else filter_log(log_file_lines(own) if own else [], level="Error")[-5:]
            raise CliError(f"editor exited during startup (code {proc.returncode})" + (f": {why}" if why else "")
                           + "".join("\n  " + l for l in tail))
        dialogs = startup_dialogs(proc.pid)
        first_seen = (first_seen or time.time()) if dialogs else None
        if dialogs and time.time() - first_seen >= 3:  # a window that stays: the boot waits on it
            # A modal dialog stalls the boot; a headless editor has nobody to answer it.
            if args.headless:
                proc.kill()
            raise CliError("editor stopped on a modal dialog during startup"
                           + (" (killed it: headless)" if args.headless else "; answer it in the editor")
                           + "".join(f"\n  {d}" for d in dialogs))
        time.sleep(0.5)
    raise CliError(f"not ready after {args.wait:.0f}s (still booting? ue status)", 4)


def cmd_close(ctx, args):
    explicit = os.environ.get("UE_EDITOR_PID")
    launched = [b for b in live_editors(ctx.project) if b.get("launched_by_tether")]
    if explicit or not launched:
        return close_editor(ctx, args, explicit)
    os.environ["UE_EDITOR_PID"] = str(launched[0]["pid"])  # with the user's editor open too: the one ue open started
    try:
        return close_editor(ctx, args, explicit)
    finally:
        os.environ.pop("UE_EDITOR_PID", None)


def close_editor(ctx, args, explicit):
    beacon = ctx.beacon
    pid = (beacon or {}).get("pid")
    if not pid_alive(pid):
        print("not running")
        return 0
    if beacon.get("launched_by_tether") is False and not explicit:
        raise CliError(f"pid {pid} is an editor you opened yourself, not one `ue open` started; close it in the "
                       f"editor, or pass --pid {pid} to close it from here")
    check_shared(ctx.project, "close", destructive=True, ignore=getattr(args, "ignore_lease", False))
    if not args.force:
        dirty = ctx.tether("py", code=(
            "U = unreal.EditorLoadingAndSavingUtils\n"
            "len(U.get_dirty_content_packages()) + len(U.get_dirty_map_packages())")).strip()
        dirty_count = 0 if dirty in ("", "0") else dirty
        if dirty_count and not (args.save or args.discard):
            raise CliError(f"{dirty_count} unsaved packages: pass --save or --discard (`ue changes` lists them, "
                           "`ue diff <asset>` shows what changed)")
        if args.save and dirty_count:
            ctx.tether("py", code="unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)")
        if args.discard and dirty_count:
            # Quitting would stop on a "Save Content?" modal; dropping the work was requested.
            print(f"discarding {dirty_count} unsaved packages")
        else:
            ctx.tether("py", code="unreal.SystemLibrary.quit_editor()")
            deadline = time.time() + 30
            while time.time() < deadline and pid_alive(pid):
                time.sleep(0.25)
    if pid_alive(pid):
        if not (args.force or args.discard):
            raise CliError("editor did not exit within 30s (modal dialog open?); retry with --force", 4)
        subprocess.run(["taskkill", "/PID", str(pid), "/F"] if os.name == "nt" else ["kill", "-9", str(pid)],
                       capture_output=True)
        # Not a crash: forget its beacon (its own exit cleanup never ran) and note the exit like a clean one.
        forget_beacon(ctx.project, beacon)
        write_json(os.path.join(state_dir(ctx.project), "last-exit.json"), {"pid": pid, "time": time.time(),
                                                                            "terminated": True})
        print("terminated")
        if args.discard and decline_package_restore(ctx.project):
            # Its autosaves would otherwise come back as a "Restore Packages" dialog at the next start.
            print("declined the restore of the discarded packages' autosaves")
    else:
        print("closed")
    if not live_editors(ctx.project):
        done, left = flush_deletes(ctx.project)
        if done or left:
            print(f"deleted {len(done)} file(s) the editor kept open (ue fab remove)"
                  + (f"; {len(left)} still locked (deleted at the next ue open)" if left else ""))
    patches = live_patches(ctx.project)
    if patches:
        print(f"note: {patches} Live Coding patch(es) since the last UBT build lived only in the editors open then: "
              "`ue open` builds them first, but an editor opened from the launcher or the .uproject loads the older binaries. "
              "Handing the project over? Run `ue build` now")
    return 0


def cmd_lease(ctx, args):
    """`ue lease take --for WHAT [--minutes N]` / `release` / (no action) show: an advisory lock on the shared
    editor that `build --restart`, `close` and `pie start` respect, and `status`, `py` and `exec` mention."""
    if args.name:
        set_session_name(ctx.project, args.name)
    if args.action == "take":
        if not args.what:
            raise CliError('say what the editor is for: ue lease take --for "map rebuild + PIE"')
        lease, renewed = take_lease(ctx.project, args.what, args.minutes)
        print(("renewed: " if renewed else "taken: ") + describe_lease(ctx.project, lease)
              + "; `ue lease release` when done")
        others = recent_sessions(ctx.project, ACTIVE_MINUTES, exclude=session_id())
        if others:
            print("  other sessions active in the last few minutes (they see it in `ue status`): "
                  + ", ".join(session_label(ctx.project, s) for s in others))
        return 0
    if args.action == "release":
        held = release_lease(ctx.project, force=args.force)
        print(f"released ({held.get('what')}, held {duration(time.time() - held['since'])})" if held
              else "no lease was held")
        waiting = lease_requests(ctx.project)
        if waiting:
            print(f"  {session_label(ctx.project, waiting[0]['session'])} asked for it ({waiting[0]['what']}): "
                  "`ue lease grant` hands it over in their name")
        return 0
    if args.action == "request":
        if not args.what:
            raise CliError('say what you need the editor for: ue lease request --for "new UCLASSes: build --restart"')
        held = request_lease(ctx.project, args.what, args.minutes)
        if not held:
            drop_request(ctx.project, session_id())
            raise CliError("nobody holds the lease: take it (ue lease take --for ...)")
        print(f"asked: the editor is {describe_lease(ctx.project, held)}. That session sees your request on its next "
              "`ue` command; once it grants it, the lease is yours (`ue wait lease` returns then, or when the editor is free)")
        return 0
    if args.action == "grant":
        lease = grant_lease(ctx.project, args.to)
        print(f"granted to {session_label(ctx.project, lease['session'])}: {lease['what']} "
              f"({max(1, round((lease['until'] - lease['since']) / 60))} min); they see it on their next `ue` command")
        return 0
    if args.action == "pending":  # for scripts: `ue lease pending && ue lease yield --back`
        rows = lease_requests(ctx.project)
        ctx.data = {"requests": rows}
        for r in rows:
            print(f"requested by {session_label(ctx.project, r['session'])} {ago(time.time() - r['time'])}: {r['what']}")
        if not rows:
            print("nobody is waiting for the editor")
        return 0 if rows else 1
    if args.action == "yield":
        return yield_lease(ctx, args)
    lease = active_lease(ctx.project)
    ctx.data = {"lease": lease, "requests": lease_requests(ctx.project)}
    print(describe_lease(ctx.project, lease) if lease else "no lease: the editor is free (ue lease take --for ...)")
    for r in ctx.data["requests"]:
        print(f"  requested by {session_label(ctx.project, r['session'])} {ago(time.time() - r['time'])}: {r['what']}")
    return 0


def yield_lease(ctx, args):
    """`ue lease yield [--back]`, for scripts between suites: hand the lease to the oldest request, if any; with
    --back, ask for it again and wait until it is this session's again (granted back, or free and taken)."""
    me, held = session_id(), active_lease(ctx.project)
    rows = lease_requests(ctx.project)
    if not rows:
        print("nobody is waiting for the editor" + ("; the lease stays yours" if held and held["session"] == me else ""))
        return 0
    lease = grant_lease(ctx.project)
    print(f"yielded to {session_label(ctx.project, lease['session'])}: {lease['what']}")
    if not args.back:
        return 0
    what = args.what or (held or {}).get("what") or "back after yielding"
    request_lease(ctx.project, what, args.minutes)
    print(f"asked for it back ({what}); waiting")
    sys.stdout.flush()
    started = time.time()
    deadline = started + (getattr(args, "timeout", None) or 4 * 3600)
    while time.time() < deadline:
        now_held = active_lease(ctx.project)
        if now_held and now_held["session"] == me:
            print(f"the lease is yours again after {duration(time.time() - started)}: {describe_lease(ctx.project, now_held)}")
            return 0
        if not now_held:
            lease, _ = take_lease(ctx.project, what, args.minutes)
            print(f"the editor is free again after {duration(time.time() - started)}; taken: "
                  + describe_lease(ctx.project, lease))
            return 0
        time.sleep(2)
    raise CliError(f"the lease did not come back within {duration(time.time() - started)} (ue lease; --timeout "
                   "waits longer)", 4)


def cmd_who(ctx, args):
    """`ue who`: the sessions that used this editor recently (last command, when), the lease and the PIE owner."""
    me = actor_id()
    if args.name:
        set_session_name(ctx.project, args.name)
        print(f"this {'agent' if '/' in me else 'session'} ({me}) is now '{args.name}' to the others")
    now, names = time.time(), session_names(ctx.project)
    recent = recent_sessions(ctx.project, args.minutes, now=now, by="actor")
    ctx.data = {"you": me, "sessions": recent, "lease": active_lease(ctx.project)}
    print(f"sessions using this editor in the last {args.minutes:g} min ({len(recent)}):" if recent
          else f"no `ue` commands in the last {args.minutes:g} min")
    for sid, row in recent.items():
        print(f"  {session_label(ctx.project, sid, names)}{' (you)' if sid == me else ''}: "
              f"{ago(now - row['time'])}, {row['cmd'][:100]}")
    if me not in recent:
        print(f"  {session_label(ctx.project, me, names)} (you): no commands in that time")
    lease = ctx.data["lease"]
    print("lease: " + (describe_lease(ctx.project, lease) if lease else "none"))
    for r in lease_requests(ctx.project, now):
        print(f"  requested by {session_label(ctx.project, r['session'], names)} {ago(now - r['time'])}: {r['what']}")
    for row in running_commands(ctx.project):
        print(f"running now: {describe_running(ctx.project, row, now, names)}")
    owner = pie_owner(ctx.project, ctx.beacon)
    if owner:
        print(f"pie: {owner}")
        for line in pie_starter_lines(ctx.project, ctx.beacon):
            print(f"  {line}")
    if me not in names:
        print(f"you are {me}; `ue who --as NAME` gives this session a name the others see")
    return 0


def pie_starter_lines(project, beacon):
    """How PIE was started, and the shells above that command still running (a loop that keeps restarting PIE)."""
    found = pie_starter(project, beacon)
    if not found:
        return []
    command, shells = found
    lines = [f"by `{command}`"] if command else []
    if shells:
        lines.append("still running above that command (a script or loop that may start PIE again; stop it with "
                     "`taskkill /PID <pid> /T` or `kill <pid>`):")
        lines += [f"  pid {pid}: {cmdline[:160]}" for pid, cmdline in shells]
    return lines


def cmd_git(ctx, args):
    """`ue git stage --mine [--dry-run]`: stage the changed files this session last wrote (the post-edit hook's
    record, else the sessions' transcripts: its Edit/Write calls, and its commands running when a file was
    written), and name the rest: the ones another session also wrote since their last commit, the ones another
    session wrote, the ones several sessions could have written, and the ones nothing explains."""
    if not args.mine:
        raise CliError("ue git stage --mine [--dry-run]: stage what this session wrote (plain git for the rest)")
    rows = changed_files(ctx.project)
    if rows is None:
        raise CliError("the project is not in a git repository")
    split = sort_changes(ctx.project, rows)
    ctx.data = split
    rel = lambda p: os.path.relpath(p, os.path.dirname(ctx.project)).replace("\\", "/")
    me = session_label(ctx.project, session_id())
    if split["mine"] and not args.dry_run:
        for i in range(0, len(split["mine"]), 50):
            run_git(ctx.project, "add", "--", *split["mine"][i:i + 50])
    verb = "would stage" if args.dry_run else "staged"
    via = ", ".join(f"{n} from the {how}" for how, n in split.get("via", {}).items())
    print(f"{verb} {len(split['mine'])} file(s) {me} wrote last" + (f" ({via})" if via else "")
          + (":" if split["mine"] else ""))
    for path in split["mine"][: max(args.limit, 200)]:
        print(f"  {rel(path)}")
    if len(split["mine"]) > max(args.limit, 200):
        print(f"  ... +{len(split['mine']) - max(args.limit, 200)} more")
    if split["shared"]:
        print(f"not staged, also written by another session since the last commit ({len(split['shared'])}): "
              "`git add -p PATH` takes your hunks only")
        for path, others in split["shared"]:
            print(f"  {rel(path)}  (also {', '.join(session_label(ctx.project, s) for s in others)})")
    if split["theirs"]:
        print(f"not staged, another session wrote them last ({len(split['theirs'])}):")
        for path, sid in split["theirs"][: args.limit]:
            print(f"  {rel(path)}  ({session_label(ctx.project, sid)})")
    if split.get("unsure"):
        print(f"not staged, written while several sessions were running commands ({len(split['unsure'])}): "
              "stage the ones you made yourself")
        for path, sids in split["unsure"][: args.limit]:
            print(f"  {rel(path)}  ({', '.join(session_label(ctx.project, s) for s in sids)})")
    if split["unknown"]:
        print(f"not staged, written when no session was running a command ({len(split['unknown'])}): the editor "
              "(autosave, a hand edit), a background job, or the user; stage the ones you made yourself")
        for path in split["unknown"][: args.limit]:
            print(f"  {rel(path)}")
        if len(split["unknown"]) > args.limit:
            print(f"  ... +{len(split['unknown']) - args.limit} more (--limit)")
    return 0


def cmd_crash(ctx, args):
    reports = crash_reports(ctx.project)
    if not reports:
        print("no crash reports in Saved/Crashes")
        return 0
    if args.list:
        for report in reports[: args.limit]:
            print(time.strftime("%Y-%m-%d %H:%M", time.localtime(report["time"])) + f"  {report['type']:<9} "
                  f"{crash_summary(report, 110)}  ({os.path.basename(report['folder'])})")
        return 0
    print(describe_crash(ctx.project, reports[min(args.index, len(reports) - 1)]))
    return 0
