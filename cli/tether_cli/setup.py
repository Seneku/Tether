"""Project setup: ue init, reload, doctor."""
import argparse
import json
import os
import re
import sys
import time

from .core import (
    CLI_DIR, CliError, crash_summary, editor_executable, editor_live, engine_root, filter_log, live_coding_holders, log_end,
    log_path,
    pick_port, pid_alive, port_open,
    read_json, recent_crash, run_git, running_editors, stall_report, state_dir, subprocess,
)
from .build import csharp_layout


def read_only_packages(project, cap=2000):
    """Content-relative paths of the .uasset/.umap files under Content/ that are read-only (the editor cannot save
    them), from a walk of at most `cap` read-only hits."""
    content, found = os.path.join(os.path.dirname(project), "Content"), []
    for top, dirs, files in os.walk(content):
        dirs[:] = [d for d in dirs if d not in ("__ExternalActors__", "__ExternalObjects__", "Developers", "Collections")]
        for f in files:
            if f.endswith((".uasset", ".umap")) and not os.access(os.path.join(top, f), os.W_OK):
                found.append(os.path.relpath(os.path.join(top, f), content).replace("\\", "/"))
                if len(found) >= cap:
                    return found
    return found


def cmd_doctor(ctx, args):
    """Check the setup Tether depends on and say how to fix what is wrong."""
    import io
    import contextlib
    import shutil
    results = []

    def report(status, what, fix=""):
        results.append((status, what, fix))

    report("ok" if sys.version_info >= (3, 9) else "FAIL", f"Python {sys.version.split()[0]}",
           "" if sys.version_info >= (3, 9) else "install Python 3.9 or newer")
    try:
        project = ctx.project
    except CliError as exc:
        report("FAIL", f"no project: {exc}", "run ue from the project folder, or pass --project / set UE_PROJECT")
        return doctor_print(results)
    data = read_json(project) or {}
    assoc = data.get("EngineAssociation", "?")
    try:
        engine = engine_root(project)
        version = read_json(os.path.join(engine, "Engine", "Build", "Build.version")) or {}
        exe = editor_executable(engine)
        if os.path.isfile(exe):
            report("ok", f"engine {assoc}: {engine} ({version.get('MajorVersion')}.{version.get('MinorVersion')}."
                   f"{version.get('PatchVersion')})")
        else:
            report("FAIL", f"engine {assoc} at {engine} has no editor binary ({exe})",
                   "finish or repair the install in the Epic launcher")
    except CliError as exc:
        engine = None
        report("FAIL", f"engine {assoc}: {exc}", "install it, or set UE_ROOT to its folder")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cmd_init(ctx, argparse.Namespace(dry_run=True, unrealsharp=False, plugin_check=False))
        text = out.getvalue().strip()
        if text.startswith("already set up"):
            report("ok", "plugins enabled and the Tether plugin current")
        else:
            report("WARN", "project setup incomplete: " + "; ".join(l.replace("would ", "") for l in text.splitlines()),
                   "ue init")
    except (CliError, OSError, ValueError) as exc:
        report("WARN", f"could not check the project setup: {exc}", "")
    where = claude_plugin_enabled(project)
    report("ok" if where else "WARN", f"Claude Code plugin {claude_plugin()[0]} "
           + (f"enabled ({where})" if where else "not enabled for this folder: sessions here get no edit hooks"),
           "" if where else "ue init --claude-plugin")
    beacon = editor_live(ctx.project)
    loaded, mine = (beacon or {}).get("tether"), tether_version()
    if beacon and loaded and loaded != mine:
        report("WARN", f"the running editor (pid {beacon.get('pid')}) loaded Tether {loaded}; this CLI is {mine}",
               "after ue init, restart the editor to load it (ue build --restart, or close and reopen it)")
    elif beacon and loaded:
        report("ok", f"the running editor loaded Tether {loaded}, the same as this CLI")
    beacon = ctx.beacon or {}
    pid = beacon.get("pid")
    if pid and pid_alive(pid):
        age = time.time() - beacon.get("heartbeat", 0)
        stall = stall_report(project)
        if stall:
            report("FAIL", f"editor pid {pid}: {stall}", "answer the dialog in the editor, or ue close --force")
        elif age > 15:
            report("FAIL", f"editor pid {pid}: no heartbeat for {age:.0f}s (busy or hung)",
                   "wait for long work to finish; ue status; ue close --force if it never recovers")
        else:
            port = beacon.get("mcp_port")
            reach = "python remote execution" if ctx.transport == "remote" else (
                f"MCP :{port} " + ("answering" if port and port_open(port) else "NOT answering"))
            report("ok" if "NOT" not in reach else "FAIL", f"editor pid {pid} ready on {beacon.get('map')}, {reach}",
                   "" if "NOT" not in reach else "check the ModelContextProtocol plugin is enabled (ue init), then ue close; ue open")
    else:
        # A crash only explains the editor's absence if it came after the editor last ran fine.
        crash = recent_crash(project, max(time.time() - 24 * 3600, beacon.get("heartbeat", 0)))
        report("ok" if not crash else "WARN", "editor not running" + (f"; it crashed: {crash_summary(crash, 90)}" if crash else ""),
               "ue crash for details" if crash else "ue open --headless when you need it")
        port = pick_port(project)
        if port_open(port):
            report("WARN", f"port {port} (this project's MCP port) is in use by another program",
                   "ue open picks the next free port; close the other program to keep this one")
    others = [(p, c) for p, c in running_editors() if p != pid]
    holders = live_coding_holders(engine) if engine else []
    mine = os.path.splitext(os.path.basename(project))[0]
    for console, name, editor in holders:
        if name != mine:  # UBT's Live Coding mutex is named after the engine's shared UnrealEditor.exe
            report("ok", f"Live Coding is active for {name} (LiveCodingConsole pid {console}"
                         + (f", editor pid {editor}" if editor else "") + ") on this engine install: `ue build` "
                         "passes -NoHotReloadFromIDE, so it is not blocked; an IDE or Build.bat build of this "
                         "project's editor would be", "")
    if engine and others:
        same = [(p, c) for p, c in others if os.path.normcase(engine) in os.path.normcase(c)
                and p not in {editor for _, _, editor in holders}]  # those already have their Live Coding line
        for p, c in same[:3]:
            name = next((os.path.basename(t.strip('"')) for t in c.split() if t.strip('"').endswith(".uproject")), "?")
            report("ok", f"another editor on this engine: pid {p} ({name}); `ue build` is not blocked by it, an IDE "
                         "build of this project would be once it turns Live Coding on", "")
    if ctx.transport == "remote":
        ini = os.path.join(os.path.dirname(project), "Config", "DefaultEngine.ini")
        enabled = os.path.isfile(ini) and re.search(r"^bRemoteExecution\s*=\s*True", open(ini, encoding="utf-8-sig",
                                                                                            errors="replace").read(), re.M | re.I)
        report("ok" if enabled else "WARN", "Python remote execution " + ("enabled" if enabled else "not enabled in DefaultEngine.ini"),
               "" if enabled else "ue init (ue open also enables it for its session)")
    if csharp_layout(project):
        dotnet = shutil.which("dotnet")
        version = subprocess.run([dotnet, "--version"], capture_output=True, text=True).stdout.strip() if dotnet else ""
        report("ok" if dotnet else "FAIL", f".NET SDK {version or 'not found'} (UnrealSharp project)",
               "" if dotnet else "install the .NET SDK 10.0.5 or later")
    git = shutil.which("git")
    if git:
        lfs_needed = os.path.isfile(os.path.join(os.path.dirname(project), ".gitattributes")) and "filter=lfs" in \
            open(os.path.join(os.path.dirname(project), ".gitattributes"), encoding="utf-8", errors="replace").read()
        lfs = run_git(project, "lfs", "version", check=False) if lfs_needed else "n/a"
        report("ok" if lfs else "WARN", "git" + (f", LFS {'installed' if lfs else 'NOT installed'}" if lfs_needed else ""),
               "" if lfs else "install Git LFS (git lfs install): assets are LFS pointers without it")
        locked = read_only_packages(project)
        if locked:
            report("WARN", f"{len(locked)} read-only package file(s) under Content/ ({', '.join(locked[:3])}"
                           + (", ..." if len(locked) > 3 else "") + "): saving them fails",
                   "git-lfs leaves `lockable` files read-only after a commit: `ue save --writable` makes the ones it "
                   "saves writable (or `git lfs lock <file>` where there is a lock server)")
    else:
        report("WARN", "git not found", "ue diff and ue validate --changed need git")
    free = shutil.disk_usage(os.path.dirname(project)).free / 1024 ** 3
    report("ok" if free >= 20 else "WARN", f"{free:.0f} GB free on the project's drive",
           "" if free >= 20 else "builds, cooks and the DDC need room: free some space")
    ctx.data = [{"status": s, "check": w, "fix": f} for s, w, f in results]
    return doctor_print(results)


