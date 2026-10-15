"""Compiling: UBT, Live Coding, the UnrealSharp C# build, and compiler diagnostics."""
import argparse
import os
import re
import time

from .core import (
    CliError, engine_root, epic_settings_dir, host_platform, live_coding_holders, live_editors, log_end, log_path,
    logged_dialogs, read_json,
    read_new_log, state_dir, subprocess, uat_command, ubt_command, write_json,
)
from .share import file_owner


# ---------------------------------------------------------------- builds

# MSVC/UHT/MSBuild diagnostics, optionally behind a "[Tool] " prefix and before a " [x.csproj]" suffix.
DIAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?P<file>(?:[A-Za-z]:)?[^(:\[]+)\((?P<line>\d+)(?:,\d+)?\)\s*:\s*"
                  r"(?P<sev>fatal error|error|warning)\s*(?P<code>[A-Z]+\d+)?\s*:?\s*(?P<msg>.*?)(?:\s+\[[^\]]+\])?$", re.I)
LINK = re.compile(r"\b(error|warning) (LNK\d+): (.*)")
# clang (Mac/Linux): "/path/File.cpp:12:5: error: message [-Wflag]"; linkers: "ld.lld: error: ..."
CLANG = re.compile(r"^\s*(?P<file>/[^:]+|[A-Za-z]:[^:]+):(?P<line>\d+):(?:\d+:)?\s*(?P<sev>fatal error|error|warning):"
                   r"\s*(?P<msg>.*?)(?:\s+\[(?P<code>-W[\w-]+)\])?$")
POSIX_LINK = re.compile(r"\b(ld(?:\.lld)?|ld64(?:\.lld)?): (error|warning): (.*)")
REFLECT = re.compile(r"\b(UCLASS|USTRUCT|UENUM|UINTERFACE|UPROPERTY|UFUNCTION|GENERATED_BODY)\b")


def ubt_log_path():
    return os.path.join(epic_settings_dir(), "UnrealBuildTool", "Log.txt")


def compact_diagnostics(lines, project_dir):
    """MSVC/clang/UHT/linker output -> unique project-relative lines; engine-header warnings only counted."""
    root = os.path.normcase(os.path.abspath(project_dir))
    seen, errors, warnings, hidden = set(), [], [], 0
    for line in lines:
        m = DIAG.match(line) or CLANG.match(line)
        if m:
            path = os.path.abspath(m.group("file").strip())
            mine = os.path.normcase(path).startswith(root)
            is_error = "error" in m.group("sev").lower()
            if not mine and not is_error:
                hidden += 1
                continue
            where = os.path.relpath(path, project_dir) if mine else path
            text = f"{where}:{m.group('line')}: {(m.group('code') or '').strip()} {m.group('msg').strip()}".replace("  ", " ")
        else:
            m = LINK.search(line)
            if m:
                is_error, text = m.group(1) == "error", f"{m.group(2)} {m.group(3).strip()}"
            else:
                m = POSIX_LINK.search(line)
                if not m:
                    continue
                is_error, text = m.group(2) == "error", f"{m.group(1)}: {m.group(3).strip()}"
        if text not in seen:
            seen.add(text)
            (errors if is_error else warnings).append(text)
    return errors, warnings, hidden


def print_diagnostics(errors, warnings, limit):
    for line in errors[:limit]:
        print("  E " + line)
    for line in warnings[: max(0, limit - len(errors))]:
        print("  W " + line)
    extra = len(errors) + len(warnings) - limit
    if extra > 0:
        print(f"  ... +{extra} more (--limit)")


ERROR_FILE = re.compile(r"^(?P<file>(?!\w:)[^:]+\.(?:h|hpp|cpp|inl|cs)):\d+:", re.I)


def print_error_owners(project, errors, limit=4):
    """Who last wrote each project file with an error: with sessions sharing a project, a build can fail on
    another session's half-written file, and that session is the one to fix it."""
    files = list(dict.fromkeys(m.group("file") for m in (ERROR_FILE.match(e) for e in errors) if m))
    for path in files[:limit]:
        owner = file_owner(project, path)
        if owner:
            print(f"  {os.path.basename(path)}: {owner}")


def classify_changes(project):
    """Source changes since the last green build: (all, module-level, reflected headers)."""
    marker = read_json(os.path.join(state_dir(project), "last-build.json")) or {}
    if "time" not in marker:
        return None
    since, changed = marker["time"], []
    for root, _, files in os.walk(os.path.join(os.path.dirname(project), "Source")):
        changed += [os.path.join(root, f) for f in files if os.path.getmtime(os.path.join(root, f)) > since]
    module = [p for p in changed if p.endswith((".Build.cs", ".Target.cs"))]
    if os.path.getmtime(project) > since:
        module.append(project)
    reflected = []
    for p in changed:
        if p.endswith(".h"):
            with open(p, encoding="utf-8", errors="replace") as f:
                if REFLECT.search(f.read()):
                    reflected.append(p)
    return changed, module, reflected


