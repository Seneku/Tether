"""Editor lifecycle: status, open, close, build, crash; the shared editor's lease and `ue who`."""
import argparse
import os
import time

from .core import (
    CliError, blocking_dialogs, crash_reports, crash_summary, describe_crash, editor_beacons, editor_executable, forget_beacon,
    editor_process,
    decline_package_restore, editor_label, engine_root, filter_log, live_coding_available, live_editors, logged_dialogs,
    pick_port, pid_alive, port_open, read_json, read_new_log, recent_crash, startup_dialogs, state_dir, subprocess,
    write_json,
)
from .share import (
    ACTIVE_MINUTES, active_lease, ago, check_shared, describe_lease, duration, lease_note, others_edits, pie_owner,
    recent_sessions, release_lease, session_id, session_label, session_names, set_session_name, take_lease,
)
from .build import (
    classify_changes, compile_check, csharp_changes, csharp_layout, csharp_reload_build, editor_env, live_coding_build,
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
    if alive and not port and ctx.transport != "remote":
        print("  no MCP server in this editor: run `ue reload` (Tether starts one), or in the editor's console: "
              "ModelContextProtocol.StartServer")
    for other in others:
        print(f"  also open on this project: {editor_label(other)}, map {other.get('map')}. Saving the same assets "
              f"from two editors loses work; `ue --pid {other.get('pid')} ...` talks to that one"
              + (f", `ue close --pid {other.get('pid')}` closes it" if other.get("launched_by_tether") else ""))
    return 0 if alive and age <= 10 else 2


OTHERS_MINUTES = 15  # another session's edits this recent may be half-written


def cmd_build(ctx, args):
    if args.check:
        code = compile_check(ctx, args.limit)
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
    code = cmd_open(ctx, argparse.Namespace(map=how.get("map"), headless=how.get("headless", False),
                                            render=how.get("render", False), sound=how.get("sound", False),
                                            unattended=how.get("unattended"), no_build=True,
                                            limit=args.limit, wait=600, no_wait=False))
    print(f"restart cycle: {time.time() - how.get('started', time.time()):.1f}s total")
    return code


def cmd_open(ctx, args):
    beacon = ctx.beacon
    if beacon and editor_process(beacon.get("pid")):  # attach to the open editor, the user's own included
        print(f"already open ({editor_label(beacon)}); using it")
        return cmd_status(ctx, args)
    has_csharp = csharp_layout(ctx.project) is not None
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
    remote = ctx.transport == "remote"
    if remote:  # no MCP server before 5.8: switch on the Python plugin's remote execution for this run
        cmd += ["-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=True"]
    write_json(os.path.join(state_dir(ctx.project), "launch.json"),
               {"headless": bool(args.headless), "render": bool(args.render or not args.headless),
                "sound": bool(getattr(args, "sound", False) or not args.headless), "unattended": unattended, "port": port,
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
            boot = read_new_log(ctx.project, from_start=True)
            errors = filter_log(boot, level="Error")
            print(f"ready in {time.time() - started:.1f}s | map {beacon.get('map')} | "
                  f"{len(errors)} errors at boot" + (" (ue log --since start --level Error)" if errors else ""))
            for line in errors[:5]:
                print("  " + line)
            for title, text in logged_dialogs(boot):
                print(f"  dialog answered by default: {title}: {text.splitlines()[0] if text else ''}")
            return 0
        if proc.poll() is not None:
            tail = filter_log(read_new_log(ctx.project, from_start=True), level="Error")[-5:]
            raise CliError(f"editor exited during startup (code {proc.returncode})"
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
        return 0
    lease = active_lease(ctx.project)
    ctx.data = lease
    print(describe_lease(ctx.project, lease) if lease else "no lease: the editor is free (ue lease take --for ...)")
    return 0


def cmd_who(ctx, args):
    """`ue who`: the sessions that used this editor recently (last command, when), the lease and the PIE owner."""
    me = session_id()
    if args.name:
        set_session_name(ctx.project, args.name)
        print(f"this session ({me}) is now '{args.name}' to the others")
    now, names = time.time(), session_names(ctx.project)
    recent = recent_sessions(ctx.project, args.minutes, now=now)
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
    owner = pie_owner(ctx.project, ctx.beacon)
    if owner:
        print(f"pie: {owner}")
    if me not in names:
        print(f"you are {me}; `ue who --as NAME` gives this session a name the others see")
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