def doctor_print(results):
    for status, what, fix in results:
        print(f"{status:<4} {what}" + (f"\n       fix: {fix}" if fix and status != "ok" else ""))
    bad = sum(1 for s, _, _ in results if s == "FAIL")
    warn = sum(1 for s, _, _ in results if s == "WARN")
    print(f"{len(results)} checks: {bad} problem(s), {warn} warning(s)")
    return 1 if bad else 0


REQUIRED_PLUGINS = ["PythonScriptPlugin", "ModelContextProtocol", "EditorToolset", "AutomationTestToolset", "SlateInspectorToolset",
                    "ConfigSettingsToolset", "Tether"]
# Epic's toolsets for engine features, enabled when the project uses the feature: (toolset plugin, feature plugin,
# whether the feature is on unless the .uproject turns it off).
FEATURE_TOOLSETS = [("NiagaraToolsets", "Niagara", True), ("PCGToolset", "PCG", False),
                    ("StateTreeToolset", "StateTree", False)]
ALL_TOOLSETS = {"EditorToolset", "AutomationTestToolset", "SlateInspectorToolset", "ConfigSettingsToolset",
                "NiagaraToolsets", "PCGToolset", "StateTreeToolset"}  # what Epic's AllToolsets plugin brings


def tether_plugins_dir():
    return os.path.join(os.path.dirname(CLI_DIR), "Plugins")


