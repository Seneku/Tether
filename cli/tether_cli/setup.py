"""Project setup: ue init, reload, doctor."""
import argparse
import json
import os
import re
import sys
import time

from .core import (
    CLI_DIR, CliError, crash_summary, editor_executable, editor_live, engine_root, filter_log, log_end, log_path,
    pick_port, pid_alive, port_open,
    read_json, recent_crash, run_git, running_editors, stall_report, state_dir, subprocess,
)
from .build import csharp_layout


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
            cmd_init(ctx, argparse.Namespace(dry_run=True, unrealsharp=False))
        text = out.getvalue().strip()
        if text.startswith("already set up"):
            report("ok", "plugins enabled and the Tether plugin current")
        else:
            report("WARN", "project setup incomplete: " + "; ".join(l.replace("would ", "") for l in text.splitlines()),
                   "ue init")
    except (CliError, OSError, ValueError) as exc:
        report("WARN", f"could not check the project setup: {exc}", "")
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
    if engine and others:
        same = [(p, c) for p, c in others if os.path.normcase(engine) in os.path.normcase(c)]
        for p, c in same[:3]:
            name = next((os.path.basename(t.strip('"')) for t in c.split() if t.strip('"').endswith(".uproject")), "?")
            report("WARN", f"another editor on this engine: pid {p} ({name})",
                   "C++ builds (UBT) fail while an editor on the same engine holds Live Coding; close it, or build there")
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


def cmd_init(ctx, args):
    """Set up a project for Tether: enable the engine plugins it needs in the .uproject and copy the Tether
    editor plugin(s) into the project's Plugins/ unless the project already reaches them."""
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
    for name, source, target, exists in copies:
        if exists:
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*COPY_SKIP))
    running = editor_live(project)
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
