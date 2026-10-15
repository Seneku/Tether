"""Shared plumbing: project and beacon files, engine discovery, ports, dialogs, crash reports, git, rendering, logs."""
import importlib
import json
import os
import re
import sys
import time


class _Lazy:
    """A module imported on first use. Hooks start a fresh Python on every edit, so keep startup lean."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)


socket, subprocess, zlib = _Lazy("socket"), _Lazy("subprocess"), _Lazy("zlib")

CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # cli/
UE_SCRIPT = os.path.join(CLI_DIR, "ue.py")  # the entry point, for the CLI's own child processes
TETHER = "tether.toolsets.TetherCore"
GATED = {"py", "console", "set_props", "undo", "save", "spawn", "delete", "duplicate", "mv", "rm", "set_material", "pie_step", "set_config", "umg", "new_asset", "drop", "pie_net", "look_stage", "scatter", "bp_component", "set_savegame", "import_files", "reimport_assets", "fix_redirectors", "foliage", "landscape_edit", "mat_graph", "editor_undo", "lint_fix", "set_assets", "wp", "set_cvars", "set_niagara", "pcg", "pie_view", "pie_loop"}  # tools that need the session token (see beacon.session_token)
LEVELS = {"Fatal": 0, "Error": 1, "Warning": 2, "Display": 3, "Log": 4, "Verbose": 5, "VeryVerbose": 6}
NOISE = {"LogModelContextProtocol", "LogToolsetRegistry"}  # our own transport chatter
# Epic logs this whenever a schema is generated for a class with delegates (props, set, describe_toolset).
NOISE_MESSAGE = re.compile(r"^Property \"\w+\" type \w+ unhandled during Json schema generation")
LOG_FRAME = re.compile(r"^\[[^\]]*\]\[\s*(\d+)\]")
TETHER_COPY = re.compile(r"[/\\]Tether[/\\]diff[/\\]")  # Saved/Tether/diff, mounted as /Temp/Tether/diff
TOOL_ENSURE = re.compile(r"Ensure condition failed: false \|\| !HasAnyFlags\(RF_ClassDefaultObject\).*NavigationSystem\.h")
LOG_LINE = re.compile(r"^(?:\[[^\]]*\]\[[^\]]*\])?([A-Za-z0-9_]+): (?:(Fatal|Error|Warning|Display|Verbose|VeryVerbose): )?(.*)$")


class CliError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- project / beacon

def find_project(explicit):
    if explicit or os.environ.get("UE_PROJECT"):
        return os.path.abspath(explicit or os.environ["UE_PROJECT"])
    here = os.getcwd()
    while True:
        hits = [f for f in os.listdir(here) if f.endswith(".uproject")]
        if hits:
            return os.path.join(here, hits[0])
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    below = [os.path.join(os.getcwd(), d, f) for d in os.listdir(os.getcwd())
             if os.path.isdir(d) for f in os.listdir(d) if f.endswith(".uproject")]
    if len(below) == 1:
        return below[0]
    raise CliError("no .uproject found; pass --project or set UE_PROJECT", 2)


def state_dir(project):
    return os.path.join(os.path.dirname(project), "Saved", "Tether")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":  # never os.kill on Windows: it terminates the process
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- platform (Windows is the tested one)

def host_platform():
    """UE's name for this host: Win64, Mac or Linux."""
    return {"win32": "Win64", "darwin": "Mac"}.get(sys.platform, "Linux")


def epic_settings_dir():
    """Where the launcher and UnrealBuildTool keep per-user state on this host."""
    if os.name == "nt":
        return os.environ.get("LOCALAPPDATA", "")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Epic")
    return os.path.expanduser("~/.config/Epic")


def launcher_manifest():
    if os.name == "nt":
        path = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "UnrealEngineLauncher",
                            "LauncherInstalled.dat")
    else:  # the Mac launcher keeps it per user; Linux has no launcher
        path = os.path.join(epic_settings_dir(), "UnrealEngineLauncher", "LauncherInstalled.dat")
    return read_json(path) or {}


def registered_builds():
    """Source builds registered with the engine's own registration tool: {association: path}."""
    if os.name == "nt":
        import winreg
        builds = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds") as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, i)
                    builds[name] = value
        except OSError:
            pass
        return builds
    import configparser
    ini = configparser.ConfigParser(strict=False)
    ini.optionxform = str
    ini.read(os.path.join(epic_settings_dir(), "UnrealEngine", "Install.ini"))
    return dict(ini["Installations"]) if ini.has_section("Installations") else {}


