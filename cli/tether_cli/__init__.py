#!/usr/bin/env python3
"""ue: a token-frugal CLI for driving a running Unreal Editor (UE 5.8+) from coding agents.

Talks to Epic's in-editor MCP server (ModelContextProtocol plugin) plus the Tether
toolset, and reads Saved/Tether/editor.json and Saved/Logs directly, so status and
logs keep working when the editor is hung or gone. Stdlib only.

Exit codes: 0 ok, 1 tool/command failed, 2 editor unavailable, 3 restart required, 4 timeout.
"""
import argparse
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
GATED = {"py", "console", "set_props", "undo", "save", "spawn", "delete", "duplicate", "mv", "rm", "set_material", "pie_step", "set_config", "umg", "new_asset", "drop", "pie_net", "look_stage", "scatter", "bp_component"}  # tools that need the session token (see beacon.session_token)
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
        import http.client
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

    def request(self, method, params=None, retry=True):
        self.next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        if msg and "error" in msg:
            if retry and method != "initialize" and "Unknown session id" in str(msg["error"].get("message")):
                self.session = None  # a cached session the editor no longer knows (restarted, expired)
                self.connect()
                return self.request(method, params, retry=False)
            raise CliError(f"{method}: {msg['error'].get('message')}")
        return (msg or {}).get("result", {})

    def connect(self, cache=None):
        """Open an MCP session, or reuse the one cached for this editor process (two editor round trips saved:
        initialize and initialized each wait for an editor tick)."""
        if cache:
            saved = read_json(cache) or {}
            if saved.get("port") == self.port and saved.get("session"):
                self.session, self.cache = saved["session"], cache
                return self
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "ue-cli", "version": "0.1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cache = cache or getattr(self, "cache", None)
        if cache and self.session:
            with __import__("contextlib").suppress(OSError):
                write_json(cache, {"port": self.port, "session": self.session})
                folder = os.path.dirname(cache)
                for name in os.listdir(folder):  # sessions of editor processes that have ended
                    if name.startswith("mcp-session-") and os.path.join(folder, name) != cache:
                        os.remove(os.path.join(folder, name))
        return self

    def call(self, toolset, tool, args):
        res = self.request("tools/call", {"name": "call_tool", "arguments": {
            "toolset_name": toolset, "tool_name": tool, "arguments": args}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))


class RemoteExec:
    """Transport for editors without Epic's MCP server (UE 5.4-5.7): the Python plugin's remote execution
    protocol (UDP discovery, then a TCP command channel), through the engine's own stdlib-only client.

    Only Tether's own tools are reachable this way; Epic's toolsets need 5.8's MCP server."""

    def __init__(self, project, timeout):
        self.project, self.timeout, self.rex = project, timeout, None

    def connect(self):
        root = engine_root(self.project)
        here = os.path.join(root, "Engine", "Plugins", "Experimental", "PythonScriptPlugin", "Content", "Python")
        if not os.path.isfile(os.path.join(here, "remote_execution.py")):
            raise CliError(f"no remote_execution.py under {root}; is the PythonScriptPlugin installed?", 2)
        sys.path.insert(0, here)
        import remote_execution as rex_module
        with socket.socket() as probe:  # a free port for the command channel, so concurrent calls don't collide
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = rex_module.RemoteExecutionConfig()
        config.command_endpoint = ("127.0.0.1", port)
        self.rex = rex_module.RemoteExecution(config)
        self.mode = rex_module.MODE_EXEC_FILE
        self.rex.start()
        want = os.path.normcase(os.path.normpath(os.path.dirname(self.project)))
        deadline = time.time() + 6
        while time.time() < deadline:
            for node in self.rex.remote_nodes:
                if os.path.normcase(os.path.normpath(node.get("project_root", ""))) == want:
                    self.rex.open_command_connection(node["node_id"])
                    return self
            time.sleep(0.05)
        self.rex.stop()
        raise CliError("no editor answered Python remote execution for this project; enable it in Project "
                       "Settings > Python > Remote Execution (`ue init` does this), or start it with `ue open`", 2)

    def run(self, code):
        """(output, ok): what the code printed, or the error's last line."""
        data = self.rex.run_command(code, unattended=True, exec_mode=self.mode)
        output = "".join(o.get("output", "") + "\n" for o in data.get("output", []) if o.get("output"))
        if data.get("success"):
            return output, True
        lines = [l for l in str(data.get("result", "")).strip().splitlines() if l.strip()]
        return (lines[-1] if lines else "remote command failed"), False

    def tether(self, tool, kwargs):
        code = ("import json\nfrom tether import toolsets as _t\n"
                f"_f = getattr(_t, '_RAW', {{}}).get({tool!r}) or getattr(_t.TetherCore, {tool!r})\n"
                f"print('\\x1eTETHER' + json.dumps(_f(**json.loads({json.dumps(json.dumps(kwargs))}))))")
        output, ok = self.run(code)
        if not ok:
            raise CliError(re.sub(r"^\w+(Error|Exception): ", "", output))
        marker = output.rfind("\x1eTETHER")
        if marker < 0:
            raise CliError("no result from the editor: " + output[-300:])
        return json.loads(output[marker + 7:].splitlines()[0])

    def close(self):
        if self.rex:
            self.rex.stop()


def transport(project):
    """'mcp' for UE 5.8+ (Epic's MCP server), 'remote' for older editors (Python remote execution).
    UE_TRANSPORT=mcp|remote overrides, e.g. to try the remote path on a 5.8 editor."""
    forced = os.environ.get("UE_TRANSPORT", "").lower()
    if forced in ("mcp", "remote"):
        return forced
    beacon = read_json(os.path.join(state_dir(project), "editor.json")) or {}
    version = beacon.get("engine", "")
    if not version:
        try:
            with open(project, encoding="utf-8-sig") as f:
                version = json.load(f).get("EngineAssociation", "")
        except (OSError, ValueError):
            version = ""
    match = re.match(r"(\d+)\.(\d+)", version)
    return "remote" if match and (int(match.group(1)), int(match.group(2))) < (5, 8) else "mcp"


class Ctx:
    """Lazily resolved project, beacon and MCP connection."""

    def __init__(self, args):
        self.args = args
        self._project = self._mcp = self._rex = self._transport = None

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
    def transport(self):
        if self._transport is None:
            self._transport = transport(self.project)
        return self._transport

    def _check_modal(self):
        # A modal dialog freezes the game thread, and every call would hang until the timeout.
        report = stall_report(self.project)
        if report and report.startswith("blocked by modal"):
            raise CliError(f"editor {report}; answer it in the editor, then retry", 4)

    @property
    def mcp(self):
        if self.transport == "remote":
            raise CliError("this needs Epic's MCP server (UE 5.8+). This editor is reached through Python remote "
                           "execution, which covers ue py, exec, actors, props, level_summary, pie, build and log")
        if self._mcp is None:
            self._check_modal()
            beacon = self.beacon or {}
            cache = os.path.join(state_dir(self.project), f"mcp-session-{beacon.get('pid', 0)}.json")
            self._mcp = Mcp(self.port, self.args.timeout).connect(cache if beacon.get("pid") else None)
        return self._mcp

    def tether(self, tool, **kwargs):
        if tool in GATED:  # the editor writes a per-session token next to the beacon
            kwargs["token"] = (read_json(os.path.join(state_dir(self.project), "session.json")) or {}).get("token", "")
        if self.transport == "remote":
            if self._rex is None:
                self._check_modal()
                self._rex = RemoteExec(self.project, self.args.timeout).connect()
            value = self._rex.tether(tool, kwargs)
            return unwrap(json.dumps({"returnValue": value}))
        text, is_error = self.mcp.call(TETHER, tool, kwargs)
        if is_error:
            raise CliError(compact_error(text))
        return unwrap(text)

    def close(self):
        if self._rex is not None:
            self._rex.close()


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
    if cached and cached.get("pid") == pid and pid_alive(pid):  # a dead editor's list is stale
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
    if cached and cached.get("pid") == pid and pid_alive(pid):  # a dead editor's list is stale
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
        if spec.get("type") == "string":  # e.g. a JSON document passed as text: keep it text
            args[key] = raw
            continue
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


def filter_log(lines, level="Warning", category=None, pattern=None):
    max_level = LEVELS.get(level.capitalize() if level.lower() != "veryverbose" else "VeryVerbose", 2)
    rx = re.compile(pattern, re.I) if pattern else None
    order, counts = [], {}
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
        print(f"{name}: no beacon (editor never started with Tether, or not yet ready)")
        return 2
    alive = pid_alive(beacon.get("pid"))
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
        state = "pie" if beacon.get("pie") else "ready"
    if ctx.transport == "remote":
        link = "via python remote execution"
    else:
        link = f"mcp :{port} " + ("up" if alive and port and port_open(port) else "down")
    dirty = beacon.get("dirty") if alive else None
    print(f"{name} | {state} | UE {beacon.get('engine', '?').split('+')[0]} | pid {beacon.get('pid')} | "
          f"map {beacon.get('map')} | {link} | heartbeat {age:.1f}s ago"
          + (f" | {dirty} unsaved (ue changes)" if dirty else "")
          + (f" | revision control {beacon['vcs']}" if alive and beacon.get("vcs") else ""))
    return 0 if alive and age <= 10 else 2


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


def stall_report(project):
    """Why a running editor is not answering, or None if it looks healthy (or is not running)."""
    beacon = read_json(os.path.join(state_dir(project), "editor.json")) or {}
    pid = beacon.get("pid")
    if not pid_alive(pid):
        return None
    dialogs = blocking_dialogs(pid)
    if dialogs:
        return "blocked by modal: " + ", ".join(f'"{d[:80]}"' for d in dialogs[:3])
    age = time.time() - beacon.get("heartbeat", 0)
    return f"no heartbeat for {age:.0f}s (game thread busy or hung)" if age > 10 else None


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


def ubt_build(ctx, args):
    root = engine_root(ctx.project)
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    target = args.target or f"{name}Editor"
    started = time.time()
    proc = subprocess.run(ubt_command(root) + [target, host_platform(), args.config, f"-Project={ctx.project}", "-WaitMutex"],
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


UNITY_COMPILE = re.compile(r"\] Compile \[\w+\] (Module\.[\w.]+\.cpp)")


def live_coding_build(ctx, args, changes):
    log, ubt_log = log_path(ctx.project), ubt_log_path()
    start, ubt_mtime = log_end(ctx.project), os.path.getmtime(ubt_log) if os.path.exists(ubt_log) else 0
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
    errors, warnings, unity = [], [], []
    if os.path.exists(ubt_log) and os.path.getmtime(ubt_log) > ubt_mtime:
        with open(ubt_log, encoding="utf-8", errors="replace") as f:
            ubt_lines = f.read().splitlines()
        errors, warnings, _ = compact_diagnostics(ubt_lines, os.path.dirname(ctx.project))
        unity = sorted(set(UNITY_COMPILE.findall("\n".join(ubt_lines))))
    print(f"live coding: {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s"
          + ("" if ok else f" ({len(errors)} errors)"))
    print_diagnostics(errors, warnings, args.limit)
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
        if (changes is None or changes[0]) and not live_coding_available():
            print("restart required: Live Coding is Windows-only, so C++ changes on this host need "
                  "`ue build --restart`")
            return 3
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
                                            render=launch.get("render", False), sound=launch.get("sound", False),
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
        "-nosplash", "-ModelContextProtocolStartServer", f"-ModelContextProtocolPort={port}"]
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
                "sound": bool(getattr(args, "sound", False) or not args.headless), "unattended": unattended, "port": port})
    started = time.time()
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})  # outlive this CLI process either way
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **detach)
    mode = ", ".join(m for m, on in (("headless", args.headless), ("offscreen render", args.headless and args.render),
                                     ("unattended", unattended)) if on)
    print(f"launched pid {proc.pid} " + ("(python remote execution)" if remote else f"on mcp :{port}")
          + (f" ({mode})" if mode else ""))
    if args.no_wait:
        return 0
    while time.time() - started < args.wait:
        beacon = ctx.beacon
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
        dirty = ctx.tether("py", code=(
            "U = unreal.EditorLoadingAndSavingUtils\n"
            "len(U.get_dirty_content_packages()) + len(U.get_dirty_map_packages())")).strip()
        dirty_count = 0 if dirty in ("", "0") else dirty
        if dirty_count and not (args.save or args.discard):
            raise CliError(f"{dirty_count} unsaved packages: pass --save or --discard")
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
    if ctx.transport == "remote" and toolset_q.lower() in ("tether", TETHER.lower()):
        given = {}
        for pair in args.kv:  # no schema without MCP: JSON values, else strings
            key, _, raw = pair.partition("=")
            try:
                given[key] = json.loads(raw)
            except ValueError:
                given[key] = raw
        emit(args, ctx.tether(tool_name, **given))
        return 0
    toolset = resolve_toolset(ctx, toolset_q)
    tools = schema(ctx, toolset)
    tool = tools.get(tool_name) or next((t for k, t in tools.items() if k.lower() == tool_name.lower()), None)
    if not tool:
        raise CliError(f"{toolset} has no tool '{tool_name}' (ue tools {toolset_q})")
    given = dict(kv.split("=", 1) for kv in args.kv)
    call_args = json.loads(args.args) if args.args else fill_args(tool, given)
    name = tool["name"].rsplit(".", 1)[-1]
    if toolset == TETHER and name in GATED and not call_args.get("token"):  # fill_args blanks it to ""
        call_args["token"] = (read_json(os.path.join(state_dir(ctx.project), "session.json")) or {}).get("token", "")
    text, is_error = ctx.mcp.call(toolset, name, call_args)
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
    out = ctx.tether("py", code=code.lstrip("﻿"))  # PowerShell pipes a BOM
    if out:
        print(out.rstrip("\n"))
    return 0


def cmd_props(ctx, args):
    if args.export:
        values = ctx.tether("get_props", target=args.target, names="", pie=args.pie)
        values = json.loads(values) if isinstance(values, str) else values
        with open(args.export, "w", encoding="utf-8", newline="\n") as f:
            json.dump(values, f, indent=2)
            f.write("\n")
        print(f"exported {len(values)} properties of {args.target} to {args.export} (edit, then ue set {args.target} "
              f"--from {os.path.basename(args.export)})")
        return 0
    print(ctx.tether("props", target=args.target, match=args.match or "", limit=args.limit, pie=args.pie))
    return 0


