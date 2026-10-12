"""Tether gap-filling toolset, registered into Epic's ToolsetRegistry.

Fills what Epic's 5.8 toolsets leave out for coding agents: unrestricted editor
Python, console commands, and compact (token-frugal) reads. Pure Python, so
`reload` picks up edits without restarting the editor.

On UE 5.4-5.7 there is no ToolsetRegistry: the same functions load as a plain class
and the ue CLI calls them over Python remote execution instead of MCP.
"""
import ast
import contextlib
import io
import json
import os
import re
import sys
import traceback

import unreal

try:
    import toolset_registry
    from toolset_registry.registration import Registration
except ImportError:  # before UE 5.8
    toolset_registry = None

# Without Epic's ToolsetLibrary (pre-5.8), props read reflection through the Python wrappers instead,
# and set_props is unavailable. TETHER_LEGACY=1 in the editor's environment forces this path on 5.8.
_LEGACY = not hasattr(unreal, "ToolsetLibrary") or bool(os.environ.get("TETHER_LEGACY"))


_RAW = {}  # tool name -> plain Python function, for callers outside MCP (Python remote execution)


def _tool(fn):
    # The registry turns tools into native bindings that report exceptions as script warnings and return
    # an empty value, so remote execution calls the plain function from _RAW to see errors.
    raw = fn.__func__ if isinstance(fn, staticmethod) else fn
    _RAW[raw.__name__] = raw
    return toolset_registry.tool_call(fn) if toolset_registry else fn


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


def _game_world():
    return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()


def _actors(pie=False):
    game = _game_world()
    if pie:
        if game is None:
            raise RuntimeError("PIE is not running")
        return unreal.GameplayStatics.get_all_actors_of_class(game, unreal.Actor)
    if game is not None:
        # During PIE the editor subsystems refuse to touch the edited level ("The Editor is currently in a
        # play mode"), but the editor world is still loaded under the PIE world's path minus UEDPIE_<n>_.
        editor_world = unreal.find_object(None, re.sub(r"UEDPIE_\d+_", "", game.get_path_name()))
        if editor_world is not None:
            return unreal.GameplayStatics.get_all_actors_of_class(editor_world, unreal.Actor)
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()


def _match_actors(ref, pie=False):
    key = ref.lower()

    def find(actors):
        exact = [a for a in actors if key in (a.get_actor_label().lower(), a.get_name().lower())]
        return exact or [a for a in actors if key in a.get_actor_label().lower()]

    found = find(_actors(pie))
    if not found and not pie and _game_world() is None:
        # get_all_level_actors() leaves out transient actors (spawned with transient=True); look wider.
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if world is not None:
            found = find(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor))
    return found


def _resolve(ref, pie=False):
    """Find an object by object/asset path, actor label or name, or 'ActorLabel.ComponentName'."""
    if "/" in ref:
        obj = unreal.find_object(None, ref)
        if obj is None:
            with contextlib.suppress(Exception):
                obj = unreal.load_asset(ref)
        if obj is not None:
            return obj
    actor_ref, _, comp_ref = ref.partition(".")
    matches = _match_actors(actor_ref, pie)
    if not matches and comp_ref:
        matches, comp_ref = _match_actors(ref, pie), ""
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
        return float(f"{value:.6g}")  # 6 digits: 4 hid a 50-unit move at 100000 (100050 -> 1.000e5)
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


def _authorize(token):
    """Gate for tools that run code or change state; see beacon.session_token."""
    import hmac
    from tether import beacon
    if not hmac.compare_digest(str(token), beacon.session_token()):
        raise RuntimeError("unauthorized: pass the session token from Saved/Tether/session.json "
                           "(the ue CLI does this automatically)")


def _csharp():
    lib = getattr(unreal, "TetherCSharpLibrary", None)
    if lib is None:
        raise RuntimeError("TetherUnrealSharp plugin is not enabled in this project")
    return lib