def tether_version(plugin_dir=None):
    """VersionName of a Tether plugin folder (default: the one next to this CLI)."""
    plugin = read_json(os.path.join(plugin_dir or os.path.join(tether_plugins_dir(), "Tether"), "Tether.uplugin")) or {}
    return plugin.get("VersionName", "?")


def plugin_reachable(project, data, name):
    """Whether UE will find plugin `name` in the project's Plugins/ or its AdditionalPluginDirectories."""
    root = os.path.dirname(project)
    dirs = [os.path.join(root, "Plugins")] + [os.path.normpath(os.path.join(root, d))
                                              for d in data.get("AdditionalPluginDirectories", [])]
    for base in dirs:
        for here, subdirs, files in os.walk(base):
            if f"{name}.uplugin" in files:
                return here
            subdirs[:] = [d for d in subdirs if d not in ("Binaries", "Intermediate", "Content", "Source", "Resources")]
    return None


COPY_SKIP = ("Binaries", "Intermediate", "__pycache__")


def tree_digest(root):
    """Hash of a plugin folder's sources (build output ignored), to tell whether a copy is current."""
    import hashlib
    digest = hashlib.sha1()
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in COPY_SKIP)
        for name in sorted(files):
            digest.update(os.path.relpath(os.path.join(here, name), root).replace("\\", "/").encode())
            with open(os.path.join(here, name), "rb") as f:
                digest.update(f.read().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def tree_files(root):
    """{relative path: contents with LF line ends} of a plugin folder's sources (build output ignored)."""
    out = {}
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in COPY_SKIP]
        for name in files:
            with open(os.path.join(here, name), "rb") as f:
                out[os.path.relpath(os.path.join(here, name), root).replace("\\", "/")] = f.read().replace(b"\r\n", b"\n")
    return out


VERSION_LINE = re.compile(rb'^\s*"(?:Version|VersionName)"\s*:.*$', re.M)


def python_only_update(source, target):
    """True when a plugin copy differs from its source only in Content/Python and the .uplugin's version fields:
    the editor picks that up with `ue reload`, no restart."""
    old, new = tree_files(target), tree_files(source)
    for path in set(old) | set(new):
        if old.get(path) == new.get(path) or path.startswith("Content/Python/"):
            continue
        if path.endswith(".uplugin") and path in old and path in new and \
                VERSION_LINE.sub(b"", old[path]) == VERSION_LINE.sub(b"", new[path]):
            continue
        return False
    return True


SKILL_REL = os.path.join(".claude", "skills", "unreal-tether", "SKILL.md")
SKILL_PARTS = re.compile(r"\A(---.*?\n---[ \t]*\n.*?^# [^\n]*\n\s*\n)(.*?\n)(\s*\n.*)\Z", re.S | re.M)


def split_skill(text):
    """(head, intro, body) of a SKILL.md: the frontmatter and title, the first paragraph under the title (how `ue`
    is reached, which a project's copy may say its own way), and everything after it."""
    m = SKILL_PARTS.match(text)
    return m.groups() if m else ("", "", text)