def same_value(a, b):
    """Equal for property purposes: numbers within 0.01 (8-bit colours, float noise), recursively."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b  # True == 1 in Python; not for properties
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.01
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    return a == b


def cmd_set(ctx, args):
    values = {}
    if args.from_file:  # apply only what differs from the object now (e.g. an exported DataAsset, edited)
        with open(args.from_file, encoding="utf-8-sig") as f:
            wanted = json.load(f)
        current = ctx.tether("get_props", target=args.target, names=",".join(wanted), pie=args.pie)
        current = json.loads(current) if isinstance(current, str) else current
        values = {k: v for k, v in wanted.items() if not same_value(current.get(k), v)}
        if not values:
            print(f"{args.target}: already matches {os.path.basename(args.from_file)}")
            return 0
        if args.dry_run:
            for key, value in values.items():
                print(f"~ {key}: {short(current.get(key), 50)} -> {short(value, 50)}")
            return 0
    elif not args.assignments:
        raise CliError("give name=value pairs, or --from file.json")
    for pair in args.assignments:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise CliError(f"expected name=value or name+=delta, got '{pair}'")
        values[key.strip()] = raw
    try:
        print(ctx.tether("set_props", target=args.target, values=json.dumps(values), pie=args.pie))
    except CliError as exc:  # Epic prefixes its own report, with the object's full path
        raise CliError(re.sub(r"SetObjectProperties on '[^']*' \((\w+)\): the following properties could not be "
                              r"set: (.*?) (?=invalid value)", r"\1.\2: ", str(exc))) from None
    return 0


NO_ANNOTATIONS = {"gridSpacing": 0, "gridExtent": 0, "gridHeight": 0, "maxLabelDistance": 0,
                  "classFilter": {"refPath": ""}, "maxLabels": 0}
ANNOTATIONS = {"gridSpacing": 500, "gridExtent": 10000, "gridHeight": 0, "maxLabelDistance": 5000,
               "classFilter": {"refPath": ""}, "maxLabels": 15}


def pie_screenshot(ctx, args):
    """The PIE world as the player sees it. Epic's CaptureViewport renders the *editor* world even during
    PIE (from whatever pose it is given), so take a HighResShot in the PIE world and collect the file."""
    if args.actor or args.at or args.look_at or args.rot or args.annotate:
        raise CliError("during PIE, ue shot shows the player's view; --actor/--at/--look-at/--rot/--annotate "
                       "only work outside PIE")
    start = log_end(ctx.project)
    ctx.tether("console", command="HighResShot 1", pie=True)
    saved = re.compile(r"High resolution screenshot saved as (.+\.png)")
    deadline = time.time() + 15
    while time.time() < deadline:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            match = saved.search(f.read().decode("utf-8", "replace"))
        if match and os.path.isfile(match.group(1).strip()):
            path = match.group(1).strip()
            with open(path, "rb") as f:
                raw = f.read()
            os.remove(path)  # it moves to Saved/Tether/shots (or -o) below
            return raw
        time.sleep(0.2)
    raise CliError("the PIE screenshot did not appear within 15 s (ue log --level Warning)", 4)


def capture_viewport(ctx, pose, annotations=None, ui=False):
    """PNG bytes of the level viewport rendered from `pose` (Epic's CaptureViewport)."""
    text, is_error = ctx.mcp.call("EditorToolset.EditorAppToolset", "CaptureViewport",
                                  {"captureTransform": pose, "annotations": annotations or NO_ANNOTATIONS, "bShowUI": ui})
    if is_error:
        hint = " (the viewport is not rendering: minimised window or -nullrhi?)" if "zero size" in text else ""
        raise CliError(compact_error(text) + hint)
    value = unwrap(text)
    image = value.get("image") if isinstance(value, dict) else None
    if not image or not image.get("data"):
        raise CliError("CaptureViewport returned no image")
    import base64
    return base64.b64decode(image["data"])


def contact_sheet(tiles, columns=3, tile_width=400, gap=4):
    """Tile decoded images (width, height, channels, bytes) into one RGB image, each scaled to tile_width
    (nearest neighbour). Returns (width, height, rgb bytes)."""
    scaled = []
    for w, h, c, px in tiles:
        tw, th = tile_width, max(1, round(h * tile_width / w))
        out = bytearray(tw * th * 3)
        for y in range(th):
            sy = min(h - 1, y * h // th) * w
            for x in range(tw):
                si = (sy + min(w - 1, x * w // tw)) * c
                o = (y * tw + x) * 3
                out[o:o + 3] = px[si:si + 3] if c >= 3 else bytes([px[si]] * 3)
        scaled.append((tw, th, out))
    rows = (len(scaled) + columns - 1) // columns
    th = max(t[1] for t in scaled)
    width, height = columns * tile_width + (columns + 1) * gap, rows * th + (rows + 1) * gap
    sheet = bytearray([24, 24, 24] * (width * height))
    for i, (tw, tht, px) in enumerate(scaled):
        x0, y0 = gap + (i % columns) * (tile_width + gap), gap + (i // columns) * (th + gap)
        for y in range(tht):
            start = ((y0 + y) * width + x0) * 3
            sheet[start:start + tw * 3] = px[y * tw * 3:(y + 1) * tw * 3]
    return width, height, bytes(sheet)


# Camera yaw/pitch per view: an asset faces +X, so "front" looks back along -X from +X.
LOOK_VIEWS = (("front", 180, -10), ("right", 270, -10), ("back", 0, -10), ("left", 90, -10), ("top", 180, -89),
              ("3/4", 225, -25))


def cmd_look(ctx, args):
    """Render an asset from six angles into one contact sheet (front, right, back / left, top, three-quarter)."""
    import math
    launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
    if launch.get("headless") and not launch.get("render"):
        raise CliError("this editor runs headless with -nullrhi and cannot render; reopen it with "
                       "`ue open --headless --render`")
    if (ctx.beacon or {}).get("pie"):
        raise CliError("stop PIE first (ue pie stop): the asset is staged in the editor level")
    stage = ctx.tether("look_stage", asset=args.asset, action="place")
    stage = json.loads(stage) if isinstance(stage, str) else stage
    tiles = []
    try:
        cx, cy, cz = stage["center"]
        distance = stage["radius"] * args.distance
        for name, yaw, pitch in LOOK_VIEWS:
            yr, pr = math.radians(yaw), math.radians(pitch)
            forward = (math.cos(pr) * math.cos(yr), math.cos(pr) * math.sin(yr), math.sin(pr))
            pose = {"location": {"x": cx - forward[0] * distance, "y": cy - forward[1] * distance,
                                 "z": cz - forward[2] * distance},
                    "rotation": {"pitch": pitch, "yaw": yaw, "roll": 0}, "scale": {"x": 1, "y": 1, "z": 1}}
            tiles.append(png_read(capture_viewport(ctx, pose)))
    finally:
        with __import__("contextlib").suppress(CliError):
            ctx.tether("look_stage", asset=args.asset, action="remove")
    width, height, rgb = contact_sheet(tiles, columns=3, tile_width=args.tile)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        folder = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"look-{args.asset.rstrip('/').rsplit('/', 1)[-1].split('.')[0]}-"
                                    f"{time.strftime('%Y%m%d-%H%M%S')}.png")
    png_write(path, width, height, rgb)
    print(f"{path}  {width}x{height}")
    print(f"{args.asset} ({stage.get('class')}, about {stage['radius'] * 2 / 100:.1f} m across): "
          "top row front, right, back; bottom row left, top, three-quarter")
    return 0


def cmd_shot(ctx, args):
    """Capture the viewport (or the player's view during PIE) to a PNG and print its path."""
    launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
    if launch.get("headless") and not launch.get("render"):
        raise CliError("this editor runs headless with -nullrhi and cannot render; "
                       "reopen it with `ue open --headless --render` (or windowed)")
    baselines = os.path.join(state_dir(ctx.project), "shots", "baselines")
    if args.compare:  # re-shoot the baseline's exact camera
        meta = read_json(os.path.join(baselines, f"{args.compare}.json"))
        if not meta:
            names = sorted(f[:-5] for f in os.listdir(baselines) if f.endswith(".json")) if os.path.isdir(baselines) else []
            raise CliError(f"no baseline '{args.compare}'" + (f" (have: {', '.join(names)})" if names else
                                                             " (save one with ue shot ... --baseline NAME)"))
        loc, rot = meta["pose"]["location"], meta["pose"]["rotation"]
        args.actor, args.look_at = None, None
        args.at, args.rot = f"{loc['x']},{loc['y']},{loc['z']}", f"{rot['pitch']},{rot['yaw']},{rot['roll']}"
    pose = ctx.tether("shot_pose", actor=args.actor or "", at=args.at or "", look_at=args.look_at or "",
                      rot=args.rot or "")
    pie, simulate = pose.pop("pie", False), pose.pop("simulate", False)
    if pie and not simulate:  # Play: CaptureViewport would render the editor world, so HighResShot the game
        raw = pie_screenshot(ctx, args)
    else:  # editor, or Simulate-In-Editor, whose simulation the level viewport shows
        annotations = dict(ANNOTATIONS, gridHeight=args.grid_z) if args.annotate else NO_ANNOTATIONS
        raw = capture_viewport(ctx, pose, annotations, args.ui)
    width, height = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")) if raw[:4] == b"\x89PNG" else (0, 0)
    if args.output:
        path = os.path.abspath(args.output)
    else:
        shots = os.path.join(state_dir(ctx.project), "shots")
        os.makedirs(shots, exist_ok=True)
        old = sorted(f for f in os.listdir(shots) if f.endswith(".png"))
        for name in old[:-49]:  # keep the 50 newest
            os.remove(os.path.join(shots, name))
        path = os.path.join(shots, time.strftime("shot-%Y%m%d-%H%M%S.png"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    loc, rot = pose["location"], pose["rotation"]
    print(f"{path}  {width}x{height}  {len(raw) // 1024} KB" + ("  (Simulate)" if simulate else "  (PIE)" if pie else ""))
    print(f"camera at ({loc['x']:.0f}, {loc['y']:.0f}, {loc['z']:.0f}) "
          f"rot ({rot['pitch']:.0f}, {rot['yaw']:.0f}, {rot['roll']:.0f})")
    if args.baseline:
        os.makedirs(baselines, exist_ok=True)
        with open(os.path.join(baselines, f"{args.baseline}.png"), "wb") as f:
            f.write(raw)
        write_json(os.path.join(baselines, f"{args.baseline}.json"), {"pose": pose, "time": time.time()})
        print(f"baseline '{args.baseline}' saved (ue shot --compare {args.baseline})")
    if args.compare:
        with open(os.path.join(baselines, f"{args.compare}.png"), "rb") as f:
            before = png_read(f.read())
        share, regions, diff = image_diff(before, png_read(raw), args.threshold)
        diff_path = os.path.splitext(path)[0] + f"-diff-{args.compare}.png"
        png_write(diff_path, before[0], before[1], diff)
        print(f"compare '{args.compare}': {share * 100:.2f}% of pixels changed"
              + ("; areas " + ", ".join(f"x {b[0]}-{b[2]} y {b[1]}-{b[3]}" for b in regions[:3])
                 + (f" (+{len(regions) - 3} more)" if len(regions) > 3 else "") if regions else "")
              + f" (tolerance {args.tolerance:g}%)")
        print(f"diff (changes in red): {diff_path}")
        return 1 if share * 100 > args.tolerance else 0
    return 0


def png_read(data):
    """(width, height, channels, pixel bytes) from an 8-bit non-interlaced PNG (stdlib only)."""
    import struct
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CliError("not a PNG")
    pos, idat, width = 8, [], 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
            if depth != 8 or channels is None or interlace:
                raise CliError(f"unsupported PNG (bit depth {depth}, color type {color}, interlace {interlace})")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b, c = prev[i], (prev[i - channels] if i >= channels else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def png_write(path, width, height, rgb):
    """Write 8-bit RGB pixels as a PNG."""
    import struct

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def image_diff(before, after, threshold=32):
    """Compare two decoded images: (share of changed pixels, bounding box (x0, y0, x1, y1) or None, diff RGB)."""
    w, h, ca, a = before
    w2, h2, cb, b = after
    if (w, h) != (w2, h2):
        raise CliError(f"sizes differ: baseline {w}x{h}, now {w2}x{h2} (same viewport size needed)")
    changed, box, blocks = 0, None, {}
    diff = bytearray(w * h * 3)
    for y in range(h):
        row_a, row_b = y * w * ca, y * w * cb
        for x in range(w):
            pa, pb = row_a + x * ca, row_b + x * cb
            delta = max(abs(a[pa + k] - b[pb + k]) for k in range(min(ca, cb, 3)))
            o = (y * w + x) * 3
            if delta > threshold:
                changed += 1
                diff[o] = 255
                blocks[(x >> 3, y >> 3)] = blocks.get((x >> 3, y >> 3), 0) + 1
            else:
                grey = (b[pb] + b[pb + (1 if cb > 2 else 0)] + b[pb + (2 if cb > 2 else 0)]) // 9
                diff[o] = diff[o + 1] = diff[o + 2] = grey
    # Changed areas: connected groups of 8x8 blocks at least a quarter changed, so lone noisy pixels (aliasing)
    # do not count, and separate changes (an object moved, clouds drifting) stay apart. Largest first.
    dense, regions = {k for k, n in blocks.items() if n >= 16}, []
    while dense:
        stack, group = [dense.pop()], []
        while stack:
            bx, by = stack.pop()
            group.append((bx, by))
            for n in ((bx + 1, by), (bx - 1, by), (bx, by + 1), (bx, by - 1)):
                if n in dense:
                    dense.remove(n)
                    stack.append(n)
        regions.append((sum(blocks[k] for k in group), (min(k[0] for k in group) * 8, min(k[1] for k in group) * 8,
                        min(max(k[0] for k in group) * 8 + 7, w - 1), min(max(k[1] for k in group) * 8 + 7, h - 1))))
    regions.sort(reverse=True)
    return changed / float(w * h), [r[1] for r in regions], bytes(diff)


TEST_LOCATION = re.compile(r"LogAutomationController: (?:Error|Warning): (.*) \[(.+)\((\d+)\)\]\s*$")


def test_locations(ctx, start):
    """Map each automation error/warning message to 'file:line', from the log lines since `start`."""
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    root = os.path.normcase(os.path.dirname(ctx.project)) + os.sep
    found = {}
    for line in lines:
        match = TEST_LOCATION.search(line)
        if match:
            path = match.group(2)
            if os.path.normcase(path).startswith(root):
                path = path[len(root):]
            found.setdefault(match.group(1).strip(), f"{path}:{match.group(3)}")
    return found


def cmd_test(ctx, args):
    """Run automation tests in the live editor through Epic's AutomationTestToolset; print only failures."""
    if not args.filter and not args.list:
        raise CliError("give a test filter (e.g. `ue test MyGame.`), or `ue test --list [filter]`")
    if not args.timeout_given:
        ctx.args.timeout = 3600.0  # a run blocks until every selected test has finished
    toolset = "AutomationTestToolset.AutomationTestToolset"
    if toolset not in catalog(ctx):
        raise CliError("Epic's AutomationTestToolset plugin is not enabled; add {\"Name\": \"AutomationTestToolset\", "
                       "\"Enabled\": true} to the .uproject's Plugins (or use AllToolsets), then `ue build --restart`")

    def call(tool, payload):
        text, is_error = ctx.mcp.call(toolset, tool, payload)
        if is_error:
            raise CliError(compact_error(text))
        return unwrap(text)

    call("DiscoverTests", {"bForceRediscover": True})  # picks up tests added by Live Coding
    if args.list:
        # ListTests takes a plain substring, so drop the run filter's anchors and prefixes.
        name = re.sub(r"^(StartsWith:|\^)|\$$", "", args.filter or "")
        found = call("ListTests", {"nameFilter": name, "tagFilter": "", "limit": 0})
        names = found.get("tests", [])
        prefix, short_names = strip_common(names)
        print(f"{len(names)} tests" + (f" under {prefix}" if prefix else "") + (f" matching '{args.filter}'" if args.filter else ""))
        for name in short_names[: args.limit]:
            print("  " + name)
        if len(names) > args.limit:
            print(f"... +{len(names) - args.limit} more (--limit)")
        return 0
    start, started = log_end(ctx.project), time.time()
    text, is_error = ctx.mcp.call(toolset, "RunTestsByFilter", {"filterExpression": args.filter})
    elapsed = time.time() - started
    result = unwrap(text) if not is_error else None
    if is_error:
        # Epic's MCP server fails the whole call when any test logs a script warning (some of Epic's own
        # tests do so on purpose), although the run completed. The results are still there.
        if "matched filter" in text:
            raise CliError(compact_error(text) + f" (ue test --list {args.filter})")
        result = call("GetTestResults", {})
        if not (isinstance(result, dict) and result.get("tests")):
            raise CliError(compact_error(text))
        print(f"note: the run call reported '{compact_error(text)[:120]}'; results read back from the controller")
    tests = result.get("tests", []) if isinstance(result, dict) else []
    if not tests:
        raise CliError(f"no tests match '{args.filter}' (ue test --list {args.filter})")
    os.makedirs(os.path.join(state_dir(ctx.project), "tests"), exist_ok=True)
    write_json(os.path.join(state_dir(ctx.project), "tests", "last.json"), dict(result, filter=args.filter))
    if args.json:
        print(json.dumps(result))
        return 1 if result.get("failed") else 0
    failed = [t for t in tests if t.get("state") not in ("Success", "Skipped", "NotRun")]
    skipped = sum(1 for t in tests if t.get("state") in ("Skipped", "NotRun"))
    warned = [t for t in tests if t.get("warnings")]
    parts = [f"{len(tests) - len(failed) - skipped} passed"] + ([f"{len(failed)} failed"] if failed else []) \
        + ([f"{skipped} skipped"] if skipped else []) + ([f"{len(warned)} with warnings"] if warned else [])
    print(f"{len(tests)} tests: {', '.join(parts)} in {elapsed:.1f}s")
    where = test_locations(ctx, start) if failed or (warned and args.verbose) else {}
    for test in failed:
        print(f"FAIL {test['name']} ({test.get('duration', 0):.2f}s)" + (f" [{test.get('state')}]" if test.get("state") != "Fail" else ""))
        for message in test.get("errors", [])[:5] or ["(no error message; ue log --level Warning)"]:
            loc = where.get(message.strip())
            print(f"  {message}" + (f"  [{loc}]" if loc else ""))
    if args.verbose:
        for test in warned:
            for message in test["warnings"][:5]:
                loc = where.get(message.strip())
                print(f"WARN {test['name']}: {message}" + (f"  [{loc}]" if loc else ""))
    return 1 if failed else 0


def asset_ref(path):
    """'/Game/X/BP_Y' -> '/Game/X/BP_Y.BP_Y' (object path), leaving full object paths alone."""
    base = path.split(":", 1)[0]
    if "." in base.rsplit("/", 1)[-1]:
        return path
    return base + "." + base.rsplit("/", 1)[-1] + (":" + path.split(":", 1)[1] if ":" in path else "")


def snapshot_path(ctx, kind, asset, name):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", asset.strip("/"))
    return os.path.join(state_dir(ctx.project), kind, safe, re.sub(r"[^A-Za-z0-9_.-]+", "_", name))


def text_diff(before, after, label):
    import difflib
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"{label} (before)", f"{label} (now)", n=2))


def mcp_tool(ctx, toolset_query, tool, payload):
    text, is_error = ctx.mcp.call(resolve_toolset(ctx, toolset_query), tool, payload)
    if is_error:
        raise CliError(compact_error(text))
    return unwrap(text)


def graph_source(ctx, bp):
    """Where to read a Blueprint's graphs from. Epic's read_graph_dsl marks the Blueprint modified, so one with
    no unsaved changes is read from a copy of its file (loaded as /Temp/...) and stays clean."""
    package = bp.split(".")[0]
    dirty = ctx.tether("py", code=f"{package!r} in [p.get_name() for p in "
                                  "unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()]")
    file = package_file(ctx.project, package) if package.startswith("/Game/") else None
    if str(dirty).strip().endswith("True") or not file or not os.path.isfile(file):
        return bp
    with open(file, "rb") as f:
        return stage_copy(ctx.project, f.read(), package.rsplit("/", 1)[-1], os.path.splitext(file)[1])


def read_graph(ctx, source, bp, graph):
    """A graph's DSL text, read from `source` (see graph_source) but naming the Blueprint `bp`."""
    text = mcp_tool(ctx, "blueprint", "read_graph_dsl", {"graph": {"refPath": f"{source}:{graph}"}})
    text = text if isinstance(text, str) else json.dumps(text)
    return text.replace(source.split(".")[0], bp.split(".")[0]) if source != bp else text


COMPILER_LINE = re.compile(r"(?:LogBlueprint|LogK2Compiler|BlueprintLog)\w*: (Error|Warning): (.*)")


def bp_check(ctx, path, args):
    """Compile every Blueprint under `path`; print only the broken ones, with the compiler's messages."""
    start, started = log_end(ctx.project), time.time()
    if not args.timeout_given:
        ctx.args.timeout = 1800.0  # big projects have hundreds of Blueprints
    result = ctx.tether("bp_check", path=path, warnings=args.warnings)
    result = json.loads(result) if isinstance(result, str) else result
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    messages = [m.groups() for m in map(COMPILER_LINE.search, lines) if m]
    problems = result.get("problems", [])
    count = result.get("checked", 0)
    print(f"{count} Blueprint{'' if count == 1 else 's'} under {path} compiled in {time.time() - started:.1f}s: "
          + (f"{len(problems)} with problems" if problems else "all clean"))
    for problem in problems[: args.limit]:
        name = problem["path"].rsplit("/", 1)[-1]
        print(f"{'ERROR' if problem['status'] == 'error' else 'WARN '} {problem['path']}")
        mine = [f"  {sev[0]} " + re.sub(r"\s+", " ", re.sub(r"^\[AssetLog\] .*?: \[Compiler\] ", "", text.strip()))
                for sev, text in messages if name in text]
        for line in list(dict.fromkeys(mine))[:5] or ["  (no compiler message in the log; open it in the editor)"]:
            print(line[:300])
    return 1 if any(p["status"] == "error" for p in problems) else 0


BP_BASIC_TYPES = {"bool": "bool", "int": "int", "float": "float", "byte": "byte", "name": "name", "string": "string",
                  "text": "text", "vector": "Vector", "rotator": "Rotator", "transform": "Transform",
                  "vector2d": "Vector2D", "linearcolor": "LinearColor"}
BP_TYPE_LOOKUP = """
import json
t = {type!r}
found = unreal.load_object(None, t) if t.startswith('/') else None
if found is None:
    w = getattr(unreal, t, None)
    found = w.static_class() if hasattr(w, 'static_class') else w.static_struct() if hasattr(w, 'static_struct') else None
print(json.dumps({{'path': found.get_path_name() if found else None,
                   'struct': isinstance(found, unreal.ScriptStruct)}}))
"""


def bp_compile_status(ctx, package):
    status = py_json(ctx, f"import json\nbp = unreal.load_asset({package!r})\nunreal.BlueprintEditorLibrary.compile_blueprint(bp)\n"
                          "print(json.dumps(str(bp.get_editor_property('status')).split('.')[-1].split(':')[0]))")
    return {"BS_UP_TO_DATE": "compiled", "BS_UP_TO_DATE_WITH_WARNINGS": "compiled with warnings",
            "BS_ERROR": "COMPILE ERROR (ue bp check for why)"}.get(status, status.lower())


def bp_edit(ctx, path, ops, args):
    """One Blueprint authoring step: add-var, set-default, add-component, add-function, add-dispatcher, remove-var."""
    if not path or not ops:
        raise CliError("ue bp edit <blueprint> add-var Name Type [default] | set-default Name=value ... | "
                       "add-component Name Class [prop=value ...] | add-function Name | add-dispatcher Name | remove-var Name")
    bp, package = asset_ref(path), path.split(".")[0]
    cdo = f"{package}.Default__{package.rsplit('/', 1)[-1]}_C"
    ref = {"blueprint": {"refPath": bp}}
    op, rest = ops[0].lower(), ops[1:]
    need = {"add-var": 2, "set-default": 1, "add-component": 2, "add-function": 1, "add-dispatcher": 1, "remove-var": 1}
    if op not in need:
        raise CliError(f"unknown edit '{op}' (add-var, set-default, add-component, add-function, add-dispatcher, remove-var)")
    if len(rest) < need[op]:
        raise CliError(f"{op} needs {need[op]} argument(s)")
    lines = []
    if op == "add-var":
        name, kind, default = rest[0], rest[1], (rest[2] if len(rest) > 2 else None)
        existing = [str(v) for v in mcp_tool(ctx, "blueprint", "list_variables", ref) or []]
        if name.lower() in {v.lower() for v in existing}:
            raise CliError(f"{package} already has a variable '{name}'")
        payload = dict(ref, name=name)
        if args.array:
            payload["container_type"] = "ARRAY"
        if kind.lower() in BP_BASIC_TYPES:
            mcp_tool(ctx, "blueprint", "add_variable", dict(payload, type_name=BP_BASIC_TYPES[kind.lower()]))
        else:
            found = py_json(ctx, BP_TYPE_LOOKUP.format(type=kind))
            if not found.get("path"):
                raise CliError(f"unknown type '{kind}': a basic type ({', '.join(BP_BASIC_TYPES)}), a class or a struct")
            if found["struct"]:
                mcp_tool(ctx, "blueprint", "add_struct_variable", dict(payload, struct_type={"refPath": found["path"]}))
            else:
                mcp_tool(ctx, "blueprint", "add_object_variable", dict(payload, object_class={"refPath": found["path"]}))
        if args.category:
            mcp_tool(ctx, "blueprint", "set_variable_category", dict(ref, variable_name=name, category=args.category))
        if args.instance_editable:
            mcp_tool(ctx, "blueprint", "set_variable_instance_editable", dict(ref, variable_name=name, instance_editable=True))
        lines.append(f"added variable {name}: {'array of ' if args.array else ''}{kind}"
                     + (f", category {args.category}" if args.category else "") + (", instance editable" if args.instance_editable else ""))
        if default is not None:
            bp_compile_status(ctx, package)
            lines.append(ctx.tether("set_props", target=cdo, values=json.dumps({name: default}), pie=False).split("\n", 1)[-1])
    elif op == "set-default":
        values = {}
        for pair in rest:
            key, eq, value = pair.partition("=")
            if not eq:
                raise CliError(f"expected Name=value, got '{pair}'")
            values[key] = value
        bp_compile_status(ctx, package)
        lines.append(ctx.tether("set_props", target=cdo, values=json.dumps(values), pie=False))
    elif op == "add-component":
        values = dict(p.partition("=")[::2] for p in rest[2:])
        lines.append(ctx.tether("bp_component", blueprint=package, name=rest[0], cls=rest[1], parent=args.parent or "",
                                values=json.dumps(values)))
    elif op == "add-function":
        mcp_tool(ctx, "blueprint", "add_function_graph", dict(ref, graph_name=rest[0]))
        lines.append(f"added function {rest[0]} (ue bp {package} {rest[0]} --apply file.dsl to give it a body)")
    elif op == "add-dispatcher":
        mcp_tool(ctx, "blueprint", "add_event_dispatcher", dict(ref, name=rest[0]))
        lines.append(f"added event dispatcher {rest[0]}")
    elif op == "remove-var":
        mcp_tool(ctx, "blueprint", "remove_variable", dict(ref, name=rest[0]))
        lines.append(f"removed variable {rest[0]}")
    status = bp_compile_status(ctx, package)
    for line in lines:
        print(line)
    print(f"{package}: {status}" + ("" if args.save else " (unsaved: --save, or ue save)"))
    if args.save:
        ctx.tether("save", paths=package)
    return 1 if status.startswith("COMPILE ERROR") else 0


def cmd_bp(ctx, args):
    """Blueprint graphs as text (Epic's graph DSL): read, diff against the last read, apply edited text."""
    if args.docs:
        print(mcp_tool(ctx, "blueprint", "get_graph_dsl_docs", {}))
        return 0
    if args.blueprint == "check":
        return bp_check(ctx, args.graph or "/Game", args)
    if args.blueprint == "edit":
        return bp_edit(ctx, args.graph, args.rest, args)
    if not args.blueprint:
        raise CliError("give a Blueprint: ue bp /Game/Path/BP_Name [graph]")
    bp = asset_ref(args.blueprint)
    graphs = mcp_tool(ctx, "blueprint", "list_graphs", {"blueprint": {"refPath": bp}})
    names = [g["refPath"].rsplit(":", 1)[-1] if isinstance(g, dict) else str(g) for g in graphs or []]
    if not names:
        print(f"{bp}: no graphs (a data-only Blueprint; `ue props {bp.split('.')[0]}_C` shows its class, "
              f"`ue props {bp.split('.')[0]}.Default__{bp.rsplit('.', 1)[-1]}_C` its defaults)")
        return 0
    if args.graph:
        match = [n for n in names if n.lower() == args.graph.lower()]
        if not match:
            raise CliError(f"{bp} has no graph '{args.graph}' (graphs: {', '.join(names)})")
        names = match
    if args.apply:
        if len(names) != 1:
            raise CliError("--apply needs one graph: ue bp <blueprint> <graph> --apply file.dsl")
        with open(args.apply, encoding="utf-8-sig") as f:
            code = f.read()
        graph = {"graph": {"refPath": f"{bp}:{names[0]}"}}
        previous = mcp_tool(ctx, "blueprint", "read_graph_dsl", graph)
        mcp_tool(ctx, "blueprint", "write_graph_dsl", dict(graph, code=code))
        deadline = time.time() + 5  # the write can land a tick later; reading at once may see the old graph
        while mcp_tool(ctx, "blueprint", "read_graph_dsl", graph) == previous and time.time() < deadline:
            time.sleep(0.25)
        print(f"applied {os.path.basename(args.apply)} to {names[0]} (compiled)")
    changed = 0
    source = bp if args.apply else graph_source(ctx, bp)
    for name in names:
        text = read_graph(ctx, source, bp, name)
        snap = snapshot_path(ctx, "bp", bp, name + ".dsl")
        before = open(snap, encoding="utf-8").read() if os.path.isfile(snap) else None
        if args.diff or args.apply:
            if before is None:
                print(f"== {name}: no earlier read to compare with; showing it in full")
                print(text.rstrip())
            elif before != text:
                changed += 1
                print(text_diff(before, text, name).rstrip())
        else:
            print(f"== {name}")
            print(text.rstrip() or "(empty)")
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    if (args.diff or args.apply) and not changed:
        print("no changes since the last read" if args.diff else
              "graph unchanged: the text matched what was there, or did not apply (ue log --level Log --grep DSL)")
    if args.save:
        mcp_tool(ctx, "AssetTools", "save_assets", {"asset_paths": [bp.split(".")[0]]})
        print("saved")
    return 0


def table_rows(ctx, dt):
    names = mcp_tool(ctx, "data_table", "list_rows", {"data_table": {"refPath": dt}}) or []
    names = [n if isinstance(n, str) else str(n) for n in names]
    if not names:
        return {}
    rows = mcp_tool(ctx, "data_table", "get_rows", {"data_table": {"refPath": dt}, "row_names": names})
    return json.loads(rows) if isinstance(rows, str) else rows


LOCTEXT = re.compile(r'^NSLOCTEXT\("[^"]*", "[^"]*", "(.*)"\)$', re.S)


def flat_row(row):
    """One row for display: nested structs flattened one level (item.MaxStack=5), localized text as its
    string, empty values ('', None, 'None') dropped."""
    out = {}
    for key, value in row.items():
        pairs = [(f"{key}.{k}", v) for k, v in value.items()] if isinstance(value, dict) else [(key, value)]
        for name, item in pairs:
            if isinstance(item, str):
                match = LOCTEXT.match(item)
                item = match.group(1) if match else item
            if item in ("", None, "None"):
                continue
            out[name] = item
    return out


def read_table_file(path):
    if path.lower().endswith(".csv"):
        import csv
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            key = reader.fieldnames[0]
            rows = {}
            for record in reader:
                name = record.pop(key)
                values = {}
                for col, cell in record.items():
                    try:
                        values[col] = json.loads(cell)
                    except (ValueError, TypeError):
                        values[col] = cell
                rows[name] = values
            return rows
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, list):  # UE's own export: [{"Name": ..., col: ...}, ...]
        data = {r.pop("Name"): r for r in data}
    return data


def cmd_table(ctx, args):
    """DataTable round-trips: show, export to JSON/CSV, import with a row-level diff (upsert by default)."""
    dt = asset_ref(args.table)
    current = table_rows(ctx, dt)
    if args.export:
        if args.export.lower().endswith(".csv"):
            import csv
            columns = sorted({c for r in current.values() for c in r})
            with open(args.export, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Name"] + columns)
                for name, row in current.items():
                    writer.writerow([name] + [v if isinstance(v := row.get(c), str) else json.dumps(v) for c in columns])
        else:
            with open(args.export, "w", encoding="utf-8", newline="\n") as f:
                json.dump(current, f, indent=2)
                f.write("\n")
        print(f"exported {len(current)} rows to {args.export}")
        return 0
    if not args.import_file:
        print(f"{dt}: {len(current)} rows")
        for name, row in list(current.items())[: args.limit]:
            print(f"  {name}: " + short(", ".join(f"{k}={short(v, 40)}" for k, v in flat_row(row).items()), 220))
        if len(current) > args.limit:
            print(f"... +{len(current) - args.limit} more (--limit, or --export)")
        return 0
    wanted = read_table_file(args.import_file)
    added = [n for n in wanted if n not in current]
    removed = [n for n in current if n not in wanted] if args.prune else []
    updates, lines = {}, []
    for name, row in wanted.items():
        old = current.get(name, {})
        delta = {k: v for k, v in row.items() if old.get(k) != v}
        if name in added:
            lines.append(f"+ {name}")
        for key, value in delta.items():
            if name not in added:
                lines.append(f"~ {name}.{key}: {short(old.get(key), 40)} -> {short(value, 40)}")
        if delta:
            updates[name] = delta
    lines += [f"- {n}" for n in removed]
    kept = len([n for n in current if n not in wanted]) if not args.prune else 0
    print(f"{len(added)} added, {sum(1 for n in updates if n not in added)} changed, {len(removed)} removed"
          + (f", {kept} rows only in the editor kept (--prune removes them)" if kept else ""))
    for line in lines[: args.limit]:
        print("  " + line)
    if len(lines) > args.limit:
        print(f"  ... +{len(lines) - args.limit} more (--limit)")
    if args.dry_run or not (added or updates or removed):
        return 0
    if added:
        mcp_tool(ctx, "data_table", "add_rows", {"data_table": {"refPath": dt}, "row_names": added})
    if updates:
        mcp_tool(ctx, "data_table", "set_rows", {"data_table": {"refPath": dt}, "values": json.dumps(updates)})
    if removed:
        mcp_tool(ctx, "data_table", "remove_rows", {"data_table": {"refPath": dt}, "row_names": removed})
    after = table_rows(ctx, dt)
    stale = [n for n, row in wanted.items() if any(after.get(n, {}).get(k) != v for k, v in row.items())]
    if stale:
        raise CliError("rows did not take every value (check column names and types): " + ", ".join(stale[:5]))
    if args.save:
        mcp_tool(ctx, "AssetTools", "save_assets", {"asset_paths": [dt.split(".")[0]]})
    print("applied" + (" and saved" if args.save else " (unsaved; --save, or save in the editor)"))
    return 0


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


def cmd_spawn(ctx, args):
    print(ctx.tether("spawn", what=args.what, at=args.at, rot=args.rot or "", label=args.label or "",
                     ground=args.ground, align=args.align))
    return 0


def cmd_scatter(ctx, args):
    if not args.area and not args.line:
        raise CliError("give --area x,y,radius or --line x,y,z x,y,z")
    print(ctx.tether("scatter", what=args.what, count=args.count, area=args.area or "",
                     line=" ".join(args.line) if args.line else "", yaw=args.yaw, scale=args.scale,
                     spacing=args.spacing, seed=args.seed, align=args.align, label=args.label or ""))
    return 0


def cmd_drop(ctx, args):
    print(ctx.tether("drop", targets=",".join(args.actors), align=args.align))
    return 0


def cmd_delete(ctx, args):
    print(ctx.tether("delete", targets=",".join(args.actors)))
    return 0


def cmd_duplicate(ctx, args):
    print(ctx.tether("duplicate", target=args.actor, offset=args.offset, count=args.count))
    return 0


def cmd_mv(ctx, args):
    print(ctx.tether("mv", asset=args.asset, dest=args.dest))
    return 0


def cmd_rm(ctx, args):
    print(ctx.tether("rm", asset=args.asset, force=args.force))
    return 0


def cmd_changes(ctx, args):
    print(ctx.tether("changes", limit=args.limit))
    return 0


def cmd_save(ctx, args):
    out = ctx.tether("save", paths=",".join(args.paths))
    print(out)
    failed = out.startswith("save failed") or any(line.startswith("not saved (") for line in out.splitlines())
    return 1 if failed else 0


def cmd_undo(ctx, args):
    print(ctx.tether("undo"))
    return 0


def cmd_watch(ctx, args):
    """Wait until a log line matches, or a property's value matches, then print it (exit 4 on timeout)."""
    deadline, start = time.time() + args.seconds, log_end(ctx.project)
    if args.prop:
        target, name = args.prop
        rx = re.compile(args.until or ".", re.I)
        while True:
            line = ctx.tether("props", target=target, match=f"^{re.escape(name)}$", limit=5, pie=args.pie)
            value = line.splitlines()[-1].split(" = ", 1)[-1] if " = " in line else ""
            if rx.search(value):
                print(f"{target}.{name} = {value}")
                return 0
            if time.time() > deadline:
                raise CliError(f"{target}.{name} is still {value} after {args.seconds:g}s", 4)
            time.sleep(0.5)
    if not args.pattern:
        raise CliError("give a log regex, or --prop <target> <name> --until <regex>")
    rx = re.compile(args.pattern, re.I)
    while True:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            hit = next((l for l in f.read().decode("utf-8", "replace").splitlines() if rx.search(l)), None)
        if hit:
            print(hit.split("]", 2)[-1].strip())
            return 0
        if time.time() > deadline:
            raise CliError(f"no log line matched /{args.pattern}/ within {args.seconds:g}s", 4)
        time.sleep(0.5)


def cmd_asset(ctx, args):
    for path in args.paths:
        print(ctx.tether("asset_info", path=path))
    return 0


def content_package(project, path):
    """A content file's package path (Content/X/Y.uasset -> /Game/X/Y, a plugin's -> /Plugin/X), or None."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.path.dirname(project))).replace("\\", "/")
    except ValueError:  # another drive
        return None
    stem, ext = os.path.splitext(rel)
    if ext.lower() not in (".uasset", ".umap"):
        return None
    parts = stem.split("/")
    if parts[0] == "Content":
        return "/Game/" + "/".join(parts[1:])
    if parts[0] == "Plugins" and "Content" in parts[1:]:
        i = parts.index("Content", 1)
        folder = os.path.join(os.path.dirname(project), *parts[:i])
        uplugin = [f for f in (os.listdir(folder) if os.path.isdir(folder) else []) if f.endswith(".uplugin")]
        mount = os.path.splitext(uplugin[0])[0] if uplugin else parts[i - 1]
        return f"/{mount}/" + "/".join(parts[i + 1:])
    return None


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


def git_changes(project, rev=None):
    """(changed, deleted) files under the project folder: uncommitted and untracked work, plus, with rev,
    everything committed since rev. Absolute paths."""
    def git(*argv):
        try:
            return run_git(project, *argv)
        except CliError as exc:
            raise CliError(f"{exc} (not a git repository? pass asset paths or folders instead)") from None

    top = git("rev-parse", "--show-toplevel").strip()
    changed, deleted = set(), set()
    entries = git("status", "--porcelain", "-z", "-uall", "--", ".").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:  # -z puts the original path in the next entry
            if "R" in status and i < len(entries):
                deleted.add(entries[i])
            i += 1
        (deleted if "D" in status else changed).add(path)
    if rev:
        fields = git("diff", "--name-status", "-z", "--no-renames", rev, "--", ".").split("\0")
        for status, path in zip(fields[0::2], fields[1::2]):
            if status:
                (deleted if status.startswith("D") else changed).add(path)
    deleted -= changed
    absolute = lambda paths: sorted(os.path.normpath(os.path.join(top, p)) for p in paths)
    return absolute(changed), absolute(deleted)


def package_file(project, package):
    """The file a package path lives in (/Game/X -> Content/X.uasset or .umap), or its .uasset path if neither
    exists on disk (it may exist in git only); None for a mount point that is not this project's."""
    root = os.path.dirname(project)
    mount, _, rest = package.strip("/").partition("/")
    if mount == "Game":
        base = os.path.join(root, "Content", *rest.split("/"))
    else:
        base = None
        for dirpath, dirs, files in os.walk(os.path.join(root, "Plugins")):
            if f"{mount}.uplugin" in files:
                base = os.path.join(dirpath, "Content", *rest.split("/"))
                break
            dirs[:] = [d for d in dirs if d not in ("Binaries", "Intermediate", "Saved", "Source", "Content")]
        if base is None:
            return None
    for ext in (".uasset", ".umap"):
        if os.path.isfile(base + ext):
            return base + ext
    return base + ".uasset"


def stage_copy(project, data, name, ext):
    """Put a package's bytes under Saved/Tether/diff (mounted as /Temp/) and return its asset's object path.
    Named by content hash, so the same version is loaded once per editor session."""
    import hashlib
    tag = hashlib.sha1(data).hexdigest()[:12]
    folder = os.path.join(os.path.dirname(project), "Saved", "Tether", "diff", tag)
    path = os.path.join(folder, name + ext)
    if not os.path.isfile(path):
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return f"/Temp/Tether/diff/{tag}/{name}.{name}"


def bp_outline(ctx, path):
    """Parent, variables, dispatchers and graph names of a Blueprint, through Epic's Blueprint tools; None if
    the asset is not a Blueprint."""
    ref = {"blueprint": {"refPath": path}}
    try:
        parent = mcp_tool(ctx, "blueprint", "get_parent", ref)
    except CliError:
        return None
    name = lambda r: (r.get("refPath", "") if isinstance(r, dict) else str(r)).rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return {"parent": name(parent),
            "variables": [str(v) for v in mcp_tool(ctx, "blueprint", "list_variables", ref) or []],
            "dispatchers": [name(d) for d in mcp_tool(ctx, "blueprint", "list_event_dispatchers", ref) or []],
            "graphs": [name(g) for g in mcp_tool(ctx, "blueprint", "list_graphs", ref) or []]}


def external_actor_changes(ctx, package, rev, to):
    """World Partition actors of a map added, removed or changed between rev and `to` (or the working tree)."""
    project = ctx.project
    mount, _, rest = package.strip("/").partition("/")
    folder = os.path.join(os.path.dirname(package_file(project, package)) or "", "")
    content = os.path.join(os.path.dirname(project), "Content") if mount == "Game" else None
    if not content:
        return []
    actors_dir = os.path.join(content, "__ExternalActors__", *rest.split("/"))
    top = run_git(project, "rev-parse", "--show-toplevel").strip()
    rel = os.path.relpath(os.path.realpath(actors_dir), top).replace("\\", "/")
    args = ["diff", "--name-status", "-z", "--no-renames", rev] + ([to] if to else []) + ["--", rel]
    fields = run_git(project, *args).split("\0")
    entries = [(s, p) for s, p in zip(fields[0::2], fields[1::2]) if s]
    if not to:  # untracked actor files are new too
        entries += [("A", p) for p in run_git(project, "ls-files", "-z", "--others", "--exclude-standard", "--", rel)
                    .split("\0") if p]
    if not entries:
        return []
    packages = {}
    for status, path in entries:
        pkg = content_package(project, os.path.join(top, path))
        if pkg:
            packages[pkg] = status[0]
    labels = ctx.tether("py", code=(
        "import json\nreg = unreal.AssetRegistryHelpers.get_asset_registry()\nout = {}\n"
        f"for p in {sorted(packages)!r}:\n"
        "    for a in reg.get_assets_by_package_name(p):\n"
        "        out[p] = f\"{a.get_tag_value('ActorLabel') or a.asset_name} ({a.asset_class_path.asset_name})\"\n"
        "print(json.dumps(out))"))
    if isinstance(labels, str):
        labels = json.loads(labels.strip().splitlines()[-1]) if labels.strip() else {}
    lines = []
    for pkg, status in sorted(packages.items(), key=lambda kv: (kv[1], kv[0])):
        mark = {"A": "+", "D": "-"}.get(status, "~")
        lines.append(f"{mark} {labels.get(pkg, 'actor package ' + pkg.rsplit('/', 1)[-1])}")
    return lines


def cmd_diff(ctx, args):
    """What changed in an asset between git revisions, or between a revision and the editor's current state."""
    project = ctx.project
    if not args.files and not args.asset:
        raise CliError("give an asset: ue diff /Game/Path/Asset [rev] [--to rev], or --files OLD NEW")
    rev, to = args.rev or "HEAD", args.to
    if args.files:
        old_file, new_file = args.files
        named = args.as_path or old_file
        name, ext = os.path.splitext(os.path.basename(named))
        with open(old_file, "rb") as f:
            old = stage_copy(project, f.read(), name, ext or ".uasset")
        live = content_package(project, new_file) if os.path.isfile(new_file) else None
        if live:  # git difftool passes the working-tree file itself when comparing with it
            new, package = asset_ref(live), live
        else:
            with open(new_file, "rb") as f:
                new = stage_copy(project, f.read(), name, ext or ".uasset")
            package = content_package(project, os.path.abspath(args.as_path)) if args.as_path else name
        header = f"{package}: old -> " + ("now (the editor's version, unsaved changes included)" if live else "new")
    else:
        target = args.asset
        package = content_package(project, target) if target.lower().endswith((".uasset", ".umap")) else target.split(".")[0]
        file = package_file(project, package or "")
        if not file:
            raise CliError(f"{target}: not a /Game/... asset of this project (or a file under its Content)")
        top = run_git(project, "rev-parse", "--show-toplevel").strip()

        def blob(revision):
            for candidate in (file, os.path.splitext(file)[0] + (".umap" if file.endswith(".uasset") else ".uasset")):
                rel = os.path.relpath(os.path.realpath(candidate), top).replace("\\", "/")
                data = run_git(project, "cat-file", "--filters", f"{revision}:{rel}", binary=True, check=False)
                if data is not None:
                    return data, os.path.splitext(candidate)[1]
            return None, None

        name = package.rsplit("/", 1)[-1]
        old_data, ext = blob(rev)
        if old_data is None:
            raise CliError(f"{package} does not exist at {rev} (added since? `git log --diff-filter=A -- <file>`)")
        if to:
            new_data, new_ext = blob(to)
            if new_data is None:
                raise CliError(f"{package} does not exist at {to}")
            new = stage_copy(project, new_data, name, new_ext)
        else:
            if not os.path.isfile(file):
                raise CliError(f"{package} was deleted since {rev}")
            with open(file, "rb") as f:
                same_file = f.read() == old_data
            if same_file and not (ctx.beacon or {}).get("dirty"):
                print(f"{package}: identical to {rev} (and nothing is unsaved in the editor)")
                return 0
            new = asset_ref(package)
        old = stage_copy(project, old_data, name, ext)
        unsaved = "" if to else " (the editor's version, unsaved changes included)"
        header = f"{package}: {rev} -> {to or 'now'}{unsaved}"
    print(header)
    old_bp, new_bp = bp_outline(ctx, old), bp_outline(ctx, new)
    if old_bp and new_bp:
        if old_bp["parent"] != new_bp["parent"]:
            print(f"parent: {old_bp['parent']} -> {new_bp['parent']}")
        for key in ("variables", "dispatchers", "graphs"):
            added = [n for n in new_bp[key] if n not in old_bp[key]]
            removed = [n for n in old_bp[key] if n not in new_bp[key]]
            if added or removed:
                print(f"{key}: " + ", ".join([f"+{n}" for n in added] + [f"-{n}" for n in removed]))
    print(ctx.tether("diff", old=old, new=new, limit=400))
    if old_bp and new_bp:
        source = new if new.startswith("/Temp/") else graph_source(ctx, new)
        for graph in [g for g in new_bp["graphs"] if g in old_bp["graphs"]]:
            before, after = read_graph(ctx, old, new, graph), read_graph(ctx, source, new, graph)
            if before != after:
                print(text_diff(before, after, graph).rstrip())
        for graph in [g for g in new_bp["graphs"] if g not in old_bp["graphs"]]:
            print(f"== {graph} (new)\n" + read_graph(ctx, source, new, graph).rstrip())
    if not args.files and package.startswith("/Game/") and ext == ".umap":
        actors = external_actor_changes(ctx, package, rev, to)
        if actors:
            print(f"World Partition actors ({len(actors)} changed; properties not compared):")
            for line in actors[: max(args.limit, 50)]:
                print("  " + line)
    return 0


MAP_CHECK = re.compile(r"Map check complete: (\d+) Error\(s\), (\d+) Warning\(s\)")


def cmd_validate(ctx, args):
    """Data validation on assets (the given ones, or those changed in git plus unsaved ones), then Map Check."""
    paths, deleted, dirty = list(args.paths), [], False
    if args.changed is not None or not paths:
        files, gone = git_changes(ctx.project, args.changed or None)
        paths += sorted({p for p in (content_package(ctx.project, f) for f in files) if p})
        deleted = sorted({p for p in (content_package(ctx.project, f) for f in gone) if p})
        dirty = True
    map_check = args.map_check or not args.paths
    start = log_end(ctx.project)
    report = ctx.tether("validate", paths=",".join(paths), dirty=dirty, deleted=",".join(deleted))
    report = json.loads(report) if isinstance(report, str) else report
    beacon = ctx.beacon or {}
    if map_check and beacon.get("pie"):
        map_check = None  # Map Check would run on the PIE world
    elif map_check:
        ctx.tether("console", command="MAP CHECK DONTDISPLAYDIALOG")  # the dialog variant logs nothing headless
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()

    errors = 0
    problems = report["problems"]
    invalid = sum(1 for p in problems if p["result"] == "invalid")
    scope = "changed and unsaved assets" if dirty else "assets"
    if not report["checked"] and dirty:
        print("no changed or unsaved assets (git status" + (f" + commits since {args.changed}" if args.changed else "") + ")")
    else:
        print(f"validated {report['checked']} {scope}" +
              (f" ({report['not_validated']} not covered by any validator)" if report["not_validated"] else "") +
              (f": {invalid} invalid, {len(problems) - invalid} with warnings" if problems else ": all valid"))
    reported = set()
    for problem in problems[: args.limit]:
        for kind, messages in (("E", problem["errors"]), ("W", problem["warnings"])):
            for message in messages[:5]:
                reported.add(message.strip())
                print(f"  {kind} {problem['path']}: {short(message.strip(), 200)}")
        if problem["result"] == "invalid" and not problem["errors"]:
            print(f"  E {problem['path']}: invalid (no message; see ue log)")
    errors += invalid
    for broken in report["broken_refs"]:
        errors += 1
        print(f"  E deleted {broken['path']} is still referenced by: " + ", ".join(broken["referencers"][:6])
              + (" ..." if len(broken["referencers"]) > 6 else ""))

    map_lines, other = [], []
    for line in lines:
        match = LOG_LINE.match(line)
        if not match:
            continue
        cat, level, message = match.group(1), match.group(2) or "Log", match.group(3).strip()
        if cat == "MapCheck":
            map_lines.append((level, message))
        elif LEVELS.get(level, 4) <= LEVELS["Warning"] and cat not in NOISE and not NOISE_MESSAGE.match(message) \
                and not any(message in r or r in message for r in reported):
            other.append(f"{level[0]} {cat}: {message}")
    if map_check:
        summary = next((MAP_CHECK.search(m) for _, m in reversed(map_lines) if MAP_CHECK.search(m)), None)
        found = [(lvl, m) for lvl, m in map_lines if lvl in ("Error", "Warning")]
        map_errors = int(summary.group(1)) if summary else sum(1 for lvl, _ in found if lvl == "Error")
        errors += map_errors
        print(f"map check {beacon.get('map', '?')}: " + (f"{map_errors} error(s), {int(summary.group(2))} warning(s)"
                                                       if summary else "no result in the log"))
        for lvl, message in found[: args.limit]:
            print(f"  {lvl[0]} {short(message, 200)}")
    elif map_check is None:
        print("map check skipped: PIE is running")
    if other:
        print(f"logged during validation: {len(other)} warning/error line(s)")
        for line in other[: min(args.limit, 10)]:
            print(f"  {short(line, 200)}")
    warnings = len(problems) - invalid
    write_json(os.path.join(state_dir(ctx.project), "validated.json"), {"time": time.time(), "errors": errors})
    return 1 if errors or (args.strict and (warnings or other)) else 0


def cmd_mat(ctx, args):
    """Material parameters: list them (with where each value comes from), or set them (undoable)."""
    if args.assignments or args.reset:
        values = {}
        for item in args.assignments:
            name, eq, value = item.partition("=")
            if not eq or not name:
                raise CliError(f"expected Name=value, got '{item}'")
            values[name.strip()] = value
        print(ctx.tether("set_material", target=args.target, values=json.dumps(values) if values else "",
                         reset=",".join(args.reset or [])))
        if args.compile:
            text = ctx.tether("material", target=args.target, compile=True, limit=0)
            print(text[text.find("compiled "):])
        return 0
    print(ctx.tether("material", target=args.target, compile=args.compile, limit=args.limit))
    return 0


def cmd_nav(ctx, args):
    """Navmesh state, path queries and actors off the navmesh."""
    action = args.action or "status"
    if action == "path" and len(args.points) != 2:
        raise CliError("ue nav path <start> <end>   (x,y,z or actor)")
    start, end = (args.points + ["", ""])[:2] if action == "path" else ("", "")
    match = " ".join(args.points) if action == "check" else ""
    print(ctx.tether("nav", action=action, start=start, end=end, match=match, pie=args.pie, limit=args.limit))
    return 0


PACKAGE_EXTS = (".uasset", ".umap", ".uexp", ".ubulk", ".uptnl")


def human_bytes(n):
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}" if n < 100 * size else f"{n / size:.0f} {unit}"
    return f"{n} B"


def package_bytes(project, package):
    """Disk size of a package (all its files), or None if it is not one of this project's files."""
    file = package_file(project, package) if package.startswith("/") else None
    if not file or not os.path.isfile(file):
        return None
    stem = os.path.splitext(file)[0]
    return sum(os.path.getsize(stem + ext) for ext in PACKAGE_EXTS if os.path.isfile(stem + ext))


def folder_packages(project, folder):
    """{package: bytes} for every package file under a content folder, from disk (no editor needed)."""
    root = package_file(project, folder.rstrip("/") + "/_")
    if root is None:
        raise CliError(f"{folder}: not a content folder of this project")
    base = os.path.dirname(root)
    if not os.path.isdir(base):
        raise CliError(f"{folder}: no such folder ({base})")
    sizes = {}
    for dirpath, _, files in os.walk(base):
        for name in files:
            stem, ext = os.path.splitext(name)
            if ext.lower() in PACKAGE_EXTS:
                package = folder.rstrip("/") + "/" + os.path.relpath(os.path.join(dirpath, stem), base).replace("\\", "/")
                sizes[package] = sizes.get(package, 0) + os.path.getsize(os.path.join(dirpath, name))
    return sizes


def size_groups(sizes, depth):
    """Totals per folder `depth` levels below the mount (/Game/Megascans at depth 1): {folder: (bytes, count)}."""
    groups = {}
    for package, size in sizes.items():
        parts = package.strip("/").split("/")
        key = "/" + "/".join(parts[: depth + 1]) if len(parts) > depth + 1 else "/" + "/".join(parts[:-1]) or "/"
        total, count = groups.get(key, (0, 0))
        groups[key] = (total + size, count + 1)
    return groups


def cmd_size(ctx, args):
    """Biggest assets on disk under a folder, or what an asset pulls in with it (--deps)."""
    project, target = ctx.project, (args.path or "/Game").rstrip("/") or "/Game"
    top = args.top
    single = package_file(project, target)
    single = single if single and os.path.isfile(single) else None
    classes = {}
    if single and args.deps:
        graph = ctx.tether("package_graph", packages=target, deps=True)
        graph = json.loads(graph) if isinstance(graph, str) else graph
        classes = graph["classes"]
        sizes, outside = {}, 0
        for package in [target] + graph["deps"]:
            size = package_bytes(project, package)
            if size is None:
                outside += 1
            else:
                sizes[package] = size
        total = sum(sizes.values())
        print(f"{target} loads {len(graph['deps'])} packages with it: {human_bytes(total)} on disk"
              + (f" (+{outside} engine/plugin packages not counted)" if outside else ""))
        depth = 2
    elif single:
        size = package_bytes(project, target)
        print(f"{target}: {human_bytes(size)} on disk (--deps: with everything it loads)")
        return 0
    else:
        sizes = folder_packages(project, target)
        total = sum(sizes.values())
        print(f"{target}: {len(sizes)} packages, {human_bytes(total)} on disk")
        depth = len(target.strip("/").split("/"))
    groups = sorted(size_groups(sizes, depth).items(), key=lambda kv: -kv[1][0])
    prefix = target + "/" if not (single and args.deps) else ""
    if len(groups) > 1:
        print("by folder: " + ", ".join(f"{k[len(prefix):] if prefix and k.startswith(prefix) else k} {human_bytes(b)} ({n})"
                                        for k, (b, n) in groups[:top]))
    biggest = sorted(sizes.items(), key=lambda kv: -kv[1])[:top]
    if not classes and editor_live(project):
        try:
            graph = ctx.tether("package_graph", packages=",".join(p for p, _ in biggest))
            classes = (json.loads(graph) if isinstance(graph, str) else graph)["classes"]
        except CliError:
            pass
    print("biggest:")
    for package, size in biggest:
        kind = classes.get(package)
        print(f"  {human_bytes(size):>8}  {package}" + (f"  ({kind})" if kind else ""))
    return 0


MEMREPORT_CLASS = re.compile(r"^\s*(\w+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s")
MEMREPORT_OBJECT = re.compile(r"^\s*(\w+) (/\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s")
MEMREPORT_TEXTURE = re.compile(r"^\d+x\d+ \(\d+ KB[^)]*\), (\d+)x(\d+) \((\d+) KB\), (\w+), (\w+), (/\S+),")


def memreport_summary(text, top=8):
    """The few numbers that matter from a memreport file."""
    sections, current = {}, None
    for line in text.splitlines():
        begin = re.match(r'MemReport: Begin command "(.*)"', line)
        if begin:
            current = begin.group(1)
            sections[current] = []
        elif line.startswith("MemReport: End command"):
            current = None
        elif current is not None:
            sections[current].append(line)
    out = []
    head = "\n".join(sections.get("Mem FromReport", []))
    process = re.search(r"Process Physical Memory: ([\d.]+) MB used, ([\d.]+) MB peak", head)
    if process:
        line = f"process: {float(process.group(1)) / 1024:.1f} GB used, {float(process.group(2)) / 1024:.1f} GB peak"
        extras = [f"{name} {float(m.group(1)):.0f} MB" for name, rx in
                  (("asset registry", r"AssetRegistry memory usage = ([\d.]+) MB"), ("names", r"Nametable memory usage = ([\d.]+) MB"))
                  for m in [re.search(rx, head)] if m]
        out.append(line + (f" (of which {', '.join(extras)})" if extras else ""))
    rows = []
    for line in sections.get("obj list -resourcesizesort", []):
        m = MEMREPORT_CLASS.match(line)
        if m and m.group(1) != "Class":
            rows.append((float(m.group(3)) + float(m.group(5)), m.group(1), int(m.group(2))))
    if rows:
        rows.sort(reverse=True)
        out.append("by class (object + resource memory): " + ", ".join(
            f"{name} x{count} {human_bytes(int(kb * 1024))}" for kb, name, count in rows[:top]))
    for cls in ("SkeletalMesh", "StaticMesh"):
        objects = []
        for line in sections.get(f"obj list class={cls} -resourcesizesort", []):
            m = MEMREPORT_OBJECT.match(line)
            if m:
                objects.append((float(m.group(3)) + float(m.group(5)), m.group(2).split(".")[0]))
        if objects:
            objects.sort(reverse=True)
            out.append(f"biggest {cls}es: " + ", ".join(f"{p.rsplit('/', 1)[-1]} {human_bytes(int(kb * 1024))}"
                                                      for kb, p in objects[:5]))
    textures = sections.get("ListTextures", [])
    total = next((re.search(r"InMem= ([\d.]+) MB", l) for l in textures if l.startswith("Total size:")), None)
    listed = []
    for line in textures:
        m = MEMREPORT_TEXTURE.match(line)
        if m:
            listed.append((int(m.group(3)), m.group(6).split(".")[0], m.group(5).replace("TEXTUREGROUP_", "")))
    if total:
        listed.sort(reverse=True)
        out.append(f"textures: {len(listed)} loaded, {float(total.group(1)):.0f} MB in memory" +
                   (("; biggest " + ", ".join(f"{p.rsplit('/', 1)[-1]} {kb / 1024:.1f} MB ({g})" for kb, p, g in listed[:5]))
                    if listed and listed[0][0] else ""))
    return out


def cmd_mem(ctx, args):
    """Memory of the running editor (and PIE), from a memreport, in a few lines."""
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Profiling", "MemReports")

    def reports():
        found = set()
        for dirpath, _, files in os.walk(folder):
            found |= {os.path.join(dirpath, f) for f in files if f.endswith(".memreport")}
        return found

    before = reports()
    ctx.tether("console", command="memreport" + (" -full" if args.full else ""))
    # The report is written on a later frame; a call answers once the game thread is free again (~20 s).
    deadline, new = time.time() + max(args.timeout, 120), []
    while not new and time.time() < deadline:
        ctx.tether("py", code="1")
        new = sorted(reports() - before, key=os.path.getmtime)
        time.sleep(0.5 if not new else 0)
    if not new:
        raise CliError("memreport wrote no file (ue log --grep MemReport)")
    with open(new[-1], encoding="utf-8", errors="replace") as f:
        lines = memreport_summary(f.read(), args.top)
    beacon = ctx.beacon or {}
    if beacon.get("pie"):
        lines.insert(0, "(PIE is running: the numbers include the play session)")
    for line in lines:
        print(line)
    print(f"full report: {os.path.relpath(new[-1], os.path.dirname(ctx.project))}")
    return 0


def cmd_config(ctx, args):
    """Project and editor settings: list sections, read one, or set values (saved like the Settings window)."""
    assignments = [a for a in args.rest if "=" in a]
    match = " ".join(a for a in args.rest if "=" not in a)
    if assignments:
        values = {}
        for item in assignments:
            name, _, value = item.partition("=")
            values[name.strip()] = value
        print(ctx.tether("set_config", section=args.section, values=json.dumps(values)))
        return 0
    print(ctx.tether("config", section=args.section or "", match=match, limit=args.limit if args.section else 200))
    return 0


SNAPSHOT_LINE = re.compile(r'^(?P<indent> *)(?P<role>[\w-]+)(?: "(?P<label>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)\s*$')
# Source files of the editor's own viewport controls (toolbars, tabs, menus) in a Slate snapshot.
EDITOR_CHROME = ("SDock", "SToolBar", "SClippingHorizontalBox", "SComboButton", "SEditorViewport", "SLevelViewport",
                 "SViewportToolBar", "SViewportToolbar", "SCommonEditorViewport", "STransformViewport", "SActorPilot",
                 "SToolMenu", "SMenuEntryBlock", "SLevelEditor", "SPIEToolbar", "SGameLayerManager", "SNotification")
INTERACTIVE = {"button", "checkbox", "combobox", "textbox", "slider", "hyperlink", "menuitem", "radiobutton", "spinbox"}
UI_TOOLSET = "SlateInspectorToolset.SlateInspectorToolset"


def parse_snapshot(text):
    nodes = []
    for line in text.splitlines():
        m = SNAPSHOT_LINE.match(line)
        if not m:
            continue
        attrs = re.findall(r"\[([^\]]*)\]", m.group("attrs") or "")
        fields = dict(a.split("=", 1) for a in attrs if "=" in a)
        nodes.append({"indent": len(m.group("indent")), "role": m.group("role"),
                      "label": (m.group("label") or "").replace('\\"', '"'), "ref": fields.get("ref", ""),
                      "src": fields.get("src", ""), "size": fields.get("size", ""),
                      "flags": [a for a in attrs if "=" not in a]})
    return nodes


def subtree(nodes, i):
    end = i + 1
    while end < len(nodes) and nodes[end]["indent"] > nodes[i]["indent"]:
        end += 1
    return nodes[i + 1:end]


def game_ui(nodes, client=0):
    """The game's widgets in a whole-editor Slate snapshot: the level viewport's panel minus the editor's own
    controls (told apart by the source file that created each widget), or for a multiplayer client (or PIE in
    a new window) that client's PIE window. Stale windows of earlier sessions are not taken for the game."""
    def pie_window(wanted):
        for i, node in enumerate(nodes):
            if node["role"] == "window" and re.search(wanted, node["label"]):
                return subtree(nodes, i)
        return None

    if client:
        return pie_window(rf"NetMode: Client {client}\]") or []
    for i, node in enumerate(nodes):
        if node["role"] != "tab" or not node["label"].startswith("Viewport") or not node["src"].startswith("SLevelEditor"):
            continue
        parent = next((j for j in range(i - 1, -1, -1) if nodes[j]["indent"] < node["indent"]), None)
        if parent is None:
            continue
        game, keep = [], False
        for n in subtree(nodes, parent):
            if n["indent"] == node["indent"]:
                keep = not n["src"].startswith(EDITOR_CHROME) and n is not node
            if keep:
                game.append(n)
        if game:
            return game
    return pie_window(r"Preview \[NetMode|\(PIE\)") or []


def ui_nodes(ctx):
    """(game widget nodes, True) via Epic's Slate inspector, or (None, False) when it is not enabled."""
    if ctx.transport == "remote" or UI_TOOLSET not in catalog(ctx):
        return None, False
    text = mcp_tool(ctx, "SlateInspectorToolset", "Snapshot", {"ref": "", "maxDepth": 120, "bIncludeSourceLocations": True})
    target = re.search(r"^\* (\d+):", pie_net(ctx, "status"), re.M)  # `ue pie target` picks a client's window
    return game_ui(parse_snapshot(text if isinstance(text, str) else json.dumps(text)),
                   int(target.group(1)) if target else 0), True


def ui_lines(nodes, show_all=False, match=None):
    base = min((n["indent"] for n in nodes), default=0)
    rx = re.compile(match, re.I) if match else None
    out = []
    for n in nodes:
        if not show_all and (n["size"].startswith("0,") or n["size"].endswith(",0")
                             or (n["role"] == "image" and not n["label"])):
            continue
        line = "  " * ((n["indent"] - base) // 2) + n["role"] + (f' "{n["label"]}"' if n["label"] else "") + \
            "".join(f" [{f}]" for f in n["flags"]) + (f"  ({n['ref']})" if n["role"] in INTERACTIVE else "")
        if not rx or rx.search(line):
            out.append(line)
    return out


def find_ui(nodes, target, roles=INTERACTIVE):
    """A game widget by ref or by label (exact, then contained; case-insensitive)."""
    if re.fullmatch(r"[a-z]+\d+", target):
        hit = [n for n in nodes if n["ref"] == target]
    else:
        key = target.lower()
        pool = [n for n in nodes if n["role"] in roles]
        hit = [n for n in pool if n["label"].lower() == key] or [n for n in pool if key in n["label"].lower()]
    if not hit:
        names = ", ".join(f'{n["role"]} "{n["label"]}"' for n in nodes if n["role"] in roles and n["label"])[:300]
        raise CliError(f"no {'/'.join(sorted(roles)) if len(roles) < 4 else 'interactive'} widget '{target}' on screen"
                       + (f" (have: {names})" if names else ""))
    if "disabled" in hit[0]["flags"]:
        raise CliError(f'{hit[0]["role"]} "{hit[0]["label"]}" is disabled')
    return hit[0]


def ui_click(ctx, target):
    nodes, slate = ui_nodes(ctx)
    if not slate:
        return ctx.tether("umg", click=target)
    node = find_ui(nodes, target)
    mcp_tool(ctx, "SlateInspectorToolset", "Click", {"ref": node["ref"]})
    return f'clicked {node["role"]} "{node["label"]}"'


def ui_has_text(ctx, text):
    nodes, slate = ui_nodes(ctx)
    lines = ui_lines(nodes, show_all=True) if slate else ctx.tether("umg").splitlines()
    return any(text.lower() in line.lower() for line in lines)


def cmd_ui(ctx, args):
    """The game's on-screen UI in PIE (Slate and UMG), and clicking, typing and keys."""
    if ctx.transport != "remote" and not (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is not running (ue pie start)")
    action = args.action or "show"
    if action == "click":
        print(ui_click(ctx, " ".join(args.rest)))
        return 0
    if action in ("type", "key"):
        nodes, slate = ui_nodes(ctx)
        if not slate:
            raise CliError(f"ue ui {action} needs Epic's SlateInspectorToolset (ue init enables it)")
        if action == "key":
            mcp_tool(ctx, "SlateInspectorToolset", "PressKey", {"key": " ".join(args.rest)})
            print(f"pressed {' '.join(args.rest)}")
            return 0
        if len(args.rest) < 2:
            raise CliError("ue ui type <textbox label|ref> <text> [--submit]")
        node = find_ui(nodes, args.rest[0], {"textbox", "spinbox"})
        mcp_tool(ctx, "SlateInspectorToolset", "Type", {"ref": node["ref"], "text": " ".join(args.rest[1:]),
                                                        "submit": args.submit})
        print(f'typed into textbox "{node["label"]}"')
        return 0
    match = " ".join(([action] if action != "show" else []) + args.rest)
    nodes, slate = ui_nodes(ctx)
    if not slate:
        print("(UMG only: enable Epic's SlateInspectorToolset, e.g. with ue init, to also see Slate widgets)")
        print(ctx.tether("umg"))
        return 0
    lines = ui_lines(nodes, args.all, match or None)
    print(f"{len(lines)} widget(s) on screen" + (f" matching /{match}/" if match else "") + (":" if lines else ""))
    for line in lines[: args.limit if args.limit != 30 else 200]:
        print(line)
    return 0


def cmd_anim(ctx, args):
    print(ctx.tether("anim", target=args.target, pie=not args.editor, limit=args.limit))
    return 0


def cmd_ai(ctx, args):
    print(ctx.tether("ai", target=args.target or "", limit=args.limit))
    return 0


def cmd_new(ctx, args):
    print(ctx.tether("new_asset", kind=args.kind, path=args.path, parent=args.parent or "", save=args.save))
    return 0


SOURCE_EXTS = (".h", ".hpp", ".cpp", ".inl", ".cs", ".ini", ".py")
SKIP_DIRS = {"Binaries", "Intermediate", "Saved", "DerivedDataCache", ".git", "ThirdParty", "__pycache__", "obj", "bin"}


def project_code_roots(project):
    """The project's own code and config folders: Source, Config, Script (C#), and each project plugin's Source."""
    root = os.path.dirname(project)
    roots = [os.path.join(root, d) for d in ("Source", "Config", "Script") if os.path.isdir(os.path.join(root, d))]
    plugins = os.path.join(root, "Plugins")
    for dirpath, dirs, files in os.walk(plugins) if os.path.isdir(plugins) else []:
        if any(f.endswith(".uplugin") for f in files):
            roots += [os.path.join(dirpath, d) for d in ("Source", "Config") if os.path.isdir(os.path.join(dirpath, d))]
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    return roots


def code_usages(project, symbol, limit=200):
    """(path, line number, text) for whole-word matches of symbol in the project's code and config."""
    # Interface and RPC functions are called and implemented under decorated names.
    rx = re.compile(r"(?<![\w])(?:Execute_|K2_)?" + re.escape(symbol) + r"(?:_Implementation|_Validate)?(?![\w])")
    hits = []
    for root in project_code_roots(project):
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not name.endswith(SOURCE_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for number, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append((path, number, line.strip()))
                                if len(hits) >= limit:
                                    return hits
                except OSError:
                    continue
    return hits


def asset_files_mentioning(project, needle, max_mb=32):
    """Packages whose bytes contain needle (a name in their name table: a function, property or class they use).
    Files over max_mb (meshes, textures) are skipped. Returns ({package: path}, skipped count)."""
    import mmap
    root = os.path.dirname(project)
    found, skipped = {}, 0
    targets = [needle.encode("utf-8")]
    content_dirs = [os.path.join(root, "Content")]
    for dirpath, dirs, files in os.walk(os.path.join(root, "Plugins")) if os.path.isdir(os.path.join(root, "Plugins")) else []:
        if "Content" in dirs and any(f.endswith(".uplugin") for f in files):
            content_dirs.append(os.path.join(dirpath, "Content"))
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "Content"]
    for content in content_dirs:
        for dirpath, dirs, files in os.walk(content):
            for name in files:
                if not name.endswith((".uasset", ".umap")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    if size > max_mb * 1024 * 1024:
                        skipped += 1
                        continue
                    if size == 0:
                        continue
                    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                        if any(data.find(t) != -1 for t in targets):
                            package = content_package(project, path)
                            if package:
                                found[package] = path
                except (OSError, ValueError):
                    continue
    return found, skipped


def cmd_usages(ctx, args):
    """Where a C++ symbol (function, property, class) or an asset is used: code, Blueprint graphs, other assets."""
    project, symbol = ctx.project, args.symbol
    if symbol.startswith("/"):  # an asset: the registry knows its referencers; code and config may name its path
        print(ctx.tether("refs", asset=symbol, deps=False, limit=args.limit))
        package = symbol.split(".")[0]
        hits = [h for h in code_usages(project, package.rsplit("/", 1)[-1]) if package in h[2]]
        if hits:
            print(f"{len(hits)} mention(s) in code and config:")
            for path, number, text in hits[: args.limit]:
                print(f"  {os.path.relpath(path, os.path.dirname(project))}:{number}: {short(text, 140)}")
        return 0
    bare = symbol.split("::")[-1].split(".")[-1]
    stem = re.sub(r"^[AUFESIT](?=[A-Z])", "", bare)  # AMyActor -> MyActor: reflected names drop the prefix
    started = time.time()
    hits = code_usages(project, bare, limit=500)
    print(f"{len(hits)} line(s) in the project's code and config" + (":" if hits else ""))
    for path, number, text in hits[: args.limit]:
        print(f"  {os.path.relpath(path, os.path.dirname(project))}:{number}: {short(text, 140)}")
    if len(hits) > args.limit:
        print(f"  ... +{len(hits) - args.limit} more (--limit)")
    assets, skipped = asset_files_mentioning(project, stem, args.max_mb)
    names = sorted(assets)
    levels = {}
    plain = []
    for package in names:
        match = re.match(r"^(/[^/]+)/__ExternalActors__/(.+)/[^/]+/[^/]+/[^/]+$", package)
        if match:
            level = f"{match.group(1)}/{match.group(2)}"
            levels[level] = levels.get(level, 0) + 1
        else:
            plain.append(package)
    print(f"{len(names)} asset(s) name '{stem}'" + (f" ({skipped} files over {args.max_mb} MB not searched)" if skipped else "")
          + (":" if names else ""))
    graphs, confirming = {}, False
    if plain and editor_live(project) and ctx.transport != "remote":
        confirming = True
        try:  # confirm in the Blueprints' graphs and show the nodes
            graph = ctx.tether("package_graph", packages=",".join(plain[:200]))
            classes = (json.loads(graph) if isinstance(graph, str) else graph)["classes"]
            rx = re.compile(r"(?<![\w])" + re.escape(stem) + r"(?![\w])")
            deadline = time.time() + args.budget  # big Blueprints take seconds per graph
            for package in [p for p in plain if classes.get(p, "").endswith("Blueprint")][:args.limit]:
                if time.time() > deadline:
                    break
                if os.path.getsize(assets[package]) > 4 * 1024 * 1024:
                    continue  # loading and reading a huge Blueprint's graphs can take minutes
                bp = asset_ref(package)
                outline = bp_outline(ctx, bp)
                if not outline:
                    continue
                source = graph_source(ctx, bp)
                lines, complete = [], True
                for name in outline["graphs"]:
                    if time.time() > deadline:
                        complete = False
                        break
                    for line in read_graph(ctx, source, bp, name).splitlines():
                        if rx.search(line):
                            lines.append(f"{name}: {line.strip()}")
                if complete or lines:
                    graphs[package] = lines
        except CliError:
            graphs = {}
    for package in plain[: args.limit]:
        lines = graphs.get(package)
        if lines:
            print(f"  {package}")
            for line in lines[:5]:
                print(f"    {short(line, 150)}")
            if len(lines) > 5:
                print(f"    ... +{len(lines) - 5} more")
        else:
            print(f"  {package}" + ("  (not in its graphs: class defaults, a pin default or a property name)"
                                    if package in graphs else ""))
    unread = [p for p in plain[: args.limit] if p not in graphs]
    if confirming and unread:
        print(f"  ({len(unread)} not checked in their graphs: over 4 MB, or past --budget {args.budget:g}s)")
    for level, count in sorted(levels.items()):
        print(f"  {level}: {count} placed actor{'' if count == 1 else 's'}")
    if args.time:
        print(f"({time.time() - started:.1f}s)", file=sys.stderr)
    return 0


CPP_CLASS = re.compile(r"^\s*class\s+(?:\w+_API\s+)?([AU]\w+)\s*(?:final\s*)?:\s*public\s+([AU]\w+)", re.M)
ROLE_BASES = (("game mode", ("GameModeBase", "GameMode")), ("game state", ("GameStateBase", "GameState")),
              ("player controller", ("PlayerController",)), ("character", ("Character",)), ("pawn", ("Pawn",)),
              ("AI controller", ("AIController",)), ("HUD", ("HUD",)), ("game instance", ("GameInstance",)),
              ("subsystem", ("GameInstanceSubsystem", "WorldSubsystem", "LocalPlayerSubsystem", "EngineSubsystem")),
              ("component", ("ActorComponent", "SceneComponent")))


def ini_value(project, ini, key):
    path = os.path.join(os.path.dirname(project), "Config", ini)
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            match = re.search(rf"^{re.escape(key)}=(.*)$", f.read(), re.M)
    except OSError:
        return None
    return match.group(1).strip() if match else None


def project_overview(project, full=True):
    """Lines describing the project from its files: engine, modules and key classes, maps, input, plugins, content."""
    root, name = os.path.dirname(project), os.path.splitext(os.path.basename(project))[0]
    data = read_json(project) or {}
    out = [f"{name}: UE {data.get('EngineAssociation') or '?'}"]
    modules = [m.get("Name") for m in data.get("Modules", [])]
    classes, by_module = [], {}
    for module in modules:
        folder = os.path.join(root, "Source", module)
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(".h"):
                    try:
                        with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as h:
                            found = CPP_CLASS.findall(h.read())
                    except OSError:
                        continue
                    classes += found
                    by_module[module] = by_module.get(module, 0) + len(found)
    if modules:
        out.append("modules: " + ", ".join(f"{m} ({by_module.get(m, 0)} classes)" for m in modules))
    bases = dict(classes)

    def derives(cls, wanted, depth=0):
        base = bases.get(cls)
        return bool(base) and depth < 8 and (base[1:] in wanted or derives(base, wanted, depth + 1))

    roles = []
    for role, wanted in ROLE_BASES:
        hits = [c for c, b in classes if b[1:] in wanted or derives(c, wanted)]
        if hits and (full or role not in ("component", "subsystem")):
            roles.append(f"{role}: " + ", ".join(hits[:5]) + (f" +{len(hits) - 5}" if len(hits) > 5 else ""))
    if roles:
        out.append("classes: " + " | ".join(roles))
    if os.path.isdir(os.path.join(root, "Script")):
        cs = sum(1 for _, _, files in os.walk(os.path.join(root, "Script")) for f in files if f.endswith(".cs"))
        out.append(f"C# (UnrealSharp): {cs} .cs files under Script/")
    short_path = lambda v: (v or "").split(".")[0] or "-"
    out.append(f"startup map {short_path(ini_value(project, 'DefaultEngine.ini', 'EditorStartupMap'))}, game default "
               f"{short_path(ini_value(project, 'DefaultEngine.ini', 'GameDefaultMap'))}, game mode "
               f"{short_path(ini_value(project, 'DefaultEngine.ini', 'GlobalDefaultGameMode')).rsplit('/', 1)[-1]}")
    maps, inputs = [], []
    content = os.path.join(root, "Content")
    for dirpath, dirs, files in os.walk(content):
        dirs[:] = [d for d in dirs if not d.startswith("__External")]
        for f in files:
            if f.endswith(".umap"):
                package = content_package(project, os.path.join(dirpath, f))
                rest = package.split("/", 2)[2] if package else ""
                wp = os.path.isdir(os.path.join(content, "__ExternalActors__", *rest.split("/")))
                maps.append(package.rsplit("/", 1)[-1] + (" (WP)" if wp else ""))
            elif f.startswith(("IA_", "IMC_")) and f.endswith(".uasset"):
                inputs.append(f[:-7])
    if maps:
        out.append(f"maps ({len(maps)}): " + ", ".join(sorted(maps)[:12]) + (" ..." if len(maps) > 12 else ""))
    if inputs:
        out.append("input: " + ", ".join(sorted(inputs)[:12]) + (" ..." if len(inputs) > 12 else ""))
    plugins = [p["Name"] for p in data.get("Plugins", []) if p.get("Enabled")]
    own = sorted({f[:-8] for d, _, files in os.walk(os.path.join(root, "Plugins")) for f in files if f.endswith(".uplugin")})
    if plugins or own:
        out.append(f"plugins: {len(plugins)} enabled in the .uproject" + (f"; in Plugins/: {', '.join(own[:8])}" if own else ""))
    if full:
        sizes = folder_packages(project, "/Game") if os.path.isdir(content) else {}
        groups = sorted(size_groups(sizes, 1).items(), key=lambda kv: -kv[1][0])
        out.append(f"content: {len(sizes)} packages, {human_bytes(sum(sizes.values()))}"
                   + (" (" + ", ".join(f"{k.rsplit('/', 1)[-1]} {human_bytes(b)}" for k, (b, n) in groups[:6]) + ")"
                      if groups else ""))
        head = run_git(project, "log", "-1", "--format=%h %s (%cr)", check=False)
        if head:
            out.append(f"git: {head.strip()[:120]}")
    return out


def cmd_overview(ctx, args):
    for line in project_overview(ctx.project, full=not args.brief):
        print(line)
    return 0


WP_BUILDERS = {"hlod": "WorldPartitionHLODsBuilder", "minimap": "WorldPartitionMiniMapBuilder",
               "nav-wp": "WorldPartitionNavigationDataBuilder", "resave": "WorldPartitionResaveActorsBuilder"}
LIGHTING_DONE = re.compile(r"LogStaticLightingSystem: (?:Warning: )?(Illumination: .*|Failed to build lighting.*|"
                           r".*Skipping Lighting Build.*|Failed to (?:initialize|connect to) Swarm.*)")


DIRTY_PACKAGES = ("import json\nU = unreal.EditorLoadingAndSavingUtils\n"
                  "print(json.dumps([p.get_name() for p in U.get_dirty_map_packages() + U.get_dirty_content_packages()]))")


def py_json(ctx, code):
    """Run editor Python that prints JSON last, and decode it (ue py output may already be decoded)."""
    out = ctx.tether("py", code=code)
    return out if not isinstance(out, str) else json.loads(out.strip().splitlines()[-1])


def new_log_lines(ctx, start):
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        return f.read().decode("utf-8", "replace").splitlines()


def cmd_world(ctx, args):
    """World builds: navmesh and lighting in the live editor; HLODs and other World Partition builders as jobs."""
    what = args.what.lower()
    if what == "nav":
        dirty_before = set(py_json(ctx, DIRTY_PACKAGES))
        started = time.time()
        ctx.tether("console", command="RebuildNavigation")
        building = "unreal.NavigationSystemV1.is_navigation_being_built(unreal.get_editor_subsystem(" \
                   "unreal.UnrealEditorSubsystem).get_editor_world())"
        time.sleep(0.5)
        while str(ctx.tether("py", code=building)).strip().endswith("True"):
            if time.time() - started > args.wait:
                raise CliError(f"navigation still building after {args.wait:g}s", 4)
            time.sleep(0.5)
        print(f"navigation rebuilt in {time.time() - started:.1f}s")
        print(ctx.tether("nav", action="status", start="", end="", match="", pie=False, limit=args.limit))
        dirty_after = py_json(ctx, DIRTY_PACKAGES)
        changed = [p for p in dirty_after if p not in dirty_before and not p.startswith("/Temp/")]
        if changed and args.save:
            print(ctx.tether("save", paths=",".join(changed)))
        elif changed:
            print(f"{len(changed)} package(s) changed, unsaved (--save, or ue save): " + ", ".join(changed[:5]))
        return 0
    if what == "lighting":
        start, started = log_end(ctx.project), time.time()
        quality = {"preview": "QUALITY_PREVIEW", "medium": "QUALITY_MEDIUM", "high": "QUALITY_HIGH",
                   "production": "QUALITY_PRODUCTION"}[args.quality.lower()]
        ctx.tether("py", code=f"unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).build_light_maps("
                              f"unreal.LightingBuildQuality.{quality}, True)")
        result = None
        while result is None:
            for line in new_log_lines(ctx, start):
                m = LIGHTING_DONE.search(line)
                if m:
                    result = m.group(1)
            if result is None:
                if time.time() - started > args.wait:
                    raise CliError(f"no lighting result after {args.wait:g}s (Lightmass may still run; ue log)", 4)
                time.sleep(1)
        problems = [l for l in filter_log(new_log_lines(ctx, start), level="Warning")
                    if "Lighting" in l or "Lightmass" in l or "MapCheck" in l]
        print(f"lighting ({args.quality}) in {time.time() - started:.0f}s: {result}")
        for line in problems[: args.limit]:
            print("  " + line)
        return 0 if result.startswith("Illumination") else 1
    builder = WP_BUILDERS.get(what, args.what)
    target_map = args.map or (ctx.beacon or {}).get("map")
    if not target_map:
        raise CliError("give the map: ue world build hlod --map /Game/Maps/L")
    live = editor_live(ctx.project)
    if live and live.get("map") == target_map and not args.force:
        raise CliError(f"{target_map} is open in the editor; a builder commandlet would write the same packages. "
                       "Close the editor (ue close) or pass --force")
    engine = engine_root(ctx.project)
    exe = editor_executable(engine)
    cmd_exe = exe.replace("UnrealEditor.exe", "UnrealEditor-Cmd.exe") if os.path.isfile(
        exe.replace("UnrealEditor.exe", "UnrealEditor-Cmd.exe")) else exe
    command = [cmd_exe, ctx.project, target_map, "-run=WorldPartitionBuilderCommandlet", f"-Builder={builder}",
               "-AllowCommandletRendering", "-unattended", "-nosplash", "-SCCProvider=None"]
    cmd_job(ctx, argparse.Namespace(action="start", platform=None, id=None, name=f"{builder} {target_map}",
                                    limit=args.limit, project=args.project, job_command=command))
    if args.no_wait:
        return 0
    jobs = sorted(f[:-5] for f in os.listdir(jobs_dir(ctx.project)) if f.endswith(".json"))
    return cmd_job(ctx, argparse.Namespace(action="wait", id=jobs[-1], wait=args.wait, verbose=False,
                                           limit=args.limit, project=args.project))


ACTIVE_SOUND = re.compile(r"LogAudio: Display: (\S.*?) \(([\d.e+-]+)\) - (.*)$")
WAVE_INSTANCE = re.compile(r"LogAudio: Display: {3,}(\S.*?) \(([\d.e+-]+)\) \((\d+)\) - ([\d.e+-]+)")


def parse_active_sounds(lines):
    """The sounds listed by au.DumpActiveSounds (the last dump in lines), each with its wave instances."""
    start = max((i for i, l in enumerate(lines) if "Active Sound Count:" in l), default=None)
    if start is None:
        return None
    sounds = []
    for line in lines[start + 1:]:
        wave = WAVE_INSTANCE.search(line)
        if wave and sounds:
            sounds[-1]["waves"].append({"wave": wave.group(1), "volume": float(wave.group(4))})
            continue
        sound = ACTIVE_SOUND.search(line)
        if sound and not line.split("Display: ", 1)[-1].startswith(" "):
            sounds.append({"sound": sound.group(1), "duration": float(sound.group(2)),
                           "component": sound.group(3).strip(), "waves": []})
    return sounds


def active_sounds(ctx):
    """Every sound the audio device is playing for PIE, fire-and-forget ones included (au.DumpActiveSounds)."""
    start = log_end(ctx.project)
    ctx.tether("console", command="au.DumpActiveSounds", pie=True)
    deadline = time.time() + 5
    while time.time() < deadline:  # the dump is written from the audio thread
        sounds = parse_active_sounds(new_log_lines(ctx, start))
        if sounds is not None:
            time.sleep(0.3)
            return parse_active_sounds(new_log_lines(ctx, start)) or []
        time.sleep(0.3)
    raise CliError("au.DumpActiveSounds wrote nothing: this editor has no audio device "
                   "(reopen with `ue open --headless --sound`, which keeps it silent)")


def cmd_audio(ctx, args):
    """Sounds playing in PIE: what, from where, how loud."""
    if not (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is not running (ue pie start)")
    sounds = active_sounds(ctx)
    comps = ctx.tether("audio_components", limit=200)
    comps = json.loads(comps) if isinstance(comps, str) else comps
    by_name = {c["component"]: c for c in comps}
    rx = re.compile(args.match, re.I) if args.match else None
    rows = []
    for s in sounds:
        comp = next((c for n, c in by_name.items() if n and n in s["component"]), None)
        length = "loop" if s["duration"] >= 10000 else f"{s['duration']:.1f} s"
        volume = max((w["volume"] for w in s["waves"]), default=None)
        row = (f"{s['sound']} ({length})" + (f" on {comp['owner']}" if comp and comp.get("owner") else
                                              f" via {s['component']}" if s["component"] not in ("", "None") else "")
               + (f", {comp['distance']} cm away" if comp and comp.get("distance") is not None else "")
               + (f", volume {volume:.2f}" if volume is not None else "")
               + (f", attenuation {comp['attenuation']}" if comp and comp.get("attenuation") else ""))
        if not rx or rx.search(row):
            rows.append(row)
    print(f"{len(rows)} sound(s) playing" + (f" matching /{args.match}/" if args.match else "") + (":" if rows else ""))
    for row in rows[: args.limit]:
        print("  " + row)
    return 0


def process_memory_mb(pid):
    """A process's private memory in MB (Windows: PrivateUsage; elsewhere: VmRSS), or None."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                                "PrivateUsage")]
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))  # query limited information | vm read
        if not handle:
            return None
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ok = psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return counters.PrivateUsage / 1024 ** 2 if ok else None
    try:
        with open(f"/proc/{int(pid)}/status") as f:
            match = re.search(r"VmRSS:\s+(\d+) kB", f.read())
        return int(match.group(1)) / 1024 if match else None
    except OSError:
        return None


def slope_per_minute(samples):
    """Least-squares slope of (seconds, value) samples, per minute."""
    if len(samples) < 3:
        return 0.0
    n = len(samples)
    mx, my = sum(t for t, _ in samples) / n, sum(v for _, v in samples) / n
    var = sum((t - mx) ** 2 for t, _ in samples)
    return 0.0 if not var else 60 * sum((t - mx) * (v - my) for t, v in samples) / var


def cmd_soak(ctx, args):
    """A long PIE run (a step file in a loop, or wandering) watching memory, frame rate and errors."""
    beacon = ctx.beacon or {}
    pid = beacon.get("pid")
    if not editor_live(ctx.project):
        raise CliError("the editor is not running (ue open --headless)")
    log_start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running"
    if not was_running and cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=2.0, limit=0, script=None)):
        raise CliError("could not start PIE")
    frames = lambda: int(str(ctx.tether("py", code="unreal.SystemLibrary.get_frame_count()")).strip().splitlines()[-1])
    wander = ("import random\nw = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
              "p = unreal.GameplayStatics.get_player_pawn(w, 0)\n"
              # with navigation off there is no navigation system, and querying one trips an engine ensure
              "nav = unreal.NavigationSystemV1.get_navigation_system(w)\n"
              "goal = unreal.NavigationSystemV1.get_random_reachable_point_in_radius(w, p.get_actor_location(), 3000) "
              "if nav else None\n"
              "if goal is None:\n    loc = p.get_actor_location()\n"
              "    goal = unreal.Vector(loc.x + random.uniform(-800, 800), loc.y + random.uniform(-800, 800), loc.z)\n"
              "    p.set_actor_location(goal, False, True)\n"
              "else:\n    unreal.AIHelperLibrary.simple_move_to_location(unreal.GameplayStatics.get_player_controller(w, 0), goal)\n"
              "print('walk' if goal else 'hop')")
    samples, fps, failures, crashed, iterations = [], [], [], None, 0
    deadline = started + args.minutes * 60
    next_sample, last_frames, last_time = time.time(), frames(), time.time()
    try:
        while time.time() < deadline:
            if args.file:
                code = pie_do(ctx, argparse.Namespace(script=[], file=args.file, reject=None, warmup=0, keep=True,
                                                      keep_going=False, quiet=True))
                if code:
                    failures.append(f"iteration {iterations + 1}: the step file failed")
                    if not args.keep_going:
                        break
            else:
                ctx.tether("py", code=wander)
                time.sleep(min(args.sample, 5))
            iterations += 1
            if time.time() >= next_sample:
                memory = process_memory_mb(pid)
                now, count = time.time(), frames()
                if memory is not None:
                    samples.append((now - started, memory))
                fps.append((count - last_frames) / max(now - last_time, 1e-3))
                last_frames, last_time, next_sample = count, now, now + args.sample
    except CliError as exc:
        crashed = recent_crash(ctx.project, started - 5)
        failures.append(f"stopped: {exc}")
    finally:
        if not crashed and not was_running and editor_live(ctx.project):
            with __import__("contextlib").suppress(CliError):
                cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    elapsed = (time.time() - started) / 60
    errors = []
    if os.path.isfile(log_path(ctx.project)):
        with open(log_path(ctx.project), "rb") as f:
            f.seek(log_start)
            errors = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Error")
    growth = slope_per_minute(samples[1:])  # the first sample includes PIE start-up
    print(f"soak {elapsed:.1f} min, {iterations} iteration(s)" + (f" of {os.path.basename(args.file)}" if args.file else " of wandering"))
    if samples:
        print(f"  memory {samples[0][1]:.0f} -> {samples[-1][1]:.0f} MB (peak {max(v for _, v in samples):.0f}), "
              f"trend {growth:+.1f} MB/min over {len(samples)} samples")
    if fps:
        print(f"  frame rate avg {sum(fps) / len(fps):.0f} fps, lowest window {min(fps):.0f} fps")
    print(f"  {len(errors)} error line(s) logged" + (":" if errors else ""))
    for line in errors[: args.limit]:
        print("    " + line)
    if crashed:
        print("  CRASHED: " + crash_summary(crashed) + " (ue crash)")
    for failure in failures[:5]:
        print("  " + failure)
    leaking = len(samples) >= 4 and growth > args.max_growth
    if leaking:
        print(f"  memory grows {growth:.1f} MB/min, over --max-growth {args.max_growth:g}")
    failed = bool(crashed or failures or leaking)
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


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
    return doctor_print(results)


def doctor_print(results):
    for status, what, fix in results:
        print(f"{status:<4} {what}" + (f"\n       fix: {fix}" if fix and status != "ok" else ""))
    bad = sum(1 for s, _, _ in results if s == "FAIL")
    warn = sum(1 for s, _, _ in results if s == "WARN")
    print(f"{len(results)} checks: {bad} problem(s), {warn} warning(s)")
    return 1 if bad else 0


def cmd_find(ctx, args):
    print(ctx.tether("find", query=args.query or "", cls=args.cls or "", under=args.under, limit=args.limit))
    return 0


def cmd_refs(ctx, args):
    print(ctx.tether("refs", asset=args.asset, deps=args.deps, limit=args.limit))
    return 0


def cmd_api(ctx, args):
    print(ctx.tether("api", name=args.name, match=args.match or "", limit=args.limit))
    return 0


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


def tether_version():
    plugin = read_json(os.path.join(tether_plugins_dir(), "Tether", "Tether.uplugin")) or {}
    return plugin.get("VersionName", "?")


FIRST_ASSET = """
import unreal
reg = unreal.AssetRegistryHelpers.get_asset_registry()
hits = [str(a.package_name) for a in reg.get_assets_by_class(unreal.TopLevelAssetPath('/Script/Engine', '{cls}'))
        if str(a.package_name).startswith('/Game/')]
print(sorted(hits)[0] if hits else '')
"""


PLANT_VALIDATOR = """
import sys
if not hasattr(sys, '_tether_validator_cls'):  # a Python uclass can be defined once per editor session
    @unreal.uclass()
    class TetherSelftestValidator(unreal.EditorValidatorBase):
        @unreal.ufunction(override=True)
        def k2_can_validate_asset(self, asset):
            return asset.get_outermost().get_name() == getattr(sys, '_tether_validator_target', '')
        @unreal.ufunction(override=True)
        def k2_validate_loaded_asset(self, asset):
            self.asset_fails(asset, 'tether selftest: planted failure')
            return unreal.DataValidationResult.INVALID
    sys._tether_validator_cls = TetherSelftestValidator
subsystem = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
if {plant}:
    sys._tether_validator_target = '{target}'
    sys._tether_validator = sys._tether_validator_cls()
    subsystem.add_validator(sys._tether_validator)
elif getattr(sys, '_tether_validator', None):
    subsystem.remove_validator(sys._tether_validator)
    sys._tether_validator = None
print('ok')
"""


def cmd_selftest(ctx, args):
    """Run every capability against the live editor, one CLI process per check, and report pass/fail/skip."""
    import tempfile
    project = ctx.project
    base = [sys.executable, UE_SCRIPT, "--project", project]
    results, notes = [], {}

    def ue(*argv, timeout=180):
        started = time.time()
        proc = subprocess.run(base + list(argv), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip(), time.time() - started

    def check(name, argv, ok=lambda code, out: code == 0, skip=None, timeout=180):
        if skip:
            results.append(("SKIP", name, skip, 0.0))
            return None
        try:
            code, out, took = ue(*argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            results.append(("FAIL", name, f"no answer within {timeout}s", float(timeout)))
            return None
        passed = ok(code, out)
        detail = "" if passed else (out.splitlines() or [f"exit {code}"])[-1][:160]
        results.append(("PASS" if passed else "FAIL", name, detail, took))
        return out if passed else None

    beacon = ctx.beacon or {}
    print(f"Tether {tether_version()} | {os.path.basename(project)} | UE {beacon.get('engine', '?').split('+')[0]} | "
          f"{host_platform()} | Python {sys.version.split()[0]} | transport {ctx.transport}")
    if not check("status", ["status"]):
        for status, name, detail, _ in results:
            print(f"{status} {name}: {detail}")
        raise CliError("the editor is not running or not answering; start it with `ue open --headless --render`", 2)
    remote = ctx.transport == "remote"
    launch = read_json(os.path.join(state_dir(project), "launch.json")) or {}
    check("doctor", ["doctor"], ok=lambda c, o: "checks:" in o)
    check("py", ["py", "6*7"], ok=lambda c, o: c == 0 and o.endswith("42"))
    check("exec", ["exec", "stat fps"], ok=lambda c, o: c == 0 and "Cmd: stat fps" in o)
    check("actors", ["actors", "--limit", "3"], ok=lambda c, o: c == 0 and "actors" in o.splitlines()[0])
    check("api", ["api", "Actor", "set_actor_location"], ok=lambda c, o: c == 0 and "set_actor_location" in o)
    check("find", ["find", "Cube", "--class", "StaticMesh", "--under", "/Engine/BasicShapes"],
          ok=lambda c, o: c == 0 and "/Engine/BasicShapes/Cube" in o)
    check("refs", ["refs", "/Engine/BasicShapes/BasicShapeMaterial", "--limit", "3"],
          ok=lambda c, o: c == 0 and "reference" in o)
    check("asset", ["asset", "/Engine/BasicShapes/Cube"], ok=lambda c, o: c == 0 and "triangles" in o)
    check("log", ["log", "--level", "Error"])
    check("changes", ["changes"], ok=lambda c, o: c == 0 and "unsaved package" in o)
    check("size", ["size", "--top", "3"], ok=lambda c, o: c == 0 and "packages" in o)
    check("mem", ["mem", "--top", "3"], timeout=300, ok=lambda c, o: c == 0 and "process:" in o)
    check("config", ["config", "Project/Project/Maps", "DefaultMap"], skip="needs UE 5.8+" if remote else None,
          ok=lambda c, o: c == 0 and "gameDefaultMap" in o)
    check("usages", ["usages", "PrintString", "--budget", "5"], ok=lambda c, o: c == 0 and "line(s)" in o)
    check("nav", ["nav"], ok=lambda c, o: c == 0 and ("bounds" in o or "NavMeshBoundsVolume" in o))
    check("call tether.level_summary", ["call", "tether.level_summary", "limit=3"],
          ok=lambda c, o: c == 0 and "actors" in o)

    # A transient actor to read and edit; never saved, destroyed at the end.
    spawned = check("spawn probe actor", ["py", "a = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)"
                    ".spawn_actor_from_class(unreal.TargetPoint, unreal.Vector(0, 0, 100000), transient=True)\n"
                    "a.set_actor_label('TetherSelftestProbe')\nprint(a.get_actor_label())"],
                    ok=lambda c, o: c == 0 and "TetherSelftestProbe" in o)
    probe_skip = None if spawned else "probe actor missing"
    check("props", ["props", "TetherSelftestProbe", "hidden"], skip=probe_skip)
    check("set", ["set", "TetherSelftestProbe", "location+=0,0,50"], skip=probe_skip,
          ok=lambda c, o: c == 0 and "-> (0, 0, 100050)" in o)
    check("undo", ["undo"], skip=probe_skip, ok=lambda c, o: c == 0 and "undone: TetherSelftestProbe" in o)
    check("set rollback", ["set", "TetherSelftestProbe", "location=0,0,0", "no_such_property_xyz=1"],
          skip=probe_skip, ok=lambda c, o: c != 0 and "no property" in o)
    check("drop", ["drop", "TetherSelftestProbe"], skip=probe_skip,
          ok=lambda c, o: c == 0 and ("-> " in o or "no ground below" in o))
    check("mat", ["mat", "/Engine/BasicShapes/BasicShapeMaterial"], ok=lambda c, o: c == 0 and "vector  Color" in o)
    instance = check("mat: transient instance", ["py",
        "mi = unreal.find_object(None, '/Engine/Transient.MI_TetherSelftest') or "
        "unreal.new_object(unreal.MaterialInstanceConstant, name='MI_TetherSelftest')\n"
        "unreal.MaterialEditingLibrary.set_material_instance_parent(mi, unreal.load_asset('/Engine/BasicShapes/BasicShapeMaterial'))\n"
        "mi.get_path_name()"], ok=lambda c, o: c == 0 and o.endswith("MI_TetherSelftest"))
    check("mat set", ["mat", "/Engine/Transient.MI_TetherSelftest", "Color=1,0,0"],
          skip=None if instance else "no transient instance", ok=lambda c, o: c == 0 and "-> (1, 0, 0, 1)" in o)
    check("undo mat", ["undo"], skip="before 5.8, ue undo covers ue set only" if remote else
          None if instance else "no transient instance", ok=lambda c, o: c == 0 and "undone: mat" in o)
    created = check("new", ["new", "bp", "/Game/TetherSelftest/BP_SelftestNew", "--parent", "Actor"],
                    ok=lambda c, o: c == 0 and "created /Game/TetherSelftest/BP_SelftestNew" in o)
    edit_skip = "needs UE 5.8+ (Epic's BlueprintTools)" if remote else None if created else "nothing created"
    check("bp edit add-var", ["bp", "edit", "/Game/TetherSelftest/BP_SelftestNew", "add-var", "Health", "float", "75"],
          skip=edit_skip, ok=lambda c, o: c == 0 and "-> 75" in o and ": compiled" in o)
    check("bp edit add-component", ["bp", "edit", "/Game/TetherSelftest/BP_SelftestNew", "add-component", "Body",
                                    "StaticMesh", "StaticMesh=/Engine/BasicShapes/Cube"],
          skip=edit_skip, ok=lambda c, o: c == 0 and "added Body" in o and "BasicShapes/Cube" in o)
    check("rm (new asset)", ["rm", "/Game/TetherSelftest/BP_SelftestNew"], skip=None if created else "nothing created",
          ok=lambda c, o: c == 0 and "deleted" in o)
    ue("py", "unreal.EditorAssetLibrary.delete_directory('/Game/TetherSelftest')")
    scattered = check("scatter", ["scatter", "/Engine/BasicShapes/Cube", "--count", "3", "--area", "0,0,400",
                                  "--label", "TetherSelftestScatter"],
                      ok=lambda c, o: c == 0 and ("placed 3 of 3" in o or "had no ground" in o))
    check("undo scatter", ["undo"], skip=None if scattered and "placed 3" in scattered else "nothing was placed",
          ok=lambda c, o: c == 0 and "undone: scatter" in o)
    check("destroy probe actor", ["py", "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
          "found = [a for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor) "
          "if a.get_actor_label() == 'TetherSelftestProbe']\n"
          "print(len(found), unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(found))"],
          skip=probe_skip, ok=lambda c, o: c == 0 and o.endswith("1 True"))

    rendering = launch.get("render", True) or not launch.get("headless")
    shot_out = check("shot", ["shot", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest.png")],
                     skip="needs 5.8 (CaptureViewport)" if remote else None if rendering else
                     "headless with -nullrhi; reopen with `ue open --headless --render`",
                     ok=lambda c, o: c == 0 and ".png" in o)
    if shot_out:
        notes["shot"] = shot_out.split()[1] if len(shot_out.split()) > 1 else ""
        based = check("shot --baseline", ["shot", "--at", "0,-500,300", "--rot", "-20,90,0", "--baseline", "tether_selftest",
                                          "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-base.png")],
                      ok=lambda c, o: c == 0 and "baseline 'tether_selftest' saved" in o)
        check("shot --compare", ["shot", "--compare", "tether_selftest", "--tolerance", "5",
                                 "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-cmp.png")],
              skip=None if based else "no baseline", ok=lambda c, o: c == 0 and "of pixels changed" in o)
        for ext in (".png", ".json"):
            with __import__("contextlib").suppress(OSError):
                os.remove(os.path.join(state_dir(project), "shots", "baselines", "tether_selftest" + ext))
    check("look", ["look", "/Engine/BasicShapes/Cube", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-look.png")],
          skip="needs 5.8 (CaptureViewport)" if remote else None if rendering else
          "headless with -nullrhi; reopen with `ue open --headless --render`",
          ok=lambda c, o: c == 0 and ".png" in o)
    tests_available = not remote and "AutomationTestToolset.AutomationTestToolset" in catalog(ctx)
    check("test", ["test", "StartsWith:System.Core.Math.Evaluate"], timeout=600,
          skip=None if tests_available else "needs Epic's AutomationTestToolset (UE 5.8+, `ue init` enables it)",
          ok=lambda c, o: c == 0 and "passed" in o)
    blueprint = (ue("py", FIRST_ASSET.format(cls="Blueprint"))[1].splitlines() or [""])[-1]
    check("bp", ["bp", blueprint or ""], skip="needs 5.8 (Epic's BlueprintTools)" if remote else
          None if blueprint and blueprint.startswith("/Game/") else "no Blueprint under /Game",
          ok=lambda c, o: c == 0 and ("==" in o or "no graphs" in o))
    if blueprint and blueprint.startswith("/Game/"):
        planted = check("validate: plant", ["py", PLANT_VALIDATOR.format(plant=True, target=blueprint)],
                        ok=lambda c, o: c == 0 and o.endswith("ok"))
        check("validate", ["validate", blueprint], skip=None if planted else "could not plant a validator",
              ok=lambda c, o: c == 1 and "planted failure" in o)
        check("validate: unplant", ["py", PLANT_VALIDATOR.format(plant=False, target="")],
              ok=lambda c, o: c == 0 and o.endswith("ok"))
    else:
        check("validate", [], skip="no Blueprint under /Game")
    blueprint_file = package_file(project, blueprint) if blueprint and blueprint.startswith("/Game/") else None
    check("diff", ["diff", "--files", blueprint_file or "", blueprint_file or ""],
          skip="needs UE 5.8+" if remote else None if blueprint_file and os.path.isfile(blueprint_file) else
          "no Blueprint file under Content/", ok=lambda c, o: c == 0 and o.startswith(blueprint or "?"))
    table = None if remote else (ue("py", FIRST_ASSET.format(cls="DataTable"))[1].splitlines() or [""])[-1]
    check("table export", ["table", table or "", "--export", os.path.join(tempfile.gettempdir(), "tether-selftest.json")],
          skip="needs 5.8 (Epic's DataTableTools)" if remote else
          None if table and table.startswith("/Game/") else "no DataTable under /Game",
          ok=lambda c, o: c == 0 and "exported" in o)
    check("perf", ["perf", "--seconds", "2", "--no-record"], timeout=300, skip="needs 5.8" if remote else None,
          ok=lambda c, o: c == 0 and "frames in" in o)
    check("perf --trace", ["perf", "--seconds", "2", "--no-record", "--trace"], timeout=600,
          skip="needs 5.8" if remote else None if rendering else "headless with -nullrhi (no render timings)",
          ok=lambda c, o: c == 0 and "top scopes" in o)
    check("pie start", ["pie", "start"], ok=lambda c, o: c == 0 and "OK" in o, timeout=300)
    check("pie status", ["pie", "status"], ok=lambda c, o: c == 0 and "running" in o)
    check("actors --pie", ["actors", "--pie", "--limit", "1"])
    check("pie do", ["pie", "do", "at 100,200,300", "near 100,200,300 50", "expect player.location ~ ."],
          ok=lambda c, o: c == 0 and "PASS" in o)
    check("ui", ["ui"], ok=lambda c, o: c == 0)
    check("ai", ["ai"], ok=lambda c, o: c == 0 and ("AI" in o))
    check("audio", ["audio"], skip=None if launch.get("sound", not launch.get("headless")) else "no audio device (ue open --headless --sound)",
          ok=lambda c, o: c == 0 and "sound(s) playing" in o)
    check("anim", ["anim", "player"], ok=lambda c, o: c == 0 and ("skeletal mesh" in o or "anim instance" in o
                                                                   or "mesh " in o))
    fixture = (ue("py", "hasattr(unreal, 'TetherProbeUILibrary')")[1].splitlines() or [""])[-1] == "True"
    check("ui click (Sandbox fixture)", ["pie", "do", "call /Script/Sandbox.Default__TetherProbeUILibrary ShowProbeUI",
                                         "expect-ui Tether probe panel", "click UMG Probe Button", "expect-ui UMG clicks: "],
          skip=None if fixture else "no UI fixture (Sandbox's TetherProbeUILibrary)", ok=lambda c, o: c == 0 and "PASS" in o)
    check("pie stop", ["pie", "stop"], ok=lambda c, o: c == 0 and "OK" in o)
    clients = check("pie --clients 2", ["pie", "start", "--clients", "2"], timeout=300, skip="needs UE 5.8+" if remote else None,
                    ok=lambda c, o: c == 0 and "1: client" in o)
    check("pie target 1", ["pie", "target", "1"], skip=None if clients else "no multiplayer session",
          ok=lambda c, o: c == 0 and "PIE world 1" in o)
    check("pie stop (restores)", ["pie", "stop"], skip=None if clients else "no multiplayer session",
          ok=lambda c, o: c == 0 and "restored" in o)
    if args.reload:
        check("reload", ["reload"])
        time.sleep(1.5)
        check("py after reload", ["py", "1+1"], ok=lambda c, o: c == 0 and o.endswith("2"))

    width = max(len(name) for _, name, _, _ in results)
    for status, name, detail, took in results:
        print(f"{status}  {name.ljust(width)}  {took:5.1f}s" + (f"  {detail}" if detail else ""))
    counts = {s: sum(1 for r in results if r[0] == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    if args.json:
        print(json.dumps([{"check": n, "status": s, "detail": d, "seconds": round(t, 2)} for s, n, d, t in results]))
    return 1 if counts["FAIL"] else 0


def cmd_ci(ctx, args):
    """A project's checks in one pass/fail command: open (if needed), tests, Blueprint compile, cook, close."""
    project, name = ctx.project, os.path.splitext(os.path.basename(ctx.project))[0]
    base = [sys.executable, UE_SCRIPT, "--project", project]
    steps, started = [], time.time()

    def step(label, argv, timeout=3600, required=True):
        t0 = time.time()
        try:
            proc = subprocess.run(base + argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
            code, out = proc.returncode, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            code, out = 4, f"no result within {timeout}s"
        steps.append({"step": label, "ok": code == 0, "exit": code, "seconds": round(time.time() - t0, 1),
                      "output": out[-4000:], "required": required})
        status = ("PASS" if code == 0 else "FAIL") if required else ("INFO" if code == 0 else "WARN")
        summary = out.splitlines()[0] if code == 0 and out else (out.splitlines()[-1] if out else f"exit {code}")
        print(f"{status}  {label:<12} {time.time() - t0:6.1f}s  {summary[:150]}")
        if code and args.verbose:
            for line in out.splitlines()[-15:]:
                print("      " + line[:200])
        return code == 0

    opened = False
    if not editor_live(project):
        opened = step("open", ["open", "--headless"], timeout=1800)
        if not opened:
            return finish_ci(args, steps, started)
    try:
        test_filter = args.tests or f"^{name}."
        listing = subprocess.run(base + ["test", "--list", test_filter], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=600)
        if listing.returncode == 0 and not listing.stdout.startswith("0 tests"):
            step("tests", ["test", test_filter])
        else:
            print(f"SKIP  tests        no tests match {test_filter}" + (" (--tests to choose)" if not args.tests else ""))
        step("blueprints", ["bp", "check", args.bp or "/Game"])
        if args.play is not None:  # scripted play tests: `ue pie do` step files
            import glob
            root = os.path.dirname(project)
            pattern = args.play or next((p for p in ("tests/play/*.txt", "Tests/Play/*.txt")
                                         if glob.glob(os.path.join(root, p))), "tests/play/*.txt")
            files = sorted(glob.glob(pattern if os.path.isabs(pattern) else os.path.join(root, pattern)))
            if not files:
                print(f"SKIP  play         no step files match {pattern}")
            for path in files:
                step(f"play {os.path.splitext(os.path.basename(path))[0]}", ["pie", "do", "-f", path], timeout=900)
        if args.validate:
            step("validate", ["validate", args.validate, "--map-check"])
        if args.cook:
            if step("cook start", ["job", "cook"], timeout=120):
                step("cook", ["job", "wait", "--wait", str(args.cook_timeout)], timeout=args.cook_timeout + 60)
        step("errors", ["log", "--since", "start", "--level", "Error"], required=False)
    finally:
        if opened:
            step("close", ["close", "--discard"], required=False)
    return finish_ci(args, steps, started)


def finish_ci(args, steps, started):
    failed = [s["step"] for s in steps if s["required"] and not s["ok"]]
    print(f"{'FAILED' if failed else 'PASSED'} in {time.time() - started:.0f}s" + (": " + ", ".join(failed) if failed else ""))
    if args.report:
        write_json(os.path.abspath(args.report), {"passed": not failed, "failed": failed, "steps": steps,
                                                  "seconds": round(time.time() - started, 1)})
    return 1 if failed else 0


def cmd_actors(ctx, args):
    print(ctx.tether("actors", match=args.match or "", limit=args.limit, pie=args.pie))
    return 0


def cmd_exec(ctx, args):
    start = log_end(ctx.project)
    world = ctx.tether("console", command=" ".join(args.command), pie=args.pie)
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
    if getattr(ctx, "transport", "mcp") == "remote":
        reachable = ctx._rex is not None  # a fresh remote connection costs ~1 s of discovery; skip it
    else:
        reachable = beacon and pid_alive(beacon.get("pid")) and beacon.get("mcp_port") and port_open(beacon["mcp_port"])
    if reachable:
        try:
            ctx.tether("console", command="FLUSHLOG")
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


def read_profile_csv(path):
    """{column: [floats per frame]} from a CsvProfiler capture (the header repeats at the end)."""
    import csv
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    header, columns = rows[0], {}
    for row in rows[1:]:
        if row and row[0] == header[0] and row[1:3] == header[1:3]:
            break  # repeated header: metadata follows
        for name, cell in zip(header, row):
            try:
                columns.setdefault(name, []).append(float(cell))
            except ValueError:
                pass
    return columns


def frame_stats(values):
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return {"avg": sum(values) / len(values), "p50": pick(0.5), "p95": pick(0.95), "max": ordered[-1]}


def cmd_perf(ctx, args):
    """Frame-time capture through the CSV profiler: frame/game/render/GPU, hitches, change vs last capture."""
    if ctx.transport == "remote":
        raise CliError("ue perf needs UE 5.8+ in this version")
    toolset = resolve_toolset(ctx, "editorapp")
    started_pie = False
    if unwrap(ctx.mcp.call(toolset, "IsPIERunning", {})[0]) is not True and not args.editor:
        if cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=1.0, limit=0, script=None)):
            raise CliError("could not start PIE to measure (or pass --editor to measure the editor itself)")
        started_pie = True
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Profiling", "CSV")
    before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    # A world context routes the command to the PIE world, where CsvProfile does nothing; None reaches it.
    path, complete = None, False
    # A headless or background editor throttles itself to a few fps ("Use Less CPU when in Background"),
    # which makes frame time meaningless; lift that for the capture and restore it afterwards.
    # Set directly, not with set_props: an editor setting is not level work, and a transaction would put it in
    # the undo history (`ue undo` would then "undo" the restore below).
    throttle = ("s = unreal.find_object(None, '/Script/UnrealEd.Default__EditorPerformanceSettings')\n"
                "s.set_editor_property('bThrottleCPUWhenNotForeground', {})\n"
                "s.get_editor_property('bThrottleCPUWhenNotForeground')")
    throttled = str(ctx.tether("py", code=throttle.format("s.get_editor_property('bThrottleCPUWhenNotForeground')"))
                    ).strip().endswith("True")
    try:
        if throttled:
            ctx.tether("py", code=throttle.format(False))
        if args.trace:  # an Unreal Insights trace over the same window, for per-scope timings
            trace_dir = os.path.join(state_dir(ctx.project), "traces")
            os.makedirs(trace_dir, exist_ok=True)
            trace_path = os.path.join(trace_dir, time.strftime("perf-%Y%m%d-%H%M%S.utrace")).replace("\\", "/")
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'trace.file \"{trace_path}\" cpu,frame')")
        ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'CsvProfile Start')")
        # The editor keeps recent trace data in a ring buffer and trace.file writes that history too, so the
        # capture window is marked as a region and the export is limited to it.
        region = time.strftime("TetherPerf%H%M%S")
        if args.trace:
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'Trace.RegionBegin {region}')")
        time.sleep(args.seconds)
        if args.trace:
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'Trace.RegionEnd {region}')")
        ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'CsvProfile Stop')")
        if args.trace:
            ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'trace.stop')")
        deadline = time.time() + 30
        while time.time() < deadline and not complete:  # the writer finishes the file with a metadata footer
            new = [f for f in (os.listdir(folder) if os.path.isdir(folder) else [])
                   if f.endswith(".csv") and f not in before]
            if new:
                path = os.path.join(folder, max(new, key=lambda f: os.path.getmtime(os.path.join(folder, f))))
                try:
                    with open(path, "rb") as f:
                        f.seek(max(0, os.path.getsize(path) - 4096))
                        complete = b"[HasHeaderRowAtEnd]" in f.read()
                except PermissionError:  # still open for writing (Windows locks it)
                    pass
            time.sleep(0.5)
    finally:
        if throttled:
            ctx.tether("py", code=throttle.format(True))
        if started_pie:
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    if not path or not complete:
        raise CliError("the CSV profiler wrote no capture (ue log --grep Csv)", 4)
    columns = read_profile_csv(path)
    frames = columns.get("FrameTime", [])
    if not frames:
        raise CliError(f"no FrameTime column in {path}")
    stats = {name: frame_stats(columns[name]) for name in ("FrameTime", "GameThreadTime", "RenderThreadTime", "GPUTime")
             if columns.get(name)}
    ft = stats["FrameTime"]
    threads = {n.replace("ThreadTime", "").replace("Time", ""): s["avg"] for n, s in stats.items() if n != "FrameTime"}
    timed = {k: v for k, v in threads.items() if v > 0}  # -nullrhi records no thread or GPU timings
    bound = max(timed, key=timed.get) if timed else None
    capped = bool(timed) and max(timed.values()) < 0.85 * ft["p50"]  # the frame waits on a cap, not on work
    hitch_limit = max(2 * ft["p50"], 33.3)
    hitches = [(i, t) for i, t in enumerate(frames) if t > hitch_limit]
    last_file = os.path.join(state_dir(ctx.project), "perf", "last.json")
    last = read_json(last_file)
    delta = lambda key: (f" ({ft[key] - last['FrameTime'][key]:+.1f})" if last and last.get("FrameTime") else "")
    print(f"{len(frames)} frames in {args.seconds:g}s{' of PIE' if not args.editor else ''}: frame avg "
          f"{ft['avg']:.1f} ms{delta('avg')} ({1000 / ft['avg']:.0f} fps), p95 {ft['p95']:.1f}{delta('p95')}, "
          f"max {ft['max']:.1f}")
    if timed:
        column = {"Game": "GameThreadTime", "Render": "RenderThreadTime", "GPU": "GPUTime"}
        moved = lambda k, v: (f" ({v - last[column[k]]['avg']:+.1f})" if last and last.get(column.get(k, ""), {}).get("avg")
                              else "")
        print("  per frame: " + ", ".join(f"{k} {v:.1f} ms{moved(k, v)}" for k, v in timed.items()) + f" | bound by {bound}"
              + (f" | frame rate capped at ~{1000 / ft['p50']:.0f} fps, so compare the per-frame work" if capped else ""))
    else:
        print("  no game/render/GPU timings: the editor runs with -nullrhi (`ue open --headless --render` for them)")
    if hitches:
        worst = sorted(hitches, key=lambda h: -h[1])[:3]
        print(f"  {len(hitches)} hitches over {hitch_limit:.0f} ms; worst: "
              + ", ".join(f"frame {i} {t:.0f} ms" for i, t in worst))
    else:
        print(f"  no hitches over {hitch_limit:.0f} ms")
    if columns.get("PhysicalUsedMB"):
        print(f"  memory {columns['PhysicalUsedMB'][-1]:.0f} MB used")
    if last:
        print(f"  (+/- vs last capture, {time.strftime('%H:%M', time.localtime(last.get('time', 0)))})")
    print(f"  csv: {path}")
    if not args.no_record:  # the baseline the next capture compares with
        write_json(last_file, dict(stats, time=time.time(), csv=path))
    if args.trace:
        perf_trace_report(ctx, trace_path, region, len(frames), args)
    return 0


# Insights adds the GPU queues to every timer-statistics export whatever the thread filter, so "gpu" (a filter
# matching no CPU thread) yields the GPU passes alone, and they are subtracted from the CPU threads.
TRACE_THREADS = (("game", "GameThread"), ("render", "RenderThread*"), ("rhi", "RHIThread"), ("gpu", "NoCpuThread"))
TIMER_FORMAT = re.compile(r"\(|\s=\s|\s%")  # 'TSR(sg.AntiAliasingQuality=%d) %dx%d' -> 'TSR'
WAIT_SCOPE = re.compile(r"Wait|Sleep|Idle|UpdateTimeAndHandleMaxTickRate|Stall", re.I)  # time spent not working


def read_timer_stats(path):
    """{timer: (count, inclusive s, exclusive s)} from an Insights ExportTimerStatistics CSV."""
    import csv
    stats = {}
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    name = TIMER_FORMAT.split(row["Name"], 1)[0].strip() or row["Name"]
                    count, incl, excl = stats.get(name, (0, 0.0, 0.0))  # same-named timers add up
                    stats[name] = (count + int(float(row["Count"])), incl + float(row["Incl"]), excl + float(row["Excl"]))
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return stats


def cpu_only(by_thread):
    """Each CPU thread's timers minus the GPU passes that Insights mixes into every export."""
    gpu = by_thread.get("gpu") or {}
    out = {"gpu": gpu}
    for key, stats in by_thread.items():
        if key == "gpu":
            continue
        out[key] = {}
        for name, (count, incl, excl) in stats.items():
            g = gpu.get(name, (0, 0.0, 0.0))
            left = (count - g[0], incl - g[1], excl - g[2])
            if left[0] > 0 and left[2] > 1e-9:
                out[key][name] = left
    return out


def trace_scopes(by_thread, frames, top=6, last=None):
    """Lines naming each thread's most expensive scopes by exclusive ms per frame, and the per-frame data."""
    frames = max(frames, 1)
    lines, data = [], {}
    for key, _ in TRACE_THREADS:
        stats = by_thread.get(key) or {}
        ranked = sorted(((excl * 1000 / frames, name) for name, (count, incl, excl) in stats.items()
                         if name != "Frame" and excl > 0 and not WAIT_SCOPE.search(name)), reverse=True)
        waiting = sum(excl for name, (_, _, excl) in stats.items() if WAIT_SCOPE.search(name)) * 1000 / frames
        data[key] = {name: round(ms, 3) for ms, name in ranked[:40]}
        if not ranked:
            continue
        before = (last or {}).get(key, {})
        parts = [f"{name} {ms:.2f}" + (f" ({ms - before[name]:+.2f})" if name in before and abs(ms - before[name]) >= 0.05
                                       else "") for ms, name in ranked[:top]]
        label = "gpu passes (timer totals, queues may overlap)" if key == "gpu" else key
        lines.append(f"  {label}: " + ", ".join(parts) + (f" | waiting {waiting:.1f}" if waiting >= 0.05 else ""))
    return lines, data


def perf_trace_report(ctx, trace_path, region, frames, args):
    """Export the trace's timer statistics with Unreal Insights (headless) and print the top scopes."""
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.isfile(trace_path):
        time.sleep(0.5)
    exe = os.path.join(engine_root(ctx.project), "Engine", "Binaries", host_platform(),
                       "UnrealInsights" + (".exe" if os.name == "nt" else ""))
    if not os.path.isfile(trace_path) or not os.path.isfile(exe):
        print("  trace: " + ("no trace was written (ue log --grep Trace)" if not os.path.isfile(trace_path)
                             else f"UnrealInsights not found at {exe}"))
        return
    out_dir = os.path.splitext(trace_path)[0]
    os.makedirs(out_dir, exist_ok=True)
    commands = os.path.join(out_dir, "export.txt")
    with open(commands, "w", encoding="utf-8") as f:
        for key, threads in TRACE_THREADS:
            f.write(f'TimingInsights.ExportTimerStatistics "{out_dir}/{key}.csv" -threads={threads} -region={region}\n')
    try:
        subprocess.run([exe, f"-OpenTraceFile={trace_path}", "-AutoQuit", "-NoUI", f"-ExecOnAnalysisCompleteCmd=@={commands}",
                        f"-ABSLOG={os.path.join(out_dir, 'insights.log')}", "-unattended"],
                       capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("  trace: Unreal Insights did not finish the export within 10 minutes")
        return
    by_thread = {key: read_timer_stats(os.path.join(out_dir, f"{key}.csv")) for key, _ in TRACE_THREADS}
    by_thread = cpu_only(by_thread)
    last_file = os.path.join(state_dir(ctx.project), "perf", "last-trace.json")
    last = read_json(last_file)
    lines, data = trace_scopes(by_thread, frames, args.top, last)
    if not lines:
        print(f"  trace: no CPU scopes in the export (see {os.path.join(out_dir, 'insights.log')})")
        return
    print(f"  top scopes, exclusive ms per frame over {frames} frames" + (" (+/- vs last trace)" if last else "") + ":")
    for line in lines:
        print(line)
    print(f"  trace: {trace_path} (open it in Unreal Insights for the full timeline)")
    if not args.no_record:
        write_json(last_file, data)
    old_traces = sorted(f for f in os.listdir(os.path.dirname(trace_path)) if f.startswith("perf-") and f.endswith(".utrace"))
    for name in old_traces[:-3]:  # ~10 MB a second: keep the last three
        with __import__("contextlib").suppress(OSError):
            os.remove(os.path.join(os.path.dirname(trace_path), name))
            __import__("shutil").rmtree(os.path.join(os.path.dirname(trace_path), name[:-7]), ignore_errors=True)


def pie_run(ctx, args):
    """A scripted gameplay check: PIE, a script in the game world, log expectations, then stop. Exit 0/1."""
    if not args.script:
        raise CliError("give a script: ue pie run checks.py --expect '<log regex>'")
    with open(args.script[0], encoding="utf-8-sig") as f:
        code = f.read()
    expects = [re.compile(e, re.I) for e in args.expect or []]
    rejects = [re.compile(r, re.I) for r in args.reject or []]
    start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running" if ctx.transport == "remote" else \
        unwrap(ctx.mcp.call(resolve_toolset(ctx, "editorapp"), "IsPIERunning", {})[0]) is True
    if not was_running:
        code_start = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup,
                                                     limit=0, script=None))
        if code_start:
            return code_start
    failures = []
    try:
        out = ctx.tether("py", code=code)
        if out:
            print("script: " + out.rstrip("\n").replace("\n", "\nscript: "))
    except CliError as exc:
        failures.append(f"script raised: {exc}")
    found, rejected = {}, []
    deadline = time.time() + args.seconds
    while True:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()
        for rx in expects:
            if rx.pattern not in found:
                hit = next((l for l in lines if rx.search(l)), None)
                if hit:
                    found[rx.pattern] = hit
        rejected = [(rx.pattern, l) for rx in rejects for l in lines if rx.search(l)]
        if rejected or len(found) == len(expects) or time.time() > deadline:
            break
        time.sleep(0.25)
    if not (args.keep or was_running):
        cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    for rx in expects:
        hit = found.get(rx.pattern)
        print(f"{'ok  ' if hit else 'MISS'} /{rx.pattern}/" + (f"  {hit.split(']', 2)[-1].strip()[:140]}" if hit else ""))
        if not hit:
            failures.append(f"no log line matched /{rx.pattern}/ within {args.seconds:g}s")
    for pattern, line in rejected[:5]:
        print(f"BAD  /{pattern}/  {line.split(']', 2)[-1].strip()[:140]}")
        failures.append(f"rejected pattern /{pattern}/ appeared")
    print(("PASS" if not failures else "FAIL") + f" in {time.time() - started:.1f}s"
          + ("" if not failures else ": " + "; ".join(dict.fromkeys(failures))))
    return 1 if failures else 0