def editor_executable(root):
    binaries = os.path.join(root, "Engine", "Binaries", host_platform())
    if os.name == "nt":
        return os.path.join(binaries, "UnrealEditor.exe")
    if sys.platform == "darwin":
        return os.path.join(binaries, "UnrealEditor.app", "Contents", "MacOS", "UnrealEditor")
    return os.path.join(binaries, "UnrealEditor")


def ubt_command(root):
    """The command that runs UnrealBuildTool on this host (arguments follow: target platform config ...)."""
    if os.name == "nt":
        return [os.path.join(root, "Engine", "Binaries", "DotNET", "UnrealBuildTool", "UnrealBuildTool.exe")]
    return ["sh", os.path.join(root, "Engine", "Build", "BatchFiles", host_platform(), "Build.sh")]


def uat_command(root):
    batch = os.path.join(root, "Engine", "Build", "BatchFiles")
    return [os.path.join(batch, "RunUAT.bat")] if os.name == "nt" else ["sh", os.path.join(batch, "RunUAT.sh")]


def live_coding_available():
    return os.name == "nt"  # Live Coding is Windows-only in UE 5


def installed_engine_dirs(version):
    """Where a launcher-installed engine may live when the launcher manifest does not name it."""
    found = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\EpicGames\Unreal Engine\{version}") as key:
                found.append(winreg.QueryValueEx(key, "InstalledDirectory")[0])
        except OSError:
            pass
        found.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Epic Games", f"UE_{version}"))
    elif sys.platform == "darwin":
        found.append(f"/Users/Shared/Epic Games/UE_{version}")
    return found


def engine_root(project):
    """Engine dir from UE_ROOT, the launcher manifest, or registered source builds."""
    if os.environ.get("UE_ROOT"):
        return os.environ["UE_ROOT"]
    with open(project, encoding="utf-8") as f:
        match = re.search(r'"EngineAssociation"\s*:\s*"([^"]*)"', f.read())
    assoc = match.group(1) if match else ""
    for item in launcher_manifest().get("InstallationList", []):
        if item.get("AppName") == f"UE_{assoc}":
            return item["InstallLocation"]
    builds = registered_builds()
    if assoc in builds:
        return builds[assoc]
    if assoc:  # a launcher install the manifest does not list (yet): the engine's own key, then default folders
        for root in installed_engine_dirs(assoc):
            if os.path.isdir(os.path.join(root, "Engine", "Binaries")):
                return root
    if not assoc:  # a project inside an engine tree (e.g. a source build checkout)
        here = os.path.dirname(os.path.abspath(project))
        while os.path.dirname(here) != here:
            if os.path.isdir(os.path.join(here, "Engine", "Binaries")):
                return here
            here = os.path.dirname(here)
    raise CliError(f"cannot find engine '{assoc}'; set UE_ROOT", 2)


def pick_port(project):
    """Stable per-project port (so two editors never fight over 8000), skipping busy ones."""
    base = 18000 + zlib.crc32(os.path.normcase(os.path.abspath(project)).encode()) % 1000  # = the plugin's
    for port in range(base, base + 50):
        if not port_open(port):
            return port
    raise CliError("no free port near %d" % base)


def port_from_log(project):
    """Fallback when there is no beacon: Epic's server logs 'Starting MCP server on port N'."""
    path = log_path(project)
    try:
        with open(path, "rb") as f:
            hits = re.findall(rb"Starting MCP server on port (\d+)", f.read())
        return int(hits[-1]) if hits else None
    except OSError:
        return None


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- rendering

def unwrap(text):
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict) and set(value) == {"returnValue"}:
        value = value["returnValue"]
        if isinstance(value, str) and value[:1] in "[{":
            try:
                value = json.loads(value)
            except ValueError:
                pass
    return value


PY_OUTPUT = "<<printed before the error>>"  # toolsets._PY_OUTPUT: what a failing `ue py` script printed follows


def compact_error(text):
    text = text.split("\nFunction schema Json", 1)[0]
    text, marker, printed = text.partition(PY_OUTPUT)
    head = re.sub(r"\s+", " ", text).strip()[:600]
    return head + (f"\n{PY_OUTPUT}\n{printed.strip(chr(10))}" if marker else "")


def strip_common(paths):
    if len(paths) < 2:
        return "", paths
    prefix = os.path.commonprefix(paths)
    cut = max(prefix.rfind(c) for c in ".:/") + 1
    return prefix[:cut], [p[cut:] for p in paths]


