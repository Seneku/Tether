"""Writes Saved/Tether/editors/<pid>.json about once a second from the editor tick.

A fresh heartbeat proves the game thread is alive; a stale heartbeat with a live
PID means the editor is hung. The `ue` CLI reads these files without any HTTP.
Each editor process has its own file (and token file), so two editors open on the
same project, say the user's and one an agent started, never overwrite each other.
"""
import json
import os
import re
import sys
import time
import zlib

import unreal

_INTERVAL = 1.0
# Stashed on sys so a hot reload (which re-executes this module in place) keeps the state.
_state = getattr(sys, "_tether_beacon_state", None) or {"last": 0.0, "warned": False, "port": None}
sys._tether_beacon_state = _state


def _dir():
    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    return os.path.join(saved, "Tether", "editors")


def _path():
    return os.path.join(_dir(), f"{os.getpid()}.json")


def _command_line():
    return unreal.SystemLibrary.get_command_line() or ""


def project_port(project):
    """The project's own MCP port, the same one `ue open` picks: stable per project, away from 8000."""
    return 18000 + zlib.crc32(os.path.normcase(os.path.abspath(project)).encode()) % 1000


def _port_busy(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _ensure_mcp_server():
    """The MCP port this editor serves on, starting Epic's server if nothing else did. `ue open` passes
    -ModelContextProtocolStartServer; an editor the user opened has no such flag and the plugin's
    auto-start setting is off by default, so it would serve nothing. TETHER_NO_MCP=1 opts out."""
    command_line = _command_line()
    match = re.search(r"ModelContextProtocolPort=(\d+)", command_line)
    # The settings class has no Python wrapper (MinimalAPI); its default object answers by property name.
    settings = unreal.find_object(None, "/Script/ModelContextProtocolEngine.Default__ModelContextProtocolSettings")
    if settings is None:
        return int(match.group(1)) if match else None  # no MCP plugin (UE < 5.8): remote execution instead
    try:
        auto_start = bool(settings.get_editor_property("bAutoStartServer"))
        settings_port = int(settings.get_editor_property("ServerPortNumber"))
    except Exception:
        auto_start, settings_port = False, 8000
    if "ModelContextProtocolStartServer" in command_line or auto_start:
        return int(match.group(1)) if match else settings_port
    if _state.get("started_port"):  # a hot reload: the server this module started is still up
        return _state["started_port"]
    if os.environ.get("TETHER_NO_MCP"):
        return None
    project = unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())
    base = project_port(project)
    port = next((p for p in range(base, base + 50) if not _port_busy(p)), None)
    if port is None:
        unreal.log_warning(f"Tether: no free port near {base} for the MCP server")
        return None
    unreal.SystemLibrary.execute_console_command(None, f"ModelContextProtocol.StartServer {port}")
    _state["started_port"] = port
    unreal.log(f"Tether: started the MCP server on port {port} for the ue CLI")
    return port


def _find_log():
    """This editor's log file. The first editor on a project writes Saved/Logs/<Project>.log, a second one
    <Project>_2.log, and Python cannot ask which: log a marker and look for it."""
    if _state.get("log") and os.path.isfile(_state["log"]):
        return _state["log"]
    marker = f"Tether: editor pid {os.getpid()} logs here"
    unreal.log(marker)
    unreal.SystemLibrary.execute_console_command(None, "FLUSHLOG")
    folder = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
    stem = os.path.splitext(os.path.basename(unreal.Paths.get_project_file_path()))[0]
    names = [n for n in os.listdir(folder) if n.startswith(stem) and n.endswith(".log") and "-backup-" not in n] \
        if os.path.isdir(folder) else []
    for name in sorted(names, key=lambda n: -os.path.getmtime(os.path.join(folder, n))):
        path = os.path.join(folder, name)
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 4_000_000))
                if marker.encode() in f.read():
                    _state["log"] = path
                    return path
        except OSError:
            continue
    return None


def _headless():
    return bool(re.search(r"(^|\s)-(nullrhi|RenderOffscreen)\b", _command_line(), re.I))


def _plugin_version():
    if "version" not in _state:
        try:
            here = os.path.dirname(os.path.abspath(__file__))  # Plugins/Tether/Content/Python/tether
            with open(os.path.join(here, "..", "..", "..", "Tether.uplugin"), encoding="utf-8") as f:
                _state["version"] = json.load(f).get("VersionName")
        except (OSError, ValueError):
            _state["version"] = None
    return _state["version"]