WITHIN = re.compile(r"\s+within\s+([\d.]+)\s*s?\s*$", re.I)


def pie_do(ctx, args):
    """Drive the player through a list of steps in PIE and check the results. Exit 0/1."""
    steps, wanted_map = list(args.script or []), None
    if args.file:
        with open(args.file, encoding="utf-8-sig") as f:
            for line in f:
                header = re.match(r"\s*#\s*map:\s*(\S+)", line)
                if header:
                    wanted_map = header.group(1).split(".")[0]
                elif line.strip() and not line.strip().startswith("#"):
                    steps.append(line.strip())
    if not steps:
        raise CliError('give steps: ue pie do "at PlayerStart" "tap IA_Jump" "until player.location ~ ..." '
                       "(or -f steps.txt, one per line)")
    rejects = [re.compile(r, re.I) for r in args.reject or []]
    log_start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running"
    if wanted_map and not was_running and (ctx.beacon or {}).get("map") != wanted_map:  # the file's '# map:' header
        dirty = ctx.tether("py", code="len([p for p in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages() "
                                      "if not p.get_name().startswith('/Temp/')])")  # an untitled map is always dirty
        if str(dirty).strip() not in ("0", ""):
            raise CliError(f"{args.file} plays on {wanted_map}, but the open map has unsaved changes (ue changes)")
        ctx.tether("py", code=f"unreal.EditorLoadingAndSavingUtils.load_map({wanted_map!r})")
        print(f"loaded {wanted_map}")
    if not was_running:
        code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup, limit=0, script=None))
        if code:
            return code

    def tool(step):
        result = ctx.tether("pie_step", step=step)
        result = json.loads(result) if isinstance(result, str) else result
        return bool(result["ok"]), result.get("detail", "")

    def log_lines():
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(log_start)
            return f.read().decode("utf-8", "replace").splitlines()

    def poll(check, seconds):
        deadline = time.time() + seconds
        while True:
            ok, detail = check()
            if ok or time.time() > deadline:
                return ok, detail + ("" if ok else f" after {seconds:g}s")
            time.sleep(0.25)

    def run(step):
        verb, _, rest = step.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        within = WITHIN.search(rest)
        seconds = float(within.group(1)) if within else None
        rest = rest[: within.start()].strip() if within else rest
        if verb == "wait":
            time.sleep(float(rest or 1))
            return True, ""
        if verb == "until":
            return poll(lambda: tool("expect " + rest), seconds or 5)
        if verb == "log":
            rx = re.compile(rest, re.I)
            return poll(lambda: (any(rx.search(l) for l in log_lines()), "no matching log line"), seconds or 5)
        if verb == "move":
            tool("move " + rest)
            return poll(lambda: tool(f"near {rest} 120"), seconds or 15)
        if verb == "press":
            name, _, hold_for = rest.partition(" ")
            if not hold_for:
                return tool(f"tap {name}")
            tool(f"hold {name}")
            held.add(name)
            time.sleep(float(hold_for))
            held.discard(name)
            return tool(f"release {name}")
        if verb == "hold":
            held.add(rest.split()[0])
        elif verb == "release":
            held.discard(rest.split()[0])
        if verb == "expect-sound":
            rx = re.compile(rest, re.I)
            def heard():
                ok = any(rx.search(s["sound"]) for s in active_sounds(ctx))
                return ok, "" if ok else "no such sound playing"
            return poll(heard, seconds or 3)
        if verb == "click":
            return True, ui_click(ctx, rest)
        if verb in ("expect-ui", "ui-gone"):
            want = verb == "expect-ui"
            def check():
                ok = ui_has_text(ctx, rest) == want
                return ok, "" if ok else ("still on screen" if not want else "not on screen")
            return poll(check, seconds or 3)
        if verb in ("type", "key"):
            import shlex
            parts = shlex.split(rest)
            if verb == "key":
                mcp_tool(ctx, "SlateInspectorToolset", "PressKey", {"key": rest})
                return True, f"pressed {rest}"
            nodes, slate = ui_nodes(ctx)
            if not slate or len(parts) < 2:
                raise CliError('type needs SlateInspectorToolset and: type "<textbox label>" <text>')
            node = find_ui(nodes, parts[0], {"textbox", "spinbox"})
            mcp_tool(ctx, "SlateInspectorToolset", "Type", {"ref": node["ref"], "text": " ".join(parts[1:]), "submit": False})
            return True, f'typed into "{node["label"]}"'
        if verb == "shot":
            shot_args = argparse.Namespace(actor=None, at=None, look_at=None, rot=None, annotate=False, grid_z=0,
                                           ui=False, output=rest or None)
            buf = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(buf):
                cmd_shot(ctx, shot_args)
            return True, buf.getvalue().strip()
        return tool(step)

    failures, held = [], set()
    try:
        for number, step in enumerate(steps, 1):
            try:
                ok, detail = run(step.strip())
            except CliError as exc:
                ok, detail = False, str(exc)
            if not ok or not getattr(args, "quiet", False):
                print(f"{number:>2} {'ok  ' if ok else 'FAIL'} {step}" + (f"  ({detail})" if detail else ""))
            bad = [(rx.pattern, l) for rx in rejects for l in log_lines() if rx.search(l)] if rejects else []
            if bad:
                print(f"   BAD  /{bad[0][0]}/  {bad[0][1].split(']', 2)[-1].strip()[:140]}")
                failures.append(f"rejected /{bad[0][0]}/ appeared")
                break
            if not ok:
                failures.append(f"step {number} failed")
                if not args.keep_going:
                    break
    finally:
        for name in held:
            try:
                ctx.tether("pie_step", step=f"release {name}")
            except CliError:
                pass
        if not (args.keep or was_running):
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    if failures or not getattr(args, "quiet", False):
        print(("PASS" if not failures else "FAIL") + f" in {time.time() - started:.1f}s"
              + ("" if not failures else ": " + "; ".join(failures)))
    return 1 if failures else 0