def short(value, width=100):
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return text if len(text) <= width else text[: width - 3] + "..."


def render(value, limit):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = value
        if items and all(isinstance(v, dict) and set(v) == {"refPath"} for v in items):
            items = [v["refPath"] for v in items]
        head = f"{len(items)} items"
        if items and all(isinstance(v, str) for v in items):
            prefix, items = strip_common(items)
            head += f" under {prefix}" if prefix else ""
        rows = [v if isinstance(v, str) else short(v) for v in items]
        return "\n".join([head] + rows[:limit] + ([f"... +{len(rows) - limit} more (--limit)"] if len(rows) > limit else []))
    if isinstance(value, dict):
        rows = [f"{k}: {short(v)}" for k, v in value.items()]
        return "\n".join(rows[:limit] + ([f"... +{len(rows) - limit} more (--limit)"] if len(rows) > limit else []))
    return json.dumps(value)


def emit(args, value):
    print(json.dumps(value) if args.json else render(value, args.limit))


# ---------------------------------------------------------------- logs (byte-offset cursor; no editor needed)

def log_path(project):
    """The chosen editor's log: <Project>.log, or <Project>_2.log for a second editor on the project."""
    try:
        logged = (chosen_editor(project) or {}).get("log")
    except CliError:  # --pid names no running editor; the error surfaces where the editor is needed
        logged = None
    if logged and os.path.isfile(logged):
        return logged
    return os.path.join(os.path.dirname(project), "Saved", "Logs",
                        os.path.splitext(os.path.basename(project))[0] + ".log")


def log_identity(stat):
    """Changes when UE rotates the log (renames it, starts a new file). st_ctime is the creation time on
    Windows but the last metadata change on POSIX, where it moves on every write; use the inode there."""
    return stat.st_ctime if os.name == "nt" else stat.st_ino


def read_new_log(project, from_start=False, advance=True):
    path = log_path(project)
    cursor_file = os.path.join(state_dir(project), "log-cursor.json")
    if not os.path.exists(path):
        raise CliError(f"no log at {path}", 2)
    stat = os.stat(path)
    cursor = read_json(cursor_file) or {}
    offset = cursor.get("offset", 0)
    if from_start or cursor.get("id") != log_identity(stat) or offset > stat.st_size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # keep a partial trailing line for next time
    if advance:
        write_json(cursor_file, {"id": log_identity(stat), "offset": offset + end})
    return data[:end].decode("utf-8", "replace").lstrip("﻿").splitlines()


def log_end(project):
    path = log_path(project)
    return os.path.getsize(path) if os.path.exists(path) else 0


# FMessageDialog under -RUNNINGUNATTENDEDSCRIPT logs instead of blocking; the text runs to the next stamped line.
DIALOG = re.compile(r"Message dialog closed, result: \w+, title: (?P<title>[^\r\n]*?), text: (?P<text>.*?)"
                    r"(?=^\[\d{4}\.\d\d\.\d\d-|\Z)", re.S | re.M)


def logged_dialogs(lines):
    """(title, text) of every dialog the editor answered by itself in these log lines."""
    return [(m.group("title"), m.group("text").strip()) for m in DIALOG.finditer("\n".join(lines))]


FRAME = re.compile(r"^\s+at (?P<where>[\w.<>`+]+)\(.*\) in (?P<file>.+):line (?P<line>\d+)")


def user_frame(lines, start):
    """First C# stack frame with a source location below an error line, as ' @ File.cs:N (Type.Method)'."""
    for line in lines[start + 1: start + 12]:
        if LOG_LINE.match(line):
            break
        m = FRAME.match(line)
        if m and "/plugins/unrealsharp/" not in m.group("file").replace("\\", "/").lower():  # skip glue
            method = ".".join(m.group("where").split(".")[-2:])
            name = re.split(r"[/\\]", m.group("file"))[-1]  # Windows paths in the log, on any host
            return f" @ {name}:{m.group('line')} ({method})"
    return ""


LOG_IGNORE = []  # compiled known-noise regexes from the project's Config/Tether/log-ignore.json (load_log_ignore)


def log_ignore_path(project):
    return os.path.join(os.path.dirname(project), "Config", "Tether", "log-ignore.json")


def log_ignore_entries(project):
    """The known-noise patterns: log-ignore.json is a list, or {"ignore": [...]}, of regexes or {"pattern", "why"}."""
    data = read_json(log_ignore_path(project)) or []
    items = data.get("ignore", []) if isinstance(data, dict) else data
    return [item if isinstance(item, str) else (item or {}).get("pattern", "") for item in items]