def mark_green(project, key="time"):
    path = os.path.join(state_dir(project), "last-build.json")
    marker = read_json(path) or {}
    marker[key] = time.time()
    write_json(path, marker)


def csharp_layout(project):
    """(Script dir, UnrealSharp plugin dir) when the project uses UnrealSharp, else None."""
    root = os.path.dirname(project)
    script, plugin = os.path.join(root, "Script"), os.path.join(root, "Plugins", "UnrealSharp")
    if os.path.isdir(plugin) and os.path.isdir(script) and any(f.endswith(".sln") for f in os.listdir(script)):
        return script, plugin
    return None


def managed_output(project):
    return os.path.join(os.path.dirname(project), "Binaries", "Managed", "net10.0")


def editor_env(project):
    """Environment for launching the editor.

    UnrealSharp finds .NET only through a PATH entry containing 'Program Files\\dotnet\\', trailing
    backslash included. Git Bash drops that backslash, and the editor then raises a modal "SDK can't be
    found" dialog from several modules. So restore the backslash, and refuse to launch without an SDK.
    """
    env = dict(os.environ)
    if os.name != "nt" or not csharp_layout(project):
        return env
    entries = [os.path.normpath(e) + "\\" if e and os.path.isfile(os.path.join(e, "dotnet.exe")) else e
               for e in env.get("PATH", "").split(os.pathsep)]
    if not any("program files\\dotnet\\" in e.lower() for e in entries):
        default = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet") + "\\"
        if not os.path.isfile(default + "dotnet.exe"):
            raise CliError("UnrealSharp needs the .NET 10 SDK in 'Program Files\\dotnet\\' (it only looks on PATH "
                           "there); not found, and the editor would stop on modal dialogs. Install the SDK.", 2)
        entries.insert(0, default)
    env["PATH"] = os.pathsep.join(entries)
    return env


def csharp_changes(project):
    """C# sources changed since the last green C# build; None if unknown or not an UnrealSharp project."""
    layout = csharp_layout(project)
    marker = read_json(os.path.join(state_dir(project), "last-build.json")) or {}
    if not layout or "cs_time" not in marker:
        return None
    changed = []
    for root, dirs, files in os.walk(layout[0]):
        dirs[:] = [d for d in dirs if d not in ("bin", "obj")]
        changed += [os.path.join(root, f) for f in files if f.endswith((".cs", ".csproj"))
                    and os.path.getmtime(os.path.join(root, f)) > marker["cs_time"]]
    return changed


def uat_user_solution(ctx, limit):
    """Out-of-editor C# build through UnrealSharp's own UAT action (also emits the load-order manifest)."""
    _, plugin = csharp_layout(ctx.project)
    uat = uat_command(engine_root(ctx.project))
    started = time.time()
    proc = subprocess.run(uat + ["BuildUserSolution", "-ScriptDir=" + os.path.join(plugin, "Build", "Scripts"),
                           "-Project=" + ctx.project, "-OutputPath=" + managed_output(ctx.project),
                           "-TargetConfiguration=Development", "-clp=ErrorsOnly"],
                          capture_output=True, text=True, errors="replace")
    errors, warnings, _ = compact_diagnostics((proc.stdout + proc.stderr).splitlines(), os.path.dirname(ctx.project))
    ok = proc.returncode == 0
    print(f"c# build: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s" + ("" if ok else f" ({len(errors)} errors)"))
    print_diagnostics(errors, warnings, limit)
    if ok:
        mark_green(ctx.project, "cs_time")
    return 0 if ok else 1