def project_skill_update(project):
    """The project's copy of the unreal-tether skill (.claude/skills/) when it differs from the CLI's: (path, new
    text, which keeps the copy's own intro paragraph). None when there is no copy or it is current."""
    path = os.path.join(os.path.dirname(project), SKILL_REL)
    source = os.path.join(os.path.dirname(CLI_DIR), "skills", "unreal-tether", "SKILL.md")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            mine = f.read()
        with open(source, encoding="utf-8") as f:
            theirs = f.read()
    except OSError:
        return None
    newline = "\r\n" if "\r\n" in mine else "\n"
    head, intro, body = split_skill(theirs.replace("\r\n", "\n"))
    own_head, own_intro, own_body = split_skill(mine.replace("\r\n", "\n"))
    if (own_head, own_body) == (head, body):
        return None
    text = head + (own_intro or intro) + body
    return path, text.replace("\n", newline)


# ---------------------------------------------------------------- new projects and template content

TEMPLATE_SKIP = {"Media", "Binaries", "Intermediate", "Saved", "Build", "DerivedDataCache"}
TEXT_EXTS = (".cpp", ".h", ".ini", ".cs")


def template_packs(engine):
    """{pack name: {detail level: Content folder}} of the engine's shared template content."""
    packs = {}
    base = os.path.join(engine, "Templates", "TemplateResources")
    for level in ("High", "Standard"):
        folder = os.path.join(base, level)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            content = os.path.join(folder, name, "Content")
            if os.path.isdir(content):
                packs.setdefault(name, {})[level] = content
    return packs


def pack_mount(content, fallback):
    """The /Game folder a pack's packages expect, read from the packages themselves ("Vehicles": /Game/Vehicles)."""
    seen = {}
    for here, _, files in os.walk(content):
        for name in files:
            if name.endswith((".uasset", ".umap")):
                with open(os.path.join(here, name), "rb") as f:
                    for m in re.finditer(rb"/Game/([A-Za-z0-9][A-Za-z0-9_]*)/", f.read(1 << 20)):
                        seen[m.group(1).decode()] = seen.get(m.group(1).decode(), 0) + 1
            if sum(seen.values()) > 50:
                break
    own = next((s for s in seen if s.lower() == fallback.lower()), None)
    return own or (max(seen, key=seen.get) if seen else fallback)


def pack_references(content, mount):
    """Other /Game folders a pack's packages refer to (other packs it needs)."""
    found = set()
    for here, _, files in os.walk(content):
        for name in files:
            if name.endswith((".uasset", ".umap")):
                with open(os.path.join(here, name), "rb") as f:
                    found.update(m.group(1).decode() for m in re.finditer(rb"/Game/([A-Za-z0-9_]+)/", f.read(1 << 20)))
    return sorted(found - {mount})


def add_template_pack(project, engine, pack, level=None, dry_run=False):
    """Copy an engine template pack into Content/<its mount>; returns (mount, files, other folders it needs)."""
    import shutil
    packs = template_packs(engine)
    match = next((p for p in packs if p.lower() == pack.lower()), None)
    if not match:
        raise CliError(f"no template pack '{pack}' in {engine} (ue template list)")
    levels = packs[match]
    level = next((l for l in levels if level and l.lower() == level.lower()), None) or levels.get("High") and "High" \
        or "Standard"
    if level not in levels:
        raise CliError(f"{match} comes in {', '.join(levels)} only")
    source = levels[level]
    mount = pack_mount(source, match)
    content = os.path.join(os.path.dirname(project), "Content")
    target = os.path.join(content, mount)
    # World Partition maps keep their actors beside the pack's Content; in a project they sit under
    # Content/__ExternalActors__/<mount>/ (and __ExternalObjects__)
    extras = [(os.path.join(os.path.dirname(source), d), os.path.join(content, d, mount))
              for d in ("__ExternalActors__", "__ExternalObjects__") if os.path.isdir(os.path.join(os.path.dirname(source), d))]
    files = sum(len(f) for folder in [source] + [s for s, _ in extras] for _, _, f in os.walk(folder))
    names = {p.lower() for p in packs}  # other packs it uses; other /Game names in its packages are often stale
    needs = [m for m in pack_references(source, mount) if m.lower() in names and not os.path.isdir(os.path.join(content, m))]
    if os.path.exists(target):
        raise CliError(f"Content/{mount} already exists: remove it first to copy {match} again")
    if not dry_run:
        shutil.copytree(source, target)
        for extra, into in extras:
            shutil.copytree(extra, into, dirs_exist_ok=True)
    return mount, level, files, needs


