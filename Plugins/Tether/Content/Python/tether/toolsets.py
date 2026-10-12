"""Tether gap-filling toolset, registered into Epic's ToolsetRegistry.

Fills what Epic's 5.8 toolsets leave out for coding agents: unrestricted editor
Python, console commands, and compact (token-frugal) reads. Pure Python, so
`reload` picks up edits without restarting the editor.
"""
import ast
import contextlib
import io
import json
import re
import sys
import traceback

import unreal
import toolset_registry
from toolset_registry.registration import Registration

_MAX_OUT = 20000

# Persistent REPL namespace for `py`; stashed on sys so it survives hot reloads.
_NAMESPACE = getattr(sys, "_tether_ns", None) or {"unreal": unreal, "__name__": "__tether_py__"}
sys._tether_ns = _NAMESPACE


def _run_python(code):
    tree = ast.parse(code, "<ue py>", "exec")
    tail = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.Expression(tree.body.pop().value)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(tree, "<ue py>", "exec"), _NAMESPACE)
            value = eval(compile(tail, "<ue py>", "eval"), _NAMESPACE) if tail else None
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        line = next((f.lineno for f in reversed(frames) if f.filename == "<ue py>"), None)
        where = f" (line {line})" if line else ""
        raise RuntimeError(f"{buf.getvalue()}{type(exc).__name__}: {exc}{where}") from None
    out = buf.getvalue()
    if value is not None:
        out += value if isinstance(value, str) else repr(value)
    if len(out) > _MAX_OUT:
        out = out[:_MAX_OUT] + f"\n... truncated {len(out) - _MAX_OUT} chars"
    return out


def _actors():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()


def _match_actors(ref):
    key = ref.lower()
    actors = _actors()
    exact = [a for a in actors if key in (a.get_actor_label().lower(), a.get_name().lower())]
    return exact or [a for a in actors if key in a.get_actor_label().lower()]


def _resolve(ref):
    """Find an object by object/asset path, actor label or name, or 'ActorLabel.ComponentName'."""
    if "/" in ref:
        obj = unreal.find_object(None, ref)
        if obj is None:
            with contextlib.suppress(Exception):
                obj = unreal.load_asset(ref)
        if obj is not None:
            return obj
    actor_ref, _, comp_ref = ref.partition(".")
    matches = _match_actors(actor_ref)
    if not matches and comp_ref:
        matches, comp_ref = _match_actors(ref), ""
    if not matches:
        raise RuntimeError(f"not found: {ref}")
    if len(matches) > 1:
        raise RuntimeError(f"ambiguous '{actor_ref}': " + ", ".join(a.get_actor_label() for a in matches[:8]))
    actor = matches[0]
    if not comp_ref:
        return actor
    comps = actor.get_components_by_class(unreal.ActorComponent)
    for comp in comps:
        if comp.get_name().lower() == comp_ref.lower():
            return comp
    raise RuntimeError(f"{actor.get_actor_label()} has no component '{comp_ref}' (has: "
                       + ", ".join(c.get_name() for c in comps) + ")")


def _short_path(path):
    return path.split(":PersistentLevel.", 1)[-1] if path else "None"


def _label(obj):
    if isinstance(obj, unreal.Actor):
        return obj.get_actor_label()
    if isinstance(obj, unreal.ActorComponent) and obj.get_owner():
        return f"{obj.get_owner().get_actor_label()}.{obj.get_name()}"
    return _short_path(obj.get_path_name())


def _type_name(entry):
    if not isinstance(entry, dict):
        return "?"
    if entry.get("type") == "array":
        return f"array<{_type_name(entry.get('items', {}))}>"
    if entry.get("title"):
        return entry["title"].rsplit(".", 1)[-1]
    return "enum" if "enum" in entry else entry.get("type", "?")


def _round(value):
    if isinstance(value, float):
        return float(f"{value:.4g}")
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def _short(value, width=90):
    if isinstance(value, dict) and set(value) == {"refPath"}:
        text = _short_path(value["refPath"])
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"))
    else:
        text = str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def _capped(head, rows, limit):
    body = rows[:limit]
    if len(rows) > limit:
        body.append(f"... +{len(rows) - limit} more")
    return "\n".join([head] + body)


def _csharp():
    lib = getattr(unreal, "TetherCSharpLibrary", None)
    if lib is None:
        raise RuntimeError("TetherUnrealSharp plugin is not enabled in this project")
    return lib


_reload = {"handle": None}


def _schedule_reload():
    if _reload["handle"] is not None:
        return

    def _do(_delta):
        unreal.unregister_slate_post_tick_callback(_reload["handle"])
        _reload["handle"] = None
        import tether
        from tether import beacon
        toolset_registry.reload_module(tether)
        beacon.start()  # re-bind the tick callback to the reloaded module
        unreal.log("Tether: reloaded")

    _reload["handle"] = unreal.register_slate_post_tick_callback(_do)


