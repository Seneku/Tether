#!/usr/bin/env python3
"""ue: a token-frugal CLI for driving a running Unreal Editor (UE 5.8+) from coding agents.

Talks to Epic's in-editor MCP server (ModelContextProtocol plugin) plus the ToolSuite
toolset, and reads Saved/ToolSuite/editor.json and Saved/Logs directly, so status and
logs keep working when the editor is hung or gone. Stdlib only.

Exit codes: 0 ok, 1 tool/command failed, 2 editor unavailable, 3 restart required, 4 timeout.
"""
import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import zlib

TOOLSUITE = "toolsuite.toolsets.ToolSuiteCore"
LEVELS = {"Fatal": 0, "Error": 1, "Warning": 2, "Display": 3, "Log": 4, "Verbose": 5, "VeryVerbose": 6}
NOISE = {"LogModelContextProtocol", "LogToolsetRegistry"}  # our own transport chatter
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
    return os.path.join(os.path.dirname(project), "Saved", "ToolSuite")


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


def engine_root(project):
    """Engine dir from UE_ROOT, the launcher manifest, or registered source builds."""
    if os.environ.get("UE_ROOT"):
        return os.environ["UE_ROOT"]
    with open(project, encoding="utf-8") as f:
        match = re.search(r'"EngineAssociation"\s*:\s*"([^"]*)"', f.read())
    assoc = match.group(1) if match else ""
    manifest = read_json(r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat") or {}
    for item in manifest.get("InstallationList", []):
        if item.get("AppName") == f"UE_{assoc}":
            return item["InstallLocation"]
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds") as key:
                return winreg.QueryValueEx(key, assoc)[0]
        except OSError:
            pass
    raise CliError(f"cannot find engine '{assoc}'; set UE_ROOT", 2)


def pick_port(project):
    """Stable per-project port (so two editors never fight over 8000), skipping busy ones."""
    base = 18000 + zlib.crc32(project.lower().encode()) % 1000
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


# ---------------------------------------------------------------- MCP client

class Mcp:
    def __init__(self, port, timeout):
        self.port, self.timeout, self.session, self.next_id = port, timeout, None, 0

    def _post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            conn.request("POST", "/mcp", json.dumps(body), headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
        except socket.timeout:
            raise CliError(f"timed out after {self.timeout}s (editor busy or hung?)", 4)
        except OSError as exc:
            raise CliError(f"editor MCP server not reachable on :{self.port} ({exc.__class__.__name__})", 2)
        finally:
            conn.close()
        self.session = resp.getheader("Mcp-Session-Id") or self.session
        if "text/event-stream" in (resp.getheader("Content-Type") or ""):
            data = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
            raw = data[-1] if data else ""
        return json.loads(raw) if raw.strip() else None

    def request(self, method, params=None):
        self.next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        if msg and "error" in msg:
            raise CliError(f"{method}: {msg['error'].get('message')}")
        return (msg or {}).get("result", {})

    def connect(self):
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "ue-cli", "version": "0.1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, toolset, tool, args):
        res = self.request("tools/call", {"name": "call_tool", "arguments": {
            "toolset_name": toolset, "tool_name": tool, "arguments": args}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))


class Ctx:
    """Lazily resolved project, beacon and MCP connection."""

    def __init__(self, args):
        self.args = args
        self._project = self._mcp = None

    @property
    def project(self):
        if self._project is None:
            self._project = find_project(self.args.project)
        return self._project

    @property
    def beacon(self):
        return read_json(os.path.join(state_dir(self.project), "editor.json"))

    @property
    def port(self):
        if self.args.port:
            return self.args.port
        if os.environ.get("UE_MCP_PORT"):
            return int(os.environ["UE_MCP_PORT"])
        beacon = self.beacon
        if (beacon or {}).get("mcp_port"):
            return beacon["mcp_port"]
        return port_from_log(self.project) or 8000

    @property
    def mcp(self):
        if self._mcp is None:
            self._mcp = Mcp(self.port, self.args.timeout).connect()
        return self._mcp

    def toolsuite(self, tool, **kwargs):
        text, is_error = self.mcp.call(TOOLSUITE, tool, kwargs)
        if is_error:
            raise CliError(compact_error(text))
        return unwrap(text)


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


def compact_error(text):
    text = text.split("\nFunction schema Json", 1)[0]
    return re.sub(r"\s+", " ", text).strip()[:600]


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


# ---------------------------------------------------------------- toolset catalog (cached per editor PID)

def catalog(ctx):
    path = os.path.join(state_dir(ctx.project), "cache", "toolsets.json")
    pid = (ctx.beacon or {}).get("pid")
    cached = read_json(path)
    if cached and cached.get("pid") == pid and pid:
        return cached["toolsets"]
    text = "".join(c.get("text", "") for c in ctx.mcp.request("tools/call", {
        "name": "list_toolsets", "arguments": {}}).get("content", []))
    toolsets = {}
    for name, desc in re.findall(r"^- ([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+): (.*)$", text, re.M):
        toolsets[name] = desc.strip()
    write_json(path, {"pid": pid, "toolsets": toolsets})
    return toolsets


def schema(ctx, toolset):
    safe = re.sub(r"[^A-Za-z0-9_.]", "_", toolset)
    path = os.path.join(state_dir(ctx.project), "cache", f"{safe}.json")
    pid = (ctx.beacon or {}).get("pid")
    cached = read_json(path)
    if cached and cached.get("pid") == pid and pid:
        return cached["tools"]
    res = ctx.mcp.request("tools/call", {"name": "describe_toolset", "arguments": {"toolset_name": toolset}})
    data = unwrap("".join(c.get("text", "") for c in res.get("content", [])))
    tools = {t["name"].rsplit(".", 1)[-1]: t for t in (data.get("tools", []) if isinstance(data, dict) else [])}
    write_json(path, {"pid": pid, "tools": tools})
    return tools


def resolve_toolset(ctx, query):
    names = list(catalog(ctx))
    q = query.lower()
    exact = [n for n in names if n.lower() == q or n.lower().endswith("." + q)]
    if not exact:
        exact = [n for n in names if n.rsplit(".", 1)[-1].lower() in (q, q + "tools", q + "toolset")]
    if not exact:
        exact = [n for n in names if q in n.lower()]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise CliError(f"no toolset matches '{query}' (try: ue tools)")
    raise CliError(f"'{query}' is ambiguous: " + ", ".join(n.rsplit(".", 1)[-1] for n in exact[:10]))


def signature(tool):
    props = tool.get("inputSchema", {}).get("properties", {})
    required = set(tool.get("inputSchema", {}).get("required", []))
    params = [p if p in required else p + "?" for p in props]
    desc = re.sub(r"\s+", " ", tool.get("description", "")).strip()
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", desc, maxsplit=1)[0][:110]
    return f"{tool['name'].rsplit('.', 1)[-1]}({', '.join(params)}) - {first}"


def fill_args(tool, given):
    props = tool.get("inputSchema", {}).get("properties", {})
    args = {}
    for key, raw in given.items():
        spec = props.get(key, {})
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        if isinstance(value, str) and "refPath" in spec.get("properties", {}):
            value = {"refPath": value}
        args[key] = value
    for key in tool.get("inputSchema", {}).get("required", []):
        if key not in args:
            kind = props.get(key, {}).get("type")
            if kind in ("string", "array", "boolean"):
                args[key] = {"string": "", "array": [], "boolean": False}[kind]
    return args


# ---------------------------------------------------------------- logs (byte-offset cursor; no editor needed)

def log_path(project):
    return os.path.join(os.path.dirname(project), "Saved", "Logs",
                        os.path.splitext(os.path.basename(project))[0] + ".log")


def read_new_log(project, from_start=False, advance=True):
    path = log_path(project)
    cursor_file = os.path.join(state_dir(project), "log-cursor.json")
    if not os.path.exists(path):
        raise CliError(f"no log at {path}", 2)
    stat = os.stat(path)
    cursor = read_json(cursor_file) or {}
    offset = cursor.get("offset", 0)
    if from_start or cursor.get("ctime") != stat.st_ctime or offset > stat.st_size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # keep a partial trailing line for next time
    if advance:
        write_json(cursor_file, {"ctime": stat.st_ctime, "offset": offset + end})
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
            return f" @ {os.path.basename(m.group('file'))}:{m.group('line')} ({method})"
    return ""


def filter_log(lines, level="Warning", category=None, pattern=None):
    max_level = LEVELS.get(level.capitalize() if level.lower() != "veryverbose" else "VeryVerbose", 2)
    rx = re.compile(pattern, re.I) if pattern else None
    order, counts = [], {}
    for index, line in enumerate(lines):
        m = LOG_LINE.match(line)
        if not m:
            continue
        cat, lvl, msg = m.group(1), m.group(2) or "Log", m.group(3)
        if cat in NOISE or (cat == "Cmd" and msg.strip() == "FLUSHLOG"):
            continue
        if lvl in ("Error", "Fatal"):
            msg += user_frame(lines, index)
        if LEVELS[lvl] > max_level or (category and cat.lower() != category.lower()):
            continue
        if rx and not rx.search(line):
            continue
        key = (lvl[0], cat, msg.strip())
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1
    return [f"{l} {c}: {m}" + (f"  x{counts[(l, c, m)]}" if counts[(l, c, m)] > 1 else "") for l, c, m in order]


# ---------------------------------------------------------------- commands

def cmd_status(ctx, args):
    beacon = ctx.beacon
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    if not beacon:
        print(f"{name}: no beacon (editor never started with ToolSuite, or not yet ready)")
        return 2
    alive = pid_alive(beacon.get("pid"))
    age = time.time() - beacon.get("heartbeat", 0)
    port = beacon.get("mcp_port")
    if not alive:
        state = "not running"
    elif age > 10:
        state = f"HUNG? (no heartbeat for {age:.0f}s)"
    else:
        state = "pie" if beacon.get("pie") else "ready"
    mcp = "up" if alive and port and port_open(port) else "down"
    print(f"{name} | {state} | UE {beacon.get('engine', '?').split('+')[0]} | pid {beacon.get('pid')} | "
          f"map {beacon.get('map')} | mcp :{port} {mcp} | heartbeat {age:.1f}s ago")
    return 0 if alive and age <= 10 else 2


# ---------------------------------------------------------------- builds

# MSVC/UHT/MSBuild diagnostics, optionally behind a "[Tool] " prefix and before a " [x.csproj]" suffix.
DIAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?P<file>(?:[A-Za-z]:)?[^(:\[]+)\((?P<line>\d+)(?:,\d+)?\)\s*:\s*"
                  r"(?P<sev>fatal error|error|warning)\s*(?P<code>[A-Z]+\d+)?\s*:?\s*(?P<msg>.*?)(?:\s+\[[^\]]+\])?$", re.I)
LINK = re.compile(r"\b(error|warning) (LNK\d+): (.*)")
REFLECT = re.compile(r"\b(UCLASS|USTRUCT|UENUM|UINTERFACE|UPROPERTY|UFUNCTION|GENERATED_BODY)\b")


def ubt_log_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealBuildTool", "Log.txt")


def compact_diagnostics(lines, project_dir):
    """MSVC/UHT/linker output -> unique project-relative lines; engine-header warnings only counted."""
    root = os.path.normcase(os.path.abspath(project_dir))
    seen, errors, warnings, hidden = set(), [], [], 0
    for line in lines:
        m = DIAG.match(line)
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
            if not m:
                continue
            is_error, text = m.group(1) == "error", f"{m.group(2)} {m.group(3).strip()}"
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
    uat = os.path.join(engine_root(ctx.project), "Engine", "Build", "BatchFiles", "RunUAT.bat")
    started = time.time()
    proc = subprocess.run([uat, "BuildUserSolution", "-ScriptDir=" + os.path.join(plugin, "Build", "Scripts"),
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
    """In-editor C# hot reload through the ToolSuiteUnrealSharp bridge (no modal dialogs)."""
    log = log_path(ctx.project)
    start, started = log_end(ctx.project), time.time()
    while ctx.toolsuite("cs_state") == "idle" and time.time() - started < 3:
        time.sleep(0.2)  # give the directory watcher a moment to register the saves

    def new_log():
        with open(log, "rb") as f:
            f.seek(start)
            return f.read().decode("utf-8", "replace")

    while True:
        state = ctx.toolsuite("cs_reload")
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


def ubt_build(ctx, args):
    root = engine_root(ctx.project)
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    target = args.target or f"{name}Editor"
    exe = os.path.join(root, "Engine", "Binaries", "DotNET", "UnrealBuildTool", "UnrealBuildTool.exe")
    started = time.time()
    proc = subprocess.run([exe, target, "Win64", args.config, f"-Project={ctx.project}", "-WaitMutex"],
                          capture_output=True, text=True, errors="replace")
    lines = (proc.stdout + proc.stderr).splitlines()
    errors, warnings, hidden = compact_diagnostics(lines, os.path.dirname(ctx.project))
    result = next((l.strip() for l in reversed(lines) if l.strip().startswith("Result:")), f"exit {proc.returncode}")
    ok = proc.returncode == 0
    print(f"ubt {target}: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s ({result})"
          + (f", {hidden} engine-header warnings hidden" if hidden else ""))
    if any("Unable to build while Live Coding is active" in l for l in lines):
        print("  Live Coding is active in an editor on this engine (the mutex is per engine, not per project):"
              " build through that editor with `ue build`, or close it")
    print_diagnostics(errors, warnings, args.limit)
    if ok:
        mark_green(ctx.project)
    return 0 if ok else 1


def live_coding_build(ctx, args, changes):
    log, ubt_log = log_path(ctx.project), ubt_log_path()
    start, ubt_mtime = log_end(ctx.project), os.path.getmtime(ubt_log) if os.path.exists(ubt_log) else 0
    started = time.time()
    ctx.toolsuite("console", command="LiveCoding.Compile")
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
    errors, warnings = [], []
    if os.path.exists(ubt_log) and os.path.getmtime(ubt_log) > ubt_mtime:
        with open(ubt_log, encoding="utf-8", errors="replace") as f:
            errors, warnings, _ = compact_diagnostics(f.read().splitlines(), os.path.dirname(ctx.project))
    print(f"live coding: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s"
          + ("" if ok else f" ({len(errors)} errors)"))
    print_diagnostics(errors, warnings, args.limit)
    if ok and changes and changes[2]:
        names = ", ".join(os.path.basename(p) for p in changes[2][:4])
        print(f"  note: reflected headers changed ({names}). Function bodies and new UFUNCTIONs are live "
              "(call new ones via cdo.call_method('Name')); new UPROPERTYs, layout/CDO changes, Python "
              "wrappers and Blueprint use need `ue build --restart`")
    if ok:
        mark_green(ctx.project)
    return 0 if ok else 1


def cmd_build(ctx, args):
    beacon = ctx.beacon
    running = bool(beacon and pid_alive(beacon.get("pid")))
    changes = classify_changes(ctx.project)
    has_csharp = csharp_layout(ctx.project) is not None
    if not running:
        code = ubt_build(ctx, args)
        return code or (uat_user_solution(ctx, args.limit) if has_csharp else 0)
    if not args.restart:
        if changes and changes[1]:
            names = ", ".join(os.path.basename(p) for p in changes[1])
            print(f"restart required: module/project files changed ({names}); run `ue build --restart`")
            return 3
        did = False
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
    launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
    reopen_map = beacon.get("map") if beacon.get("map") and not beacon["map"].startswith("/Temp") else None
    started = time.time()
    cmd_close(ctx, argparse.Namespace(save=args.save, discard=args.discard, force=False))
    code = ubt_build(ctx, args) or (uat_user_solution(ctx, args.limit) if has_csharp else 0)
    if code:
        print("  editor left closed because the build failed")
        return code
    code = cmd_open(ctx, argparse.Namespace(map=reopen_map, headless=launch.get("headless", False),
                                            unattended=launch.get("unattended"), no_build=True,
                                            limit=args.limit, wait=600, no_wait=False))
    print(f"restart cycle: {time.time() - started:.1f}s total")
    return code


def cmd_open(ctx, args):
    beacon = ctx.beacon
    if beacon and pid_alive(beacon.get("pid")):
        print("already running")
        return cmd_status(ctx, args)
    has_csharp = csharp_layout(ctx.project) is not None
    env = editor_env(ctx.project)  # fails fast, before the prebuild, when UnrealSharp could not start
    if not args.no_build:  # a stale module would raise the rebuild prompt (and quits headless editors)
        if os.path.isdir(os.path.join(os.path.dirname(ctx.project), "Source")):
            if ubt_build(ctx, argparse.Namespace(target=None, config="Development", limit=args.limit)):
                raise CliError("C++ build failed; not launching (fix the errors above, or pass --no-build)")
        if has_csharp and uat_user_solution(ctx, args.limit):
            raise CliError("C# build failed; not launching (fix the errors above, or pass --no-build)")
    exe = os.path.join(engine_root(ctx.project), "Engine", "Binaries", "Win64", "UnrealEditor.exe")
    port = pick_port(ctx.project)
    cmd = [exe, ctx.project] + ([args.map] if args.map else []) + [
        "-nosplash", "-ModelContextProtocolStartServer", f"-ModelContextProtocolPort={port}"]
    # UnrealSharp disables C# hot reload under -unattended, so headless UnrealSharp editors stay
    # attended; the bridge suppresses dialogs per operation instead.
    unattended = args.unattended if args.unattended is not None else bool(args.headless and not has_csharp)
    if args.headless:
        # Agent-only session: never block on the "restore unsaved packages?" prompt.
        cmd += ["-nullrhi", "-nosound", "-AutoDeclinePackageRecovery"]
        if not unattended:
            # Attended for UnrealSharp, but nobody can see a dialog: log each one and take its default.
            cmd += ["-RUNNINGUNATTENDEDSCRIPT"]
    if unattended:
        cmd += ["-unattended"]
    write_json(os.path.join(state_dir(ctx.project), "launch.json"),
               {"headless": bool(args.headless), "unattended": unattended, "port": port})
    started = time.time()
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    mode = ", ".join(m for m, on in (("headless", args.headless), ("unattended", unattended)) if on)
    print(f"launched pid {proc.pid} on mcp :{port}" + (f" ({mode})" if mode else ""))
    if args.no_wait:
        return 0
    while time.time() - started < args.wait:
        beacon = ctx.beacon
        if beacon and beacon.get("pid") == proc.pid and beacon.get("heartbeat", 0) > started and port_open(port):
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
        dialogs = modal_dialogs(proc.pid)
        if dialogs:
            # A modal dialog stalls the boot; a headless editor has nobody to answer it.
            if args.headless:
                proc.kill()
            raise CliError("editor stopped on a modal dialog during startup"
                           + (" (killed it: headless)" if args.headless else "; answer it in the editor")
                           + "".join(f"\n  {d}" for d in dialogs))
        time.sleep(0.5)
    raise CliError(f"not ready after {args.wait:.0f}s (still booting? ue status)", 4)


def cmd_close(ctx, args):
    beacon = ctx.beacon
    pid = (beacon or {}).get("pid")
    if not pid_alive(pid):
        print("not running")
        return 0
    if not args.force:
        dirty = ctx.toolsuite("py", code=(
            "U = unreal.EditorLoadingAndSavingUtils\n"
            "len(U.get_dirty_content_packages()) + len(U.get_dirty_map_packages())")).strip()
        dirty_count = 0 if dirty in ("", "0") else dirty
        if dirty_count and not (args.save or args.discard):
            raise CliError(f"{dirty_count} unsaved packages: pass --save or --discard")
        if args.save and dirty_count:
            ctx.toolsuite("py", code="unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)")
        if args.discard and dirty_count:
            # Quitting would stop on a "Save Content?" modal; dropping the work was requested.
            print(f"discarding {dirty_count} unsaved packages")
        else:
            ctx.toolsuite("py", code="unreal.SystemLibrary.quit_editor()")
            deadline = time.time() + 30
            while time.time() < deadline and pid_alive(pid):
                time.sleep(0.25)
    if pid_alive(pid):
        if not (args.force or args.discard):
            raise CliError("editor did not exit within 30s (modal dialog open?); retry with --force", 4)
        subprocess.run(["taskkill", "/PID", str(pid), "/F"] if os.name == "nt" else ["kill", "-9", str(pid)],
                       capture_output=True)
        print("terminated")
    else:
        print("closed")
    return 0


def cmd_tools(ctx, args):
    if not args.query:
        names = sorted(catalog(ctx))
        print(f"{len(names)} toolsets (ue tools <name> for tools, -v for descriptions):")
        for n in names:
            print(f"  {n}" + (f" - {catalog(ctx)[n][:80]}" if args.verbose else ""))
        return 0
    toolset_q, _, tool_q = args.query.partition(":")
    try:
        toolset = resolve_toolset(ctx, toolset_q)
    except CliError:
        hits = [f"  {n} - {d[:80]}" for n, d in sorted(catalog(ctx).items())
                if toolset_q.lower() in (n + d).lower()]
        if not hits:
            raise
        print(f"{len(hits)} toolsets mention '{toolset_q}':\n" + "\n".join(hits))
        return 0
    tools = schema(ctx, toolset)
    if tool_q:
        tool = tools.get(tool_q) or next((t for k, t in tools.items() if k.lower() == tool_q.lower()), None)
        if not tool:
            raise CliError(f"{toolset} has no tool '{tool_q}'")
        print(json.dumps({"description": re.sub(r"\s+", " ", tool.get("description", "")).strip(),
                          "input": tool.get("inputSchema")}, separators=(",", ":")))
        return 0
    print(f"{toolset} ({len(tools)} tools; ue tools {toolset_q}:<tool> for one schema)")
    for t in tools.values():
        print("  " + signature(t))
    return 0


def cmd_call(ctx, args):
    toolset_q, _, tool_name = args.target.rpartition(".")
    if not toolset_q:
        raise CliError("target must be <toolset>.<tool>, e.g. scene.find_actors")
    toolset = resolve_toolset(ctx, toolset_q)
    tools = schema(ctx, toolset)
    tool = tools.get(tool_name) or next((t for k, t in tools.items() if k.lower() == tool_name.lower()), None)
    if not tool:
        raise CliError(f"{toolset} has no tool '{tool_name}' (ue tools {toolset_q})")
    given = dict(kv.split("=", 1) for kv in args.kv)
    call_args = json.loads(args.args) if args.args else fill_args(tool, given)
    text, is_error = ctx.mcp.call(toolset, tool["name"].rsplit(".", 1)[-1], call_args)
    if is_error:
        raise CliError(compact_error(text))
    emit(args, unwrap(text))
    return 0


def cmd_py(ctx, args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            code = f.read()
    elif args.code == ["-"]:
        code = sys.stdin.read()
    else:
        code = " ".join(args.code)
    out = ctx.toolsuite("py", code=code.lstrip("﻿"))  # PowerShell pipes a BOM
    if out:
        print(out.rstrip("\n"))
    return 0


def cmd_props(ctx, args):
    print(ctx.toolsuite("props", target=args.target, match=args.match or "", limit=args.limit))
    return 0


def cmd_actors(ctx, args):
    print(ctx.toolsuite("actors", match=args.match or "", limit=args.limit))
    return 0


def cmd_exec(ctx, args):
    start = log_end(ctx.project)
    world = ctx.toolsuite("console", command=" ".join(args.command), pie=args.pie)
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    shown = filter_log(lines, level="Log")
    print(f"ran in {world}" + ("" if shown else " (no log output)"))
    for line in shown[: args.limit]:
        print(line)
    return 0


def flush_editor_log(ctx):
    """The editor writes its log asynchronously; ask it to flush so just-logged lines are on disk."""
    beacon = ctx.beacon
    if beacon and pid_alive(beacon.get("pid")) and beacon.get("mcp_port") and port_open(beacon["mcp_port"]):
        try:
            ctx.toolsuite("console", command="FLUSHLOG")
        except CliError:
            pass  # hung or busy editor: read whatever is on disk


def cmd_log(ctx, args):
    flush_editor_log(ctx)
    lines = read_new_log(ctx.project, from_start=args.since == "start")
    shown = filter_log(lines, args.level, args.cat, args.grep)
    print(f"{len(shown)} entries (>= {args.level}) from {len(lines)} new lines")
    for line in shown[-args.limit:] if args.since == "start" else shown[: args.limit]:
        print(line)
    if len(shown) > args.limit:
        print(f"... +{len(shown) - args.limit} more (--limit)")
    return 0


def cmd_pie(ctx, args):
    """Play-In-Editor through Epic's EditorAppToolset, reporting only what went wrong."""
    toolset, start = resolve_toolset(ctx, "editorapp"), log_end(ctx.project)
    started = time.time()
    if args.action == "start" and csharp_layout(ctx.project):
        # UnrealSharp recompiles a saved but unreloaded C# type in place on its first lookup, from the old
        # reflection data. That drops the type's subsystems from every live world, PIE included, for the
        # rest of the session. So PIE only ever starts on reloaded code.
        if csharp_changes(ctx.project) or ctx.toolsuite("cs_state") == "pending":
            if csharp_reload_build(ctx, argparse.Namespace(wait=120, limit=args.limit)):
                raise CliError("PIE not started: the C# reload failed (errors above)")
            start, started = log_end(ctx.project), time.time()
    if args.action == "start":
        options = {"bSimulate": args.simulate, "playMode": "PlayMode_InViewPort", "warmupSeconds": args.warmup}
        text, is_error = ctx.mcp.call(toolset, "StartPIE", {"options": options})
        if is_error:
            with open(log_path(ctx.project), "rb") as f:
                f.seek(start)
                fatal = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Error",
                                   pattern="Fatal error detected")
            hint = ""
            if any("InvalidCastException" in l or "NullReferenceException" in l and "@" not in l for l in fatal):
                hint = "\n  hint: stale hot-reloaded C# type (common after structural C# changes); run `ue build --restart`"
            raise CliError(compact_error(text) + "".join("\n  " + l for l in fatal[:3]) + hint)
    elif args.action == "stop":
        text, is_error = ctx.mcp.call(toolset, "StopPIE", {})
    else:
        text, is_error = ctx.mcp.call(toolset, "IsPIERunning", {})
        if is_error:
            raise CliError(compact_error(text))
        if unwrap(text) is True:
            print("pie: running")
            return 0
        # Say why the last session ended if it died rather than being stopped.
        with open(log_path(ctx.project), "rb") as f:
            text = f.read().decode("utf-8", "replace")
        last = text.rfind("Creating play world package")
        fatal = filter_log(text[last:].splitlines(), level="Error", pattern="Fatal error detected") if last >= 0 else []
        print("pie: not running" + (f" (last session ended by: {fatal[0][2:]})" if fatal else ""))
        return 0
    if is_error:
        raise CliError(compact_error(text))
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        issues = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Warning")
    print(f"pie {args.action}: OK in {time.time() - started:.1f}s" + (f", {len(issues)} warnings/errors" if issues else ""))
    for line in issues[: args.limit]:
        print("  " + line)
    return 0


def editor_live(project):
    """The project's beacon if its editor is running and ticking, else None."""
    beacon = read_json(os.path.join(state_dir(project), "editor.json")) or {}
    alive = pid_alive(beacon.get("pid")) and time.time() - beacon.get("heartbeat", 0) < 15
    return beacon if alive else None


def hook_pre_edit(payload):
    """Deny edits to C# under an UnrealSharp project's Script/ while that project's editor is in PIE:
    UnrealSharp reprocesses saved C# at once, which can crash the running session."""
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.lower().endswith(".cs"):
        return None
    path, project = os.path.abspath(path), None
    here = os.path.dirname(path)
    for _ in range(12):  # nearest .uproject above the file (the file may not exist yet)
        if os.path.isdir(here):
            hits = [f for f in os.listdir(here) if f.endswith(".uproject")]
            if hits:
                project = os.path.join(here, hits[0])
                break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    layout = csharp_layout(project) if project else None
    if not layout or not os.path.normcase(path).startswith(os.path.normcase(layout[0]) + os.sep):
        return None
    if not (editor_live(project) or {}).get("pie"):
        return None
    name = os.path.splitext(os.path.basename(project))[0]
    return (f"{name}'s editor is in Play-In-Editor. UnrealSharp reprocesses saved C# files immediately, "
            "which can crash the running PIE session. Run `ue pie stop` first, make the edit, then `ue build`.")


def hook_pre_mcp(payload):
    """Deny Epic's StartPIE (called directly or from a batch script) while saved C# is not hot-reloaded:
    UnrealSharp would recompile the stale type on its first lookup and drop its subsystems from PIE."""
    if "StartPIE" not in (payload.get("tool_name") or "") + json.dumps(payload.get("tool_input") or {}):
        return None
    os.chdir(payload.get("cwd") or os.getcwd())
    project = find_project(None)
    if not editor_live(project):
        return None
    changed = csharp_changes(project)
    if not changed:
        return None
    names = ", ".join(os.path.basename(p) for p in changed[:3]) + (f" +{len(changed) - 3} more" if len(changed) > 3 else "")
    return (f"C# changed since the last reload ({names}). Starting PIE now breaks UnrealSharp subsystems for the "
            "whole session: UnrealSharp recompiles the stale type on its first lookup and drops its subsystems "
            "from the PIE world. Use `ue pie start` instead (it hot-reloads first), or run `ue build`, then retry.")


def cmd_hook(ctx, args):
    """Claude Code PreToolUse hook entry points (hook JSON on stdin); see hook_pre_edit / hook_pre_mcp."""
    try:
        reason = {"pre-edit": hook_pre_edit, "pre-mcp": hook_pre_mcp}[args.event](json.load(sys.stdin))
        if reason:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    except Exception:
        pass  # a broken guard must never block a tool call
    return 0


def cmd_reload(ctx, args):
    print(ctx.toolsuite("reload"))
    cache = os.path.join(state_dir(ctx.project), "cache")
    for name in os.listdir(cache) if os.path.isdir(cache) else []:
        os.remove(os.path.join(cache, name))  # tool schemas may have changed
    return 0


def main(argv=None):
    # Global options are accepted before or after the subcommand (agents write both).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help=".uproject path (default: search upward from cwd, or UE_PROJECT)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="MCP port (default: beacon, UE_MCP_PORT, then 8000)")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds to wait (120)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw JSON output")
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="max rows to print (30)")
    common.add_argument("--time", action="store_true", default=argparse.SUPPRESS, help="elapsed time to stderr")

    p = argparse.ArgumentParser(prog="ue", description=__doc__.split("\n\n")[0], parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text, parents=[common])
        s.set_defaults(fn=fn)
        return s

    add("status", cmd_status, "editor state from the beacon (no HTTP needed)")
    s = add("open", cmd_open, "launch the editor with the MCP server on a per-project port and wait until ready")
    s.add_argument("--map", help="map to open, e.g. /Game/Maps/Lvl_Test")
    s.add_argument("--headless", action="store_true", help="no window: -nullrhi -nosound (+ -unattended, see below)")
    s.add_argument("--unattended", dest="unattended", action="store_true", default=None,
                   help="force -unattended (default for headless, except UnrealSharp projects)")
    s.add_argument("--attended", dest="unattended", action="store_false", help="never pass -unattended")
    s.add_argument("--no-build", action="store_true", help="skip the C++/C# pre-build")
    s.add_argument("--wait", type=float, default=600, help="seconds to wait for ready (600)")
    s.add_argument("--no-wait", action="store_true")
    s = add("build", cmd_build, "compile: Live Coding if the editor is up, UBT if not; --restart for reflection changes")
    s.add_argument("--restart", action="store_true", help="save/close editor, UBT build, reopen same map")
    s.add_argument("--save", action="store_true", help="with --restart: save dirty packages first")
    s.add_argument("--discard", action="store_true", help="with --restart: drop unsaved changes")
    s.add_argument("--target", help="UBT target (default <Project>Editor)")
    s.add_argument("--config", default="Development")
    s.add_argument("--wait", type=float, default=300, help="seconds to wait for Live Coding (300)")
    s = add("close", cmd_close, "quit the editor; refuses to drop unsaved work unless --save/--discard")
    s.add_argument("--save", action="store_true", help="save dirty packages first")
    s.add_argument("--discard", action="store_true", help="drop unsaved changes (terminates if a dialog blocks)")
    s.add_argument("--force", action="store_true", help="terminate immediately")
    s = add("tools", cmd_tools, "list toolsets, a toolset's tools, or one tool's schema (toolset:tool)")
    s.add_argument("query", nargs="?")
    s.add_argument("-v", "--verbose", action="store_true")
    s = add("call", cmd_call, "invoke <toolset>.<tool> k=v ... (required blanks auto-filled)")
    s.add_argument("target")
    s.add_argument("kv", nargs="*")
    s.add_argument("--args", help="raw JSON arguments instead of k=v")
    s = add("py", cmd_py, "run Python in the live editor (code, -f file, or - for stdin)")
    s.add_argument("code", nargs="*")
    s.add_argument("-f", "--file")
    s = add("props", cmd_props, "reflected properties of an object/actor/component")
    s.add_argument("target")
    s.add_argument("match", nargs="?")
    s = add("actors", cmd_actors, "actors in the editor level")
    s.add_argument("match", nargs="?")
    s = add("exec", cmd_exec, "console command; prints the log lines it produced")
    s.add_argument("command", nargs="+")
    s.add_argument("--pie", action="store_true", help="run in the PIE world")
    s = add("log", cmd_log, "new log entries since the last call (cursor kept on disk)")
    s.add_argument("--since", choices=["last", "start"], default="last")
    s.add_argument("--level", default="Warning", help="Error, Warning, Display, Log, Verbose")
    s.add_argument("--cat", help="only this log category")
    s.add_argument("--grep", help="regex filter")
    s = add("pie", cmd_pie, "Play-In-Editor: start | stop | status (prints only new warnings/errors)")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.add_argument("--simulate", action="store_true", help="Simulate-In-Editor (no player pawn)")
    s.add_argument("--warmup", type=float, default=0.5, help="seconds to let BeginPlay settle (0.5)")
    add("reload", cmd_reload, "hot-reload the ToolSuite Python package in the editor")
    s = add("hook", cmd_hook, "Claude Code hook entry points (hook JSON on stdin)")
    s.add_argument("event", choices=["pre-edit", "pre-mcp"])

    args = p.parse_args(argv)
    for key, default in (("project", None), ("port", None), ("timeout", 120.0), ("json", False),
                         ("limit", 30), ("time", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    started = time.perf_counter()
    try:
        code = args.fn(Ctx(args), args)
    except CliError as exc:
        sys.stdout.flush()  # keep the error after the output that explains it when both are piped
        print(f"error: {exc}", file=sys.stderr)
        code = exc.code
    if args.time:
        sys.stdout.flush()
        print(f"({(time.perf_counter() - started) * 1000:.0f} ms)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