def cmd_template(ctx, args):
    """`ue template list` / `ue template add <Pack> [--level High|Standard]`: the engine's template content (the
    cars of the Vehicle template, the level prototyping meshes, the mannequins...) copied into the project at the
    /Game path its packages expect, as the new-project wizard does."""
    engine = engine_root(ctx.project)
    packs = template_packs(engine)
    if args.action == "list" or not args.pack:
        print(f"{len(packs)} template content pack(s) in {engine} (ue template add <Pack>):")
        for name, levels in packs.items():
            files = sum(len(f) for _, _, f in os.walk(levels.get("High") or levels["Standard"]))
            print(f"  {name:<22} {'/'.join(levels)}  {files} files")
        return 0
    mount, level, files, needs = add_template_pack(ctx.project, engine, args.pack, args.level, args.dry_run)
    print(("would copy " if args.dry_run else "copied ") + f"{args.pack} ({level}, {files} files) to /Game/{mount}"
          + ("" if args.dry_run else "; a running editor picks the new files up on its own"))
    source = next(v for k, v in packs.items() if k.lower() == args.pack.lower())[level]
    for folder in sorted(os.listdir(source)):
        if os.path.isdir(os.path.join(source, folder)):
            names = [f for _, _, fs in os.walk(os.path.join(source, folder)) for f in fs if f.endswith((".uasset", ".umap"))]
            print(f"  /Game/{mount}/{folder}: {len(names)} asset(s)" + (f", e.g. {', '.join(n[:-7] for n in names[:4])}"
                                                                       if names else ""))
    if needs:
        print(f"note: its packages also refer to /Game/{', /Game/'.join(needs)}, which this project lacks"
              + "".join(f" (ue template add {n})" for n in needs if any(p.lower() == n.lower() for p in packs)))
    return 0


def ini_append(text, section, lines):
    """An .ini file's `text` with `lines` added at the end of [section] after a blank line, or in a new [section] at
    the end of the file: where the editor's new-project wizard writes the values it adds."""
    rows, header = text.rstrip("\n").split("\n"), f"[{section}]"
    start = next((i + 1 for i, row in enumerate(rows) if row.strip() == header), None)
    if start is None:
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + "\n".join([header] + lines) + "\n"
    after = next((i for i in range(start, len(rows)) if rows[i].startswith("[")), len(rows))
    end = after
    while end > start and not rows[end - 1].strip():
        end -= 1
    return "\n".join(rows[:end] + ([""] if end > start else []) + lines + ([""] if after < len(rows) else [])
                     + rows[after:]) + "\n"