def cmd_pie(ctx, args):
    """Play-In-Editor through Epic's EditorAppToolset, reporting only what went wrong."""
    if args.action == "run":
        return pie_run(ctx, args)
    if args.action == "do":
        return pie_do(ctx, args)
    if args.action == "target":
        index = int(args.script[0]) if args.script else -1
        print(pie_net(ctx, "target", target=index))
        return 0
    if args.action == "start" and getattr(args, "clients", None):
        print(pie_net(ctx, "configure", clients=args.clients, net=args.net or "listen"))
    toolset = None if ctx.transport == "remote" else resolve_toolset(ctx, "editorapp")
    start = log_end(ctx.project)
    started = time.time()
    if args.action == "start" and csharp_layout(ctx.project):
        # UnrealSharp recompiles a saved but unreloaded C# type in place on its first lookup, from the old
        # reflection data. That drops the type's subsystems from every live world, PIE included, for the
        # rest of the session. So PIE only ever starts on reloaded code.
        if csharp_changes(ctx.project) or ctx.tether("cs_state") == "pending":
            if csharp_reload_build(ctx, argparse.Namespace(wait=120, limit=args.limit)):
                raise CliError("PIE not started: the C# reload failed (errors above)")
            start, started = log_end(ctx.project), time.time()
    if ctx.transport == "remote":  # no EditorAppToolset before 5.8: LevelEditorSubsystem requests instead
        if args.action == "status":
            print("pie: " + ctx.tether("pie", action="status"))
            return 0
        ctx.tether("pie", action=args.action, simulate=args.simulate)
        want, deadline = ("running" if args.action == "start" else "not running"), time.time() + 60
        while ctx.tether("pie", action="status") != want:  # the request takes effect on a later tick
            if time.time() > deadline:
                raise CliError(f"PIE still not {want} after 60 s (ue log --level Warning)", 4)
            time.sleep(0.25)
        time.sleep(args.warmup if args.action == "start" else 0)
        text, is_error = "", False
    elif args.action == "start":
        options = {"bSimulate": args.simulate, "playMode": "PlayMode_InViewPort", "warmupSeconds": args.warmup}
        text, is_error = ctx.mcp.call(toolset, "StartPIE", {"options": options})
        if is_error and unwrap(ctx.mcp.call(toolset, "IsPIERunning", {})[0]) is True:
            # Epic fails the call when anything logged a script error during startup (e.g. a Blueprint
            # drawing to a render target under -nullrhi), although PIE is running. Report, don't fail.
            print(f"note: PIE started, but startup logged: {compact_error(text)[:160]}")
            is_error = False
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
            net = pie_net(ctx, "status")
            if net.count(": ") > 1:  # several PIE worlds: multiplayer
                print(net)
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
    if args.action == "stop":  # put back play settings a `--clients` start changed, and the world target
        with __import__("contextlib").suppress(CliError):
            note = pie_net(ctx, "restore")
            if note != "nothing to restore":
                print(note)
    elif args.action == "start" and getattr(args, "clients", None):
        print(pie_net(ctx, "status"))
    return 0