def csharp_reload_build(ctx, args):
    """In-editor C# hot reload through the TetherUnrealSharp bridge (no modal dialogs)."""
    log = log_path(ctx.project)
    start, started = log_end(ctx.project), time.time()
    while ctx.tether("cs_state") == "idle" and time.time() - started < 3:
        time.sleep(0.2)  # give the directory watcher a moment to register the saves

    def new_log():
        with open(log, "rb") as f:
            f.seek(start)
            return f.read().decode("utf-8", "replace")

    while True:
        state = ctx.tether("cs_reload")
        chunk = new_log()
        if state != "not reloaded" or "C# Compilation Failed" in chunk or time.time() - started > args.wait:
            break
        time.sleep(0.5)  # paused while UnrealSharp finishes its initial C# load
    for _ in range(10):  # the log flushes asynchronously
        if state not in ("reloaded", "not reloaded") or re.search(r"C# Hot Reload completed|C# Compilation Failed", chunk):
            break
        time.sleep(0.15)
        chunk = new_log()
    done = re.search(r"C# Hot Reload completed in ([\d.]+) seconds", chunk)
    if state == "nothing pending" and csharp_changes(ctx.project):
        # Files changed but nothing is pending: UnrealSharp refused them at save time. A syntax error raises
        # its "C# Hot Reload Error" dialog straight from the file watcher, before any reload.
        session = read_new_log(ctx.project, from_start=True, advance=False)
        last_ok = max((i for i, l in enumerate(session) if "C# Hot Reload completed" in l), default=-1)
        rejected = [text for title, text in logged_dialogs(session[last_ok + 1:]) if title == "C# Hot Reload Error"]
        if rejected:
            errors, warnings, _ = compact_diagnostics(rejected[-1].splitlines(), os.path.dirname(ctx.project))
            print(f"c# reload: FAILED (UnrealSharp rejected the saved files; {len(errors)} errors)")
            print_diagnostics(errors or rejected[-1].splitlines()[:1], warnings, args.limit)
            return 1
    if state in ("reloaded", "nothing pending"):
        print(f"c# reload: {'OK' if state == 'reloaded' else 'nothing pending'} in {time.time() - started:.1f}s"
              + (f" (UnrealSharp {done.group(1)}s)" if done else ""))
        mark_green(ctx.project, "cs_time")
        return 0
    # The compiler output arrives inside the suppressed dialog's log line: "... title: X, text: <output>".
    lines = [l.split(", text: ", 1)[1] if "Message dialog closed" in l and ", text: " in l else l
             for l in chunk.splitlines()]
    errors, warnings, _ = compact_diagnostics(lines, os.path.dirname(ctx.project))
    print(f"c# reload: FAILED in {time.time() - started:.1f}s ({state}; {len(errors)} errors)")
    print_diagnostics(errors, warnings, args.limit)
    return 1


def game_target(project):
    """The project's game target name, if it has one (Source/<Project>.Target.cs)."""
    name = os.path.splitext(os.path.basename(project))[0]
    return name if os.path.isfile(os.path.join(os.path.dirname(project), "Source", f"{name}.Target.cs")) else None


def compile_check(ctx, limit):
    """Compile the game target with UBT: its intermediates and binaries are separate from the editor's, so a
    running editor and its Live Coding are left alone (a `-Module=` build of the editor target breaks Live Coding
    until a restart). Misses WITH_EDITOR code and editor-only modules. None when there is no game target."""
    target = game_target(ctx.project)
    if not target:
        return None
    return ubt_build(ctx, argparse.Namespace(target=target, config="Development", limit=limit), check=True)


def ubt_build(ctx, args, check=False):
    root = engine_root(ctx.project)
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    target = args.target or f"{name}Editor"
    started = time.time()
    extra = []
    if check or not live_editors(ctx.project):
        # UBT refuses to build while any editor on this engine has Live Coding on: its mutex is named after the
        # shared UnrealEditor.exe, so another project's editor blocks this one. The check protects a running
        # editor of this target from having its object files rewritten, and none is running.
        extra.append("-NoHotReloadFromIDE")
    proc = subprocess.run(ubt_command(root) + [target, host_platform(), args.config, f"-Project={ctx.project}", "-WaitMutex"]
                          + extra, capture_output=True, text=True, errors="replace")
    lines = (proc.stdout + proc.stderr).splitlines()
    errors, warnings, hidden = compact_diagnostics(lines, os.path.dirname(ctx.project))
    result = next((l.strip() for l in reversed(lines) if l.strip().startswith("Result:")), f"exit {proc.returncode}")
    ok = proc.returncode == 0
    print(("compile check, game target " if check else "ubt ")
          + f"{target}: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s ({result})"
          + (f", {hidden} engine-header warnings hidden" if hidden else ""))
    if any("Unable to build while Live Coding is active" in l for l in lines):
        holders = live_coding_holders(root)
        who = "; ".join(f"{name} (LiveCodingConsole pid {pid}" + (f", editor pid {editor})" if editor else ")")
                        for pid, name, editor in holders)
        print("  Live Coding is active in an editor on this engine (the mutex is per engine install, not per project)"
              + (f": {who}" if who else "") + ". Build through that editor with `ue build`, or close it")
    print_diagnostics(errors, warnings, args.limit)
    if not ok:
        print_error_owners(ctx.project, errors)
    if ok and not check:  # a check builds the game, not the editor this marker is about
        mark_green(ctx.project)
    return 0 if ok else 1


LC_ERROR = re.compile(r"(?:error|fatal error) (?:LNK|C)\d+\s*:")


def live_coding_console_log(project):
    """The Live Coding console's own log: compile and link errors of a patch land there, not in the editor log."""
    try:
        root = engine_root(project)
    except CliError:
        return None
    return os.path.join(root, "Engine", "Programs", "LiveCodingConsole", "Saved", "Logs", "LiveCodingConsole.log")


