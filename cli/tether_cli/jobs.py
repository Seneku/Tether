"""Background jobs (ue job) and packaging (ue package)."""
import argparse
import os
import re
import sys
import time

from .core import (
    CliError, UE_SCRIPT, describe_crash, engine_root, filter_log, host_platform, pid_alive, read_json,
    recent_crash, state_dir, subprocess, uat_command, write_json,
)
from .build import compact_diagnostics, editor_env, print_diagnostics


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


def boot_test(ctx, exe, seconds, nullrhi=False, strict=False):
    """Run a packaged game headless for a while and report crashes, fatal errors and logged errors."""
    project_name = os.path.splitext(os.path.basename(ctx.project))[0]
    game_dir = os.path.join(os.path.dirname(exe), project_name)
    fake_project = os.path.join(game_dir, project_name + ".uproject")  # crash_reports looks next to a .uproject
    log = os.path.join(game_dir, "Saved", "Logs", project_name + ".log")
    started = time.time()
    flags = ["-nullrhi"] if nullrhi else ["-RenderOffscreen"]
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


def cmd_package(ctx, args):
    """Package the game (BuildCookRun as a background job), then boot-test the result."""
    platform = args.platform or host_platform()
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
    if args.no_boot:
        print(f"packaged: {exe}")
        return 0
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