def _snapshot():
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    # get_editor_world() logs "The Editor is currently in a play mode" during PIE, so check PIE first.
    game = editor.get_game_world()
    if game is None:
        world = editor.get_editor_world()
        level = world.get_path_name().split(".")[0] if world else None
    else:
        level = _state.get("map")  # keep the edited map's name while PIE runs
    if level and level != _state.get("snapshot_map") and game is None:
        _state["snapshot_map"] = level  # a map was loaded: baseline for `ue changes`
        try:
            from tether import toolsets
            toolsets.snapshot_level(level)
            toolsets._UNDO.clear()  # loading a map resets the editor's undo history too
        except Exception:
            pass
    _state["map"] = level
    try:
        utils = unreal.EditorLoadingAndSavingUtils
        dirty = len(utils.get_dirty_content_packages()) + len(utils.get_dirty_map_packages())
    except Exception:
        dirty = None
    try:
        sc = unreal.SourceControl
        vcs = str(sc.current_provider()) if sc.is_enabled() else None
        vcs = None if vcs in ("None", "") else vcs + ("" if sc.is_available() else " (not connected)")
    except Exception:
        vcs = None
    return {
        "dirty": dirty,
        "vcs": vcs,
        "pid": os.getpid(),
        "project": unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path()),
        "engine": unreal.SystemLibrary.get_engine_version(),
        "mcp_port": _state["port"],
        "map": level,
        "pie": game is not None,
        "headless": _state["headless"],
        "nullrhi": _state["nullrhi"],  # no rendering at all: no ue shot
        "sound": _state["sound"],
        "launched_by_tether": _state["launched"],  # `ue open` marks its editors; the rest are the user's
        "tether": _plugin_version(),  # the plugin version this editor loaded (ue doctor compares it with the CLI)
        "started": _state["started"],
        "log": _state.get("log"),
        "heartbeat": time.time(),
    }


def _tick(_delta):
    now = time.time()
    if now - _state["last"] < _INTERVAL:
        return
    _state["last"] = now
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_snapshot(), f)
        os.replace(tmp, path)
    except Exception as exc:  # never let the beacon break or spam the tick
        if not _state["warned"]:
            _state["warned"] = True
            unreal.log_warning(f"Tether beacon: {exc}")


def session_token():
    """Random per-editor-process secret that gates the tools able to run code or change state.

    Epic's MCP server only binds to localhost and checks Origin; anything else that can reach the port
    could run editor Python. The token lives in Saved/Tether/editors/<pid>.token, which the ue CLI reads,
    so callers must be able to read the project folder. It survives hot reloads (stashed on sys)."""
    token = getattr(sys, "_tether_token", None)
    if token is None:
        import secrets
        token = sys._tether_token = secrets.token_hex(16)
    return token


def _write_session():
    path = os.path.join(_dir(), f"{os.getpid()}.token")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "token": session_token()}, f)
    os.replace(tmp, path)


def _others():
    """Other editors' beacons with a fresh heartbeat (written in the last few seconds)."""
    found = []
    for name in os.listdir(_dir()) if os.path.isdir(_dir()) else []:
        if name.endswith(".json") and name != f"{os.getpid()}.json":
            try:
                with open(os.path.join(_dir(), name), encoding="utf-8") as f:
                    beacon = json.load(f)
            except (OSError, ValueError):
                continue
            if time.time() - beacon.get("heartbeat", 0) < 10:
                found.append(beacon)
    return found


def start():
    """(Re)start the beacon; safe to call again after a hot reload."""
    _state.setdefault("started", time.time())
    _state["headless"] = _headless()
    _state["nullrhi"] = bool(re.search(r"(^|\s)-nullrhi\b", _command_line(), re.I))
    _state["sound"] = not re.search(r"(^|\s)-nosound\b", _command_line(), re.I)
    # `ue open` adds -TetherLaunched; before 0.11 it was recognisable by -ModelContextProtocolStartServer alone
    _state["launched"] = any(flag in _command_line() for flag in ("-TetherLaunched", "-ModelContextProtocolStartServer"))
    _write_session()
    _state["port"] = _ensure_mcp_server()
    try:
        _find_log()
    except Exception:
        pass
    for other in _others():
        unreal.log_warning(
            f"Tether: another editor is open on this project (pid {other.get('pid')}, "
            + ("headless, started by the ue CLI" if other.get("launched_by_tether") else "opened by hand")
            + "). Saving the same assets from both will lose work; close one of them.")
    previous = getattr(sys, "_tether_beacon", None)
    if previous is not None:
        unreal.unregister_slate_post_tick_callback(previous)
    sys._tether_beacon = unreal.register_slate_post_tick_callback(_tick)
    if not getattr(sys, "_tether_cleanup", False):  # a clean exit removes this editor's files
        import atexit
        atexit.register(_remove_files, _dir(), os.getpid())
        sys._tether_cleanup = True


def _remove_files(folder, pid):
    for name in (f"{pid}.json", f"{pid}.token"):
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    try:  # a clean exit: older beacons left by crashed or killed editors are stale from now on (the CLI prunes them)
        with open(os.path.join(os.path.dirname(folder), "last-exit.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "time": time.time()}, f)
    except OSError:
        pass
