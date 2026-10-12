"""Writes Saved/Tether/editor.json about once a second from the editor tick.

A fresh heartbeat proves the game thread is alive; a stale heartbeat with a live
PID means the editor is hung. The `ue` CLI reads this file without any HTTP.
"""
import json
import os
import re
import sys
import time

import unreal

_INTERVAL = 1.0
# Stashed on sys so a hot reload (which re-executes this module in place) keeps the state.
_state = getattr(sys, "_tether_beacon_state", None) or {"last": 0.0, "warned": False, "port": None}
sys._tether_beacon_state = _state


def _path():
    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    return os.path.join(saved, "Tether", "editor.json")


def _mcp_port():
    match = re.search(r"ModelContextProtocolPort=(\d+)", unreal.SystemLibrary.get_command_line() or "")
    if match:
        return int(match.group(1))
    try:
        settings = unreal.get_default_object(unreal.ModelContextProtocolSettings)
        return int(settings.get_editor_property("server_port_number"))
    except Exception:
        return 8000


def _snapshot():
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    # get_editor_world() logs "The Editor is currently in a play mode" during PIE, so check PIE first.
    game = editor.get_game_world()
    if game is None:
        world = editor.get_editor_world()
        level = world.get_path_name().split(".")[0] if world else None
    else:
        level = _state.get("map")  # keep the edited map's name while PIE runs
    _state["map"] = level
    return {
        "pid": os.getpid(),
        "project": unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path()),
        "engine": unreal.SystemLibrary.get_engine_version(),
        "mcp_port": _state["port"],
        "map": level,
        "pie": game is not None,
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
    could run editor Python. The token lives in Saved/Tether/session.json, which the ue CLI reads, so
    callers must be able to read the project folder. It survives hot reloads (stashed on sys)."""
    token = getattr(sys, "_tether_token", None)
    if token is None:
        import secrets
        token = sys._tether_token = secrets.token_hex(16)
    return token


def _write_session():
    path = os.path.join(os.path.dirname(_path()), "session.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "token": session_token()}, f)
    os.replace(tmp, path)


def start():
    """(Re)start the beacon; safe to call again after a hot reload."""
    _write_session()
    _state["port"] = _mcp_port()
    previous = getattr(sys, "_tether_beacon", None)
    if previous is not None:
        unreal.unregister_slate_post_tick_callback(previous)
    sys._tether_beacon = unreal.register_slate_post_tick_callback(_tick)