def new_project(ctx, args):
    """`ue new project <Folder/Name> [--cpp] [--template ThirdPerson] [--engine 5.8]`: a project from one of the
    engine's templates as the wizard makes it (names replaced, with the redirects that keep the template's
    Blueprints on its renamed C++ classes, its shared content packs copied), plus what a session would otherwise
    discover by failing: the module root on the include path, a C++ game mode set as the default, then `ue init`."""
    import shutil
    import uuid
    folder = os.path.abspath(args.path)
    name = os.path.basename(folder.rstrip("\\/"))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,19}", name):
        raise CliError(f"'{name}': a project name is a letter then letters, digits or _ (20 at most)")
    if os.path.isdir(folder) and os.listdir(folder):
        raise CliError(f"{folder} exists and is not empty")
    template = "TP_" + (args.template or "Blank") + ("" if args.cpp else "BP")
    probe = os.path.join(folder, f"{name}.uproject")
    os.makedirs(folder, exist_ok=True)
    with open(probe, "w", encoding="utf-8") as f:  # engine_root reads the association from it
        json.dump({"FileVersion": 3, "EngineAssociation": args.engine}, f)
    try:
        engine = engine_root(probe)
    except CliError:
        os.remove(probe)
        raise
    source = os.path.join(engine, "Templates", template)
    if not os.path.isdir(source):
        os.remove(probe)
        have = sorted(d[3:] for d in os.listdir(os.path.join(engine, "Templates")) if d.startswith("TP_"))
        raise CliError(f"no template {template} in {engine} (have: {', '.join(have)})")
    old = template

    def renamed(text):
        text = text.replace(old.upper(), name.upper()).replace(old.lower(), name.lower())
        return re.sub(re.escape(old), name, text, flags=re.I)
    classes = []  # (template class, project class): the wizard's rule, a renamed header with a .generated.h
    for here, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in TEMPLATE_SKIP]
        rel = os.path.relpath(here, source)
        for f in files:
            if f in ("TemplateDefs.ini", "config.ini") or f.endswith((".uproject", ".png", ".sln")):
                continue
            target = os.path.join(folder, renamed(rel) if rel != "." else "", renamed(f))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if f.endswith(TEXT_EXTS):
                with open(os.path.join(here, f), encoding="utf-8-sig", errors="replace") as fh:
                    text = renamed(fh.read())
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(text)
                if f.endswith(".h") and renamed(f) != f and '.generated.h"' in text.lower():
                    classes.append((f[:-2], renamed(f)[:-2]))
            else:
                shutil.copy2(os.path.join(here, f), target)
    descriptor = read_json(os.path.join(source, f"{template}.uproject")) or {"FileVersion": 3}
    descriptor["EngineAssociation"] = args.engine
    for module in descriptor.get("Modules", []):
        module["Name"] = renamed(module["Name"])
    module_dir = os.path.join(folder, "Source", name)
    if args.cpp and os.path.isdir(module_dir):
        build = os.path.join(module_dir, f"{name}.Build.cs")
        with open(build, encoding="utf-8") as f:
            text = f.read()
        if "PublicIncludePaths.Add(ModuleDirectory)" not in text:  # 5.8 no longer puts the module root on the path
            text = re.sub(r"(PCHUsage = [^;]+;\n)", r"\1\n\t\tPublicIncludePaths.Add(ModuleDirectory);\n", text, count=1)
        with open(build, "w", encoding="utf-8") as f:
            f.write(text)
        ini = os.path.join(folder, "Config", "DefaultEngine.ini")
        with open(ini, encoding="utf-8") as f:
            text = f.read()
        mode = os.path.join(module_dir, f"{name}GameMode.h")
        if not any(f.endswith("GameMode.h") for f in os.listdir(module_dir)):
            with open(mode, "w", encoding="utf-8") as f:
                f.write(f'#pragma once\n\n#include "CoreMinimal.h"\n#include "GameFramework/GameModeBase.h"\n'
                        f'#include "{name}GameMode.generated.h"\n\nUCLASS()\nclass {name.upper()}_API A{name}GameMode '
                        f": public AGameModeBase\n{{\n\tGENERATED_BODY()\n}};\n")
            with open(mode[:-2] + ".cpp", "w", encoding="utf-8") as f:
                f.write(f'#include "{name}GameMode.h"\n')
            line = f"GlobalDefaultGameMode=/Script/{name}.{name}GameMode"
            section = "[/Script/EngineSettings.GameMapsSettings]"
            text = text.replace(section, f"{section}\n{line}", 1) if section in text else \
                text.rstrip("\n") + f"\n\n{section}\n{line}\n"
        # The template's Blueprints still name /Script/<template> and its classes, so redirect them as the wizard
        # does, in its order (it finds the classes A-Z and writes each above the last). Blueprint templates need
        # none: no /Script/TP_ path in their packages, nor in the shared packs.
        game = [f'+ActiveGameNameRedirects=(OldGameName="{n}",NewGameName="/Script/{name}")'
                for n in (old, f"/Script/{old}")]
        moved = [f'+ActiveClassRedirects=(OldClassName="{a}",NewClassName="{b}")'
                 for a, b in sorted(classes, reverse=True)]
        text = ini_append(text, "/Script/Engine.Engine", game + [""] + moved if moved else game)
        with open(ini, "w", encoding="utf-8") as f:
            f.write(text)
    game_ini = os.path.join(folder, "Config", "DefaultGame.ini")
    with open(game_ini, "a", encoding="utf-8") as f:
        f.write(f"\n[/Script/EngineSettings.GeneralProjectSettings]\nProjectID={uuid.uuid4().hex.upper()}\n"
                f"ProjectName={name}\n")
    with open(probe, "w", encoding="utf-8") as f:
        json.dump(descriptor, f, indent="\t")
        f.write("\n")
    print(f"created {folder} from {template} (engine {args.engine})")
    if args.cpp and os.path.isdir(module_dir):
        print(f"  redirects in Config/DefaultEngine.ini: /Script/{old} -> /Script/{name}, "
              f"{len(classes)} renamed class(es)")
    defs = os.path.join(source, "Config", "TemplateDefs.ini")
    with open(defs, encoding="utf-8-sig", errors="replace") as f:
        wanted = re.findall(r'^SharedContentPacks=\(MountName="(\w+)",DetailLevels=\("?(\w+)', f.read(), re.M)
    for pack, level in wanted:
        mount, level, files, _ = add_template_pack(probe, engine, pack, level)
        print(f"  template content {pack} ({level}, {files} files) -> /Game/{mount}")
    ctx._project = probe
    cmd_init(ctx, argparse.Namespace(unrealsharp=False, dry_run=False, claude_plugin=args.claude_plugin))
    where = folder if os.path.splitdrive(folder)[0].lower() != os.path.splitdrive(os.getcwd())[0].lower() \
        else os.path.relpath(folder)  # relpath cannot cross drives
    print(f"next: cd {where}; " + ("`ue build` compiles the module, then " if args.cpp else "")
          + "`ue open --headless`")
    return 0