_NUMBERS = re.compile(r"^\(?\s*(-?[\d.]+(?:[eE][-+]?\d+)?(?:\s*,\s*-?[\d.]+(?:[eE][-+]?\d+)?)*)\s*\)?$")
_ACTOR_POSE = ("location", "rotation", "scale")  # set through the actor, not reflected properties


def _numbers(text):
    match = _NUMBERS.match(text.strip())
    if not match:
        return None
    return [float(v) if any(c in v for c in ".eE") else int(v) for v in match.group(1).split(",")]


def _parse(raw, entry, old):
    """Turn compact text into the JSON value ToolsetLibrary expects, using the property's schema.

    Accepts JSON, true/false, numbers, positional tuples for structs ('1,2,3' fills x,y,z in schema
    order; missing trailing fields keep their current values), object paths, and enum names."""
    text = raw.strip()
    if isinstance(entry, dict) and entry.get("type") == "string":
        return raw
    props = entry.get("properties") if isinstance(entry, dict) else None
    nums = _numbers(text)
    if nums is not None and props and "refPath" not in props:
        if len(nums) > len(props):
            raise ValueError(f"{len(nums)} values for {len(props)} fields ({', '.join(props)})")
        merged = dict(old) if isinstance(old, dict) else {}
        merged.update(zip(props, nums))
        return merged
    kind = entry.get("type") if isinstance(entry, dict) else None
    if kind == "boolean":
        flag = {"true": True, "1": True, "yes": True, "on": True,
                "false": False, "0": False, "no": False, "off": False}.get(text.lower())
        if flag is None:
            raise ValueError(f"expected true/false, got '{text}'")
        return flag
    if isinstance(entry, dict) and "enum" in entry:
        names = {str(e).lower(): e for e in entry["enum"]}
        bare = text.split("::")[-1].lower()
        if bare not in names:
            raise ValueError(f"expected one of {', '.join(map(str, entry['enum'][:12]))}, got '{text}'")
        return names[bare]
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if kind in ("number", "integer"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"expected a number, got '{text}'")
        return value
    if value is not None:
        return value
    if props and "refPath" in props:
        return {"refPath": text}
    if props:
        raise ValueError(f"expected {','.join(props)} (or JSON), got '{text}'")
    return text


def _add(old, delta, name):
    if isinstance(old, (int, float)) and isinstance(delta, (int, float)):
        return old + delta
    if isinstance(old, dict) and isinstance(delta, dict):
        return {k: (v + delta[k] if k in delta and isinstance(v, (int, float)) else v) for k, v in old.items()}
    raise ValueError(f"'{name}' does not support += (not a number or numeric struct)")


def _took(wanted, actual):
    """Whether a written value reads back (numbers, bools and numeric structs; other kinds are trusted,
    since the engine may normalise names and paths)."""
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return not isinstance(wanted, bool) or wanted == actual
    if isinstance(wanted, (int, float)):  # 0.01 absorbs 8-bit colour (1/255) and float32 at 1e5 (~0.008)
        return isinstance(actual, (int, float)) and abs(actual - wanted) <= max(0.01, 1e-7 * abs(wanted))
    if isinstance(wanted, dict) and isinstance(actual, dict):
        if {"pitch", "yaw", "roll"} <= set(wanted):  # rotators read back normalised (270 -> -90)
            return all(isinstance(actual.get(k), (int, float)) and abs((actual[k] - wanted[k] + 180) % 360 - 180) < 0.01
                       for k in ("pitch", "yaw", "roll"))
        return all(_took(v, actual.get(k)) for k, v in wanted.items() if isinstance(v, (int, float, bool)))
    return True


def _compact(value):
    """Numeric structs as '(1, 2, 3)', everything else as in props."""
    value = _round(value)
    if isinstance(value, dict) and value and all(isinstance(v, (int, float)) for v in value.values()):
        return "(" + ", ".join(f"{0 if abs(v) < 1e-6 else v:g}" for v in value.values()) + ")"
    return _short(value)


def _vec(d):
    return unreal.Vector(d["x"], d["y"], d["z"])


def _rot(d):
    return unreal.Rotator(roll=d["roll"], pitch=d["pitch"], yaw=d["yaw"])