def load_log_ignore(project):
    LOG_IGNORE[:] = []
    for pattern in log_ignore_entries(project):
        try:
            LOG_IGNORE.append(re.compile(pattern, re.I))
        except re.error as exc:
            print(f"warning: log-ignore.json: bad regex {pattern!r}: {exc}", file=sys.stderr)


def noise_match(patterns, category, level, message):
    """Whether a known-noise pattern matches the line, written either as 'Category: message' or with its level,
    'Category: Error: message', as the log shows it."""
    forms = (f"{category}: {message}", f"{category}: {level}: {message}")
    return any(rx.search(form) for rx in patterns for form in forms)


class LogLines(list):
    """filter_log's result; `hidden` counts the entries the project's known-noise list left out, `folded` the
    repeats shown as "xN" on another line."""
    hidden = 0
    folded = 0


def filter_log(lines, level="Warning", category=None, pattern=None, ignore=True, consecutive=False):
    max_level = LEVELS.get(level.capitalize() if level.lower() != "veryverbose" else "VeryVerbose", 2)
    rx = re.compile(pattern, re.I) if pattern else None
    known = LOG_IGNORE if ignore else []
    order, counts, hidden = [], {}, 0
    # Tether's own /Temp copies of assets (ue diff, ue bp, ue usages) compile on load, and a copy can fail where
    # the real asset does not; leave out compiler output from the frames that loaded one.
    frame = lambda line: (LOG_FRAME.match(line) or [None, None])[1]  # the editor's frame counter, [342]
    copy_frames = {frame(l) for l in lines if TETHER_COPY.search(l)} - {None}
    # Python calls UNavigationSystemV1's static functions through its default object, which is Within=World: the
    # engine raises a handled ensure about it once per session. Leave out that ensure's report (same frame).
    ensure_frames = {frame(l) for l in lines if TOOL_ENSURE.search(l)} - {None}
    for index, line in enumerate(lines):
        m = LOG_LINE.match(line)
        if not m:
            continue
        cat, lvl, msg = m.group(1), m.group(2) or "Log", m.group(3)
        if cat in NOISE or (cat == "Cmd" and msg.strip() == "FLUSHLOG") or (cat == "LogJson" and NOISE_MESSAGE.match(msg)):
            continue
        if cat == "LogConsoleManager" and "FindConsoleObject() for 'FLUSHLOG'" in msg:  # our own log flushes
            continue
        if "BP_TetherDeferredExec" in msg:  # building Tether's helper for commands in multiplayer PIE
            continue
        if copy_frames and (TETHER_COPY.search(line) or (cat in ("BlueprintLog", "LogBlueprint", "LogK2Compiler")
                                                          and frame(line) in copy_frames)):
            continue
        if ensure_frames and cat in ("LogOutputDevice", "EnsureFailed", "LogStats") and frame(line) in ensure_frames:
            continue
        if lvl in ("Error", "Fatal"):
            msg += user_frame(lines, index)
        if LEVELS[lvl] > max_level or (category and cat.lower() != category.lower()):
            continue
        if rx and not rx.search(line):
            continue
        if known and noise_match(known, cat, lvl, msg):
            hidden += 1
            continue
        key = (lvl[0], cat, msg.strip())
        if consecutive:  # order matters (a sequence of events): fold only repeats in a row
            if order and order[-1][0] == key:
                order[-1][1] += 1
            else:
                order.append([key, 1])
            continue
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1
    if consecutive:
        result = LogLines(f"{k[0]} {k[1]}: {k[2]}" + (f"  x{n}" if n > 1 else "") for k, n in order)
        result.hidden, result.folded = hidden, sum(n - 1 for _, n in order)
        return result
    result = LogLines(f"{l} {c}: {m}" + (f"  x{counts[(l, c, m)]}" if counts[(l, c, m)] > 1 else "") for l, c, m in order)
    result.hidden, result.folded = hidden, sum(counts.values()) - len(order)
    return result