def claude_plugin():
    """('tether@tether', marketplace name, this checkout's folder): the Claude Code plugin this CLI belongs to."""
    root = os.path.dirname(CLI_DIR)
    market = read_json(os.path.join(root, ".claude-plugin", "marketplace.json")) or {}
    plugin = read_json(os.path.join(root, ".claude-plugin", "plugin.json")) or {}
    name = market.get("name", "tether")
    return f"{plugin.get('name', 'tether')}@{name}", name, root


def claude_plugin_enabled(project):
    """The settings file that enables Tether's Claude Code plugin for sessions in this project, or None. Without
    it a session has no `ue` on PATH, no session-start note and no edit hooks (edit records, guards)."""
    ref = claude_plugin()[0]
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    folders = [os.path.dirname(project)]
    top = (run_git(project, "rev-parse", "--show-toplevel", check=False) or "").strip()
    if top:
        folders.append(os.path.normpath(top))
    files = [os.path.join(home, "settings.json")] + [os.path.join(d, ".claude", f) for d in folders
                                                     for f in ("settings.json", "settings.local.json")]
    return next((f for f in files if ((read_json(f) or {}).get("enabledPlugins") or {}).get(ref)), None)


def enable_claude_plugin(project):
    """Enable the plugin for this folder in .claude/settings.local.json (machine-local: the marketplace is this
    checkout's path), keeping what the file already holds."""
    ref, market, root = claude_plugin()
    path = os.path.join(os.path.dirname(project), ".claude", "settings.local.json")
    settings = read_json(path) or {}
    settings.setdefault("extraKnownMarketplaces", {})[market] = {"source": {"source": "directory", "path": root}}
    settings.setdefault("enabledPlugins", {})[ref] = True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return path


def cmd_init(ctx, args):
    """Set up a project for Tether: enable the engine plugins it needs in the .uproject, copy the Tether editor
    plugin(s) into the project's Plugins/ unless the project already reaches them, and check that Claude Code
    sessions here load Tether's plugin (`--claude-plugin` enables it)."""
    code = init_project(ctx, args)
    if not getattr(args, "plugin_check", True):  # ue doctor reports it on its own line
        return code
    where = claude_plugin_enabled(ctx.project)
    ref = claude_plugin()[0]
    if where:
        return code
    if getattr(args, "claude_plugin", False) and not args.dry_run:
        path = enable_claude_plugin(ctx.project)
        print(f"enabled the Claude Code plugin {ref} in {os.path.relpath(path, os.path.dirname(ctx.project))}: new "
              "sessions in this folder get `ue` on PATH, the session-start note and the edit hooks (Claude Code "
              "may ask once to trust it)")
    else:
        print(f"note: Claude Code sessions here do not load the plugin {ref}: no `ue` on PATH, no session-start "
              "note, no edit hooks. `ue init --claude-plugin` enables it for this folder (.claude/settings.local.json)")
    return code