def _actor_pose(actor):
    loc, rot, scl = actor.get_actor_location(), actor.get_actor_rotation(), actor.get_actor_scale3d()
    return {"location": {"x": loc.x, "y": loc.y, "z": loc.z},
            "rotation": {"pitch": rot.pitch, "yaw": rot.yaw, "roll": rot.roll},
            "scale": {"x": scl.x, "y": scl.y, "z": scl.z}}


_POSE_FIELDS = {"location": {"properties": {"x": 0, "y": 0, "z": 0}},
                "rotation": {"properties": {"pitch": 0, "yaw": 0, "roll": 0}},
                "scale": {"properties": {"x": 0, "y": 0, "z": 0}}}


def _point_and_extent(ref, pie):
    """An actor's (or component owner's) bounds centre and extent."""
    obj = _resolve(ref, pie)
    actor = obj if isinstance(obj, unreal.Actor) else (obj.get_owner() if isinstance(obj, unreal.ActorComponent) else None)
    if actor is None:
        raise RuntimeError(f"'{ref}' is not an actor or component")
    return actor.get_actor_bounds(False)


def _point(ref, pie):
    """'x,y,z' or an actor reference (its bounds centre) as an unreal.Vector."""
    nums = _numbers(ref)
    if nums is not None:
        if len(nums) != 3:
            raise RuntimeError(f"expected x,y,z, got '{ref}'")
        return unreal.Vector(*nums)
    return _point_and_extent(ref, pie)[0]


_DOC_PROPERTY = re.compile(r"^- ``(\w+)`` \(([^)]*)\)", re.M)


def _legacy_props(obj, match, limit):
    """props without ToolsetLibrary (UE < 5.8): names and types from the Python wrapper's docstring, which
    lists every editor property (inherited ones included), and values through get_editor_property."""
    schema = dict(_DOC_PROPERTY.findall(type(obj).__doc__ or ""))
    names = list(schema)
    if match:
        rx = re.compile(match, re.I)
        names = [n for n in names if rx.search(n) or rx.search(schema[n])]

    def text(value):
        if isinstance(value, unreal.StructBase):
            return value.export_text()
        if isinstance(value, unreal.EnumBase):
            return value.name
        if isinstance(value, unreal.Object):
            return _short_path(value.get_path_name())
        if isinstance(value, (unreal.Array, list, tuple)):
            return "[" + ", ".join(str(text(v)) for v in list(value)[:8]) + (", ..." if len(value) > 8 else "") + "]"
        return _short(_round(value))

    rows = []
    for name in names:
        try:
            rows.append(f"{name}: {schema[name]} = {_short(text(obj.get_editor_property(name)))}")
        except Exception:  # write-only or unreadable in this context
            rows.append(f"{name}: {schema[name]} = ?")
    cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
    head = f"{_label(obj)} ({cls.get_name()}, {len(schema)} props" + (f", {len(names)} match" if match else "") + ")"
    return _capped(head, rows, limit)


_reload = {"handle": None}


def _schedule_reload():
    if _reload["handle"] is not None:
        return

    def _do(_delta):
        unreal.unregister_slate_post_tick_callback(_reload["handle"])
        _reload["handle"] = None
        import tether
        from tether import beacon
        if toolset_registry:
            toolset_registry.reload_module(tether)
        else:  # before UE 5.8: plain module reload (the CLI imports the functions per call)
            import importlib
            from tether import toolsets
            importlib.reload(beacon)
            importlib.reload(toolsets)
        beacon.start()  # re-bind the tick callback to the reloaded module
        unreal.log("Tether: reloaded")

    _reload["handle"] = unreal.register_slate_post_tick_callback(_do)


_Base = getattr(unreal, "ToolsetDefinition", object)


def _uclass(cls):
    return unreal.uclass()(cls) if _Base is not object else cls  # plain class before UE 5.8