def pie_net(ctx, action, clients=1, net="", target=-1):
    # Epic's MCP layer treats a string parameter with an empty default as required, so pass them all.
    return ctx.tether("pie_net", action=action, clients=clients, net=net or "-", target=target)


def editor_live(project):
    """The project's beacon if its editor is running and ticking, else None."""
    beacon = read_json(os.path.join(state_dir(project), "editor.json")) or {}
    alive = pid_alive(beacon.get("pid")) and time.time() - beacon.get("heartbeat", 0) < 15
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


def hook_pre_edit(payload):
    """Deny edits to C# under an UnrealSharp project's Script/ while that project's editor is in PIE:
    UnrealSharp reprocesses saved C# at once, which can crash the running session."""
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.lower().endswith(".cs"):
        return None
    path = os.path.abspath(path)
    project = project_of(path)
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


def project_in(cwd):
    """The project a session in `cwd` works on (the .uproject at or above it, or one level below), or None."""
    try:
        here = os.getcwd()
        os.chdir(cwd or here)
        try:
            return find_project(None)
        finally:
            os.chdir(here)
    except (CliError, OSError):
        return None


def hook_session_start(payload):
    """Editor state plus a brief project overview at the start of a session, so the agent need not explore."""
    project = project_in(payload.get("cwd"))
    if not project:
        return None
    name = os.path.splitext(os.path.basename(project))[0]
    try:
        brief = "\n".join(project_overview(project, full=False)[1:])
    except Exception:  # a hook must never fail
        brief = ""
    beacon = read_json(os.path.join(state_dir(project), "editor.json")) or {}
    if not pid_alive(beacon.get("pid")):
        head = (f"Unreal project {name}: editor not running. Use the `ue` CLI (unreal-tether skill); "
                "start it with `ue open --headless` (`--render` if you need `ue shot`).")
    else:
        report = stall_report(project)
        state = report or ("PIE running" if beacon.get("pie") else "ready")
        head = (f"Unreal project {name}: editor {state} | map {beacon.get('map')} | UE "
                f"{beacon.get('engine', '?').split('+')[0]} | mcp :{beacon.get('mcp_port')}\n"
                "Use the `ue` CLI (unreal-tether skill); `ue status` refreshes this.")
    return head + (f"\nProject (ue overview for more):\n{brief}" if brief else "")