@unreal.uclass()
class TetherCore(unreal.ToolsetDefinition):
    """Tether core tools for coding agents: unrestricted editor Python with a
    persistent namespace, console commands, compact property and actor reads,
    and hot reload of the Tether package. Output is compact text by design."""

    @toolset_registry.tool_call
    @staticmethod
    def py(code: str) -> str:
        """Execute Python in the editor interpreter.

        The namespace persists between calls (like a REPL) and `unreal` is
        pre-imported. Returns captured stdout/stderr followed by the value of a
        trailing expression, if any.

        Args:
            code: Python source to execute.

        Returns:
            Captured output and the trailing expression value.
        """
        return _run_python(code)

    @toolset_registry.tool_call
    @staticmethod
    def console(command: str, pie: bool = False) -> str:
        """Run a console command, e.g. a cheat or 'stat unit'. Its output goes to the log.

        While PIE runs, commands go to the PIE world.

        Args:
            command: The console command line.
            pie: Require a PIE world (error if PIE is not running).

        Returns:
            Which world the command ran in.
        """
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        game = editor.get_game_world()  # checked first: get_editor_world() logs an error during PIE
        if pie and game is None:
            raise RuntimeError("PIE is not running")
        world = game if game is not None else editor.get_editor_world()
        if world is None:
            raise RuntimeError("no editor world")
        unreal.SystemLibrary.execute_console_command(world, command)
        return ("PIE world " if game is not None else "editor world ") + world.get_name()

    @toolset_registry.tool_call
    @staticmethod
    def props(target: str, match: str = "", limit: int = 80) -> str:
        """List an object's reflected properties as 'name: Type = value' lines.

        Args:
            target: Object or asset path, actor label or name, or 'ActorLabel.ComponentName'.
            match: Case-insensitive regex matched against property name and type.
            limit: Maximum number of property lines.

        Returns:
            A header naming the resolved object, then one line per property.
        """
        obj = _resolve(target)
        cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
        schema = json.loads(unreal.ToolsetLibrary.list_struct_properties(cls))
        names = list(schema)
        if match:
            rx = re.compile(match, re.I)
            names = [n for n in names if rx.search(n) or rx.search(_type_name(schema[n]))]
        values = json.loads(unreal.ToolsetLibrary.get_object_properties(obj, names)) if names else {}
        head = f"{_label(obj)} ({cls.get_name()}, {len(schema)} props" + (f", {len(names)} match" if match else "") + ")"
        rows = [f"{n}: {_type_name(schema[n])} = {_short(_round(values.get(n)))}" for n in names]
        return _capped(head, rows, limit)

    @toolset_registry.tool_call
    @staticmethod
    def actors(match: str = "", limit: int = 60) -> str:
        """List actors in the editor level as 'label  Class  (x, y, z)' lines.

        Args:
            match: Case-insensitive regex matched against actor label and class.
            limit: Maximum number of actor lines.

        Returns:
            A count header, then one line per actor.
        """
        rx = re.compile(match, re.I) if match else None
        rows = []
        for actor in _actors():
            label, cls = actor.get_actor_label(), actor.get_class().get_name()
            if rx and not (rx.search(label) or rx.search(cls)):
                continue
            loc = actor.get_actor_location()
            rows.append(f"{label}  {cls}  ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})")
        return _capped(f"{len(rows)} actors" + (f" matching /{match}/" if match else ""), rows, limit)

    @toolset_registry.tool_call
    @staticmethod
    def level_summary(limit: int = 12) -> str:
        """One-call overview of the open level: actor count, most common classes, PIE state.

        Args:
            limit: Maximum number of classes to list.

        Returns:
            A two-line summary.
        """
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = editor.get_editor_world()
        counts = {}
        for actor in _actors():
            name = actor.get_class().get_name()
            counts[name] = counts.get(name, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])
        head = (f"{world.get_path_name().split('.')[0]}: {sum(counts.values())} actors, "
                f"{len(counts)} classes, PIE {'on' if editor.get_game_world() else 'off'}")
        tail = f", +{len(top) - limit} more" if len(top) > limit else ""
        return head + "\n" + ", ".join(f"{n} x{c}" for n, c in top[:limit]) + tail

    @toolset_registry.tool_call
    @staticmethod
    def cs_reload() -> str:
        """Reload pending UnrealSharp C# changes now, with modal dialogs suppressed.

        Returns:
            reloaded, not reloaded (failed or paused), nothing pending, busy, pie or unavailable.
        """
        return _csharp().reload_csharp()

    @toolset_registry.tool_call
    @staticmethod
    def cs_state() -> str:
        """UnrealSharp C# hot-reload state.

        Returns:
            idle, pending, reloading or unavailable.
        """
        return _csharp().csharp_reload_state()

    @toolset_registry.tool_call
    @staticmethod
    def reload() -> str:
        """Hot-reload the tether Python package and re-register its toolsets on the next editor tick.

        Returns:
            Confirmation that the reload was scheduled.
        """
        _schedule_reload()
        return "reload scheduled"


registration = Registration([TetherCore])