@_uclass
class TetherCore(_Base):
    """Tether core tools for coding agents: unrestricted editor Python with a
    persistent namespace, console commands, compact property and actor reads,
    and hot reload of the Tether package. Output is compact text by design."""

    @_tool
    @staticmethod
    def py(code: str, token: str = "") -> str:
        """Execute Python in the editor interpreter.

        The namespace persists between calls (like a REPL) and `unreal` is
        pre-imported. Returns captured stdout/stderr followed by the value of a
        trailing expression, if any.

        Args:
            code: Python source to execute.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

        Returns:
            Captured output and the trailing expression value.
        """
        _authorize(token)
        return _run_python(code)

    @_tool
    @staticmethod
    def console(command: str, pie: bool = False, token: str = "") -> str:
        """Run a console command, e.g. a cheat or 'stat unit'. Its output goes to the log.

        While PIE runs, commands go to the PIE world.

        Args:
            command: The console command line.
            pie: Require a PIE world (error if PIE is not running).
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

        Returns:
            Which world the command ran in.
        """
        _authorize(token)
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        game = editor.get_game_world()  # checked first: get_editor_world() logs an error during PIE
        if pie and game is None:
            raise RuntimeError("PIE is not running")
        world = game if game is not None else editor.get_editor_world()
        if world is None:
            raise RuntimeError("no editor world")
        unreal.SystemLibrary.execute_console_command(world, command)
        return ("PIE world " if game is not None else "editor world ") + world.get_name()

    @_tool
    @staticmethod
    def props(target: str, match: str = "", limit: int = 80, pie: bool = False) -> str:
        """List an object's reflected properties as 'name: Type = value' lines.

        Args:
            target: Object or asset path, actor label or name, or 'ActorLabel.ComponentName'.
            match: Case-insensitive regex matched against property name and type.
            limit: Maximum number of property lines.
            pie: Resolve actors in the PIE world instead of the editor level.

        Returns:
            A header naming the resolved object, then one line per property.
        """
        obj = _resolve(target, pie)
        if _LEGACY:
            return _legacy_props(obj, match, limit)
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

    @_tool
    @staticmethod
    def set_props(target: str, values: str, pie: bool = False, token: str = "") -> str:
        """Set reflected properties from compact text, as one undoable editor transaction.

        Values are JSON, true/false, numbers, enum names, object paths, or positional tuples for
        structs ('0,0,200' for a Vector; missing trailing fields keep their values). A key ending
        in '+' adds to the current value. Actors also accept location, rotation and scale. If any
        property fails, the whole change is rolled back.

        Args:
            target: Object or asset path, actor label or name, or 'ActorLabel.ComponentName'.
            values: JSON object of property name to value text, e.g. {"Intensity": "10", "location+": "0,0,200"}.
            pie: Change the PIE world instead of the editor level (not undoable, not saved).
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

        Returns:
            One 'name: old -> new' line per property.
        """
        _authorize(token)
        if _LEGACY:
            raise RuntimeError("ue set needs UE 5.8+ (Epic's ToolsetLibrary); on older engines use ue py with "
                               "obj.set_editor_property(name, value) inside unreal.ScopedEditorTransaction")
        obj = _resolve(target, pie)
        cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
        schema = json.loads(unreal.ToolsetLibrary.list_struct_properties(cls))
        by_lower = {n.lower(): n for n in schema}
        is_actor = isinstance(obj, unreal.Actor)
        requested = json.loads(values)
        if not isinstance(requested, dict) or not requested:
            raise RuntimeError('values must be a non-empty JSON object, e.g. {"Intensity": "10"}')

        plan = []  # (name, relative, raw)
        for key, raw in requested.items():
            relative = key.endswith("+")
            base = key[:-1] if relative else key
            if is_actor and base.lower() in _ACTOR_POSE and base.lower() not in by_lower:
                plan.append((base.lower(), relative, raw))
                continue
            name = by_lower.get(base.lower())
            if name is None:
                import difflib
                close = difflib.get_close_matches(base.lower(), list(by_lower), n=4, cutoff=0.6)
                owners = []
                if is_actor:  # actors usually keep the interesting properties on a component
                    for comp in obj.get_components_by_class(unreal.ActorComponent):
                        names = json.loads(unreal.ToolsetLibrary.list_struct_properties(comp.get_class()))
                        if base.lower() in (n.lower() for n in names):
                            owners.append(f"{obj.get_actor_label()}.{comp.get_name()}")
                raise RuntimeError(f"{_label(obj)} has no property '{base}'"
                                   + (f"; it is on {', '.join(owners[:3])}" if owners else "")
                                   + (f" (did you mean: {', '.join(by_lower[c] for c in close)})" if close and not owners else ""))
            plan.append((name, relative, raw))

        reflected = [n for n, _, _ in plan if n in schema]
        old = json.loads(unreal.ToolsetLibrary.get_object_properties(obj, reflected)) if reflected else {}
        if is_actor:
            old.update({k: v for k, v in _actor_pose(obj).items() if any(n == k for n, _, _ in plan)})
        new = {}
        for name, relative, raw in plan:
            entry = schema.get(name) or _POSE_FIELDS[name]
            try:
                # A relative change parses as a pure delta: unspecified fields are 0, not the current value.
                value = _parse(raw if isinstance(raw, str) else json.dumps(raw), entry,
                               {k: 0 for k in entry.get("properties", {})} if relative else old.get(name))
                new[name] = _add(old.get(name), value, name) if relative else value
            except ValueError as exc:
                raise RuntimeError(f"{name}: {exc}") from None

        def apply():
            props = {n: v for n, v in new.items() if n in schema}
            try:
                ok = not props or unreal.ToolsetLibrary.set_object_properties(obj, json.dumps(props))
            except Exception as exc:  # Epic raises with the full object path; keep the useful tail
                detail = re.split(r"could not be set: ", str(exc), maxsplit=1)[-1].strip()
                raise RuntimeError(f"could not set {detail}") from None
            if not ok:
                raise RuntimeError("invalid value (see ue log)")
            if "location" in new:
                obj.set_actor_location(_vec(new["location"]), False, True)
            if "rotation" in new:
                obj.set_actor_rotation(_rot(new["rotation"]), True)
            if "scale" in new:
                obj.set_actor_scale3d(_vec(new["scale"]))

        if pie:
            apply()
        else:
            failure, before = None, unreal.ToolsetLibrary.get_active_undo_count()
            with unreal.ScopedEditorTransaction(f"ue set {_label(obj)}"):
                obj.modify()
                if is_actor and obj.root_component and any(n in _ACTOR_POSE for n in new):
                    obj.root_component.modify()
                try:
                    apply()
                except Exception as exc:  # roll back below, once the transaction is closed
                    failure = exc
            if failure is not None:
                # Undo only if our transaction was recorded; an empty one is dropped, and undoing then
                # would revert the user's previous action instead.
                rolled = unreal.ToolsetLibrary.get_active_undo_count() > before
                if rolled:
                    unreal.ToolsetLibrary.undo_transaction(False)
                raise RuntimeError(f"{failure}" + (" (rolled back)" if rolled else " (nothing was changed)"))

        after = json.loads(unreal.ToolsetLibrary.get_object_properties(obj, reflected)) if reflected else {}
        if is_actor:
            after.update(_actor_pose(obj))
        ignored = [n for n in new if not _took(new[n], after.get(n))]
        if ignored:  # e.g. location on an actor without a root component: report it rather than "succeed"
            if not pie and unreal.ToolsetLibrary.get_active_undo_count() > before:
                unreal.ToolsetLibrary.undo_transaction(False)
            hint = " (the actor has no root component)" if is_actor and not obj.root_component and \
                any(n in _ACTOR_POSE for n in ignored) else ""
            raise RuntimeError("did not take: " + ", ".join(f"{n} stayed {_compact(after.get(n))}" for n in ignored)
                               + hint + ("" if pie else " (rolled back)"))
        rows = [f"{n}: {_compact(old.get(n))} -> {_compact(after.get(n))}" for n in new]
        return "\n".join([_label(obj) + (" (PIE)" if pie else "")] + rows)

    @_tool
    @staticmethod
    def shot_pose(actor: str = "", at: str = "", look_at: str = "", rot: str = "") -> str:
        """Camera pose for a viewport capture, as ToolsetTransform JSON.

        Starts from the level viewport camera (or the player camera while PIE runs), then applies
        the options in order: frame an actor, move to a point, aim at a point, set a rotation.

        Args:
            actor: Frame this actor: keep the view direction and back off to fit its bounds.
            at: Camera location 'x,y,z'.
            look_at: Aim at 'x,y,z' or at an actor's bounds centre.
            rot: Camera rotation 'pitch,yaw,roll'.

        Returns:
            {"location": ..., "rotation": ..., "scale": ...}
        """
        game = _game_world()
        pie = game is not None
        cam = unreal.GameplayStatics.get_player_camera_manager(game, 0) if pie else None
        if cam is not None:
            loc, rotation = cam.get_camera_location(), cam.get_camera_rotation()
        elif pie:  # Simulate-In-Editor has no player; the shot is the viewport either way
            loc, rotation = unreal.Vector(), unreal.Rotator()
        else:
            loc, rotation = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_level_viewport_camera_info()
        if actor:
            centre, extent = _point_and_extent(actor, pie)
            distance = max(extent.length() * 2.2, 150.0)
            loc = centre - rotation.get_forward_vector() * distance
            rotation = unreal.MathLibrary.find_look_at_rotation(loc, centre)
        if at:
            nums = _numbers(at)
            if not nums or len(nums) != 3:
                raise RuntimeError(f"at: expected x,y,z, got '{at}'")
            loc = unreal.Vector(*nums)
        if look_at:
            rotation = unreal.MathLibrary.find_look_at_rotation(loc, _point(look_at, pie))
        if rot:
            nums = _numbers(rot)
            if not nums or len(nums) != 3:
                raise RuntimeError(f"rot: expected pitch,yaw,roll, got '{rot}'")
            rotation = unreal.Rotator(roll=nums[2], pitch=nums[0], yaw=nums[1])
        return json.dumps({"location": {"x": loc.x, "y": loc.y, "z": loc.z},
                           "rotation": {"pitch": rotation.pitch, "yaw": rotation.yaw, "roll": rotation.roll},
                           "scale": {"x": 1, "y": 1, "z": 1}, "pie": pie,
                           # Simulate-In-Editor possesses a SpectatorPawn and has no game viewport to shoot
                           "simulate": pie and (cam is None or isinstance(
                               unreal.GameplayStatics.get_player_pawn(game, 0), unreal.SpectatorPawn))})

    @_tool
    @staticmethod
    def actors(match: str = "", limit: int = 60, pie: bool = False) -> str:
        """List actors in the editor level as 'label  Class  (x, y, z)' lines.

        Args:
            match: Case-insensitive regex matched against actor label and class.
            limit: Maximum number of actor lines.
            pie: List the PIE world's actors instead.

        Returns:
            A count header, then one line per actor.
        """
        rx = re.compile(match, re.I) if match else None
        rows = []
        for actor in _actors(pie):
            label, cls = actor.get_actor_label(), actor.get_class().get_name()
            if rx and not (rx.search(label) or rx.search(cls)):
                continue
            loc = actor.get_actor_location()
            rows.append(f"{label}  {cls}  ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})")
        return _capped(f"{len(rows)} actors" + (f" matching /{match}/" if match else ""), rows, limit)

    @_tool
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

    @_tool
    @staticmethod
    def api(name: str, match: str = "", limit: int = 60) -> str:
        """Compact reference for the `unreal` Python API: a class's methods and editor properties, an enum's
        values, or one member's documentation.

        Args:
            name: Class or Class.member, e.g. EditorActorSubsystem or Actor.set_actor_location.
            match: Case-insensitive regex on member names.
            limit: Maximum number of lines.

        Returns:
            One line per member ('name(args) -> result' or 'name: Type'), or the member's docstring.
        """
        cls_name, _, member = name[7:].partition(".") if name.startswith("unreal.") else name.partition(".")
        target = getattr(unreal, cls_name, None)
        if target is None:
            import difflib
            close = difflib.get_close_matches(cls_name, [n for n in dir(unreal) if not n.startswith("_")], n=5)
            raise RuntimeError(f"unreal has no '{cls_name}'" + (f" (did you mean: {', '.join(close)})" if close else ""))
        if member:
            properties = dict(_DOC_PROPERTY.findall(getattr(target, "__doc__", "") or ""))
            if not hasattr(target, member) and member in properties:
                line = next(l for l in (target.__doc__ or "").splitlines() if l.startswith(f"- ``{member}``"))
                return re.sub(r"^- ``(\w+)``", r"\1", line)
            attr = getattr(target, member, None)
            if attr is None:
                raise RuntimeError(f"unreal.{cls_name} has no member '{member}'")
            lines = [l.rstrip() for l in (attr.__doc__ or "(no documentation)").strip().splitlines()]
            return "\n".join(lines[:limit])
        rx = re.compile(match, re.I) if match else None
        doc = (getattr(target, "__doc__", "") or "").strip().splitlines()
        head = f"unreal.{cls_name}"
        if isinstance(target, type):
            bases = [b.__name__ for b in target.__mro__[1:5] if b.__name__ not in ("object", "_ObjectBase", "_WrapperBase")]
            head += f" ({' < '.join(bases)})" if bases else ""
        rows = [head + (": " + doc[0][:120] if doc else "")]
        if isinstance(target, type) and issubclass(target, unreal.EnumBase):
            values = [n for n in dir(target) if n.isupper() and (not rx or rx.search(n))]
            return _capped(rows[0], ["  " + ", ".join(values)], limit)
        for attr_name in sorted(n for n in dir(target) if not n.startswith("_")):
            if rx and not rx.search(attr_name):
                continue
            attr = getattr(target, attr_name, None)
            if callable(attr) and not isinstance(attr, type):
                first = ((attr.__doc__ or "").strip().splitlines() or [attr_name + "(...)"])[0]
                rows.append("  " + re.sub(r"^\w+\.", "", first)[:140])
        for prop, kind in _DOC_PROPERTY.findall(getattr(target, "__doc__", "") or ""):
            if not rx or rx.search(prop):
                rows.append(f"  {prop}: {kind}")
        return _capped(rows[0], rows[1:], limit)

    @_tool
    @staticmethod
    def find(query: str = "", cls: str = "", under: str = "/Game", limit: int = 50) -> str:
        """Search the asset registry by name and/or class.

        Args:
            query: Case-insensitive regex on the asset name ('' for any).
            cls: Case-insensitive regex on the asset class name, e.g. 'StaticMesh|Material$'.
            under: Content folder to search recursively.
            limit: Maximum number of rows.

        Returns:
            A count header, then 'path  Class' rows.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        name_rx, cls_rx = re.compile(query or ".", re.I), re.compile(cls or ".", re.I)
        rows = []
        for data in registry.get_assets_by_path(under.rstrip("/") or "/Game", recursive=True):
            name, kind = str(data.asset_name), str(data.asset_class_path.asset_name)
            if name_rx.search(name) and cls_rx.search(kind):
                rows.append((str(data.package_name), kind))
        rows.sort()
        what = " ".join(p for p in (f"/{query}/" if query else "", f"class /{cls}/" if cls else "") if p)
        return _capped(f"{len(rows)} assets under {under}" + (f" matching {what}" if what else ""),
                       [f"{p}  {k}" for p, k in rows], limit)

    @_tool
    @staticmethod
    def refs(asset: str, deps: bool = False, limit: int = 50) -> str:
        """What references an asset (or, with deps, what it depends on), from the asset registry.

        Args:
            asset: Package path, e.g. /Game/Props/SM_Rock (an object path also works).
            deps: List dependencies instead of referencers.
            limit: Maximum number of rows.

        Returns:
            A header, then one package per line (/Script modules counted, not listed).
        """
        package = asset.split(".")[0]
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        if not registry.get_assets_by_package_name(package):
            raise RuntimeError(f"no asset at {package} (ue find)")
        found = (registry.get_dependencies if deps else registry.get_referencers)(package, options) or []
        names = sorted({str(n) for n in found} - {package})
        assets = [n for n in names if not n.startswith("/Script/")]
        scripts = len(names) - len(assets)
        # World Partition saves each placed actor as its own package under __ExternalActors__; report those
        # per level ("/Game/Maps/L: 12 placed actors") instead of as dozens of opaque ids.
        levels, rows = {}, []
        for name in assets:
            match = re.match(r"^(/[^/]+)/__ExternalActors__/(.+)/[^/]+/[^/]+/[^/]+$", name)
            if match:
                level = f"{match.group(1)}/{match.group(2)}"
                levels[level] = levels.get(level, 0) + 1
            else:
                rows.append(name)
        rows += [f"{level}: {count} placed actor{'' if count == 1 else 's'}" for level, count in sorted(levels.items())]
        head = (f"{package} depends on {len(assets)} assets" if deps else f"{len(assets)} assets reference {package}")
        if scripts:
            head += f" (+{scripts} /Script modules)"
        return _capped(head, rows, limit)

    @_tool
    @staticmethod
    def bp_check(path: str = "/Game", warnings: bool = False) -> str:
        """Compile every Blueprint under a content path and report the ones that do not compile cleanly.

        Args:
            path: Content folder to scan recursively, e.g. /Game or /Game/Characters.
            warnings: Also report Blueprints that compile with warnings.

        Returns:
            JSON: {"checked": N, "problems": [{"path": ..., "status": "error"|"warnings"}]}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        assets = registry.get_assets_by_path(path.rstrip("/") or "/Game", recursive=True)
        names = sorted({str(a.package_name) for a in assets
                        if str(a.asset_class_path.asset_name).endswith("Blueprint")})
        problems = []
        for name in names:
            bp = unreal.load_asset(name)
            if not isinstance(bp, unreal.Blueprint):
                continue
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            status = bp.get_editor_property("status")
            if status == unreal.BlueprintStatus.BS_ERROR:
                problems.append({"path": name, "status": "error"})
            elif warnings and status == unreal.BlueprintStatus.BS_UP_TO_DATE_WITH_WARNINGS:
                problems.append({"path": name, "status": "warnings"})
        return json.dumps({"checked": len(names), "problems": problems})

    @_tool
    @staticmethod
    def pie(action: str, simulate: bool = False) -> str:
        """Play-In-Editor through LevelEditorSubsystem (works on UE 5.x without Epic's EditorAppToolset).

        Start and stop are requests that take effect on a later tick; poll with action 'status'.

        Args:
            action: start, stop or status.
            simulate: With start, Simulate-In-Editor instead of play.

        Returns:
            'running' or 'not running' for status; 'requested' otherwise.
        """
        level = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if action == "status":
            return "running" if level.is_in_play_in_editor() else "not running"
        if action == "start":
            level.editor_play_simulate() if simulate else level.editor_request_begin_play()
        elif action == "stop":
            level.editor_request_end_play()
        else:
            raise RuntimeError("action must be start, stop or status")
        return "requested"

    @_tool
    @staticmethod
    def cs_reload() -> str:
        """Reload pending UnrealSharp C# changes now, with modal dialogs suppressed.

        Returns:
            reloaded, not reloaded (failed or paused), nothing pending, busy, pie or unavailable.
        """
        return _csharp().reload_csharp()

    @_tool
    @staticmethod
    def cs_state() -> str:
        """UnrealSharp C# hot-reload state.

        Returns:
            idle, pending, reloading or unavailable.
        """
        return _csharp().csharp_reload_state()

    @_tool
    @staticmethod
    def reload() -> str:
        """Hot-reload the tether Python package and re-register its toolsets on the next editor tick.

        Returns:
            Confirmation that the reload was scheduled.
        """
        _schedule_reload()
        return "reload scheduled"


registration = Registration([TetherCore]) if toolset_registry else None