LOG_SHAPE = [  # what varies between otherwise identical messages, for `ue log --summary`
    (re.compile(r"(?:[A-Za-z]:)?[/\\](?:[\w .+-]+[/\\])+[\w .+-]*"), "<path>"),
    (re.compile(r"/(?:Game|Engine|Script|Temp|Memory)/[\w/.:-]*"), "<asset>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "'…'"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"(?<![A-Za-z])-?\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\b(\w+?)_(?:C_)?\d+\b"), r"\1_<n>"),  # instance names: BP_Door_C_12
]


def log_shape(message):
    for rx, repl in LOG_SHAPE:
        message = rx.sub(repl, message)
    return re.sub(r"\s+", " ", message).strip()


def summarize_log(lines, level="Warning", ignore=True):
    """Warnings and errors grouped by (level, category, message shape): [{level, category, shape, count, first}],
    errors first, then by count. Also returns how many entries the known-noise list hid."""
    groups, hidden = {}, 0
    max_level = LEVELS.get(level.capitalize(), 2)
    known = LOG_IGNORE if ignore else []
    for line in lines:
        m = LOG_LINE.match(line)
        if not m:
            continue
        cat, lvl, msg = m.group(1), m.group(2) or "Log", m.group(3).strip()
        if LEVELS[lvl] > max_level or cat in NOISE or (cat == "LogJson" and NOISE_MESSAGE.match(msg)):
            continue
        if known and noise_match(known, cat, lvl, msg):
            hidden += 1
            continue
        key = (lvl, cat, log_shape(msg))
        group = groups.setdefault(key, {"level": lvl, "category": cat, "shape": key[2], "count": 0, "first": msg})
        group["count"] += 1
    rows = sorted(groups.values(), key=lambda g: (LEVELS[g["level"]], -g["count"]))
    return rows, hidden


def modal_dialogs(pid):
    """Text of the native modal dialogs (#32770 windows) a process is showing; Windows only."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text(hwnd, getter=user32.GetWindowTextW, size=1024):
        buf = ctypes.create_unicode_buffer(size)
        getter(hwnd, buf, size)
        return buf.value

    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and text(hwnd, user32.GetClassNameW, 64) == "#32770":
            parts = []
            user32.EnumChildWindows(hwnd, callback(lambda child, _: parts.append(text(child)) or True), 0)
            found.append(" ".join(p for p in parts if p and p not in ("OK", "Cancel", "&Yes", "&No")) or text(hwnd))
        return True

    user32.EnumWindows(callback(visit), 0)
    return found


def editor_windows(pid):
    """(class, title, enabled) for each visible top-level window of a process; Windows only."""
    if os.name != "nt" or not pid:
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            cls, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 64)
            user32.GetWindowTextW(hwnd, title, 512)
            found.append((cls.value, title.value, bool(user32.IsWindowEnabled(hwnd))))
        return True

    user32.EnumWindows(callback(visit), 0)
    return found


def blocking_dialogs(pid):
    """Titles of the dialogs a (windowed) editor is blocked on: native message boxes, plus Slate modals.

    A Slate modal (e.g. "Save Content") is an ordinary UnrealWindow, but while it is open Slate disables
    every other editor window, so an enabled titled window beside a disabled main frame is the modal."""
    found = modal_dialogs(pid)
    windows = editor_windows(pid)
    main_disabled = any(c == "UnrealWindow" and t.endswith(" - Unreal Editor") and not e for c, t, e in windows)
    if main_disabled:
        found += [t for c, t, e in windows if c == "UnrealWindow" and e and t and not t.endswith(" - Unreal Editor")]
    return found


def startup_dialogs(pid):
    """Titles of the dialogs a booting editor waits on: native message boxes, plus any titled Slate window shown
    before the main frame exists (e.g. "Restore Packages" after an editor was terminated)."""
    found = modal_dialogs(pid)
    windows = editor_windows(pid)
    if not any(c == "UnrealWindow" and t.endswith(" - Unreal Editor") for c, t, e in windows):
        found += [t for c, t, e in windows if c == "UnrealWindow" and t]
    return found


def decline_package_restore(project):
    """Turn off the "Restore Packages" offer a terminated editor leaves in Saved/Autosaves/PackageRestoreData.json
    (it is offered at the next start, before anything loads). True if it held packages to offer. The file is UTF-16
    without a BOM."""
    path = os.path.join(os.path.dirname(project), "Saved", "Autosaves", "PackageRestoreData.json")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le" if b"\x00" in raw[:4] else "utf-8-sig"
    try:
        data = json.loads(raw.decode(encoding))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not data.get("RestoreEnabled"):
        return False
    data["RestoreEnabled"] = False
    text = json.dumps(data, indent="\t").replace("\n", "\r\n")
    with open(path, "wb") as f:
        f.write(text.encode("utf-8" if encoding == "utf-8-sig" else encoding))
    return bool(data.get("Packages"))


def stall_report(project):
    """Why a running editor is not answering, or None if it looks healthy (or is not running)."""
    beacon = chosen_editor(project) or {}
    pid = beacon.get("pid")
    if not pid_alive(pid):
        return None
    dialogs = blocking_dialogs(pid)
    if dialogs:
        return "blocked by modal: " + ", ".join(f'"{d[:80]}"' for d in dialogs[:3])
    age = time.time() - beacon.get("heartbeat", 0)
    return f"no heartbeat for {age:.0f}s (game thread busy or hung)" if age > 10 else None


CRASH_NOISE = re.compile(r"^\[[^\]]*\]\[[^\]]*\](LogModuleManager|LogPython: Display: Running start-up|LogUObjectHash|"
                         r"LogStreaming|LogCsvProfiler|LogEOSSDK|LogInit: Display: Engine is initialized)")


def crash_reports(project):
    """Crash folders under Saved/Crashes, newest first, as dicts parsed from CrashContext.runtime-xml."""
    root = os.path.join(os.path.dirname(project), "Saved", "Crashes")
    reports = []
    for name in os.listdir(root) if os.path.isdir(root) else []:
        folder = os.path.join(root, name)
        context = os.path.join(folder, "CrashContext.runtime-xml")
        if not os.path.isfile(context):
            continue
        with open(context, encoding="utf-8", errors="replace") as f:
            xml = f.read()
        tag = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", xml, re.S) or [None, ""])[1].strip()
        ticks = tag("TimeOfCrash")  # .NET ticks (100 ns since 0001-01-01, UTC)
        when = (int(ticks) - 621355968000000000) / 1e7 if ticks.isdigit() else os.path.getmtime(context)
        reports.append({"folder": folder, "time": when, "type": tag("CrashType"),
                        "error": re.sub(r"\s+", " ", tag("ErrorMessage")), "stack": tag("CallStack").splitlines()})
    return sorted(reports, key=lambda r: -r["time"])


def describe_crash(project, report, lines=8):
    """A short account: when, what, the module chain, project modules involved, and what the log said last."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["time"]))
    out = [f"crash {when} ({report['type'] or 'unknown'}): {report['error'][:300]}"]
    chain = []  # each module once, in the order it first appears from the innermost frame
    for frame in report["stack"]:
        module = frame.strip().replace("UnrealEditor_", "").replace("UnrealEditor-", "")
        if module and module not in chain:
            chain.append(module)
    if chain:
        out.append("  modules (innermost first): " + " <- ".join(chain[:10]) + (" ..." if len(chain) > 10 else ""))
    source = os.path.join(os.path.dirname(project), "Source")
    mine = sorted({d for d in (os.listdir(source) if os.path.isdir(source) else [])
                   if os.path.isdir(os.path.join(source, d))} & set(chain))
    if mine:
        out.append("  your modules on the stack: " + ", ".join(mine))
    logs = [f for f in os.listdir(report["folder"]) if f.endswith(".log")]
    if logs:
        with open(os.path.join(report["folder"], logs[0]), encoding="utf-8", errors="replace") as f:
            tail = [l.split("]", 2)[-1].strip() for l in f.read().splitlines()
                    if l.strip() and not CRASH_NOISE.match(l) and "GetLastError" not in l]
        tail = [l for i, l in enumerate(tail) if l and (i == 0 or l != tail[i - 1])]  # Python logs some lines twice
        out.append(f"  last log lines ({logs[0]}):")
        out += ["    " + l[:180] for l in tail[-lines:]]
    out.append(f"  folder: {report['folder']}")
    return "\n".join(out)


def crash_summary(report, width=120):
    """One line: the error without the engine-source '[File:...] [Line: N]' noise."""
    text = re.sub(r"\s*\[File:[^\]]*\]\s*\[Line:\s*\d+\]\s*", " ", report["error"]).strip()
    return text[:width]


def recent_crash(project, since):
    """The newest crash after `since` (epoch seconds), or None. Ensure reports (the editor carried on) are not
    crashes."""
    reports = [r for r in crash_reports(project) if r["type"].lower() != "ensure"]
    return reports[0] if reports and reports[0]["time"] >= since else None


def run_git(project, *argv, binary=False, check=True):
    """git in the project folder: stdout (bytes with binary), or None on failure when check is False."""
    try:
        proc = subprocess.run(["git", "-C", os.path.dirname(project), *argv], capture_output=True)
    except OSError:
        raise CliError("git is not installed") from None
    if proc.returncode:
        if not check:
            return None
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise CliError(f"git {argv[0]}: {err.splitlines()[-1] if err else 'failed'}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def human_bytes(n):
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}" if n < 100 * size else f"{n / size:.0f} {unit}"
    return f"{n} B"


def running_editors():
    """[(pid, command line)] of every running Unreal Editor process on this machine."""
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_Process -Filter \"Name like 'UnrealEditor%'\" | "
                                  "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                                 capture_output=True, text=True, timeout=30).stdout
        else:
            out = "\n".join(l.strip().replace(" ", "\t", 1) for l in subprocess.run(
                ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=30).stdout.splitlines()
                if "UnrealEditor" in l)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        pid, _, command = line.partition("\t")
        if pid.strip().isdigit():
            rows.append((int(pid), command.strip()))
    return rows


def parse_res(text):
    m = re.fullmatch(r"(\d{2,5})[xX](\d{2,5})", (text or "").strip())
    if not m:
        raise CliError(f"--res WIDTHxHEIGHT, e.g. 2560x1440 (not '{text}')")
    return int(m.group(1)), int(m.group(2))


def process_tree(pid):
    """A process and all its descendants' pids (a packaged game's launcher runs the real game as a child)."""
    if os.name != "nt":
        return [pid]
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Entry))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    parents = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    tree, queue = [], [pid]
    while queue:
        p = queue.pop()
        if p not in tree:
            tree.append(p)
            queue += [c for c in parents.get(p, []) if c != p]
    return tree