def init_project(ctx, args):
    import shutil
    project = ctx.project
    with open(project, encoding="utf-8-sig", newline="") as f:  # newline="": keep CRLF files CRLF
        text = f.read()
    data = json.loads(text)
    remote = ctx.transport == "remote"
    base = ["PythonScriptPlugin", "Tether"] if remote else REQUIRED_PLUGINS  # MCP and toolsets are 5.8+
    wanted = base + (["TetherUnrealSharp"] if args.unrealsharp or csharp_layout(project) else [])
    entries = data.setdefault("Plugins", [])
    enabled = {e.get("Name"): e.get("Enabled", False) for e in entries}
    if not remote:
        wanted += [toolset for toolset, feature, default_on in FEATURE_TOOLSETS if enabled.get(feature, default_on)]
    if enabled.get("AllToolsets"):
        wanted = [n for n in wanted if n not in ALL_TOOLSETS]  # AllToolsets has them
    changes = []
    for name in wanted:
        entry = next((e for e in entries if e.get("Name") == name), None)
        if entry is None:
            entries.append({"Name": name, "Enabled": True})
            changes.append(f"enable {name}")
        elif not entry.get("Enabled", False):
            entry["Enabled"] = True
            changes.append(f"re-enable {name} (was disabled)")
    copies = []
    for name in ("Tether", "TetherUnrealSharp"):
        if name not in wanted:
            continue
        found = plugin_reachable(project, data, name)
        source = os.path.join(tether_plugins_dir(), name)
        target = os.path.join(os.path.dirname(project), "Plugins", name)
        if found and os.path.normcase(os.path.abspath(found)) != os.path.normcase(os.path.abspath(target)):
            continue  # reachable elsewhere, e.g. AdditionalPluginDirectories pointing at a Tether checkout
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
            continue
        if found and tree_digest(source) == tree_digest(target):
            continue  # the project's copy is current
        copies.append((name, source, target, bool(found)))
    ini = os.path.join(os.path.dirname(project), "Config", "DefaultEngine.ini")
    ini_section = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]"
    ini_text = ""
    if os.path.isfile(ini):
        with open(ini, encoding="utf-8-sig", newline="") as f:
            ini_text = f.read()
    if remote and not re.search(r"^bRemoteExecution\s*=\s*True", ini_text, re.M | re.I):
        changes.append("enable Python remote execution in Config/DefaultEngine.ini (how ue reaches UE < 5.8)")
    else:
        ini = None
    skill = project_skill_update(project)
    if not changes and not copies and not skill:
        print("already set up: plugins enabled, Tether reachable")
        return 0
    for line in changes:
        print(("would " if args.dry_run else "") + line)
    if skill:
        print(("would " if args.dry_run else "") + f"update {SKILL_REL.replace(os.sep, '/')} (the project's copy of "
              f"the skill) to Tether {tether_version()}, keeping its own first paragraph")
    for name, source, target, exists in copies:
        versions = (f" (the project has {tether_version(target)}, this CLI {tether_version(source)})"
                    if exists and name == "Tether" else "")
        print(("would " if args.dry_run else "") + ("update" if exists else "copy") + f" {name} -> "
              + os.path.relpath(target, os.path.dirname(project)) + versions)
    if args.dry_run:
        return 0
    if skill:
        with open(skill[0], "w", encoding="utf-8", newline="") as f:
            f.write(skill[1])
    if not changes and not copies:
        return 0  # only the skill: nothing for the editor to load
    if ini:
        newline = "\r\n" if "\r\n" in ini_text else "\n"
        if ini_section in ini_text:
            ini_text = ini_text.replace(ini_section, ini_section + newline + "bRemoteExecution=True", 1)
        else:
            ini_text += ("" if not ini_text or ini_text.endswith("\n") else newline) + newline + ini_section \
                + newline + "bRemoteExecution=True" + newline
        os.makedirs(os.path.dirname(ini), exist_ok=True)
        with open(ini, "w", encoding="utf-8", newline="") as f:
            f.write(ini_text)
    if any(not c.startswith("enable Python remote") for c in changes):
        newline = "\r\n" if "\r\n" in text else "\n"
        out = json.dumps(data, indent="\t") + (newline if text.endswith(("\n", "\r\n")) else "")
        with open(project, "w", encoding="utf-8", newline="") as f:
            f.write(out.replace("\n", newline) if newline != "\n" else out)
    # Python only (the usual Tether update): the running editor reloads it without a restart
    python_only = not changes and all(exists and python_only_update(source, target)
                                      for name, source, target, exists in copies)
    for name, source, target, exists in copies:
        if exists:
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*COPY_SKIP))
    running = editor_live(project)
    if running and python_only:
        print("next: `ue reload` (only the plugin's Python changed: no restart needed)")
        return 0
    print("next: " + ("`ue build --restart` to load the plugins" if running else "`ue open --headless`")
          + ("; TetherUnrealSharp is C++, so the first build compiles it" if any(n == "TetherUnrealSharp" for n, *_ in copies) else ""))
    return 0


def cmd_reload(ctx, args):
    start = log_end(ctx.project)
    ctx.tether("reload")
    deadline, done = time.time() + 15, None
    while time.time() < deadline and done is None:  # the reload runs on the next editor tick
        time.sleep(0.3)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
        if "Tether: reloaded" in text:
            done = True
        elif re.search(r"LogPython: Error: .*(tether|Traceback)", text):
            done = False
    if done is False:
        errors = [l for l in filter_log(text.splitlines(), level="Error") if "LogPython" in l]
        raise CliError("reload failed: " + (" | ".join(errors[:3]) or "see ue log"))
    print("reloaded" if done else "reload scheduled (not confirmed within 15 s; ue log)")
    cache = os.path.join(state_dir(ctx.project), "cache")
    for name in os.listdir(cache) if os.path.isdir(cache) else []:
        os.remove(os.path.join(cache, name))  # tool schemas may have changed
    return 0
