"""Compiling: UBT, Live Coding, the UnrealSharp C# build, and compiler diagnostics."""
import argparse
import contextlib
import json
import os
import re
import time

from .core import (
    CliError, chosen_editor, engine_root, epic_settings_dir, host_platform, live_coding_holders, live_editors, log_end,
    log_path, logged_dialogs, read_json,
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


def layout_marker(project):
    return os.path.join(state_dir(project), "live-coding-layout.json")


def layout_patch_note(project, beacon):
    """A warning when Live Coding patched reflected headers in the running editor process since it started: class
    layouts may then disagree with the objects in memory, and UMG and replication misbehave until a restart."""
    marker = read_json(layout_marker(project)) or {}
    if not marker or marker.get("pid") != (beacon or {}).get("pid"):
        return None
    return (f"Live Coding patched reflected headers in this editor since it started ({', '.join(marker['headers'][:4])}): "
            "class layouts may not match the objects in memory, so UMG, replication and Blueprints can misbehave "
            "(a HUD that draws nothing). `ue build --restart` makes it clean")


UNRESOLVED = re.compile(r'unresolved external symbol "([^"]+)"|unresolved external symbol (\S+)')
SYMBOL = re.compile(r"\b(\w+)::(~?\w+)\b(?!.*\w+::~?\w+)")  # the symbol's own Class::member: its last one


def unresolved_symbols(errors):
    """[(class, member, imported)] of the unresolved externals in link errors: the missing symbol itself (the quoted
    one), not the function that references it; imported = __declspec(dllimport), so another module's."""
    found = []
    for error in errors:
        m = UNRESOLVED.search(error)
        if not m:
            continue
        symbol = m.group(1) or m.group(2)
        name = SYMBOL.search(re.sub(r"\(.*$", "", symbol)) or SYMBOL.search(symbol)  # a function: before its params
        if name:
            row = (name.group(1), name.group(2), "dllimport" in symbol)
            if row not in found:
                found.append(row)
    return found


def engine_module_of(project, cls):
    """The engine module that defines a class or struct ('InputCore' for EKeys), from its definition in the
    engine's public headers (a few seconds the first time; kept in Saved/Tether/engine-modules.json), or None."""
    cache_path = os.path.join(state_dir(project), "engine-modules.json")
    cache = read_json(cache_path) or {}
    if cls in cache:
        return cache[cls]
    try:
        root = os.path.join(engine_root(project), "Engine", "Source")
    except CliError:
        return None
    # its definition, not a friend or forward declaration (EKeys exports its members, not the struct)
    rx = re.compile(rf"^\s*(?:class|struct)\s+(?:\w+_API\s+)?{re.escape(cls)}\b(?!\s*;)", re.M)
    found = None
    for group in ("Runtime", "Editor", "Developer"):
        base = os.path.join(root, group)
        for folder, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("Private", "Internal", "ThirdParty", "Tests")]
            for name in files:
                if name.endswith(".h") and not found:
                    try:
                        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                            if rx.search(f.read()):
                                found = os.path.relpath(folder, base).split(os.sep)[0]
                    except OSError:
                        pass
            if found:
                break
        if found:
            break
    cache[cls] = found
    write_json(cache_path, cache)
    return found


def print_link_owners(project, errors, limit=4):
    """For unresolved externals, the header that declares the class and who last wrote it: a declaration with no
    definition yet is usually another session's work in progress. A symbol imported from another module (an
    engine type) means a module missing from Build.cs: name it."""
    wanted = unresolved_symbols(errors)
    if not wanted:
        return
    headers = {}
    root = os.path.join(os.path.dirname(project), "Source")
    for folder, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".h"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for cls, _, _ in wanted:
                if cls not in headers and re.search(rf"\b(?:class|struct)\s+(?:\w+_API\s+)?{cls}\b", text):
                    headers[cls] = path
    for cls, member, imported in wanted[:limit]:
        path = headers.get(cls)
        if path and not imported:
            owner = file_owner(project, path)
            print(f"  {cls}::{member}: declared in {os.path.basename(path)}"
                  + (f" ({owner})" if owner else "") + ", no definition linked")
        elif imported or not path:
            module = engine_module_of(project, cls)
            print(f"  {cls}::{member}: from another module's DLL"
                  + (f": add \"{module}\" to the Build.cs dependencies" if module else
                     ": add the module that defines it to the Build.cs dependencies"))


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