def live_coding_holders(engine):
    """[(console pid, project name, editor pid)] for the LiveCodingConsoles running from this engine install. UBT's
    mutex is Global\\LiveCoding_ + the UnrealEditor.exe path, so any of them blocks editor builds of every project on
    the install. Windows only (Live Coding is)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name like 'LiveCodingConsole%'\" | ForEach-Object "
                              "{ \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.ExecutablePath)`t$($_.CommandLine)\" }"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    root = os.path.normcase(os.path.abspath(engine))
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4 or not parts[0].strip().isdigit():
            continue
        pid, parent, exe, command = parts
        if exe and not os.path.normcase(os.path.abspath(exe)).startswith(root):
            continue  # another engine install: its own mutex
        m = re.search(r'-ProjectName="?([^"\s]+)', command) or re.search(r"-Group=UE_(\w+?)_0x", command)
        rows.append((int(pid), m.group(1) if m else "?", int(parent) if parent.strip().isdigit() else None))
    return rows


def process_name(pid):
    """The executable name of a running process, or None when it cannot be told."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.QueryFullProcessImageNameW.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
                                                            ctypes.POINTER(wintypes.DWORD))
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                return os.path.basename(buf.value) if ok else None
            finally:
                kernel32.CloseHandle(handle)
        if os.path.exists(f"/proc/{int(pid)}/comm"):
            with open(f"/proc/{int(pid)}/comm", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="], capture_output=True, text=True).stdout.strip()
        return os.path.basename(out) or None
    except (OSError, ValueError, AttributeError):
        return None


def compile_cpu(pid):
    """(editor CPU seconds, shader-worker CPU seconds, worker count) for an editor and its ShaderCompileWorker
    children, from the processes' own CPU times; Windows only (None elsewhere). Shader compiles run in those
    workers and asset builds (textures, meshes, Nanite) on the editor's threads, and neither queue is visible to
    editor Python, so their CPU use is what shows whether compilation is still going on."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Entry))
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    def cpu(p):
        handle = kernel32.OpenProcess(0x1000, False, int(p))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 0.0
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return 0.0
            return sum((t.dwHighDateTime << 32 | t.dwLowDateTime) for t in times[2:]) / 1e7  # kernel + user
        finally:
            kernel32.CloseHandle(handle)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    workers = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == pid and entry.szExeFile.lower().startswith("shadercompileworker"):
                workers.append(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return cpu(pid), sum(cpu(w) for w in workers), len(workers)


def editor_process(pid):
    """True while pid is a running Unreal Editor; a dead editor's pid that a later process reused is not."""
    if not pid_alive(pid):
        return False
    name = process_name(pid)
    return name is None or "editor" in name.lower()


def editor_beacons(project):
    """Every editor beacon this project has, newest heartbeat first: one file per editor process
    (Saved/Tether/editors/<pid>.json), plus the single editor.json that Tether before 0.11 wrote.

    An editor that was killed or crashed leaves its files behind (they are removed on a clean exit). Of those,
    only the newest is kept, for status and crash reports, and only if it is newer than the last clean exit
    (Saved/Tether/last-exit.json); the rest are deleted."""
    folder = os.path.join(state_dir(project), "editors")
    found = {}
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if name.endswith(".json"):
            beacon = read_json(os.path.join(folder, name))
            if beacon and beacon.get("pid"):
                found[beacon["pid"]] = beacon
    legacy = read_json(os.path.join(state_dir(project), "editor.json"))
    if legacy and legacy.get("pid") and legacy["pid"] not in found:
        found[legacy["pid"]] = dict(legacy, legacy=True)
    ordered = sorted(found.values(), key=lambda b: -b.get("heartbeat", 0))
    clean_exit = (read_json(os.path.join(state_dir(project), "last-exit.json")) or {}).get("time", 0)
    kept, dead_kept = [], False
    for beacon in ordered:
        if editor_process(beacon.get("pid")):
            kept.append(beacon)
        elif not dead_kept and beacon.get("heartbeat", 0) > clean_exit:
            kept.append(beacon)
            dead_kept = True
        else:
            forget_beacon(project, beacon)
    return kept


def forget_beacon(project, beacon):
    """Delete a dead editor's beacon and token files."""
    if beacon.get("legacy"):
        paths = [os.path.join(state_dir(project), n) for n in ("editor.json", "session.json")]
    else:
        paths = [os.path.join(state_dir(project), "editors", f"{beacon['pid']}{ext}") for ext in (".json", ".token")]
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def live_editors(project):
    """Beacons of the project's editors whose process is still running (ticking or hung)."""
    return [b for b in editor_beacons(project) if editor_process(b.get("pid"))]


def chosen_editor(project):
    """The editor the CLI talks to, or the last one seen when none is running (for status and crash
    reports). With several open: --pid / UE_EDITOR_PID if given, else the one the user opened rather
    than one `ue open` launched, else the most recently started."""
    wanted = os.environ.get("UE_EDITOR_PID")
    beacons = editor_beacons(project)
    live = [b for b in beacons if editor_process(b.get("pid"))]
    if wanted:
        match = next((b for b in beacons if str(b.get("pid")) == wanted), None)
        if match is None or match not in live:
            running = ", ".join(str(b["pid"]) for b in live) or "none"
            raise CliError(f"no editor with pid {wanted} is open on this project (running: {running})", 2)
        return match
    if live:
        return sorted(live, key=lambda b: (bool(b.get("launched_by_tether")), -b.get("started", 0)))[0]
    return beacons[0] if beacons else None


def editor_token(project, beacon):
    """The session token of the editor this beacon belongs to."""
    pid = (beacon or {}).get("pid")
    token = read_json(os.path.join(state_dir(project), "editors", f"{pid}.token")) or {}
    if not token:  # Tether before 0.11: one session.json per project
        token = read_json(os.path.join(state_dir(project), "session.json")) or {}
        if token.get("pid") not in (None, pid):
            token = {}
    return token.get("token", "")


def editor_label(beacon):
    """'pid 123, yours' / 'pid 456, headless, started by ue open'."""
    if not beacon:
        return "none"
    parts = [f"pid {beacon.get('pid')}"]
    if beacon.get("headless"):
        parts.append("headless")
    parts.append("started by ue open" if beacon.get("launched_by_tether") else
                 "opened by hand" if "launched_by_tether" in beacon else "")
    return ", ".join(p for p in parts if p)


def editor_mode(project, beacon):
    """How the editor was started: {headless, render, sound}. Its beacon says (Tether 0.11+); older plugins
    leave it to launch.json, which only `ue open` writes."""
    if beacon and "nullrhi" in beacon:
        return {"headless": bool(beacon.get("headless")), "render": not beacon["nullrhi"],
                "sound": bool(beacon.get("sound", True))}
    return read_json(os.path.join(state_dir(project), "launch.json")) or {}


def editor_live(project):
    """The chosen editor's beacon if it is running and ticking, else None."""
    beacon = chosen_editor(project) or {}
    alive = editor_process(beacon.get("pid")) and time.time() - beacon.get("heartbeat", 0) < 15
    return beacon if alive else None


def project_of(path):
    """The nearest .uproject above a file (which may not exist yet), or None."""
    here = os.path.dirname(os.path.abspath(path))
    for _ in range(12):
        if os.path.isdir(here):
            hits = [f for f in os.listdir(here) if f.endswith(".uproject")]
            if hits:
                return os.path.join(here, hits[0])
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    return None
