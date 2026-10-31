"""Talking to the editor: Epic's MCP server (5.8+), Python remote execution (5.4-5.7), the toolset catalog."""
import json
import os
import re
import sys
import time

from .core import (
    CliError, GATED, TETHER, chosen_editor, compact_error, editor_label, editor_token, engine_root, find_project,
    live_editors, load_log_ignore, pid_alive, port_from_log, read_json, session_id, socket, stall_report, state_dir, unwrap,
    write_json,
)
from .share import check_shared, lease_note

# Tools that write packages to disk: while another session holds the editor lease (PIE runs, before/after shots,
# GPU baselines), these refuse instead of changing the files under it; --ignore-lease goes ahead.
SAVES = {"save", "import_files", "reimport_assets", "mv", "rm", "move_folder", "fix_redirectors"}
NOTED = {"py", "console"}  # say the lease themselves


# ---------------------------------------------------------------- MCP client

# Epic's Slate inspector tools that send input. While a Fab tab is open none of them run: the store page lives in
# a web browser the inspector cannot see into, so a click or a key could land on Buy or Add to My Library.
SLATE_INPUT = {"Click", "Type", "PressKey", "Drag", "FillForm", "SelectOption"}
FAB_TAB = re.compile(r'^\s*(?:tab|window) "Fab\b', re.M)
FAB_GUARD = ("a Fab tab is open in the editor: Tether sends no clicks or keys while the Fab store is open (they could "
             "land on Buy or Add to My Library). Ask the user to close the Fab tab, or to do this step themselves")


def fab_tab_open(snapshot):
    return bool(FAB_TAB.search(snapshot or ""))


class Mcp:
    def __init__(self, port, timeout):
        self.port, self.timeout, self.session, self.next_id = port, timeout, None, 0
        self.slate_checked = 0.0

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
        if tool in SLATE_INPUT and "SlateInspector" in toolset and time.time() - self.slate_checked > 2:
            text, failed = self.call(toolset, "Snapshot", {"ref": "", "maxDepth": 120, "bIncludeSourceLocations": False})
            if not failed and fab_tab_open(text):
                raise CliError(FAB_GUARD)
            self.slate_checked = time.time()
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
    beacon = chosen_editor(project) or {}
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
        self._warned = self._lease_noted = False
        self.data = None  # structured result a command offers for --json

    @property
    def project(self):
        if self._project is None:
            self._project = find_project(self.args.project)
            load_log_ignore(self._project)
        return self._project

    @property
    def beacon(self):
        return chosen_editor(self.project)

    @property
    def port(self):
        if self.args.port:
            return self.args.port
        if os.environ.get("UE_MCP_PORT"):
            return int(os.environ["UE_MCP_PORT"])
        beacon = self.beacon
        if (beacon or {}).get("mcp_port"):
            return beacon["mcp_port"]
        if (beacon or {}).get("mcp_error"):  # its log names a port another editor answers on
            raise CliError(f"editor pid {beacon.get('pid')}: {beacon['mcp_error']}, so nothing can reach it; "
                           "`ue close --force` ends it, then `ue open` again", 2)
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

    def _first_note(self, beacon, others):
        """Say which of several editors a command goes to once per session and set of editors (`ue status` keeps
        saying it), not on every command."""
        path = os.path.join(state_dir(self.project), "editors-noted.json")
        seen = read_json(path) or {}
        key = f"{(beacon or {}).get('pid')}:" + ",".join(sorted(str(b.get("pid")) for b in others))
        if seen.get(session_id()) == key:
            return False
        seen[session_id()] = key
        write_json(path, seen)
        return True

    def token(self, beacon):
        # --bulk rides on the token: the editor's revision-control guard then allows more check-outs
        return editor_token(self.project, beacon) + ("+bulk" if getattr(self.args, "bulk", False) else "")

    def tether(self, tool, **kwargs):
        if tool in GATED:  # the editor writes a per-session token next to the beacon
            beacon = self.beacon
            kwargs["token"] = self.token(beacon)
            if tool in SAVES or kwargs.get("save") is True:
                check_shared(self.project, f"{tool} (it writes to disk)", destructive=False)
            elif tool not in NOTED and not self._lease_noted:
                self._lease_noted = True
                note = lease_note(self.project)
                if note:
                    print(note, file=sys.stderr)
            others = [b for b in live_editors(self.project) if b.get("pid") != (beacon or {}).get("pid")]
            if others and not self._warned and self._first_note(beacon, others):
                self._warned = True
                print(f"note: {len(others) + 1} editors are open on this project; this goes to {editor_label(beacon)} "
                      f"(--pid to choose). Saving the same assets from both loses work.", file=sys.stderr)
        if self.transport == "remote":
            if self._rex is None:
                self._check_modal()
                self._rex = RemoteExec(self.project, self.args.timeout).connect()
            value = self._rex.tether(tool, kwargs)
            return unwrap(json.dumps({"returnValue": value}))
        text, is_error = self.mcp.call(TETHER, tool, kwargs)
        if is_error and tool in GATED and "unauthorized" in text:
            # a token read while the beacon rewrote it (a reload): read it again and retry once
            time.sleep(0.2)
            kwargs["token"] = self.token(self.beacon)
            text, is_error = self.mcp.call(TETHER, tool, kwargs)
        if is_error:
            raise CliError(compact_error(text))
        return unwrap(text)

    def close(self):
        if self._rex is not None:
            self._rex.close()


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


TOOLSET_PLUGINS = {"niagara": "NiagaraToolsets", "pcg": "PCGToolset", "slateinspector": "SlateInspectorToolset",
                   "configsettings": "ConfigSettingsToolset", "automationtest": "AutomationTestToolset",
                   "statetree": "StateTreeToolset", "blueprinttools": "EditorToolset", "editorapp": "EditorToolset"}


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
        plugin = next((p for key, p in TOOLSET_PLUGINS.items() if key in q), None)
        raise CliError(f"no toolset matches '{query}'" + (f": enable Epic's {plugin} plugin (ue init does), then restart "
                                                           "the editor" if plugin else " (try: ue tools)"))
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


def py_json(ctx, code):
    """Run editor Python that prints JSON last, and decode it (ue py output may already be decoded)."""
    out = ctx.tether("py", code=code)
    if not isinstance(out, str):
        return out
    lines = out.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (ValueError, IndexError):
        cut = re.fullmatch(r"\.\.\. truncated (\d+) chars", lines[-1]) if lines else None
        if cut:  # ue py caps its output (toolsets._MAX_OUT)
            size = len(out) - len(lines[-1]) - 1 + int(cut.group(1))
            raise CliError(f"the editor's reply was cut short: {size} chars, more than ue py returns "
                           f"({size - int(cut.group(1))}); the code should print less") from None
        text = " ".join(lines[-3:]) if lines else "nothing"
        raise CliError("the editor's reply was not JSON: " + (text[:300] + " ..." if len(text) > 300 else text)) from None