def mark_green(project, key="time", live=False, pid=None):
    """A green build. Live Coding patches exist only in the editor process that loaded them (`pid`): count them
    until a UBT build makes them real, so `ue close` can say the code a launcher start would load is older, and
    `ue status` which other editors run without them."""
    path = os.path.join(state_dir(project), "last-build.json")
    marker = read_json(path) or {}
    marker[key] = time.time()
    if key == "time":
        marker["live_patches"] = marker.get("live_patches", 0) + 1 if live else 0
        marker["live_pid"] = pid if live else None
    write_json(path, marker)


def stale_code_note(project, beacon):
    """For an editor that runs without this project's Live Coding patches (another editor got them): a note."""
    marker = read_json(os.path.join(state_dir(project), "last-build.json")) or {}
    patched = marker.get("live_pid")
    if not marker.get("live_patches") or not patched or beacon.get("pid") == patched:
        return None
    return (f"runs the last UBT build, without the {marker['live_patches']} Live Coding patch(es) made in pid "
            f"{patched}: `ue build` once every editor is closed (`ue build --when-closed` waits for that)")


def live_patches(project):
    """Live Coding builds since the last UBT build of the editor target."""
    return (read_json(os.path.join(state_dir(project), "last-build.json")) or {}).get("live_patches", 0)


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


SHADOW_SKIP = ("Binaries", "Intermediate", "Saved", "DerivedDataCache", "Content", ".git", ".vs")


def code_files(project):
    """{relative path: absolute path} of what a UBT build of the project reads: the .uproject, Source/, Config/,
    and each project plugin's folder (UBT plugins and C# build scripts included), build output and content left
    out."""
    root = os.path.dirname(project)
    found = {os.path.basename(project): project}

    def take(folder):
        for here, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SHADOW_SKIP]
            for f in files:
                full = os.path.join(here, f)
                found[os.path.relpath(full, root).replace("\\", "/")] = full
    for part in ("Source", "Config", "Plugins"):
        take(os.path.join(root, part))
    return found