def live_coding_errors(lines):
    """The compile/link errors in Live Coding console log lines, de-duplicated, mangled names dropped; plus a hint
    for unresolved externals (usually a module missing from the Build.cs dependencies)."""
    errors = []
    for line in lines:
        text = re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", line).strip()
        if not LC_ERROR.search(text):
            continue
        text = re.sub(r"\s*\((?:__imp_)?\?[^)]*\)", "", text)  # (?Get@IPlatformInputDeviceMapper@@...) mangled names
        text = re.sub(r"\b__declspec\(dllimport\)\s*", "", text)
        if text not in errors:
            errors.append(text)
    hints = []
    if any("LNK2019" in e or "LNK2001" in e for e in errors):
        hints.append("unresolved external: the module that defines it is probably missing from your Build.cs "
                     "dependencies (the type's header tells which module; add it, then ue build --restart)")
    return errors, hints


UNITY_COMPILE = re.compile(r"\] Compile \[\w+\] (Module\.[\w.]+\.cpp)")


def live_coding_build(ctx, args, changes):
    log, ubt_log = log_path(ctx.project), ubt_log_path()
    start, ubt_mtime = log_end(ctx.project), os.path.getmtime(ubt_log) if os.path.exists(ubt_log) else 0
    lc_log = live_coding_console_log(ctx.project)
    lc_start = os.path.getsize(lc_log) if lc_log and os.path.isfile(lc_log) else 0
    started = time.time()
    ctx.tether("console", command="LiveCoding.Compile")
    result = None
    while time.time() - started < args.wait and result is None:
        time.sleep(0.25)
        with open(log, "rb") as f:
            f.seek(start)
            chunk = f.read().decode("utf-8", "replace")
        m = re.search(r"LogLiveCoding: (?:\w+: )?((?:Live coding (?:succeeded|failed)|No changes)[^\r\n]*)", chunk)
        result = m.group(1) if m else None
    if result is None:
        raise CliError(f"no Live Coding result within {args.wait:.0f}s (ue log --cat LogLiveCoding)", 4)
    ok = not result.startswith("Live coding failed")
    # The result reaches the editor log before the UBT and Live Coding console logs are on disk: a failure with
    # no errors found yet gets a few more reads.
    retry_until = time.time() + (5 if not ok else 0)
    while True:
        errors, warnings, unity, lc_lines = [], [], [], []
        if os.path.exists(ubt_log) and os.path.getmtime(ubt_log) > ubt_mtime:
            with open(ubt_log, encoding="utf-8", errors="replace") as f:
                ubt_lines = f.read().splitlines()
            errors, warnings, _ = compact_diagnostics(ubt_lines, os.path.dirname(ctx.project))
            unity = sorted(set(UNITY_COMPILE.findall("\n".join(ubt_lines))))
        link_errors, hints = [], []
        if not ok and not errors and lc_log and os.path.isfile(lc_log):  # a failed link: only its console knows
            with open(lc_log, "rb") as f:
                f.seek(lc_start if os.path.getsize(lc_log) >= lc_start else 0)
                lc_lines = f.read().decode("utf-8", "replace").splitlines()
            link_errors, hints = live_coding_errors(lc_lines)
        if ok or errors or link_errors or time.time() > retry_until:
            break
        time.sleep(0.5)
    print(f"live coding: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s"
          + ("" if ok else f" ({len(errors) + len(link_errors)} errors)"))
    print_diagnostics(errors, warnings, args.limit)
    print_error_owners(ctx.project, errors)
    for line in link_errors[: args.limit]:
        print(f"  {line}")
    for hint in hints:
        print(f"  hint: {hint}")
    if not ok and not errors and not link_errors:
        first = next((l for l in lc_lines if re.search(r"\berror\b", l, re.I)), None)
        if first:
            print("  first error line in the Live Coding console log: "
                  + re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", first).strip())
        print(f"  (no compiler or linker errors parsed; the Live Coding console log is {lc_log})")
    if unity and time.time() - started > 15:
        print(f"  note: Live Coding recompiled whole-module unity file(s) ({', '.join(unity)}). Setting "
              "`bUseUnity = false;` in those modules' Build.cs makes it compile only the changed files "
              "(measured on a mid-size game: 30 s -> 6 s per change)")
    if ok and changes and changes[2]:
        names = ", ".join(os.path.basename(p) for p in changes[2][:4])
        print(f"  note: reflected headers changed ({names}). Function bodies and new UFUNCTIONs are live "
              "(call new ones via cdo.call_method('Name')); new UPROPERTYs, layout/CDO changes, Python "
              "wrappers and Blueprint use need `ue build --restart`")
    if ok:
        mark_green(ctx.project)
    return 0 if ok else 1