RESTART_MACROS = re.compile(r"\b(UPROPERTY|UCLASS|USTRUCT|UENUM|UINTERFACE|GENERATED_BODY|GENERATED_UCLASS_BODY)\b")
FUNCTION_MACRO = re.compile(r"\bUFUNCTION\b")


def classify_edit(path, tool_input):
    """How an edit to project source reaches the running editor: (route, reason)."""
    lower = path.lower()
    if lower.endswith((".build.cs", ".target.cs", ".uproject", ".uplugin")):
        return "restart", "module or project files changed"
    if lower.endswith(".cs"):
        return "csharp", "C# hot reload"
    before = tool_input.get("old_string", "")
    after = tool_input.get("new_string", tool_input.get("content", ""))
    if "content" in tool_input and "new_string" not in tool_input:  # Write: no old text to compare
        if RESTART_MACROS.search(after) and lower.endswith((".h", ".hpp")):
            return "maybe-restart", "rewrote a reflected header"
        return "live", "function bodies"
    if sorted(RESTART_MACROS.findall(before)) != sorted(RESTART_MACROS.findall(after)) or (
            RESTART_MACROS.search(before + after) and lower.endswith((".h", ".hpp"))):
        return "restart", "reflected declarations changed"
    if len(FUNCTION_MACRO.findall(after)) > len(FUNCTION_MACRO.findall(before)):
        return "live-ufunction", "new UFUNCTION"
    if lower.endswith((".h", ".hpp")):  # inline bodies patch live; new members change the class layout
        return "maybe-restart", "header edit"
    return "live", "function bodies"