def shadow_project(project, overlay, sid):
    """The project's code copied to Saved/Tether/shadow/<session>, with the files under `overlay` (laid out like
    the project: overlay/Source/Game/X.h) on top, for compiling work that may not be written into Source/ yet
    (another session's Live Coding compiles every changed file there). Kept between runs, so UBT builds it
    incrementally: a file is rewritten only when its content changed (and is then newer than any object file
    built from the old content); unchanged sizes and times are not even read again.
    Returns (shadow .uproject, [overlaid relative paths])."""
    import hashlib
    shadow = os.path.join(state_dir(project), "shadow", sid)
    wanted = code_files(project)
    overlaid = []
    for here, _, files in os.walk(overlay):
        for f in files:
            rel = os.path.relpath(os.path.join(here, f), overlay).replace("\\", "/")
            wanted[rel] = os.path.join(here, f)
            overlaid.append(rel)
    if not overlaid:
        raise CliError(f"nothing under {overlay}: lay files out as in the project (Source/<Module>/File.h)")
    manifest_path = os.path.join(shadow, ".tether-shadow.json")
    manifest = read_json(manifest_path) or {}
    fresh = {}
    for rel, source in wanted.items():
        stat = os.stat(source)
        stamp = [source, stat.st_size, stat.st_mtime]
        old = manifest.get(rel) or {}
        target = os.path.join(shadow, rel)
        if old.get("stamp") == stamp and os.path.isfile(target):
            fresh[rel] = old
            continue
        with open(source, "rb") as f:
            data = f.read()
        if rel.endswith(".uproject"):  # plugin folders outside the project, relative to it, must still resolve
            descriptor = json.loads(data.decode("utf-8-sig"))
            descriptor["AdditionalPluginDirectories"] = [
                os.path.normpath(os.path.join(os.path.dirname(project), d))
                for d in descriptor.get("AdditionalPluginDirectories", [])]
            data = json.dumps(descriptor, indent="\t").encode()
        digest = hashlib.sha1(data).hexdigest()
        fresh[rel] = {"stamp": stamp, "digest": digest}
        if old.get("digest") != digest or not os.path.isfile(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
    for rel in set(manifest) - set(fresh):  # gone from the project (or the overlay): gone from the copy
        with contextlib.suppress(OSError):
            os.remove(os.path.join(shadow, rel))
    write_json(manifest_path, fresh)
    return os.path.join(shadow, os.path.basename(project)), sorted(overlaid)


OVERLAY_HOURS = 12  # a staging folder another session compiled within this long is that session's


def default_overlay(project, sid):
    return os.path.join(state_dir(project), "staging", sid or "user")


def claim_overlay(project, overlay, sid, now=None):
    """The staging folder for this session's overlay, as an absolute path: its own under Saved/Tether/staging when
    none is given. Refuses a folder another session compiled lately (on Windows staging/ and Staging/ are one
    folder): two sessions staging into one folder copy each other's work into Source/."""
    now = time.time() if now is None else now
    if overlay in (None, "", "-"):
        overlay = default_overlay(project, sid)
        os.makedirs(os.path.join(overlay, "Source"), exist_ok=True)
    overlay = os.path.abspath(overlay)
    if not os.path.isdir(overlay):
        raise CliError(f"no folder {overlay}")
    path = os.path.join(state_dir(project), "overlays.json")
    claims = {k: v for k, v in (read_json(path) or {}).items() if now - v.get("time", 0) < OVERLAY_HOURS * 3600}
    key = os.path.normcase(os.path.realpath(overlay))
    other = claims.get(key)
    if other and other.get("session") != sid:
        raise CliError(f"{overlay} is the staging folder session {other['session']} compiled "
                       f"{max(1, round((now - other['time']) / 60))} min ago (on Windows staging/ and Staging/ are "
                       f"one folder): stage your files in your own, e.g. `ue build --check --overlay` with no folder "
                       f"uses {default_overlay(project, sid)}")
    claims[key] = {"session": sid, "time": now}
    write_json(path, claims)
    return overlay


def compile_overlay(ctx, overlay, limit, sid):
    """`ue build --check --overlay DIR`: compile the game target of a copy of the project with DIR's files on top,
    so work staged outside Source/ is checked without another session's Live Coding picking it up. Errors in
    overlaid files name the staged file."""
    target = game_target(ctx.project)
    if not target:
        raise CliError("--overlay compiles the game target, and this project has none (Source/<Project>.Target.cs)")
    overlay = claim_overlay(ctx.project, overlay, sid)
    shadow, overlaid = shadow_project(ctx.project, overlay, sid)
    print(f"overlay: {len(overlaid)} staged file(s) on a copy of the project's code "
          f"({os.path.relpath(os.path.dirname(shadow), os.path.dirname(ctx.project))}); the real Source/ is untouched")
    code = ubt_build(argparse.Namespace(project=shadow), argparse.Namespace(target=target, config="Development",
                                                                            limit=limit), check=True)
    if code:
        print("errors in " + ", ".join(overlaid[:6]) + " are in the staged copies under " + overlay)
    return code


def compile_check(ctx, limit, files=None):
    """Compile the game target with UBT: its intermediates and binaries are separate from the editor's, so a
    running editor and its Live Coding are left alone (a `-Module=` build of the editor target breaks Live Coding
    until a restart). Misses WITH_EDITOR code and editor-only modules. None when there is no game target.
    With files: only those translation units (a header: the .cpp files that include it), compiled, not linked."""
    target = game_target(ctx.project)
    if not target:
        return None
    extra = []
    for path in files or []:
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            raise CliError(f"no such file: {path}")
        extra.append(f"-SingleFile={full}")
    if any(p.lower().endswith((".h", ".hpp", ".inl")) for p in files or []):
        extra.append("-SingleFileBuildDependents")  # a header compiles through the files that include it
    return ubt_build(ctx, argparse.Namespace(target=target, config="Development", limit=limit), check=True, extra=extra)


def ubt_build(ctx, args, check=False, extra=None):
    root = engine_root(ctx.project)
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    target = args.target or f"{name}Editor"
    started = time.time()
    single = bool(extra)
    extra = list(extra or [])
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
    print(("compile check, game target " if check else "ubt ") + (f"({len([e for e in extra if e.startswith('-SingleFile=')])} "
                                                                  "file(s), no link) " if single else "")
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
        print_link_owners(ctx.project, errors)
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
    print_link_owners(ctx.project, errors + link_errors)
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
        # Remembered for this editor process: PIE starts and empty `ue ui` results say it again
        editor = chosen_editor(ctx.project) or {}
        marker = read_json(layout_marker(ctx.project)) or {}
        headers = marker.get("headers", []) if marker.get("pid") == editor.get("pid") else []
        write_json(layout_marker(ctx.project), {"pid": editor.get("pid"), "time": time.time(),
                                                "headers": list(dict.fromkeys(headers + [os.path.basename(p) for p in changes[2]]))})
    if ok:
        mark_green(ctx.project, live=True, pid=(chosen_editor(ctx.project) or {}).get("pid"))
    return 0 if ok else 1
