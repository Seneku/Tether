"""Background jobs (ue job) and packaging (ue package)."""
import argparse
import os
import re
import sys
import time

from .core import (
    CliError, UE_SCRIPT, describe_crash, editor_executable, engine_root, filter_log, host_platform, pid_alive, process_tree,
    read_json, recent_crash, stamped_name, state_dir, subprocess, uat_command, write_json,
)
from .images import window_png
from .build import compact_diagnostics, editor_env, print_diagnostics
from .assets import ini_value


def jobs_dir(project):
    return os.path.join(state_dir(project), "jobs")


def cmd_job(ctx, args):
    """Long external work (cooks, commandlets, any command) as background jobs with a log and exit code."""
    folder = jobs_dir(ctx.project)
    if args.action == "_run":  # internal: the detached runner that owns the child and records its exit
        meta = read_json(os.path.join(folder, f"{args.id}.json"))
        child = None
        try:  # the editor's environment: UAT starts editors (cooks), and UnrealSharp needs the .NET PATH fix
            env = editor_env(ctx.project)
        except CliError:
            env = None
        with open(os.path.join(folder, f"{args.id}.log"), "w", encoding="utf-8", errors="replace") as log:
            try:
                proc = subprocess.Popen(meta["command"], stdout=log, stderr=subprocess.STDOUT, env=env,
                                        stdin=subprocess.DEVNULL, cwd=os.path.dirname(ctx.project))
                child = proc.pid
                write_json(os.path.join(folder, f"{args.id}.json"), dict(meta, child=child))
                code = proc.wait()
            except OSError as exc:  # e.g. the program does not exist: record it instead of dying silently
                print(f"Error: could not start {meta['command'][0]!r}: {exc}", file=log)
                code = 127
        write_json(os.path.join(folder, f"{args.id}.json"), dict(meta, child=child, exit=code, ended=time.time()))
        return 0
    if args.action in ("start", "cook", "package"):
        if args.action == "package":
            platform = args.platform or host_platform()
            config = getattr(args, "config", None) or "Development"
            command = uat_command(engine_root(ctx.project)) + [
                "BuildCookRun", f"-project={ctx.project}", f"-platform={platform}", f"-clientconfig={config}",
                "-build", "-cook", "-stage", "-pak", "-archive", f"-archivedirectory={package_dir(ctx.project, platform)}",
                "-unattended", "-utf8output", "-nocompileeditor"]
            name = f"package {platform} {config}"
        elif args.action == "cook":
            command = uat_command(engine_root(ctx.project)) + [
                "BuildCookRun", f"-project={ctx.project}", f"-platform={args.platform or host_platform()}",
                "-clientconfig=Development", "-cook", "-skipstage", "-unattended", "-utf8output", "-nocompileeditor"]
            name = f"cook {args.platform or host_platform()}"
        else:
            command = args.job_command  # everything after `--` (split off before argparse)
            if not command:
                raise CliError("give the command after --: ue job start -- <program> <args...>")
            name = args.name or os.path.basename(command[0])
        job_id = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(folder, exist_ok=True)
        write_json(os.path.join(folder, f"{job_id}.json"), {"id": job_id, "name": name, "command": command,
                                                             "started": time.time()})
        runner = [sys.executable, UE_SCRIPT, "--project", ctx.project, "job", "_run", job_id]
        detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                  if os.name == "nt" else {"start_new_session": True})
        proc = subprocess.Popen(runner, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True, **detach)
        write_json(os.path.join(folder, f"{job_id}.json"), dict(read_json(os.path.join(folder, f"{job_id}.json")),
                                                                 runner=proc.pid))
        print(f"job {job_id} started: {name} (ue job wait {job_id}; ue job log {job_id})")
        return 0
    jobs = sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json")) if os.path.isdir(folder) else []
    if args.action == "status" and not args.id:
        if not jobs:
            print("no jobs")
        for job_id in jobs[-args.limit:]:
            print(job_line(folder, job_id))
        return 0
    job_id = args.id or (jobs[-1] if jobs else None)
    if not job_id or job_id not in jobs:
        raise CliError(f"no job '{args.id}' (ue job status)")
    if args.action == "status":
        print(job_line(folder, job_id))
        stuck = job_stuck(folder, job_id) if "running" in job_line(folder, job_id) else None
        if stuck:
            print(f"  warning: {stuck}; `ue job stop {job_id}`")
        return 0
    if args.action == "stop":
        meta = read_json(os.path.join(folder, f"{job_id}.json")) or {}
        for pid in (meta.get("child"), meta.get("runner")):  # the child's tree first (UAT -> editor -> ...)
            if pid and pid_alive(pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    import signal
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except OSError:
                        pass
        if "exit" not in meta:
            write_json(os.path.join(folder, f"{job_id}.json"), dict(meta, exit=-1, ended=time.time(), stopped=True))
        print(f"job {job_id} stopped")
        return 0
    if args.action == "log":
        with open(os.path.join(folder, f"{job_id}.log"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        for line in lines[-args.limit:]:
            print(line)
        return 0
    if args.action == "wait":
        deadline = time.time() + args.wait
        while "exit" not in (read_json(os.path.join(folder, f"{job_id}.json")) or {}):
            if job_line(folder, job_id).split("  ")[2] == "lost":
                raise CliError(f"job {job_id} stopped without recording an exit code (ue job log {job_id})")
            stuck = job_stuck(folder, job_id)
            if stuck:  # nothing will answer it: stop the job rather than let its log fill the disk
                cmd_job(ctx, argparse.Namespace(action="stop", id=job_id, limit=args.limit, project=args.project))
                raise CliError(f"job {job_id} was {stuck}; stopped it")
            if time.time() > deadline:
                raise CliError(f"job {job_id} still running after {args.wait:.0f}s ({job_line(folder, job_id)})", 4)
            time.sleep(1)
        meta = read_json(os.path.join(folder, f"{job_id}.json"))
        with open(os.path.join(folder, f"{job_id}.log"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        errors, warnings, _ = compact_diagnostics(lines, os.path.dirname(ctx.project))
        errors += [l.strip() for l in lines if re.search(r"\bError:", l) and l.strip() not in errors
                   and not l.startswith("LogInit: Display:")][:10]  # skip the commandlet's own error summary
        print(job_line(folder, job_id))
        summary = [m.group(1) for m in (JOB_SUMMARY.search(l) for l in lines) if m]
        for line in summary[-6:]:  # what a commandlet or builder says it did
            print("  " + line.strip())
        print_diagnostics(errors, warnings if args.verbose else [], args.limit)
        if meta["exit"] and not errors:
            for line in lines[-5:]:
                print("  " + line)
        return 0 if meta["exit"] == 0 else 1
    raise CliError(f"unknown job action {args.action}")


def package_dir(project, platform):
    return os.path.join(state_dir(project), "package", platform)


def packaged_executable(project, platform):
    """The archived game's launcher executable (<archive>/<Platform dir>/<Project>[-...].exe), or None."""
    root = package_dir(project, platform)
    name = os.path.splitext(os.path.basename(project))[0]
    found = []
    for dirpath, dirs, files in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep)
        if depth > 1:
            dirs[:] = []
            continue
        for f in files:
            stem, ext = os.path.splitext(f)
            if stem.startswith(name) and (ext.lower() == ".exe" if os.name == "nt" else ext in ("", ".sh")):
                found.append(os.path.join(dirpath, f))
    return min(found, key=len) if found else None


def newest_file(folder, skip=("Saved", "Intermediate", "Binaries", "DerivedDataCache", ".git", "obj", "bin")):
    """(mtime, path) of the most recently changed file under a folder, build output skipped."""
    best = (0.0, None)
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            path = os.path.join(dirpath, f)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best[0]:
                best = (mtime, path)
    return best


def package_staleness(project, exe):
    """A warning when the project's sources changed after the package was made (a packaged run then tests old
    code or content), else None."""
    built = 0.0
    for dirpath, _, files in os.walk(os.path.dirname(exe)):
        for f in files:
            if f.lower().endswith((".exe", ".pak", ".utoc", ".ucas", ".dll", ".so", ".sh")) or "." not in f:
                try:
                    built = max(built, os.path.getmtime(os.path.join(dirpath, f)))
                except OSError:
                    pass
    root = os.path.dirname(project)
    newest = max((newest_file(os.path.join(root, d)) for d in ("Source", "Content", "Config", "Script")
                  if os.path.isdir(os.path.join(root, d))), default=(0.0, None))
    if not built or newest[0] <= built + 1:
        return None
    stamp = time.strftime("%H:%M", time.localtime(built)) if time.time() - built < 86400 else \
        time.strftime("%Y-%m-%d %H:%M", time.localtime(built))
    return (f"the package is from {stamp}, older than your changes ({os.path.relpath(newest[1], root)} at "
            f"{time.strftime('%H:%M', time.localtime(newest[0]))}): this runs the old build; ue package first")


def boot_test(ctx, exe, seconds, nullrhi=False, strict=False):
    """Run a packaged game headless for a while and report crashes, fatal errors and logged errors."""
    project_name = os.path.splitext(os.path.basename(ctx.project))[0]
    game_dir = os.path.join(os.path.dirname(exe), project_name)
    fake_project = os.path.join(game_dir, project_name + ".uproject")  # crash_reports looks next to a .uproject
    log = os.path.join(game_dir, "Saved", "Logs", project_name + ".log")
    started = time.time()
    flags = ["-nullrhi"] if nullrhi else ["-RenderOffscreen"]
    # Garbage collection early and every few seconds: cooked games fail at their first GC on references the editor
    # allows (a settings default object holding a loaded asset: "Disregard for GC object ... not part of root set"),
    # which a short boot otherwise never reaches.
    flags += ["-ExecCmds=obj gc", "-ini:Engine:[/Script/Engine.GarbageCollectionSettings]:gc.TimeBetweenPurgingPendingKillObjects=3"]
    proc = subprocess.Popen([exe, *flags, "-nosound", "-unattended", "-log", "-ResX=640", "-ResY=360", "-windowed"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    exited = None
    while time.time() - started < seconds:
        if proc.poll() is not None:
            exited = proc.returncode
            break
        time.sleep(0.5)
    if exited is None:  # still running: it booted; stop it (and anything it started)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
        proc.wait(timeout=30)
    time.sleep(1)
    lines = []
    if os.path.isfile(log) and os.path.getmtime(log) >= started - 1:
        with open(log, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    shown = filter_log(lines, level="Error")
    crash = recent_crash(fake_project, started - 1)
    fatal = [l for l in lines if re.search(r"\bFatal\b|Assertion failed", l)]
    maps = [m.group(1) for m in (re.search(r"LoadMap: (\S+)", l) for l in lines) if m]
    alive = exited is None
    print(f"boot: {'ran' if alive else f'exited with code {exited} after'} {min(seconds, time.time() - started):.0f}s"
          + (f", loaded {maps[-1].split('?')[0]}" if maps else "") + f", {len(shown)} error line(s)")
    if crash:
        for line in describe_crash(fake_project, crash).splitlines()[:6]:
            print("  " + line)
    for line in shown[:10]:
        print("  " + line)
    if not lines:
        print(f"  (no log at {log})")
    failed = bool(crash or fatal or (not alive and exited != 0) or (strict and shown))
    print(("PASS" if not failed else "FAIL") + f"  {exe}")
    return 1 if failed else 0


def package_processes(project, platform):
    """PIDs of processes running an executable from the package folder (test runs left behind: they lock its
    files, and the next package loops on "file is being used by another process")."""
    root = os.path.normcase(os.path.abspath(package_dir(project, platform)))
    found = []
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, path = line.partition("|")
            if path and os.path.normcase(path).startswith(root) and pid.strip().isdigit():
                found.append(int(pid))
    else:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if os.path.normcase(cmd).startswith(root):
                found.append(int(pid))
    return found


def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)


def cmd_package(ctx, args):
    """Package the game (BuildCookRun as a background job), then boot-test the result."""
    platform = args.platform or host_platform()
    leftovers = package_processes(ctx.project, platform)
    if leftovers:
        for pid in leftovers:
            kill_tree(pid)
        print(f"stopped {len(leftovers)} process(es) still running from the last package (they lock its files)")
    if not args.boot_only:
        cmd_job(ctx, argparse.Namespace(action="package", platform=platform, config=args.config, id=None,
                                        name=None, limit=args.limit, project=args.project, job_command=[]))
        if args.no_wait:
            return 0
        jobs = sorted(f[:-5] for f in os.listdir(jobs_dir(ctx.project)) if f.endswith(".json"))
        code = cmd_job(ctx, argparse.Namespace(action="wait", id=jobs[-1], wait=args.wait, verbose=False,
                                               limit=args.limit, project=args.project))
        if code:
            return code
    exe = packaged_executable(ctx.project, platform)
    if not exe:
        raise CliError(f"no packaged game under {package_dir(ctx.project, platform)} (ue package first)")
    if args.boot_only:
        stale = package_staleness(ctx.project, exe)
        if stale:
            print(f"warning: {stale}")
    if args.no_boot:
        print(f"packaged: {exe}")
        return 0
    if getattr(args, "play", None):  # instead of the boot test: a listen server and clients from this build
        return cmd_game(ctx, argparse.Namespace(action="start", packaged=True, clients=args.play, server="listen", map=None,
                                                game=None, windowed=False, game_port=None, server_args="", client_args="",
                                                wait=120, server_delay=3, platform=platform, limit=args.limit,
                                                checked=True))
    return boot_test(ctx, exe, args.seconds, args.nullrhi, args.strict)


JOB_SUMMARY = re.compile(r"(?:LogInit: Display: |Display: )((?:Success|Failure) - \d+ error\(s\).*|#### (?:Built|World contains) .*)")
COMMANDLET_LOG = re.compile(r"Commandlet log file is (.+)$", re.M)


def job_stuck(folder, job_id):
    """Why a running job looks stuck, or None. UAT runs commandlets (cooks) with their own log; a dialog
    that keeps coming back there (answered by -unattended, raised again) never ends and the log grows
    without bound, so read that log's tail too."""
    log = os.path.join(folder, f"{job_id}.log")
    if not os.path.isfile(log):
        return None
    with open(log, encoding="utf-8", errors="replace") as f:
        match = COMMANDLET_LOG.search(f.read(200000))
    paths = [match.group(1).strip()] if match else []
    for path in paths + [log]:
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
        dialogs = re.findall(r"Message dialog closed, result: \w+, title: [^,]*, text: ([^\r\n]{0,160})", tail)
        if len(dialogs) >= 10 and len(set(dialogs)) <= 2:
            return f"stuck on a repeating dialog ({len(dialogs)}x in the last 64 KB of its log): {dialogs[-1]}"
        if size > 2 * 1024 ** 3:
            return f"its log is {size / 1024 ** 3:.1f} GB ({path})"
    return None


def job_line(folder, job_id):
    meta = read_json(os.path.join(folder, f"{job_id}.json")) or {}
    end = meta.get("ended") or time.time()
    if "exit" in meta:
        state = "done" if meta["exit"] == 0 else f"FAILED (exit {meta['exit']})"
    else:
        alive = pid_alive(meta.get("runner")) or pid_alive(meta.get("child"))
        state = "running" if alive or time.time() - meta.get("started", 0) < 5 else "lost"
    last = ""
    log = os.path.join(folder, f"{job_id}.log")
    if os.path.isfile(log) and state == "running":
        with open(log, "rb") as f:
            f.seek(max(0, os.path.getsize(log) - 400))
            tail = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
            last = f" | {tail[-1].strip()[:90]}" if tail else ""
    return f"{job_id}  {meta.get('name', '?')}  {state}  {end - meta.get('started', end):.0f}s{last}"


GAME_JOIN = re.compile(r"LogNet: Join succeeded: (\S+)")
GAME_WELCOMED = re.compile(r"Welcomed by server|LogNet: Browse: .*\?|Join request")
GAME_LISTENING = re.compile(r"IpNetDriver listening on port (\d+)")


def free_udp_port(start, tries=50):
    """The first UDP port from `start` nobody holds (PIE and other projects' games use 17777 too)."""
    import socket
    for port in range(start, start + tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return start


def listening_port(lines):
    """The port a game server's log says it listens on (the engine moves on to the next one when it is taken)."""
    ports = [int(m.group(1)) for line in lines for m in [GAME_LISTENING.search(line)] if m]
    return ports[-1] if ports else None


def game_dir(project):
    return os.path.join(state_dir(project), "game")


def default_map(project):
    value = ini_value(project, "DefaultEngine.ini", "GameDefaultMap") or ""
    return value.split(".")[0] or None


def game_command(ctx, args, role, index, port, log):
    """The command line of one game process: listen/dedicated server or client, editor binary or packaged."""
    headless = not args.windowed
    if args.packaged:
        exe = packaged_executable(ctx.project, args.platform or host_platform())
        if not exe:
            raise CliError("no packaged game (ue package first)")
        if role == "dedicated":
            raise CliError("a packaged client build has no dedicated server: use --server listen")
        base = [exe]
    else:
        # -DisablePython: the editor plugins' Python startup scripts (Tether's, Epic's toolsets) expect an editor.
        # --python keeps it, as Play > Standalone Game does, so the project's own start-up scripts run (and can crash)
        base = [editor_executable(engine_root(ctx.project)), ctx.project] + ([] if getattr(args, "python", False)
                                                                           else ["-DisablePython"])
    level = args.map or default_map(ctx.project)
    if role in ("listen", "dedicated") and not level:
        raise CliError("no map: pass --map /Game/Maps/MyMap (the project has no GameDefaultMap)")
    if role != "client":
        url = level + ("?listen" if role == "listen" else "") + (f"?game={args.game}" if args.game else "")
    elif getattr(args, "connect", None):
        url = args.connect
    elif getattr(args, "server", "listen") == "none":  # each client plays on its own, on the map
        url = (level or "") + (f"?game={args.game}" if args.game and level else "")
    else:
        url = f"127.0.0.1:{port}"
    mode = ["-server"] if role == "dedicated" else ([] if args.packaged else ["-game"])
    extra = (args.server_args if role != "client" else args.client_args) or ""
    view = ["-nullrhi", "-nosound"] if headless else ["-windowed", "-ResX=960", "-ResY=540", f"-WinX={60 + index * 40}",
                                                       f"-WinY={60 + index * 40}"]
    return base + ([url] if url else []) + mode + [f"-port={port}", "-log", f"-abslog={log}", "-unattended", "-nosplash",
                                 "-NoVerifyGC"] + view + extra.split()


def read_game_log(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


MAP_LOADED = re.compile(r"LogLoad: Took [\d.]+ seconds to LoadMap\(")
GAME_PROBLEM = re.compile(r"Accessed None|Script Msg|Fatal error|Unhandled Exception|Assertion failed|Ensure condition")


def smoke_maps(project, wanted):
    """The maps for `ue game smoke`: the given ones, else the default map and every .umap under Content/ (not
    Developers/ or World Partition's external actors), up to 12."""
    if wanted:
        return [m.strip() for m in wanted.split(",") if m.strip()], 0
    content, found = os.path.join(os.path.dirname(project), "Content"), []
    for here, dirs, files in os.walk(content):
        dirs[:] = [d for d in dirs if d not in ("Developers", "Collections") and not d.startswith("__External")]
        found += ["/Game/" + os.path.relpath(os.path.join(here, f[:-5]), content).replace("\\", "/")
                  for f in files if f.endswith(".umap")]
    first = default_map(project)
    found = ([first] if first else []) + sorted(m for m in found if m != first)
    return found[:12], max(0, len(found) - 12)


def game_smoke(ctx, args):
    """`ue game smoke [--maps a,b] [--seconds 15]`: open each map in a headless `-game` process (the editor
    binary, or --packaged) and play it for a few seconds: what PIE hides shows up there (editor-only components
    are null, start-up scripts differ). Reports errors, Accessed None and crashes per map."""
    maps, skipped = smoke_maps(ctx.project, args.maps)
    if not maps:
        raise CliError("no maps (pass --maps /Game/Maps/A,/Game/Maps/B)")
    folder = os.path.join(game_dir(ctx.project), "smoke")
    os.makedirs(folder, exist_ok=True)
    env = editor_env(ctx.project) if not args.packaged else None
    seconds = args.seconds if args.seconds is not None else 15
    failed, results = 0, []
    print(f"smoke: {len(maps)} map(s), {seconds:g}s each after loading, headless -game"
          + (f" ({skipped} more maps not run: --maps)" if skipped else ""))
    for i, level in enumerate(maps):
        log = os.path.join(folder, f"{i}-{level.rsplit('/', 1)[-1]}.log")
        if os.path.exists(log):
            os.remove(log)
        run = argparse.Namespace(packaged=args.packaged, windowed=False, map=level, game=args.game, server_args="",
                                 client_args="", platform=args.platform, server="none", connect=None, python=args.python)
        proc = subprocess.Popen(game_command(ctx, run, "client", i, free_udp_port(17777), log),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
        started, loaded = time.time(), None
        while time.time() - started < args.wait and proc.poll() is None:
            if loaded is None and any(MAP_LOADED.search(l) for l in read_game_log(log)):
                loaded = time.time()
            if loaded and time.time() - loaded >= seconds:
                break
            time.sleep(0.5)
        exited = proc.poll()
        if exited is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"] if os.name == "nt"
                           else ["kill", "-9", str(proc.pid)], capture_output=True)
        lines = read_game_log(log)
        errors = list(filter_log(lines, level="Error"))
        problems = [l for l in filter_log(lines, level="Warning") if GAME_PROBLEM.search(l)]
        crashed = exited is not None or any("Fatal error" in l for l in lines)
        state = ("CRASHED" if crashed else "no load" if loaded is None else
                 f"{len(errors)} error(s)" if errors or problems else "OK")
        failed += state != "OK"
        took = f"loaded in {loaded - started:.0f}s" if loaded else f"not loaded after {time.time() - started:.0f}s"
        print(f"  {level}: {state} ({took})")
        for line in list(dict.fromkeys(problems + errors))[:4]:
            print(f"    {line[:200]}")
        results.append({"map": level, "state": state, "log": log, "errors": errors[:20], "problems": problems[:20]})
    ctx.data = results
    print(f"{len(maps) - failed} of {len(maps)} map(s) clean" + (f"; logs in {folder}" if failed else ""))
    return 1 if failed else 0


def cmd_game(ctx, args):
    """Multiplayer outside PIE: a server and clients as separate game processes (editor binary, or the packaged
    build), each with its own log; start, status, log, stop."""
    if args.action == "smoke":
        return game_smoke(ctx, args)
    folder = game_dir(ctx.project)
    state_file = os.path.join(folder, "session.json")
    state = read_json(state_file) or {}
    action = args.action or "status"
    if action == "start":
        if state.get("instances") and any(pid_alive(i["pid"]) for i in state["instances"]):
            raise CliError("a game session is running (ue game status; ue game stop first)")
        os.makedirs(folder, exist_ok=True)
        if args.packaged and not getattr(args, "checked", False):
            exe = packaged_executable(ctx.project, args.platform or host_platform())
            stale = exe and package_staleness(ctx.project, exe)
            if stale:
                print(f"warning: {stale}")
        asked = getattr(args, "game_port", None)
        port = asked or free_udp_port(17777)
        roles = ([args.server] if args.server != "none" else []) + ["client"] * args.clients
        env = editor_env(ctx.project) if not args.packaged else None
        instances = []
        started = time.time()
        for index, role in enumerate(roles):
            log = os.path.join(folder, f"{index}-{role}.log")
            if os.path.exists(log):
                os.remove(log)
            command = game_command(ctx, args, role, index, port, log)
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    env=env)
            instances.append({"index": index, "role": role, "pid": proc.pid, "log": log, "command": command})
            if role != "client" and args.clients:
                # Clients go to the port the server really listens on: the engine silently takes the next port
                # when the asked one is busy, and a client knocking on the old one may reach someone else's game.
                time.sleep(args.server_delay)
                deadline = time.time() + max(args.wait, 30)
                while time.time() < deadline and pid_alive(proc.pid):
                    heard = listening_port(read_game_log(log))
                    if heard:
                        if heard != port:
                            print(f"note: port {port} was taken; the server listens on {heard}")
                        port = heard
                        break
                    time.sleep(0.5)
        state = {"started": started, "port": port, "instances": instances, "packaged": bool(args.packaged)}
        write_json(state_file, state)
        server = next((i for i in instances if i["role"] != "client"), None)
        joined, deadline = [], time.time() + args.wait
        while server and args.clients and time.time() < deadline:
            joined = [m.group(1) for line in read_game_log(server["log"]) for m in [GAME_JOIN.search(line)] if m]
            dead = [i for i in instances if not pid_alive(i["pid"])]
            if len(joined) >= args.clients or dead:
                break
            time.sleep(1)
        print(f"game: {len(instances)} process(es) on port {port} ({'packaged' if args.packaged else 'editor binary'}, "
              f"{'windowed' if args.windowed else 'headless'})")
        for i in instances:
            print(f"  {i['index']} {i['role']:<9} pid {i['pid']}  log {i['log']}" + ("" if pid_alive(i["pid"]) else "  EXITED"))
        if server and args.clients:
            ok = len(joined) >= args.clients
            print(f"{len(joined)} of {args.clients} client(s) joined the server" + (f" ({', '.join(joined)})" if joined else "")
                  + ("" if ok else f" within {args.wait:g}s (ue game log 0)"))
            return 0 if ok else 1
        return 0
    if not state.get("instances"):
        raise CliError("no game session (ue game start --clients 1)")
    instances = state["instances"]
    if action == "shot":
        index = args.index if args.index is not None else 0
        inst = next((i for i in instances if i["index"] == index), None)
        if inst is None:
            raise CliError(f"no instance {index} (0..{len(instances) - 1})")
        if not pid_alive(inst["pid"]):
            raise CliError(f"{inst['role']} {index} has exited (ue game status)")
        shot = window_png(set(process_tree(inst["pid"])))  # a packaged launcher's game is its child process
        if shot is None:
            raise CliError(f"{inst['role']} {index} has no window (started headless? ue game start --windowed)")
        title, png = shot
        path = os.path.abspath(args.output) if args.output else stamped_name(
            os.path.join(state_dir(ctx.project), "shots"), f"game{index}", ".png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        print(f"{path}  {int.from_bytes(png[16:20], 'big')}x{int.from_bytes(png[20:24], 'big')}  "
              f"({inst['role']} {index}, window \"{title}\")")
        return 0
    if action == "log":
        index = args.index if args.index is not None else 0
        inst = next((i for i in instances if i["index"] == index), None)
        if inst is None:
            raise CliError(f"no instance {index} (0..{len(instances) - 1})")
        lines = read_game_log(inst["log"])
        shown = filter_log(lines, args.level or ("Log" if args.grep else "Warning"), None, args.grep, consecutive=True)
        print(f"{inst['role']} {index}: {len(shown)} entries of {len(lines)} lines ({inst['log']})")
        for line in shown[-args.limit:]:
            print(line)
        return 0
    if action == "stop":
        for i in instances:
            if pid_alive(i["pid"]):
                kill_tree(i["pid"])
        time.sleep(1)
    crash = recent_crash(ctx.project, state.get("started", 0) - 1) if not state.get("packaged") else None
    worst = 0
    for i in instances:
        lines = read_game_log(i["log"])
        errors = filter_log(lines, "Error")
        joins = sum(1 for line in lines if GAME_JOIN.search(line))
        fatal = [line for line in lines if re.search(r"\bFatal\b|Assertion failed", line)]
        alive = pid_alive(i["pid"])
        state_text = "stopped" if action == "stop" else ("running" if alive else "EXITED")
        print(f"  {i['index']} {i['role']:<9} {state_text}, {len(errors)} error line(s)"
              + (f", {joins} join(s)" if i["role"] != "client" else "") + (f", FATAL: {fatal[0].split(']')[-1].strip()[:120]}" if fatal else ""))
        for line in errors[:3]:
            print(f"      {line[:160]}")
        worst = max(worst, 1 if fatal or (not alive and action != "stop") else 0)
    if crash:
        print("  crash: " + describe_crash(ctx.project, crash).splitlines()[0])
        worst = 1
    if action == "stop":
        print(f"stopped {len(instances)} process(es)")
    return worst