EDIT_ADVICE = {
    "restart": "needs `ue build --restart` (Live Coding cannot patch class layout or reflection); batch such changes, "
               "then `ue bp check` to find Blueprints that used what changed",
    "maybe-restart": "`ue build` (Live Coding) if only inline function bodies changed; `ue build --restart` if "
                     "members, UPROPERTYs, class layout or defaults changed",
    "live-ufunction": "`ue build` (Live Coding) makes it callable via reflection (cdo.call_method); Python and "
                      "Blueprint wrappers stay stale until `ue build --restart`",
    "live": "`ue build` (Live Coding, ~5 s) when you are done editing; no restart needed",
    "csharp": "`ue build` hot-reloads it in ~1 s when you are done editing",
}


def hook_post_edit(payload):
    """After an edit to project C++/C#/build files, one line on how to get it into the running editor."""
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if not path.lower().endswith((".h", ".hpp", ".cpp", ".inl", ".cs", ".uproject", ".uplugin")):
        return None
    project = project_of(path)
    if not project:
        return None
    root = os.path.normcase(os.path.dirname(project)) + os.sep
    full = os.path.normcase(os.path.abspath(path))
    rel = full[len(root):].lower() if full.startswith(root) else ""  # normcase lowercases only on Windows
    if not (rel.startswith(("source" + os.sep, "plugins" + os.sep, "script" + os.sep)) or rel.endswith(".uproject")):
        return None
    if rel.endswith(".cs") and not rel.endswith((".build.cs", ".target.cs")) and not csharp_layout(project):
        return None
    route, reason = classify_edit(path, tool_input)
    live = editor_live(project)
    advice = EDIT_ADVICE[route] if live else "editor not running: `ue build` compiles with UBT"
    return f"Tether: {os.path.basename(path)} ({reason}): {advice}."


def hook_stop(payload):
    """At the end of a turn, nudge once to build if project code changed since the last green build."""
    if payload.get("stop_hook_active"):
        return None  # already nudged this stop; never loop
    project = project_in(payload.get("cwd"))
    if not project or not editor_live(project):
        return None
    recent = time.time() - 3600  # ignore edits older than an hour: likely not this session's
    cpp = classify_changes(project)
    changed = [p for p in (cpp[0] if cpp else []) if p.endswith((".h", ".hpp", ".cpp", ".inl", ".cs"))
               and os.path.getmtime(p) > recent]
    changed += [p for p in (csharp_changes(project) or []) if os.path.getmtime(p) > recent]
    advice = []
    if changed:
        names = ", ".join(os.path.basename(p) for p in changed[:3]) + (f" +{len(changed) - 3} more" if len(changed) > 3 else "")
        restart = cpp and (cpp[1] or cpp[2])
        advice.append(f"Project code changed since the last green build ({names}) and the editor is running. Run "
                      f"`ue build{' --restart' if restart else ''}` to compile and check it")
    # Assets saved since the last `ue validate` (or the last nudge about them): suggest validating once.
    marker_path = os.path.join(state_dir(project), "validated.json")
    marker = read_json(marker_path) or {}
    saved = assets_saved_since(project, max(recent, marker.get("time", 0), marker.get("nudged", 0)))
    if saved:
        names = ", ".join(os.path.basename(p) for p in saved[:3]) + (f" +{len(saved) - 3} more" if len(saved) > 3 else "")
        advice.append(f"{len(saved)} asset(s) were saved since the last validation ({names}). Run `ue validate` "
                      "(data validation + Map Check on changed and unsaved assets)")
        marker["nudged"] = time.time()
        write_json(marker_path, marker)
    if not advice:
        return None
    return "; ".join(advice) + ", or tell the user why it should wait."


def assets_saved_since(project, since):
    """.uasset/.umap files under the project's Content modified after `since` (a scandir walk; ~0.2 s per 3k)."""
    found, stack = [], [os.path.join(os.path.dirname(project), "Content")]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif entry.name.endswith((".uasset", ".umap")) and entry.stat().st_mtime > since:
                found.append(entry.path)
    return sorted(found)


def cmd_hook(ctx, args):
    """Claude Code hook entry points (hook JSON on stdin). A failing hook stays silent: it must never block."""
    try:
        payload = json.load(sys.stdin)
        if args.event in ("pre-edit", "pre-mcp"):
            reason = {"pre-edit": hook_pre_edit, "pre-mcp": hook_pre_mcp}[args.event](payload)
            if reason:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
        elif args.event == "session-start":
            context = hook_session_start(payload)
            if context:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
        elif args.event == "post-edit":
            context = hook_post_edit(payload)
            if context:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}))
        elif args.event == "stop":
            reason = hook_stop(payload)
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        pass
    return 0


REQUIRED_PLUGINS = ["PythonScriptPlugin", "ModelContextProtocol", "EditorToolset", "AutomationTestToolset", "SlateInspectorToolset",
                    "Tether"]


def tether_plugins_dir():
    return os.path.join(os.path.dirname(CLI_DIR), "Plugins")


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
    if any(e.get("Name") == "AllToolsets" and e.get("Enabled") for e in entries):
        wanted = [n for n in wanted if n not in ("EditorToolset", "AutomationTestToolset")]  # AllToolsets has them
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
    if not changes and not copies:
        print("already set up: plugins enabled, Tether reachable")
        return 0
    for line in changes:
        print(("would " if args.dry_run else "") + line)
    for name, source, target, exists in copies:
        print(("would " if args.dry_run else "") + ("update" if exists else "copy") + f" {name} -> "
              + os.path.relpath(target, os.path.dirname(project)))
    if args.dry_run:
        return 0
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
    print(ctx.tether("reload"))
    cache = os.path.join(state_dir(ctx.project), "cache")
    for name in os.listdir(cache) if os.path.isdir(cache) else []:
        os.remove(os.path.join(cache, name))  # tool schemas may have changed
    return 0


UNREAL_ROOTS = ("Game", "Script", "Engine", "Temp", "Memory")


def undo_msys_paths(argv):
    """Git Bash (MSYS) rewrites arguments that look like POSIX paths when it starts a Windows program, so
    `/Game/Maps/L` arrives as `C:/Program Files/Git/Game/Maps/L` (also after `key=`). Undo that for
    Unreal package roots, bare (`/Game`) or with a path."""
    if os.name != "nt" or not os.environ.get("MSYSTEM"):
        return argv
    import shutil
    bash = shutil.which("bash")
    if not bash:
        return argv
    root = os.path.dirname(os.path.dirname(os.path.dirname(bash))).replace("\\", "/")  # ...\Git\usr\bin\bash
    rx = re.compile("(^|=)" + re.escape(root) + "(?=/(?:" + "|".join(UNREAL_ROOTS) + r")(?:[/,]|$))", re.I)
    return [rx.sub(r"\1", a) for a in argv]


def build_parser():
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
    add("doctor", cmd_doctor, "check the setup (engine, plugins, editor, ports, Live Coding lock, .NET, git/LFS, disk)")
    s = add("open", cmd_open, "launch the editor with the MCP server on a per-project port and wait until ready")
    s.add_argument("--map", help="map to open, e.g. /Game/Maps/Lvl_Test")
    s.add_argument("--headless", action="store_true", help="no window: -nullrhi -nosound (+ -unattended, see below)")
    s.add_argument("--render", action="store_true", help="with --headless: render offscreen instead of -nullrhi (for ue shot)")
    s.add_argument("--sound", action="store_true", help="with --headless: keep a (silent) audio device, for ue audio")
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
    s.add_argument("--pie", action="store_true", help="read the PIE world's actor instead")
    s.add_argument("--export", metavar="FILE", help="write every property value to FILE as JSON (e.g. a DataAsset)")
    s = add("set", cmd_set, "set properties (name=value, name+=delta) as one undoable transaction")
    s.add_argument("target")
    s.add_argument("assignments", nargs="*", metavar="name=value")
    s.add_argument("--pie", action="store_true", help="change the PIE world (not undoable, not saved)")
    s.add_argument("--from", dest="from_file", metavar="FILE", help="apply the values in FILE (JSON) that differ")
    s.add_argument("--dry-run", action="store_true", help="with --from: show what would change")
    add("undo", cmd_undo, "undo Tether's last `ue set` (only if nothing else changed since)")
    s = add("spawn", cmd_spawn, "place an actor from a class or asset (undoable)")
    s.add_argument("what", help="class (PointLight, /Script/Module.Class) or asset (/Game/Meshes/SM_Rock)")
    s.add_argument("--at", default="0,0,0", help="location x,y,z")
    s.add_argument("--rot", help="rotation pitch,yaw,roll")
    s.add_argument("--label", help="actor label")
    s.add_argument("--ground", action="store_true", help="drop it onto the surface below --at")
    s.add_argument("--align", action="store_true", help="with --ground: tilt it to the surface")
    s = add("scatter", cmd_scatter, "place many copies on the ground in an area or along a line (one undo step)")
    s.add_argument("what", help="asset (/Game/...) or class")
    s.add_argument("--count", type=int, default=10)
    s.add_argument("--area", help="x,y,radius")
    s.add_argument("--line", nargs=2, metavar=("X,Y,Z", "X,Y,Z"), help="place evenly along this segment")
    s.add_argument("--yaw", default="0,360", help="random yaw range min,max (0,360)")
    s.add_argument("--scale", default="1,1", help="random uniform scale range min,max (1,1)")
    s.add_argument("--spacing", type=float, default=0, help="minimum distance between copies, cm")
    s.add_argument("--seed", type=int, default=0, help="same seed, same layout")
    s.add_argument("--align", action="store_true", help="tilt each to the surface")
    s.add_argument("--label", help="label prefix (default: the asset name)")
    s = add("drop", cmd_drop, "put actors on the ground below them (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
    s.add_argument("--align", action="store_true", help="also tilt each to the surface normal")
    s = add("delete", cmd_delete, "delete actors from the level (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
    s = add("duplicate", cmd_duplicate, "duplicate an actor (undoable)")
    s.add_argument("actor")
    s.add_argument("--offset", default="100,0,0", help="shift per copy x,y,z (100,0,0)")
    s.add_argument("--count", type=int, default=1)
    s = add("mv", cmd_mv, "move/rename an asset and fix up references (not undoable)")
    s.add_argument("asset")
    s.add_argument("dest", help="new path, or a folder ending in /")
    s = add("rm", cmd_rm, "delete an asset; refuses while referenced (not undoable)")
    s.add_argument("asset")
    s.add_argument("--force", action="store_true", help="delete even if referenced")
    s = add("crash", cmd_crash, "why the editor died: the last crash's error, module chain and last log lines")
    s.add_argument("index", nargs="?", type=int, default=0, help="0 = newest (default), 1 = the one before, ...")
    s.add_argument("--list", action="store_true", help="list recent crashes instead")
    add("changes", cmd_changes, "unsaved work: dirty packages and the level's actors added/removed/moved since load")
    s = add("save", cmd_save, "save unsaved packages (all, or the given ones); untitled /Temp maps are skipped")
    s.add_argument("paths", nargs="*", help="package paths (default: everything unsaved)")
    s = add("watch", cmd_watch, "wait for a log line, or for a property value (exit 4 on timeout)")
    s.add_argument("pattern", nargs="?", help="log regex to wait for")
    s.add_argument("--prop", nargs=2, metavar=("TARGET", "NAME"), help="watch this property instead")
    s.add_argument("--until", help="with --prop: regex the value must match")
    s.add_argument("--seconds", type=float, default=60, help="give up after this long (60)")
    s.add_argument("--pie", action="store_true", help="with --prop: read the PIE world")
    s = add("shot", cmd_shot, "capture the viewport (or the PIE view) to a PNG; prints the path")
    s.add_argument("--actor", help="frame this actor, keeping the current view direction")
    s.add_argument("--at", help="camera location x,y,z")
    s.add_argument("--look-at", help="aim at x,y,z or an actor")
    s.add_argument("--rot", help="camera rotation pitch,yaw,roll")
    s.add_argument("--annotate", action="store_true", help="overlay Epic's world grid and actor labels")
    s.add_argument("--grid-z", type=float, default=0, help="with --annotate: grid height (0)")
    s.add_argument("--ui", action="store_true", help="include gizmos and selection outlines")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/shot-<time>.png)")
    s.add_argument("--baseline", metavar="NAME", help="also save this shot and its camera as a named baseline")
    s.add_argument("--compare", metavar="NAME", help="re-shoot a baseline's camera and report what changed (exit 1)")
    s.add_argument("--tolerance", type=float, default=0.5, help="with --compare: changed pixels allowed, %% (0.5)")
    s.add_argument("--threshold", type=int, default=32, help="with --compare: per-channel difference that counts (32)")
    s = add("look", cmd_look, "render an asset (mesh, Blueprint, material) from six angles into one contact sheet")
    s.add_argument("asset", help="/Game/... asset path")
    s.add_argument("--tile", type=int, default=400, help="width of each view in pixels (400)")
    s.add_argument("--distance", type=float, default=2.6, help="camera distance in bounds radii (2.6)")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/look-<asset>-<time>.png)")
    s = add("test", cmd_test, "run automation tests in the live editor; prints failures only (exit 1 on failure)")
    s.add_argument("filter", nargs="?", help="Epic filter: substring, ^Prefix, Suffix$, StartsWith:X, Group:Name; '+' joins")
    s.add_argument("--list", action="store_true", help="list matching tests instead of running them")
    s.add_argument("-v", "--verbose", action="store_true", help="also print warnings")
    s = add("actors", cmd_actors, "actors in the editor level")
    s.add_argument("match", nargs="?")
    s.add_argument("--pie", action="store_true", help="list the PIE world's actors instead")
    s = add("exec", cmd_exec, "console command; prints the log lines it produced")
    s.add_argument("command", nargs="+")
    s.add_argument("--pie", action="store_true", help="run in the PIE world")
    s = add("log", cmd_log, "new log entries since the last call (cursor kept on disk)")
    s.add_argument("--since", choices=["last", "start"], default="last")
    s.add_argument("--level", default="Warning", help="Error, Warning, Display, Log, Verbose")
    s.add_argument("--cat", help="only this log category")
    s.add_argument("--grep", help="regex filter")
    s = add("pie", cmd_pie, "Play-In-Editor: start | stop | status | run <script> | do <steps...>")
    s.add_argument("action", choices=["start", "stop", "status", "run", "do", "target"])
    s.add_argument("script", nargs="*", help="with run: Python file to execute in the PIE world; with do: steps")
    s.add_argument("-f", "--file", help="with do: a file of steps, one per line (# comments)")
    s.add_argument("--keep-going", action="store_true", help="with do: run every step even after a failure")
    s.add_argument("--clients", type=int, help="with start: multiplayer PIE with this many clients")
    s.add_argument("--net", choices=["listen", "client", "standalone"],
                   help="with --clients: listen server (default), client (dedicated server), or standalone")
    s.add_argument("--expect", action="append", help="with run: log regex that must appear (repeatable)")
    s.add_argument("--reject", action="append", help="with run/do: log regex that must not appear (repeatable)")
    s.add_argument("--seconds", type=float, default=5, help="with run: how long to wait for expectations (5)")
    s.add_argument("--keep", action="store_true", help="with run/do: leave PIE running afterwards")
    s.add_argument("--simulate", action="store_true", help="Simulate-In-Editor (no player pawn)")
    s.add_argument("--warmup", type=float, default=0.5, help="seconds to let BeginPlay settle (0.5)")
    s = add("bp", cmd_bp, "Blueprint graphs as text: read, --diff against the last read, --apply edited text")
    s.add_argument("blueprint", nargs="?", help="/Game/Path/BP_Name, or `check [/Game/path]` to compile all and list the broken")
    s.add_argument("graph", nargs="?", help="graph name (default: all); with edit: the Blueprint")
    s.add_argument("rest", nargs="*", help="with edit: add-var Name Type [default] | set-default Name=v ... | "
                                           "add-component Name Class [prop=v ...] | add-function Name | add-dispatcher Name | remove-var Name")
    s.add_argument("--category", help="with edit add-var: the variable's category")
    s.add_argument("--instance-editable", action="store_true", help="with edit add-var: editable on placed instances")
    s.add_argument("--array", action="store_true", help="with edit add-var: an array of the type")
    s.add_argument("--parent", help="with edit add-component: attach under this component (default: the root)")
    s.add_argument("--diff", action="store_true", help="show only what changed since the last ue bp read")
    s.add_argument("--apply", metavar="FILE", help="write this DSL into the graph (compiles), then show the diff")
    s.add_argument("--save", action="store_true", help="save the Blueprint afterwards")
    s.add_argument("--docs", action="store_true", help="print Epic's graph DSL syntax reference")
    s.add_argument("--warnings", action="store_true", help="with check: also list Blueprints with warnings")
    s = add("table", cmd_table, "DataTable round-trips: show, --export JSON/CSV, --import with a row diff")
    s.add_argument("table", help="/Game/Path/DT_Name")
    s.add_argument("--export", metavar="FILE", help="write all rows to FILE (.json or .csv)")
    s.add_argument("--import", dest="import_file", metavar="FILE", help="upsert rows from FILE (.json or .csv)")
    s.add_argument("--prune", action="store_true", help="with --import: remove rows missing from FILE")
    s.add_argument("--dry-run", action="store_true", help="with --import: show the diff only")
    s.add_argument("--save", action="store_true", help="save the DataTable afterwards")
    s = add("perf", cmd_perf, "frame-time capture in PIE: frame/game/render/GPU ms, hitches, change vs last capture")
    s.add_argument("--seconds", type=float, default=10, help="capture length (10)")
    s.add_argument("--editor", action="store_true", help="measure the editor as is, without starting PIE")
    s.add_argument("--no-record", action="store_true", help="do not make this capture the comparison baseline")
    s.add_argument("--trace", action="store_true", help="also an Unreal Insights trace: top CPU scopes per thread")
    s.add_argument("--top", type=int, default=6, help="with --trace: scopes per thread (6)")
    s = add("asset", cmd_asset, "a few lines about assets, by type (mesh, texture, material, Blueprint, ...)")
    s.add_argument("paths", nargs="+", help="/Game/... asset paths")
    s = add("diff", cmd_diff, "what changed in an asset between git revisions, or a revision and the editor now")
    s.add_argument("asset", nargs="?", help="/Game/... asset, or its file under Content/")
    s.add_argument("rev", nargs="?", help="git revision to compare with (default HEAD)")
    s.add_argument("--to", metavar="REV", help="compare with this revision instead of the editor's current state")
    s.add_argument("--files", nargs=2, metavar=("OLD", "NEW"), help="compare two files (for git difftool)")
    s.add_argument("--as", dest="as_path", metavar="PATH", help="with --files: the asset's path in the repo ($MERGED)")
    s = add("validate", cmd_validate, "data validation + Map Check; default: assets changed in git or unsaved")
    s.add_argument("paths", nargs="*", help="assets or content folders (default: changed and unsaved assets)")
    s.add_argument("--changed", nargs="?", const="", metavar="REV",
                   help="also the assets changed in git (uncommitted, plus committed since REV if given)")
    s.add_argument("--map-check", action="store_true", help="also Map Check the open level (default without paths)")
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s = add("mat", cmd_mat, "material parameters with values and sources; Name=value sets them (undoable)")
    s.add_argument("target", help="material or instance path, or an actor (lists its materials)")
    s.add_argument("assignments", nargs="*", metavar="Name=value", help="scalar, r,g,b[,a], true/false, texture path")
    s.add_argument("--reset", action="append", metavar="NAME", help="stop overriding NAME (instances; repeatable)")
    s.add_argument("--compile", action="store_true", help="recompile the base material and list compile errors")
    s = add("nav", cmd_nav, "navmesh: status (and why it is missing), path <a> <b>, check [actor regex]")
    s.add_argument("action", nargs="?", choices=["status", "path", "check"])
    s.add_argument("points", nargs="*", help="path: start end (x,y,z or actor); check: actor regex")
    s.add_argument("--pie", action="store_true", help="the PIE world's (runtime-generated) navmesh")
    s = add("size", cmd_size, "biggest assets on disk under a folder, or what an asset loads with it (--deps)")
    s.add_argument("path", nargs="?", help="content folder (default /Game) or an asset")
    s.add_argument("--deps", action="store_true", help="with an asset: total it with everything it loads")
    s.add_argument("--top", type=int, default=10, help="rows per list (10)")
    s = add("mem", cmd_mem, "memory of the running editor/PIE from a memreport: process, classes, meshes, textures")
    s.add_argument("--full", action="store_true", help="memreport -full: adds the texture list (slower)")
    s.add_argument("--top", type=int, default=8, help="rows per list (8)")
    s = add("config", cmd_config, "project/editor settings: list sections, read one, or Name=value to set (saved)")
    s.add_argument("section", nargs="?", help="Maps, Rendering, Engine/General, or Container/Category/Section")
    s.add_argument("rest", nargs="*", metavar="regex|Name=value", help="a regex to filter settings, or values to set")
    s = add("ui", cmd_ui, "the game's on-screen UI in PIE (Slate + UMG); click | type | key to interact")
    s.add_argument("action", nargs="?", help="click <label|ref>, type <textbox> <text>, key <Key>, or a regex filter")
    s.add_argument("rest", nargs="*")
    s.add_argument("--all", action="store_true", help="also images and zero-size widgets")
    s.add_argument("--submit", action="store_true", help="with type: press Enter afterwards")
    s = add("anim", cmd_anim, "animation state in PIE: anim BP, montage, curves, its variables, movement")
    s.add_argument("target", help="actor, 'player', or Actor.Component")
    s.add_argument("--editor", action="store_true", help="read the editor level instead of PIE")
    s = add("ai", cmd_ai, "AI in PIE: movement/path, focus, perception, brain, blackboard, project getters")
    s.add_argument("target", nargs="?", help="a pawn or controller (default: one line per AI)")
    s = add("new", cmd_new, "create an asset: blueprint, material, mi, datatable, dataasset, level, folder")
    s.add_argument("kind", choices=["blueprint", "bp", "material", "mi", "datatable", "dataasset", "level", "folder"])
    s.add_argument("path", help="/Game/Folder/Name")
    s.add_argument("--parent", help="blueprint: parent class; mi: parent material; datatable: row struct; dataasset: class")
    s.add_argument("--save", action="store_true", help="save it right away")
    s = add("usages", cmd_usages, "where a C++ function/property/class or an asset is used: code, Blueprints, assets")
    s.add_argument("symbol", help="e.g. TryAttack, AEnemyCharacter, Health, or /Game/Path/Asset")
    s.add_argument("--max-mb", type=float, default=32, help="skip asset files bigger than this (32)")
    s.add_argument("--budget", type=float, default=20, help="seconds to spend reading Blueprint graphs (20)")
    s = add("overview", cmd_overview, "one-screen project brief from its files: modules, key classes, maps, input, content")
    s.add_argument("--brief", action="store_true", help="the short version the session-start hook uses")
    s = add("world", cmd_world, "world builds: build nav|lighting (live editor), hlod|minimap|nav-wp|<Builder> (job)")
    s.add_argument("action", choices=["build"])
    s.add_argument("what", help="nav, lighting, hlod, minimap, nav-wp, resave, or a WorldPartition builder class")
    s.add_argument("--map", help="with builders: the map (default: the editor's)")
    s.add_argument("--quality", default="Preview", help="with lighting: Preview, Medium, High, Production")
    s.add_argument("--save", action="store_true", help="with nav: save the packages the build changed")
    s.add_argument("--force", action="store_true", help="run a builder even though the map is open in the editor")
    s.add_argument("--no-wait", action="store_true", help="with builders: start the job and return")
    s.add_argument("--wait", type=float, default=1800, help="seconds to wait for nav or lighting (1800)")
    s = add("audio", cmd_audio, "sounds playing in PIE: sound, owner, distance, volume, attenuation")
    s.add_argument("match", nargs="?", help="regex filter")
    s = add("soak", cmd_soak, "long PIE run (a step file in a loop, or wandering): memory trend, fps, errors, crashes")
    s.add_argument("--minutes", type=float, default=5, help="how long (5)")
    s.add_argument("-f", "--file", help="a `ue pie do` step file to loop (default: wander the player)")
    s.add_argument("--sample", type=float, default=10, help="seconds between memory/fps samples (10)")
    s.add_argument("--max-growth", type=float, default=20, help="MB/min of memory growth that fails the run (20)")
    s.add_argument("--keep-going", action="store_true", help="keep looping after a failing iteration")
    s = add("find", cmd_find, "search assets by name and/or class (asset registry)")
    s.add_argument("query", nargs="?", help="regex on the asset name")
    s.add_argument("--class", dest="cls", help="regex on the class name, e.g. StaticMesh or 'Material$'")
    s.add_argument("--under", default="/Game", help="content folder to search (default /Game)")
    s = add("refs", cmd_refs, "what references an asset (--deps: what it depends on)")
    s.add_argument("asset", help="/Game/Path/Asset")
    s.add_argument("--deps", action="store_true", help="list dependencies instead of referencers")
    s = add("api", cmd_api, "the unreal Python API: a class's methods and properties, or one member's doc")
    s.add_argument("name", help="class or class.member, e.g. EditorActorSubsystem or Actor.set_actor_location")
    s.add_argument("match", nargs="?", help="regex filter on member names")
    s = add("job", cmd_job, "background jobs for long work: start -- <cmd>, cook, status, wait, log")
    s.add_argument("action", choices=["start", "cook", "package", "status", "wait", "log", "stop", "_run"])
    s.add_argument("id", nargs="?", help="job id (default: the latest)")
    s.add_argument("--name", help="label for a started job")
    s.add_argument("--platform", help="with cook: target platform (default: this host)")
    s.add_argument("--wait", type=float, default=7200, help="with wait: seconds before giving up (7200)")
    s.add_argument("-v", "--verbose", action="store_true", help="with wait: also print warnings")
    s = add("package", cmd_package, "package the game (background job), then boot it headless and report crashes")
    s.add_argument("--platform", help="target platform (default: this host)")
    s.add_argument("--config", default="Development", help="Development (default), Shipping, DebugGame")
    s.add_argument("--seconds", type=float, default=20, help="how long the boot test runs (20)")
    s.add_argument("--wait", type=float, default=7200, help="seconds to allow the packaging job (7200)")
    s.add_argument("--no-wait", action="store_true", help="start the packaging job and return")
    s.add_argument("--no-boot", action="store_true", help="package only")
    s.add_argument("--boot-only", action="store_true", help="boot-test the last package without rebuilding")
    s.add_argument("--nullrhi", action="store_true", help="boot without rendering (-nullrhi)")
    s.add_argument("--strict", action="store_true", help="fail on logged errors too")
    s = add("ci", cmd_ci, "project checks in one pass/fail: open if needed, tests, Blueprint compile, validation, cook, close")
    s.add_argument("--tests", help="test filter (default ^<Project>.)")
    s.add_argument("--bp", help="content path for the Blueprint check (default /Game)")
    s.add_argument("--play", nargs="?", const="", metavar="GLOB",
                   help="also run `ue pie do` step files (default tests/play/*.txt or Tests/Play/*.txt)")
    s.add_argument("--validate", nargs="?", const="/Game", metavar="PATH",
                   help="also run data validation on PATH (default /Game; ~0.1 s per asset) and Map Check")
    s.add_argument("--cook", action="store_true", help="also cook for this platform")
    s.add_argument("--cook-timeout", type=float, default=3600, help="seconds to allow the cook (3600)")
    s.add_argument("--report", metavar="FILE", help="write a JSON report (for CI artifacts)")
    s.add_argument("-v", "--verbose", action="store_true", help="print the tail of failing steps")
    s = add("selftest", cmd_selftest, "check every capability against the running editor (pass/fail/skip)")
    s.add_argument("--reload", action="store_true", help="also hot-reload the Tether package and re-check")
    s = add("init", cmd_init, "set up this project: enable the needed plugins, copy the Tether editor plugin in")
    s.add_argument("--unrealsharp", action="store_true", help="also install TetherUnrealSharp (auto if Script/ exists)")
    s.add_argument("--dry-run", action="store_true", help="show what would change")
    s = add("batch", cmd_batch, "run many ue commands in one process and editor session (one per line)")
    s.add_argument("file", nargs="?", help="file of ue command lines (default: stdin); # comments")
    s.add_argument("--stop-on-error", action="store_true", help="stop at the first failing command")
    add("reload", cmd_reload, "hot-reload the Tether Python package in the editor")
    s = add("hook", cmd_hook, "Claude Code hook entry points (hook JSON on stdin)")
    s.add_argument("event", choices=["pre-edit", "pre-mcp", "session-start", "post-edit", "stop"])

    return p


def parse_command(parser, raw):
    """Parsed arguments for one ue command line, with the global options' defaults filled in."""
    command = []
    if "job" in raw and "--" in raw[raw.index("job"):]:  # `ue job start [opts] -- <program> <args...>`
        cut = raw.index("--", raw.index("job"))
        raw, command = raw[:cut], raw[cut + 1:]
    args = parser.parse_args(raw)
    args.job_command = command
    args.timeout_given = hasattr(args, "timeout")
    for key, default in (("project", None), ("port", None), ("timeout", 120.0), ("json", False),
                         ("limit", 30), ("time", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    return args


def cmd_batch(ctx, args):
    """Run many ue commands in one process and one editor session: one per line of a file or stdin."""
    import shlex
    if args.file and args.file != "-":
        with open(args.file, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    parser, worst, started = build_parser(), 0, time.perf_counter()
    for line in lines:
        argv = shlex.split(line, posix=True)
        if argv and argv[0] == "ue":
            argv = argv[1:]
        print(f"$ ue {line[3:] if line.startswith('ue ') else line}", flush=True)
        try:
            sub = parse_command(parser, undo_msys_paths(argv))
            if sub.fn in (cmd_batch, cmd_hook):
                raise CliError("not inside ue batch")
            code = sub.fn(ctx, sub) or 0
        except CliError as exc:
            print(f"error: {exc}", flush=True)
            code = exc.code
        except SystemExit as exc:  # argparse rejected the line (its message is already printed)
            code = exc.code if isinstance(exc.code, int) else 2
        worst = max(worst, code)
        if code and args.stop_on_error:
            print(f"(stopped at the first failure; {len(lines) - lines.index(line) - 1} line(s) not run)")
            break
    sys.stdout.flush()
    print(f"({len(lines)} command(s) in {time.perf_counter() - started:.1f}s, exit {worst})", file=sys.stderr)
    return worst


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252; editor text is often not
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_command(build_parser(), undo_msys_paths(sys.argv[1:] if argv is None else argv))
    started, started_wall = time.perf_counter(), time.time()
    try:
        ctx = Ctx(args)
        try:
            code = args.fn(ctx, args)
        finally:
            ctx.close()
    except CliError as exc:
        sys.stdout.flush()  # keep the error after the output that explains it when both are piped
        message = str(exc)
        if exc.code in (2, 4) and ("not reachable" in message or "timed out" in message):
            try:  # the editor may have died during this very call
                crash = recent_crash(find_project(args.project), started_wall - 5)
                message += f"; the editor crashed: {crash_summary(crash)} (ue crash)" if crash else ""
            except CliError:
                pass
        if exc.code == 4 and "blocked by modal" not in message:
            try:  # say why the editor stopped answering, if it is a dialog or a stalled game thread
                report = stall_report(find_project(args.project))
                message += f"; editor {report}" if report else ""
            except CliError:
                pass
        print(f"error: {message}", file=sys.stderr)
        code = exc.code
    if args.time:
        sys.stdout.flush()
        print(f"({(time.perf_counter() - started) * 1000:.0f} ms)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
