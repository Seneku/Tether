"""Tether gap-filling toolset, registered into Epic's ToolsetRegistry.

Fills what Epic's 5.8 toolsets leave out for coding agents: unrestricted editor
Python, console commands, and compact (token-frugal) reads. Pure Python, so
`reload` picks up edits without restarting the editor.

On UE 5.4-5.7 there is no ToolsetRegistry: the same functions load as a plain class
and the ue CLI calls them over Python remote execution instead of MCP.
"""
import ast
import contextlib
import functools
import inspect
import io
import json
import math
import os
import stat
import re
import sys
import time
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


def _play_saved():
    """Where the play settings a multiplayer `ue pie start --clients` replaced are kept (see pie_net)."""
    return os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                        "Tether", "play-settings.json")


_RAW = {}  # tool name -> plain Python function, for callers outside MCP (Python remote execution)


def _tool(fn):
    # The registry turns tools into native bindings that report exceptions as script warnings and return
    # an empty value, so remote execution calls the plain function from _RAW to see errors.
    raw = fn.__func__ if isinstance(fn, staticmethod) else fn
    if "token" in inspect.signature(raw).parameters:  # tools that change things: under the revision-control guard
        raw = _guarded(raw)
        fn = staticmethod(raw) if isinstance(fn, staticmethod) else raw
    _RAW[raw.__name__] = raw
    return toolset_registry.tool_call(fn) if toolset_registry else fn


BULK = "+bulk"  # appended to the session token (ue --bulk): allow more check-outs than the bulk limit
_GUARD = {"depth": 0, "bulk": False, "claimed": 0}  # claimed: files checked out so far in this call


def _guarded(raw):
    """Wrap a tool that changes things in the revision-control guard. Under a check-out provider (Perforce,
    Plastic), the files of packages it newly modified are checked out at once, so teammates see them locked
    while the work goes on. An edit to a file someone else has checked out, to one behind the depot, or to
    more files than the bulk limit (without --bulk) is undone and refused: it could not be submitted."""
    signature = inspect.signature(raw)

    @functools.wraps(raw)
    def call(*args, **kwargs):
        if _GUARD["depth"]:  # a tool calling another: the outer call guards both
            return raw(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)  # the registry passes arguments by position
        token = bound.arguments.get("token")
        bulk = isinstance(token, str) and token.endswith(BULK)
        if bulk:
            bound.arguments["token"] = token[:-len(BULK)]
        _GUARD.update(depth=1, bulk=bulk, claimed=0)
        try:
            vcs = _Vcs()
            before = vcs.snapshot() if vcs.checkouts else None
            result = raw(*bound.args, **bound.kwargs)
            if before is not None:
                vcs.claim(before)
            return result
        finally:
            _GUARD.update(depth=0, bulk=False, claimed=0)
    call.__signature__ = signature
    return call


_MAX_OUT = 20000

# Tether's own edits, newest last, with the undo-history depth right after each; stashed on sys like below.
_UNDO = getattr(sys, "_tether_undo", None) or []
sys._tether_undo = _UNDO

# Persistent REPL namespace for `py`; stashed on sys so it survives hot reloads.
_NAMESPACE = getattr(sys, "_tether_ns", None) or {"unreal": unreal, "__name__": "__tether_py__"}
sys._tether_ns = _NAMESPACE
_NAMESPACE["tether"] = sys.modules.get("tether")  # tether.world(), player(), console() without an import


_PY_OUTPUT = "<<printed before the error>>"


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
        printed = buf.getvalue()  # after a marker, so the CLI can show it with its newlines (errors get compacted)
        raise RuntimeError(f"{type(exc).__name__}: {exc}{where}" + (f"\n{_PY_OUTPUT}\n{printed}" if printed else "")) from None
    out = buf.getvalue()
    if value is not None:
        out += value if isinstance(value, str) else repr(value)
    if len(out) > _MAX_OUT:
        out = out[:_MAX_OUT] + f"\n... truncated {len(out) - _MAX_OUT} chars"
    return out


def _pie_worlds():
    """{PIE instance index: world} for every running PIE world (UEDPIE_<n>_<map>)."""
    worlds = {}
    for world in unreal.ObjectIterator(unreal.World):
        package = _try(lambda: world.get_outermost().get_name(), "")
        match = re.match(r"UEDPIE_(\d+)_", package.rsplit("/", 1)[-1])  # the package carries the prefix
        if match and _try(lambda: world.get_name(), "") == package.rsplit("/", 1)[-1][len(match.group(0)):]:
            worlds[int(match.group(1))] = world
    return worlds


def _game_world():
    """The PIE world the tools act on: the one `ue pie target` picked, else the editor's (the first)."""
    target = getattr(sys, "_tether_pie_target", None)
    if target is not None:
        world = _pie_worlds().get(target)
        if world is not None:
            return world
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
        if "." not in ref.rsplit("/", 1)[-1]:  # a package path names its asset: /Game/X/A -> /Game/X/A.A
            ref = f"{ref}.{ref.rsplit('/', 1)[-1]}"
        obj = unreal.find_object(None, ref)
        if obj is None:
            with contextlib.suppress(Exception):
                obj = unreal.load_asset(ref)
        if obj is not None:
            return obj
    titled = re.fullmatch(r"(.+?) \((\w+)\)(?:\.(.+))?", ref)  # 'Label (Name)[.Component]', as _label prints
    matches = []
    if titled:  # a label with brackets of its own ("Crate (2)") falls through to the plain lookup
        actor_ref, comp_ref = titled.group(2), titled.group(3) or ""
        matches = [a for a in _match_actors(actor_ref, pie) if a.get_name().lower() == actor_ref.lower()]
    if not matches:
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
    found = _component_named(actor, comps, comp_ref)
    if found is not None:
        return found
    raise RuntimeError(f"{actor.get_actor_label()} has no component '{comp_ref}' (has: "
                       + ", ".join(c.get_name() for c in comps) + ")")


def _short_path(path):
    if not path:
        return "None"
    package, dot, name = path.rpartition(".")
    if dot and ":" not in name and package.rsplit("/", 1)[-1] == name:  # /Game/X/A.A -> /Game/X/A
        return package
    return path.split(":PersistentLevel.", 1)[-1]


def _actor_label(actor):
    """An actor's label, except in PIE when it is the automatic one (SDRobotCharacter3 for SDRobotCharacter_3, as
    actors spawned at runtime get): then its object name, which Python, logs and the game's own code use."""
    label, name = actor.get_actor_label(), actor.get_name()
    if label != name and label == re.sub(r"_(\d+)$", r"\1", name) and "UEDPIE_" in actor.get_outermost().get_name():
        return name
    return label


def _actor_title(actor):
    """'Label (Name)' when an actor's label and object name differ (`ue ai` and the outliner show labels; Python,
    logs and `pie watch` use names), else the one of them."""
    label, name = _actor_label(actor), actor.get_name()
    return label if label == name else f"{label} ({name})"


def _label(obj):
    if obj.get_name().startswith("Default__"):  # a class default object: its "actor label" is meaningless
        return _short_path(obj.get_path_name())
    if isinstance(obj, unreal.Actor):
        return _actor_title(obj)
    if isinstance(obj, unreal.ActorComponent) and obj.get_owner():
        return f"{_actor_title(obj.get_owner())}.{obj.get_name()}"
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


_SECRET = re.compile(r"token|password|secret|api_?key|credential", re.I)


def _masked(name, value):
    """A credential-like property's value is never printed (e.g. Fab's CustomAuthToken)."""
    if _SECRET.search(name or "") and value not in (None, "", "None", "null"):
        return f"(hidden: {len(str(value))} chars)"
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
        raise RuntimeError("unauthorized: pass the session token from Saved/Tether/editors/<pid>.token "
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
        if text.startswith("/") and "." not in text.rsplit("/", 1)[-1] and ":" not in text:
            text = f"{text}.{text.rsplit('/', 1)[-1]}"  # a package path names its asset: /Game/X/A -> /Game/X/A.A
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


def _visible_bounds(actor, game=False):
    """(centre, extent) of the actor's visible primitive components. GetActorBounds counts hidden ones too (a hidden
    laser mesh left at the world origin moved a robot's centre 37 m); with none visible, the actor's location."""
    lo = hi = None
    for comp in actor.get_components_by_class(unreal.PrimitiveComponent) or []:
        if not _try(lambda: comp.is_visible(), True) or (game and _try(lambda: comp.get_editor_property("hidden_in_game"), False)):
            continue
        bounds = _try(lambda: unreal.SystemLibrary.get_component_bounds(comp))
        if not bounds:
            continue
        origin, extent = bounds[0], bounds[1]
        if extent.x <= 0 and extent.y <= 0 and extent.z <= 0:
            continue
        a, b = origin - extent, origin + extent
        lo = a if lo is None else unreal.Vector(min(lo.x, a.x), min(lo.y, a.y), min(lo.z, a.z))
        hi = b if hi is None else unreal.Vector(max(hi.x, b.x), max(hi.y, b.y), max(hi.z, b.z))
    if lo is None:
        return actor.get_actor_location(), unreal.Vector(0, 0, 0)
    return (lo + hi) * 0.5, (hi - lo) * 0.5


def _mesh_outline(actor):
    """The four XY corners of a StaticMeshActor's mesh box, turned and scaled with the actor; None otherwise."""
    comp = _try(lambda: actor.static_mesh_component)
    mesh = _try(lambda: comp.get_editor_property("static_mesh")) if comp else None
    box = _try(lambda: mesh.get_bounding_box()) if mesh else None
    if box is None:
        return None
    xf = comp.get_world_transform()
    lo, hi = box.min, box.max
    corners = [xf.transform_location(unreal.Vector(x, y, (lo.z + hi.z) / 2))
               for x, y in ((lo.x, lo.y), (hi.x, lo.y), (hi.x, hi.y), (lo.x, hi.y))]
    return [[round(c.x, 1), round(c.y, 1)] for c in corners]


def _component_named(actor, comps, ref):
    """A component by its object name (CharMoveComp0), that name without the trailing number, the actor property
    that holds it (CharacterMovement, as the C++ and Blueprints call it), or its class (CharacterMovementComponent)."""
    want = ref.lower()
    for test in (lambda c: c.get_name().lower() == want,
                 lambda c: re.sub(r"_?\d+$", "", c.get_name()).lower() == want):
        hit = next((c for c in comps if test(c)), None)
        if hit is not None:
            return hit
    held = _try(lambda: actor.get_editor_property(_snake(ref)))
    if isinstance(held, unreal.ActorComponent):
        return held
    classes = [c for c in comps if c.get_class().get_name().lower() in (want, want + "component")]
    return classes[0] if len(classes) == 1 else None


def _point_and_extent(ref, pie):
    """An actor's (or component owner's) visible bounds centre and extent."""
    obj = _resolve(ref, pie)
    actor = obj if isinstance(obj, unreal.Actor) else (obj.get_owner() if isinstance(obj, unreal.ActorComponent) else None)
    if actor is None:
        raise RuntimeError(f"'{ref}' is not an actor or component")
    return _visible_bounds(actor, pie)


def _point(ref, pie):
    """'x,y,z' or an actor reference (its bounds centre) as an unreal.Vector."""
    nums = _numbers(ref)
    if nums is not None:
        if len(nums) != 3:
            raise RuntimeError(f"expected x,y,z, got '{ref}'")
        return unreal.Vector(*nums)
    return _point_and_extent(ref, pie)[0]


_DOC_PROPERTY = re.compile(r"^- ``(\w+)`` \(([^)]*)\)", re.M)
_DOC_ARG = re.compile(r"^\s+(\w+) \(([\w.]+)\):", re.M)  # 'Args:' lines of a method's docstring
_STRUCT_ORDER = {"Rotator": ("pitch", "yaw", "roll"), "Vector": ("x", "y", "z"), "Vector2D": ("x", "y"),
                 "Vector4": ("x", "y", "z", "w"), "LinearColor": ("r", "g", "b", "a"), "Color": ("r", "g", "b", "a")}


def _struct_arg(kind, value, param):
    """A JSON value as the struct a parameter takes: a dict with C++ field names (Yaw, X, bSweep) or Python ones,
    or a list in UE order ([pitch, yaw, roll], [x, y, z]). A field the struct does not have is an error: Python's
    own conversion drops it silently and passes zeros."""
    cls = getattr(unreal, kind, None)
    if not (isinstance(cls, type) and issubclass(cls, unreal.StructBase)) or isinstance(value, cls):
        return value
    fields = [n for n, _ in _DOC_PROPERTY.findall(cls.__doc__ or "")]
    if isinstance(value, (list, tuple)):
        order = _STRUCT_ORDER.get(kind)
        if not order or len(value) != len(order):
            raise RuntimeError(f"{param} is a {kind}: give " + (f"[{', '.join(order)}]" if order else
                               "an object with its fields (" + ", ".join(fields) + ")"))
        value = dict(zip(order, value))
    if not isinstance(value, dict):
        return value
    lookup = {n.replace("_", "").lower(): n for n in fields}
    struct = cls()
    for key, v in value.items():
        flat = key.replace("_", "").lower()
        name = lookup.get(flat) or (lookup.get(flat[1:]) if flat.startswith("b") else None)  # bSweep -> sweep
        if name is None:
            raise RuntimeError(f"{param} ({kind}) has no field '{key}' (fields: {', '.join(fields)})")
        struct.set_editor_property(name, v)
    return struct


def _call_args(obj, function, args):
    """`call` arguments converted to the function's parameter types, read from its Python docstring."""
    method = None
    for name in (_snake(function), _snake(re.sub(r"^K2_", "", function))):
        method = _try(lambda n=name: getattr(obj, n))
        if callable(method):
            break
    params = _DOC_ARG.findall(getattr(method, "__doc__", "") or "") if callable(method) else []
    out = []
    for i, value in enumerate(args):
        if i < len(params) and isinstance(value, (dict, list)):
            value = _struct_arg(params[i][1].split(".")[-1], value, params[i][0])
        elif isinstance(value, dict):  # no signature to go by: at least the Python field names
            value = {_snake(k).lower(): v for k, v in value.items()}
        out.append(value)
    return tuple(out)


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
            rows.append(f"{name}: {schema[name]} = {_short(_masked(name, text(obj.get_editor_property(name))))}")
        except Exception:  # write-only or unreadable in this context
            rows.append(f"{name}: {schema[name]} = ?")
    cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
    head = f"{_label(obj)} ({cls.get_name()}, {len(schema)} props" + (f", {len(names)} match" if match else "") + ")"
    return _capped(head, rows, limit)


_LEVELS = getattr(sys, "_tether_levels", None) or {}  # map -> {actor path: (label, class, pose)} at load/save
sys._tether_levels = _LEVELS


def _pose_key(actor):
    loc, rot, scl = actor.get_actor_location(), actor.get_actor_rotation(), actor.get_actor_scale3d()
    return tuple(round(v, 1) for v in (loc.x, loc.y, loc.z, rot.pitch, rot.yaw, rot.roll, scl.x, scl.y, scl.z))


def snapshot_level(level=None):
    """Remember the edited level's actors, as the baseline `changes` compares with (at map load and save)."""
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        return
    level = level or world.get_path_name().split(".")[0]
    _LEVELS[level] = {a.get_path_name(): (_actor_label(a), a.get_class().get_name(), _pose_key(a))
                      for a in _authored_actors()}


def _authored_actors():
    """The edited level's actors minus World Partition HLOD actors, which the editor generates (and spawns
    after a map loads) rather than anyone placing them."""
    return [a for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
            if not a.get_class().get_name().startswith("WorldPartitionHLOD")]


def _try(fn, default=None):
    """fn(), or `default` if the engine raises (properties and helpers vary between engine versions)."""
    try:
        return fn()
    except Exception:
        return default


def _record_undo(before, label):
    """Note an editor-transacted change of ours for `ue undo`, if the editor recorded it."""
    if _LEGACY:
        return
    count = unreal.ToolsetLibrary.get_active_undo_count()
    if count > before:
        _UNDO.append({"count": count, "label": label})


def _reflect(text, obj):
    """Parse ToolsetLibrary JSON; it returns '' for an object in a transient state (e.g. just after a rename)."""
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"the editor returned no property data for {_label(obj)} (it may be mid-load or "
                           "mid-rename; retry, or save and reload it)") from None


def _dirty_packages():
    utils = unreal.EditorLoadingAndSavingUtils
    maps = [p.get_name() for p in utils.get_dirty_map_packages()]
    content = [p.get_name() for p in utils.get_dirty_content_packages()]
    return maps, content


_MISSING = object()
# Bookkeeping that differs between any two loads and says nothing about the asset's content.
_DIFF_NOISE = {"actorGuid", "actorInstanceGuid", "contentBundleGuid", "externalDataLayerAsset", "bIsEditorPreviewActor",
               "thumbnailInfo", "status", "shouldCookPropertyGuidsValue", "blueprintGuid"}


def _props_json(obj):
    cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
    names = [n for n in _reflect(unreal.ToolsetLibrary.list_struct_properties(cls), obj) if n not in _DIFF_NOISE]
    return _reflect(unreal.ToolsetLibrary.get_object_properties(obj, names), obj) if names else {}


def _rebase(value, old_root, new_root):
    """References into the old copy's own package, rewritten to the new package so they compare equal."""
    if isinstance(value, str):
        return new_root + value[len(old_root):] if value.startswith(old_root) and value[len(old_root):][:1] in ("", ".", ":") \
            else value
    if isinstance(value, list):
        return [_rebase(v, old_root, new_root) for v in value]
    if isinstance(value, dict):
        return {k: _rebase(v, old_root, new_root) for k, v in value.items()}
    return value


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= 1e-4 * max(1.0, abs(a), abs(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _diff_value(path, a, b, out):
    if _same(a, b):
        return
    numeric = lambda v: isinstance(v, dict) and v and all(isinstance(x, (int, float)) for x in v.values())
    if isinstance(a, dict) and isinstance(b, dict) and set(a) != {"refPath"} and not (numeric(a) and numeric(b)):
        for key in list(a) + [k for k in b if k not in a]:
            _diff_value(f"{path}.{key}", a.get(key, _MISSING), b.get(key, _MISSING), out)
        return
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and len(a) <= 64:
        for i, (x, y) in enumerate(zip(a, b)):
            _diff_value(f"{path}[{i}]", x, y, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        out.append(f"{path}: {len(a)} -> {len(b)} items")
        return
    if a is _MISSING:
        out.append(f"+ {path} = {_compact(b)}")
    elif b is _MISSING:
        out.append(f"- {path} (was {_compact(a)})")
    else:
        out.append(f"{path}: {_compact(a)} -> {_compact(b)}")


def _diff_props(label, old, new, roots, out, limit=40, skip=()):
    lines = []
    a = _rebase(_round(_props_json(old)), *roots)
    b = _round(_props_json(new))
    for key in [k for k in list(a) + [k for k in b if k not in a] if k.lower() not in skip]:
        _diff_value(key, a.get(key, _MISSING), b.get(key, _MISSING), lines)
    if lines:
        out.append(f"{label}:")
        out.extend("  " + line for line in lines[:limit])
        if len(lines) > limit:
            out.append(f"  ... +{len(lines) - limit} more")


def _bp_parts(bp):
    """A Blueprint's class defaults and component templates by variable name."""
    generated = unreal.BlueprintEditorLibrary.generated_class(bp)
    parts = {"defaults": unreal.get_default_object(generated)} if generated else {}
    subsystem = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    for handle in subsystem.k2_gather_subobject_data_for_blueprint(bp) or []:
        data = lib.get_data(handle)
        obj = lib.get_object(data)
        name = str(lib.get_variable_name(data))
        if obj is not None and name and name != "None":
            parts[f"component {name}"] = obj
    return parts


def _actor_parts(actor):
    parts = {"actor": actor}
    for comp in actor.get_components_by_class(unreal.ActorComponent):
        if not _try(lambda: comp.get_editor_property("is_editor_only"), False):  # billboards, arrows: editor state
            parts[f"component {comp.get_name()}"] = comp
    return parts


def _diff_parts(old_parts, new_parts, roots, out):
    added = [k for k in new_parts if k not in old_parts]
    removed = [k for k in old_parts if k not in new_parts]
    if added:
        out.append("added: " + ", ".join(added))
    if removed:
        out.append("removed: " + ", ".join(removed))
    # A Blueprint's class defaults hold a reference per component; the component lines already say it.
    components = {k.split(" ", 1)[1].lower() for k in added + removed if k.startswith("component ")}
    for key in [k for k in new_parts if k in old_parts]:
        _diff_props(key, old_parts[key], new_parts[key], roots, out, skip=components)


def _level_actors(world):
    """The live actors in a map package (open or just loaded), by object name, and the names of those the
    editor would save (None when every one would: a map loaded from disk has no transient actors)."""
    package = world.get_outermost()
    actors = {a.get_name(): a for a in unreal.ObjectIterator(unreal.Actor) if a.get_outermost() == package
              and unreal.SystemLibrary.is_valid(a) and not a.get_name().startswith("Default__")}
    editor_world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    saved = None
    if editor_world is not None and editor_world.get_outermost() == package:
        # The open map also holds transient actors (nav data, debug managers); the editor's list leaves them out.
        saved = {a.get_name() for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()}
    return actors, saved


def _table_rows(table):
    rows = json.loads(unreal.DataTableFunctionLibrary.export_data_table_to_json_string(table) or "[]")
    return {str(r.get("Name")): {k: v for k, v in r.items() if k != "Name"} for r in rows}


_MAT_KINDS = ("scalar", "vector", "texture", "switch")


def _mat_names(material):
    lib = unreal.MaterialEditingLibrary
    getters = {"scalar": lib.get_scalar_parameter_names, "vector": lib.get_vector_parameter_names,
               "texture": lib.get_texture_parameter_names, "switch": lib.get_static_switch_parameter_names}
    return {kind: [str(n) for n in (_try(lambda: getters[kind](material), []) or [])] for kind in _MAT_KINDS}


def _mat_value(material, kind, name):
    lib = unreal.MaterialEditingLibrary
    if isinstance(material, unreal.MaterialInstance):
        get = {"scalar": lib.get_material_instance_scalar_parameter_value,
               "vector": lib.get_material_instance_vector_parameter_value,
               "texture": lib.get_material_instance_texture_parameter_value,
               "switch": lib.get_material_instance_static_switch_parameter_value}[kind]
    else:
        get = {"scalar": lib.get_material_default_scalar_parameter_value,
               "vector": lib.get_material_default_vector_parameter_value,
               "texture": lib.get_material_default_texture_parameter_value,
               "switch": lib.get_material_default_static_switch_parameter_value}[kind]
    return get(material, name)


def _mat_text(value):
    if isinstance(value, unreal.LinearColor):
        return "(" + ", ".join(f"{v:.4g}" for v in (value.r, value.g, value.b, value.a)) + ")"
    if isinstance(value, unreal.Object):
        return _short_path(value.get_path_name())
    if isinstance(value, float):
        return f"{value:.6g}"
    return "None" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def _mat_parse(kind, text):
    raw = text.strip()
    if kind == "scalar":
        nums = _numbers(raw)
        if not nums or len(nums) != 1:
            raise ValueError(f"expected a number, got '{text}'")
        return float(nums[0])
    if kind == "vector":
        nums = _numbers(raw)
        if not nums or len(nums) not in (3, 4):
            raise ValueError(f"expected r,g,b[,a], got '{text}'")
        return unreal.LinearColor(*[float(n) for n in nums] + ([1.0] if len(nums) == 3 else []))
    if kind == "switch":
        flag = {"true": True, "1": True, "on": True, "false": False, "0": False, "off": False}.get(raw.lower())
        if flag is None:
            raise ValueError(f"expected true/false, got '{text}'")
        return flag
    if raw.lower() in ("", "none", "null"):
        return None
    texture = unreal.load_asset(raw.split(".")[0])
    if not isinstance(texture, unreal.Texture):
        raise ValueError(f"no texture at {raw} (ue find {raw.rsplit('/', 1)[-1]} --class Texture)")
    return texture


def _material_of(target):
    obj = _resolve(target)
    if not isinstance(obj, unreal.MaterialInterface):
        raise RuntimeError(f"{_label(obj)} is a {obj.get_class().get_name()}, not a material or material instance"
                           + (" (for an actor, `ue mat <actor>` lists its materials)" if isinstance(obj, unreal.Actor) else ""))
    return obj


def _pie_player():
    game = _game_world()
    if game is None:
        raise RuntimeError("PIE is not running (ue pie start; ue pie do starts it for you)")
    pawn = unreal.GameplayStatics.get_player_pawn(game, 0)
    controller = unreal.GameplayStatics.get_player_controller(game, 0)
    if pawn is None or controller is None:
        raise RuntimeError("no player pawn (Simulate has none; use play)")
    return game, pawn, controller


def _pie_object(ref, pawn, controller):
    """'player'/'pawn', 'controller', or an actor/component in the PIE world."""
    key = ref.lower()
    if key in ("player", "pawn"):
        return pawn
    if key == "controller":
        return controller
    if key.startswith(("player.", "pawn.")):
        comp = key.split(".", 1)[1]
        for c in pawn.get_components_by_class(unreal.ActorComponent):
            if c.get_name().lower() == comp:
                return c
        raise RuntimeError(f"the player pawn has no component '{ref.split('.', 1)[1]}'")
    return _resolve(ref, pie=True)


def _simulating(game):
    """Simulate-In-Editor (the level viewport shows the simulation) rather than Play. Both have a player
    controller; Simulate's possesses a SpectatorPawn that is also its view target. A Play session whose
    controller spectates while a camera actor is the view target (a title screen, a showroom) is Play."""
    controller = unreal.GameplayStatics.get_player_controller(game, 0)
    if controller is None or unreal.GameplayStatics.get_player_camera_manager(game, 0) is None:
        return True
    pawn = unreal.GameplayStatics.get_player_pawn(game, 0)
    view = _try(lambda: controller.get_view_target())
    return isinstance(pawn, unreal.SpectatorPawn) and view in (None, pawn)


def _player_target(ref):
    """'player', 'pawn', 'controller' (and 'player.Component') mean the PIE player's, whatever --pie says: not
    the editor actors whose label happens to contain 'player'. None for any other reference."""
    if ref.split(".", 1)[0].lower() not in ("player", "pawn", "controller"):
        return None
    if _game_world() is None:
        raise RuntimeError(f"'{ref}' is the PIE player's: PIE is not running (ue pie start)")
    _, pawn, controller = _pie_player()
    return _pie_object(ref, pawn, controller)


def _pie_point(ref, pawn, controller):
    nums = _numbers(ref)
    if nums is not None:
        if len(nums) != 3:
            raise RuntimeError(f"expected x,y,z, got '{ref}'")
        return unreal.Vector(*nums)
    obj = _pie_object(ref, pawn, controller)
    actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
    return _visible_bounds(actor, True)[0]


def _input_action(name, pawn):
    if name.startswith("/"):
        action = unreal.load_asset(name.split(".")[0])
        if isinstance(action, unreal.InputAction):
            return action
        raise RuntimeError(f"no InputAction at {name}")
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    found = [a for a in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputAction"))
             if str(a.asset_name).lower() == name.lower()]
    if found:
        return unreal.load_asset(str(found[0].package_name))
    # Actions can also be created in C++ as subobjects of the character or its controller
    # (CreateDefaultSubobject<UInputAction>), or at runtime with NewObject (a player controller building its own
    # mapping context): look on both, then at every live InputAction.
    controller = _try(lambda: pawn.get_controller()) if pawn else None
    for owner in (o for o in (pawn, controller) if o is not None):
        action = _try(lambda: owner.get_editor_property(name))
        if isinstance(action, unreal.InputAction):
            return action
        action = _try(lambda: unreal.find_object(owner, name))
        if isinstance(action, unreal.InputAction):
            return action
    live = [a for a in unreal.ObjectIterator(unreal.InputAction) if a.get_name().lower() == name.lower()]
    if live:  # one inside the PIE world (UEDPIE_...) first: others may be left from an earlier run
        return sorted(live, key=lambda a: "UEDPIE" not in a.get_path_name())[0]
    names = sorted(str(a.asset_name) for a in
                   registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputAction")))
    runtime = sorted({a.get_name() for a in unreal.ObjectIterator(unreal.InputAction)} - set(names))
    raise RuntimeError(f"no InputAction '{name}' (assets: {', '.join(names[:12])}"
                       + (f"; created at runtime: {', '.join(runtime[:12])}" if runtime else "")
                       + "; or a property/subobject of the pawn or its controller)")


def _input_subsystem(game):
    for subsystem in unreal.ObjectIterator(unreal.EnhancedInputLocalPlayerSubsystem):
        if _try(lambda: subsystem.get_world()) == game:
            return subsystem
    raise RuntimeError("no Enhanced Input subsystem for the PIE player (does the project use Enhanced Input?)")


# Held input actions, re-injected every tick: FInputActionValue has no Python-settable fields, so the
# subsystem's own continuous injection cannot be given a value. Stashed on sys to survive hot reloads.
_HELD = getattr(sys, "_tether_held", None) or {"actions": {}, "handle": None}
sys._tether_held = _HELD


def _hold_tick(_delta):
    game = _game_world()
    if game is None or not _HELD["actions"]:
        _HELD["actions"].clear()
        unreal.unregister_slate_post_tick_callback(_HELD["handle"])
        _HELD["handle"] = None
        return
    for action, vector in list(_HELD["actions"].values()):
        _try(lambda: _input_subsystem(game).inject_input_vector_for_action(action, vector, [], []))


def _hold(action, vector):
    _HELD["actions"][action.get_path_name()] = (action, vector)
    if _HELD["handle"] is None:
        _HELD["handle"] = unreal.register_slate_post_tick_callback(_hold_tick)


def _release(action):
    _HELD["actions"].pop(action.get_path_name(), None)


# `ue pie watch`: properties sampled on the editor tick (CLI round trips are too slow for 10 Hz). Stashed on sys
# like _HELD, so a hot reload neither loses a running watch nor leaves a second callback behind.
_WATCH = getattr(sys, "_tether_watch", None) or {"handle": None}
sys._tether_watch = _WATCH
_WATCH_LIMIT = 20000


# `ue pie loop`: console commands and waits run from the editor tick, on a schedule kept from its start, so a
# soak neither drifts nor pays a CLI round trip per command. Stashed on sys like _WATCH.
_LOOP = getattr(sys, "_tether_loop", None) or {"handle": None}
sys._tether_loop = _LOOP


def _loop_steps(script):
    """[("wait", seconds) | ("cmd", text)] from 'cmd; wait 5; cmd' (also newlines)."""
    steps = []
    for part in (p.strip() for p in script.replace("\n", ";").split(";")):
        if not part:
            continue
        verb, _, rest = part.partition(" ")
        if verb.lower() == "wait":
            try:
                steps.append(("wait", max(0.0, float(rest))))
            except ValueError:
                raise RuntimeError(f"'{part}': wait takes seconds, e.g. wait 2.5")
        else:
            steps.append(("cmd", part))
    return steps


def _loop_run(state, command):
    try:
        if _networked(state["world"]):  # as pie_step's exec: RPCs need the player's own console
            _queue_command(state["world"], command)
        else:
            unreal.SystemLibrary.execute_console_command(state["world"], command)
        state["ran"] += 1
    except Exception as exc:
        state["errors"] = (state["errors"] + [f"{command}: {exc}"])[-20:]


def _loop_end(state, why):
    state["ended"] = why
    state["elapsed"] = round(time.time() - state["start"], 2)
    if state.get("handle") is not None:
        unreal.unregister_slate_post_tick_callback(state["handle"])
        state["handle"] = None


def _loop_tick(_delta):
    state = _LOOP
    game = _game_world()
    if game is None or game != state.get("world"):
        return _loop_end(state, "PIE ended")
    now = time.time()
    if state["end"] and now >= state["end"]:
        return _loop_end(state, "done")
    while state["every_steps"] and now >= state["every_at"]:
        for _, command in state["every_steps"]:
            _loop_run(state, command)
        state["every_runs"] += 1
        state["every_at"] += state["every"]  # from the schedule, not from now: no drift
    budget = 64  # commands per tick at most, should a script without waits come round again
    while budget and now >= state["next"]:
        kind, value = state["steps"][state["index"]]
        if kind == "wait":
            state["next"] += value
        else:
            _loop_run(state, value)
            budget -= 1
        state["index"] += 1
        if state["index"] >= len(state["steps"]):
            state["index"], state["passes"] = 0, state["passes"] + 1
            if not state["end"]:  # no --seconds: one pass
                return _loop_end(state, "done")


def _loop_status(state):
    return {k: state.get(k) for k in ("ran", "passes", "every_runs", "errors", "ended")} | {
        "elapsed": state.get("elapsed") if state.get("ended") else round(time.time() - state.get("start", time.time()), 2),
        "seconds": state.get("seconds"), "running": state.get("handle") is not None}


def _watch_value(value):
    """A sampled value as JSON: numbers and booleans as is, vectors/rotators as lists, the rest as short text."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, list) and all(isinstance(v, (int, float)) for v in value):
        return value
    if isinstance(value, unreal.Vector):
        return [round(value.x, 1), round(value.y, 1), round(value.z, 1)]
    if isinstance(value, unreal.Rotator):
        return [round(value.pitch, 1), round(value.yaw, 1), round(value.roll, 1)]
    if isinstance(value, dict) and set(value) >= {"x", "y", "z"}:
        return [round(value["x"], 1), round(value["y"], 1), round(value["z"], 1)]
    if isinstance(value, unreal.Object):
        return _label(value)
    if isinstance(value, unreal.EnumBase):
        return str(value.name)
    if isinstance(value, (unreal.StructBase,)):
        value = _try(lambda: value.to_dict(), str(value))
    return _short(_compact(value) if isinstance(value, (dict, list, tuple)) else str(value), 80)


_SCHEMAS = {}  # (class path, visible only) -> (time, schema text), for reads repeated during PIE


def _pie_schema(cls, visible_only=True):
    """Epic's property schema for a class, kept for a minute: `pie watch` and `expect` read the same classes
    many times, and each schema build logs a warning per delegate property (LogJson ... unhandled)."""
    key, now = (cls.get_path_name(), visible_only), time.time()
    cached = _SCHEMAS.get(key)
    if cached and now - cached[0] < 60:
        return cached[1]
    library = unreal.ToolsetLibrary
    text = library.list_struct_properties(cls) if visible_only else library.list_struct_properties(cls, False)
    _SCHEMAS[key] = (now, text)
    return text


def _dump_props(obj):
    """[(class, name, value text)] for every UPROPERTY of an object, Blueprint-visible or not, as the engine
    exports them (`obj dump`): Python and Epic's reflection only reach the visible ones."""
    rows, owner, arrays = [], "", {}
    for line in _console_output(None, [f"obj dump {obj.get_path_name()}"]):
        head = re.match(r"^=== (\w+) properties ===", line)
        if head:
            owner = head.group(1)
            continue
        m = re.match(r"^  (\w+)(?:\((\d+)\))?=(.*)$", line)  # arrays come one element per line: Racers(0)=(...)
        if m and owner:
            if m.group(2) is None:
                rows.append((owner, m.group(1), m.group(3)))
            else:
                if m.group(1) not in arrays:
                    arrays[m.group(1)] = []
                    rows.append((owner, m.group(1), arrays[m.group(1)]))
                arrays[m.group(1)].append(m.group(3))
    # an array reads as '[3] (first), (second), ...' (what fits), so `ue props` and `expect` can see it
    return [(o, n, f"[{len(v)}] " + ", ".join(v) if isinstance(v, list) else v) for o, n, v in rows]


def _text_value(text):
    """A number from exported text when it is one ('3', '0.500000'), else the text."""
    nums = _numbers(text) if text is not None else None
    if nums and len(nums) == 1 and re.fullmatch(r"-?[\d.]+(?:[eE][-+]?\d+)?", text.strip()):
        return nums[0]
    return text


def _hidden_reader(obj, name, every=0.5):
    """Read a UPROPERTY Python cannot see through the engine's `getall Class Prop name=Obj` (one log line), at
    most every `every` seconds: it goes through the log."""
    cls, world, cache = obj.get_class().get_name(), obj.get_world(), {"t": 0.0, "v": None}
    # Components share names across actors (every robot has a "Health"), so filter on the outer too and only take
    # the line with this object's own path.
    pattern = re.compile(re.escape(obj.get_path_name()) + r"\." + re.escape(name) + r" = (.*)$", re.I)
    outer = obj.get_outer()
    where = f"name={obj.get_name()}" + (f" outer={outer.get_name()}" if outer and not isinstance(obj, unreal.Actor) else "")

    def read():
        if time.time() - cache["t"] >= every:
            cache["t"] = time.time()
            for line in _console_output(world, [f"getall {cls} {name} {where}"]):
                m = pattern.search(line)
                if m:
                    cache["v"] = _text_value(m.group(1).strip())
        return cache["v"]
    return read


def _snake(name):
    """'GetControlRotation' -> 'get_control_rotation' (how Python names Unreal functions)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def _watch_reader(spec, pawn, controller):
    """A function reading one watched value: 'Actor' (its location), 'Actor.Prop', 'player.Comp.Prop', or an
    input action ('IA_Look': its value, worked out from the keys behind it)."""
    if "." not in spec and spec.upper().startswith("IA_"):
        action = _action_reader(spec, controller)
        if action is None:
            raise RuntimeError(f"no input action {spec} (ue input)")
        size = {"boolean": 1, "axis1d": 1, "axis2d": 2}.get(action.value_type.lower(), 3)
        return lambda: (lambda v: v[0][0] if size == 1 else v[0][:size])(action())
    if "@" in spec:  # 'player.Mesh@hand_r', 'BP_Robot_C_1@Hips': a bone or socket's world location
        target, bone = spec.split("@", 1)
        try:
            obj = _pie_object(target, pawn, controller)
        except RuntimeError:  # 'player.Mesh': the component by its property name (CharacterMesh0)
            owner, _, prop = target.rpartition(".")
            obj = _try(lambda: _pie_object(owner, pawn, controller).get_editor_property(prop)) if owner else None
            if obj is None:
                raise
        mesh = obj if isinstance(obj, unreal.SceneComponent) else \
            (_try(lambda: obj.get_components_by_class(unreal.SkeletalMeshComponent), []) or [None])[0]
        if mesh is None:
            raise RuntimeError(f"{target} has no skeletal mesh for @{bone}")
        if not mesh.does_socket_exist(bone):
            names = [str(n) for n in _try(lambda: mesh.get_all_socket_names(), []) or []][:12]
            raise RuntimeError(f"{target} has no bone or socket '{bone}' (some: {', '.join(names)})")
        return lambda: mesh.get_socket_location(bone)
    target, _, prop = spec.rpartition(".")
    if not target:
        target, prop = spec, "location"
    obj = _pie_object(target, pawn, controller)
    key = prop.lower()
    if key in ("location", "rotation"):
        actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
        return lambda: actor.get_actor_location() if key == "location" else actor.get_actor_rotation()
    if key in ("control", "controlrotation"):  # protected on AController: through its getter
        ctrl = obj if isinstance(obj, unreal.Controller) else _try(lambda: obj.get_controller())
        if ctrl is None:
            raise RuntimeError(f"{_label(obj)} has no controller (watch player.control or a controller)")
        return lambda: ctrl.get_control_rotation()
    if prop.endswith("()"):  # a getter: player.GetVelocity(), Comp.GetCurrentHealth()
        method = _try(lambda: getattr(obj, _snake(prop[:-2])))
        if not callable(method):
            raise RuntimeError(f"{_label(obj)} has no function {prop[:-2]} Python can call (ue api {obj.get_class().get_name()})")
        return method
    library = getattr(unreal, "ToolsetLibrary", None)
    name = prop
    if library:
        schema = _reflect(_pie_schema(obj.get_class(), False), obj)  # every UPROPERTY, hidden too
        name = {n.lower(): n for n in schema}.get(key)
        if name is None:
            getter = _try(lambda: getattr(obj, "get_" + _snake(prop)))
            if callable(getter):  # e.g. a protected property with a public getter
                return getter
            raise RuntimeError(f"{_label(obj)} has no property '{prop}' (a getter works too: {prop}() calls get_...)")
    if _try(lambda: obj.get_editor_property(name), _WATCH) is not _WATCH:
        return lambda: obj.get_editor_property(name)
    # Epic's reflection reads the visible ones (asking it for a hidden one logs a script error that fails the call).
    visible = {n.lower() for n in _reflect(_pie_schema(obj.get_class()), obj)} if library else set()
    if library and name.lower() in visible:
        return lambda: _reflect(library.get_object_properties(obj, [name]), obj).get(name)
    # Not Blueprint-visible: neither Python nor Epic's reflection reads it, the engine's own export does.
    return _hidden_reader(obj, _cpp_property_name(name))


def _cpp_property_name(name):
    """The C++ name of a property from reflection's camelCase one: 'currentHealth' -> 'CurrentHealth', but bools
    keep their b ('bTelegraphing')."""
    return name if re.match(r"b[A-Z]", name) else name[:1].upper() + name[1:]


def _watch_tick(_delta):
    state = _WATCH
    game = _game_world()
    if game is None or game != state.get("world"):
        state["ended"] = state.get("ended") or "PIE ended"
        unreal.unregister_slate_post_tick_callback(state["handle"])
        state["handle"] = None
        return
    now = time.time()
    if now - state["last"] < state["every"] or len(state["samples"]) >= _WATCH_LIMIT:
        return
    state["last"] = now
    if now - state.get("rebind", 0) > 1.0:  # 'player...' follows the possessed pawn (spawned or swapped later)
        state["rebind"] = now
        pawn = _try(lambda: unreal.GameplayStatics.get_player_pawn(game, 0))
        controller = _try(lambda: unreal.GameplayStatics.get_player_controller(game, 0))
        for i, spec in enumerate(state["specs"]):
            head = spec.split(".", 1)[0].lower()
            if head in ("player", "pawn", "controller") and pawn is not None and state["bound"][i] is not pawn:
                state["readers"][i] = _try(lambda: _watch_reader(spec, pawn, controller))
                state["bound"][i] = pawn
    row = [round(now - state["start"], 3), round(unreal.GameplayStatics.get_time_seconds(game), 3)]
    for i, spec in enumerate(state["specs"]):
        reader = state["readers"][i]
        if reader is None and now - state["retry"][i] > 1.0:  # not there (yet): spawned later, or destroyed
            state["retry"][i] = now
            _, pawn, controller = _try(_pie_player, (None, None, None))
            state["readers"][i] = reader = _try(lambda: _watch_reader(spec, pawn, controller)) if pawn else None
        value = None
        if reader is not None:
            try:
                raw = reader()
                if i not in state.setdefault("kinds", {}) and isinstance(raw, (unreal.Rotator, unreal.Vector)):
                    state["kinds"][i] = "rotator" if isinstance(raw, unreal.Rotator) else "vector"
                value = _watch_value(raw)
            except Exception:  # the object went away mid-session
                state["readers"][i] = None
        row.append(value)
    state["samples"].append(row)


# `ue pie record`: the keys of every mapping context (Python cannot read Enhanced Input action values, but the
# controller's key state is public), the pawn's pose and the camera, sampled on the editor tick.
_REC = getattr(sys, "_tether_rec", None) or {"handle": None}
sys._tether_rec = _REC
_REC_LIMIT = 50000
_LOOK_KEYS = re.compile(r"^(Mouse(X|Y|2D|WheelAxis)|Gamepad_Right(X|Y|2D)|MotionController)", re.I)


def _mapping_rows(imc):
    holder = _try(lambda: imc.get_editor_property("default_key_mappings"))
    mappings = _try(lambda: holder.get_editor_property("mappings"), None) if holder else None
    return mappings if mappings is not None else _try(lambda: imc.get_editor_property("mappings"), [])


def _modifier_text(modifier):
    """A modifier as the converter needs it: negate (with axes), swizzle (with order), scalar (with factor)."""
    kind = modifier.get_class().get_name().replace("InputModifier", "").lower()
    if kind == "negate":
        axes = "".join(a for a in "xyz" if _try(lambda: modifier.get_editor_property(a), True))
        return f"negate:{axes}"
    if kind == "swizzleaxis":
        order = str(_try(lambda: modifier.get_editor_property("order"), "YXZ")).split(".")[-1].split(":")[0]
        return f"swizzle:{order.upper()}"
    if kind == "scalar":
        s = _try(lambda: modifier.get_editor_property("scalar"))
        return f"scalar:{s.x:g},{s.y:g},{s.z:g}" if s else "scalar"
    return kind


def _apply_input_modifiers(value, modifiers):
    """A key's raw (x, y, z) through Enhanced Input modifiers: negate, swizzle, scalar and dead zone are applied;
    others (smoothing, response curves, FOV scaling) are listed by the caller but not modelled."""
    x = list(value)
    for mod in modifiers:
        kind = mod.get_class().get_name().replace("InputModifier", "").lower()
        get = lambda name, default=None: _try(lambda: mod.get_editor_property(name), default)  # noqa: E731
        if kind == "negate":
            x = [-v if get(a, True) else v for v, a in zip(x, "xyz")]
        elif kind == "swizzleaxis":
            order = str(get("order", "YXZ")).split(".")[-1].split(":")[0].upper()
            x = [x["XYZ".index(c)] for c in order]
        elif kind == "scalar":
            sc = get("scalar")
            x = [v * f for v, f in zip(x, (sc.x, sc.y, sc.z))] if sc else x
        elif kind == "deadzone":
            low, high = float(get("lower_threshold", 0.2)), float(get("upper_threshold", 1.0))
            radial = "radial" in str(get("type", "")).lower()

            def remap(v):
                return 0.0 if abs(v) < low else (1 if v > 0 else -1) * min(1.0, (abs(v) - low) / max(1e-6, high - low))
            if radial:
                size = (x[0] ** 2 + x[1] ** 2 + x[2] ** 2) ** 0.5
                x = [v / size * remap(size) for v in x] if size else x
            else:
                x = [remap(v) for v in x]
    return x


def _action_reader(name, controller):
    """A function giving an input action's value as Enhanced Input combines it, worked out from the live state of
    its mapped keys through each mapping's modifiers (Python cannot read action values themselves; triggers are
    not modelled). Returns ([x, y, z], [(key, raw)]) per call."""
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    found = [d for d in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputAction"))
             if str(d.asset_name).lower() == name.lower()]
    if not found:
        return None
    action = found[0].get_asset()
    action_mods = list(_try(lambda: action.get_editor_property("modifiers"), []) or [])
    mappings = []
    for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputMappingContext")):
        imc = _try(lambda: data.get_asset())
        for row in (_mapping_rows(imc) or []) if imc else []:
            if _try(lambda: row.get_editor_property("action")) == action:
                key = row.get_editor_property("key")
                kind = "2d" if _try(lambda: unreal.InputLibrary.key_is_axis2d(key), False) else \
                    "1d" if _try(lambda: unreal.InputLibrary.key_is_analog(key), False) else "button"
                mappings.append((key, str(key.get_editor_property("key_name")), kind,
                                 list(_try(lambda: row.get_editor_property("modifiers"), []) or [])))

    def read():
        combined, sources = [0.0, 0.0, 0.0], []
        for key, key_name, kind, mods in mappings:
            if kind == "2d":
                v = _try(lambda: controller.get_input_vector_key_state(key))
                raw = [v.x, v.y, v.z] if v else [0.0, 0.0, 0.0]
            elif kind == "1d":
                raw = [float(_try(lambda: controller.get_input_analog_key_state(key), 0.0)), 0.0, 0.0]
            else:
                raw = [1.0 if _try(lambda: controller.is_input_key_down(key), False) else 0.0, 0.0, 0.0]
            if any(raw):
                sources.append((key_name, [round(r, 3) for r in raw if r] or [0]))
            out = _apply_input_modifiers(_apply_input_modifiers(raw, mods), action_mods)
            combined = [c if abs(c) >= abs(o) else o for c, o in zip(combined, out)]  # highest absolute value
        return [round(c, 4) for c in combined], sources
    read.value_type = str(_try(lambda: action.get_editor_property("value_type"), "")).split(".")[-1].split(":")[0]
    read.mappings = [(k, kind, [m.get_class().get_name().replace("InputModifier", "") for m in mods])
                     for _, k, kind, mods in mappings]
    return read


def _rec_tick(_delta):
    state = _REC
    game = _game_world()
    if game is None or game != state.get("world"):
        state["ended"] = state.get("ended") or "PIE ended"
        unreal.unregister_slate_post_tick_callback(state["handle"])
        state["handle"] = None
        return
    controller, pawn = state["controller"], unreal.GameplayStatics.get_player_pawn(game, 0)
    now = time.time() - state["start"]
    events = state["events"]
    if len(events) >= _REC_LIMIT:
        return
    for name, key, analog in state["keys"]:
        if analog:
            if analog == "2d":
                value = _try(lambda: controller.get_input_vector_key_state(key))
                vec = (round(value.x, 2), round(value.y, 2)) if value else (0.0, 0.0)
            else:
                vec = (round(_try(lambda: controller.get_input_analog_key_state(key), 0.0), 2), 0.0)
            last = state["analog"].get(name, (0.0, 0.0))
            if abs(vec[0] - last[0]) > 0.15 or abs(vec[1] - last[1]) > 0.15 or (vec != last and vec == (0.0, 0.0)):
                state["analog"][name] = vec
                events.append([round(now, 3), "axis", name, list(vec)])
            continue
        down = bool(_try(lambda: controller.is_input_key_down(key), False))
        was = name in state["down"]
        if down and not was:
            state["down"].add(name)
            events.append([round(now, 3), "down", name])
        elif not down and was:
            state["down"].discard(name)
            events.append([round(now, 3), "up", name])
        elif not down and _try(lambda: controller.was_input_key_just_pressed(key), False):
            events.append([round(now, 3), "down", name])  # pressed and released between two ticks
            events.append([round(now, 3), "up", name])
    if pawn and now - state["last_pose"] >= state["every"]:
        state["last_pose"] = now
        camera = unreal.GameplayStatics.get_player_camera_manager(game, 0)
        loc, rot = pawn.get_actor_location(), controller.get_control_rotation()
        eye = camera.get_camera_location() if camera else loc
        events.append([round(now, 3), "pose", [round(loc.x), round(loc.y), round(loc.z)],
                       [round(rot.pitch, 1), round(rot.yaw, 1)], [round(eye.x), round(eye.y), round(eye.z)]])


_MASK_SUFFIX = re.compile(r"_(ORM|ARM|MRA|RMA|ORD|Masks?|AO|Rough(ness)?|Metal(lic)?|Height)$", re.I)


def _dependency_options():
    return unreal.AssetRegistryDependencyOptions(
        include_soft_package_references=True, include_hard_package_references=True, include_searchable_names=False,
        include_soft_management_references=False, include_hard_management_references=False)


_GAIN_NAME = re.compile(r"^(volume|gain|volume_?multiplier|volume_?scale)$", re.I)


def _ref_paths(value, wanted, path="", out=None, gain=None):
    """{package: [(property path, gain)]} where `value` (props JSON) references one of `wanted` packages: 'FireSound',
    'Cues[Explosion].Sounds[2]' (map entries by their key). `gain` is a Volume-like number in the nearest struct
    around the reference (e.g. FSoundCue{Sounds, Volume}), which is what the game multiplies the sound by."""
    out = {} if out is None else out
    if isinstance(value, dict):
        ref = value.get("refPath") if set(value) == {"refPath"} else None
        if isinstance(ref, str):
            package = ref.split(".")[0]
            if package in wanted:
                out.setdefault(package, []).append((path or "(self)", gain))
            return out
        here = next((v for k, v in value.items() if _GAIN_NAME.match(str(k)) and isinstance(v, (int, float))), gain)
        for key, item in value.items():
            _ref_paths(item, wanted, f"{path}.{key}" if path else str(key), out, here)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict) and "key" in item and "value" in item:  # a map entry
                key = item["key"]
                key = key.get("refPath", "").rsplit(".", 1)[-1] if isinstance(key, dict) else key
                _ref_paths(item["value"], wanted, f"{path}[{key}]", out, gain)
            else:
                _ref_paths(item, wanted, f"{path}[{i}]", out, gain)
    elif isinstance(value, str) and value.split(".")[0] in wanted:  # soft references come back as text
        out.setdefault(value.split(".")[0], []).append((path, gain))
    return out


def _referencing_props(package, wanted):
    """{sound package: [property paths]} in a data asset or a Blueprint's defaults; {} for anything else."""
    asset = _try(lambda: unreal.load_asset(package))
    if asset is None:
        return {}
    if isinstance(asset, unreal.Blueprint):
        generated = _try(lambda: unreal.BlueprintEditorLibrary.generated_class(asset))
        asset = _try(lambda: unreal.get_default_object(generated)) if generated else None
    if asset is None or isinstance(asset, (unreal.SoundBase, unreal.World)):
        return {}
    data = _try(lambda: _props_json(asset), {}) or {}
    return _ref_paths(data, wanted)


_SOUND_SKIP = {"World", "MapBuildDataRegistry", "Texture2D", "TextureCube", "TextureRenderTarget2D", "Material",
               "MaterialInstanceConstant", "MaterialFunction", "StaticMesh", "SkeletalMesh", "Skeleton", "PhysicsAsset",
               "AnimSequence", "AnimMontage", "BlendSpace", "Font", "LandscapeLayerInfoObject"}
_AUDIO_LOG = {"handle": None}


def _wave_row(registry, package):
    """{channels, duration, sample_rate, looping} of a SoundWave from its registry tags (nothing loaded)."""
    for data in registry.get_assets_by_package_name(package) or []:
        if str(data.asset_class_path.asset_name) != "SoundWave":
            continue
        tag = lambda key: _try(lambda: str(data.get_tag_value(key)), None)  # noqa: E731
        num = lambda key: _try(lambda: float(tag(key)), None)  # noqa: E731
        return {"channels": int(num("NumChannels") or 0) or None, "duration": num("Duration"),
                "sample_rate": int(num("SampleRate") or 0) or None, "looping": tag("bLooping") == "True"}
    return None


def _audio_resolved(comp):
    """What the mixer uses for a playing component: spatialized?, attenuation (where from), sound class, waves."""
    get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731
    sound = get(comp, "sound")
    settings, source = None, None
    if get(comp, "override_attenuation", False):
        settings, source = get(comp, "attenuation_overrides"), "component override"
    elif get(comp, "attenuation_settings") is not None:
        asset = get(comp, "attenuation_settings")
        settings, source = get(asset, "attenuation"), asset.get_name()
    elif sound is not None and get(sound, "override_attenuation", False):
        settings, source = get(sound, "attenuation_overrides"), "on the sound"
    elif sound is not None and get(sound, "attenuation_settings") is not None:
        asset = get(sound, "attenuation_settings")
        settings, source = get(asset, "attenuation"), asset.get_name()
    attenuate = bool(get(settings, "attenuate", True)) if settings is not None else False
    spatial = settings is not None and bool(get(settings, "spatialize", True)) and bool(get(comp, "allow_spatialization", True))
    falloff = get(settings, "falloff_distance") if settings is not None else None
    sound_class = get(comp, "sound_class_override") or (get(sound, "sound_class_object") if sound is not None else None)
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    waves = []
    if sound is not None:
        package = sound.get_outermost().get_name()
        row = _wave_row(registry, package)
        if row:
            waves.append(dict(row, wave=sound.get_name()))
        else:  # a cue or MetaSound: the waves it can play
            for dep in registry.get_dependencies(package, _dependency_options()) or []:
                row = _wave_row(registry, str(dep))
                if row:
                    waves.append(dict(row, wave=str(dep).rsplit("/", 1)[-1]))
    return {"sound": sound.get_name() if sound else None, "sound_path": sound.get_path_name() if sound else None,
            "spatialized": spatial, "attenuated": settings is not None and attenuate, "attenuation": source,
            "falloff_m": round(falloff / 100, 1) if falloff else None,
            "stereo_spread": get(settings, "stereo_spread") if settings is not None else None,
            "sound_class": sound_class.get_name() if sound_class else None, "waves": waves[:16]}


def _audio_log_tick(_delta):
    state = _AUDIO_LOG
    game = _game_world()
    if game is None or game != state.get("world"):
        state["ended"] = state.get("ended") or "PIE ended"
        unreal.unregister_slate_post_tick_callback(state["handle"])
        state["handle"] = None
        return
    now = time.time()
    if now - state["last"] < state["every"]:
        return
    state["last"] = now
    camera = unreal.GameplayStatics.get_player_camera_manager(game, 0)
    listener = camera.get_camera_location() if camera else None
    playing = {}
    for comp in unreal.ObjectIterator(unreal.AudioComponent):
        if _try(lambda: comp.get_world()) != game or not _try(lambda: comp.is_playing(), False):
            continue
        key = comp.get_path_name()
        sound = _try(lambda: comp.get_editor_property("sound"))
        sound_key = sound.get_path_name() if sound else None
        playing[key] = sound_key
        live = state["live"].get(key)
        if live is not None and _try(lambda: comp.is_virtualized(), False):
            state["starts"][live[0]]["virtualized"] = True
        if state["playing"].get(key) == sound_key or len(state["starts"]) >= 2000:
            continue  # still the same sound on the same component
        row = _try(lambda: _audio_resolved(comp), None) or {"sound": sound.get_name() if sound else None}
        owner = comp.get_owner()
        where = comp.get_world_location()
        row.update(t=round(now - state["start"], 2), owner=_label(owner) if owner else None,
                   attached=owner is not None and not isinstance(owner, unreal.WorldSettings),
                   location=[round(where.x), round(where.y), round(where.z)],
                   distance=round(where.distance(listener)) if listener else None)
        state["starts"].append(row)
        state["live"][key] = (len(state["starts"]) - 1, now)
    for key, (index, began) in list(state["live"].items()):
        if key not in playing:  # stopped: right away (a concurrency limit, or code) when far shorter than the sound
            del state["live"][key]
            start = state["starts"][index]
            length = max((w.get("duration") or 0 for w in start.get("waves", [])), default=0)
            if now - began <= max(0.15, 2 * state["every"]) and length > 0.4:
                start["cut"] = True
    state["playing"] = playing  # a component that stops and plays again counts as a new start


def _fixup_redirector(package, vcs):
    """Point everything that references a redirector at its target, then delete it. Python has no
    fixup_referencers in 5.8, so do what it does: load each referencer (which follows the redirector) and save
    it (which stores the target). Returns (referencers saved, problems)."""
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    referencers = sorted({str(n) for n in registry.get_referencers(package, _dependency_options()) or []} - {package})
    files = [_package_file(r) for r in referencers]
    problems, _, _ = vcs.prepare(files)
    if problems:
        return 0, problems
    saved = 0
    for ref in referencers:  # external actor packages have no asset of their own name, so save packages
        pkg = _try(lambda: unreal.load_package(ref))
        if pkg and unreal.EditorLoadingAndSavingUtils.save_packages([pkg], False):
            saved += 1
        else:
            problems.append(f"could not resave {ref}")
    left = sorted({str(n) for n in registry.get_referencers(package, _dependency_options()) or []} - {package})
    if left:
        problems.append(f"{package} is still referenced by {', '.join(left[:3])}")
        return saved, problems
    # Delete the file itself: Python cannot see redirector objects (no unreal.ObjectRedirector), and
    # EditorAssetLibrary.delete_asset only marks the package deleted in memory, so it would come back.
    file = _package_file(package)
    issues, _, _ = vcs.prepare([file])
    if issues:
        return saved, problems + issues
    loaded = unreal.find_package(package)
    if loaded:
        unreal.EditorLoadingAndSavingUtils.unload_packages([loaded])
    try:
        os.remove(file)
    except OSError as exc:
        return saved, problems + [f"could not delete {file}: {exc}"]
    vcs.deleted([file])
    unreal.AssetRegistryHelpers.get_asset_registry().scan_files_synchronous([file], True)
    return saved, problems


def _texture_dims(texture):
    """'2048 x 2048' as built for this platform. GetSizeX/Y read the platform data, which is a 32 x 32 placeholder
    while the texture still compiles in the background (right after an import); the built size is worked out
    from the source and settings without waiting."""
    size = _try(lambda: texture.blueprint_get_built_texture_size())
    if size is not None and size.x:
        return f"{size.x:.0f} x {size.y:.0f}"
    return f"{texture.blueprint_get_size_x()} x {texture.blueprint_get_size_y()}"


def _set_lods(mesh, lods):
    """Generate `lods` LODs for a static mesh, each with half the triangles of the one before."""
    Options = getattr(unreal, "StaticMeshReductionOptions", None) or unreal.EditorScriptingMeshReductionOptions
    Settings = getattr(unreal, "StaticMeshReductionSettings", None) or unreal.EditorScriptingMeshReductionSettings
    settings = []
    for i in range(lods):
        s = Settings()
        s.set_editor_property("percent_triangles", 0.5 ** i)
        s.set_editor_property("screen_size", 1.0 if i == 0 else 0.5 ** i)
        settings.append(s)
    options = Options()
    options.set_editor_property("auto_compute_lod_screen_size", True)
    options.set_editor_property("reduction_settings", settings)
    unreal.EditorStaticMeshLibrary.set_lods(mesh, options)


_IMPORT_TAG = "TetherImport"  # asset metadata: the options `ue import` applied, re-applied on reimport


def _after_import(asset, opts):
    """Apply import options to a freshly imported asset and describe it in one line."""
    kept = {k: v for k, v in opts.items() if k in ("collision", "lods", "nanite", "texture_group") and v is not None}
    if kept:
        unreal.EditorAssetLibrary.set_metadata_tag(asset, _IMPORT_TAG, json.dumps(kept))
    if isinstance(asset, unreal.StaticMesh):
        lib = unreal.EditorStaticMeshLibrary
        collision = (opts.get("collision") or "").lower()
        if collision:
            lib.remove_collisions(asset)
            if collision in ("box", "sphere", "capsule"):
                lib.add_simple_collisions(asset, getattr(unreal.ScriptCollisionShapeType, collision.upper()))
            elif collision == "convex":
                lib.set_convex_decomposition_collisions(asset, 4, 16, 100000)
            elif collision == "complex":
                setup = asset.get_editor_property("body_setup")
                setup.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
            elif collision != "none":
                raise RuntimeError(f"collision must be none, box, sphere, capsule, convex or complex, not '{collision}'")
        if opts.get("nanite") is not None:
            nanite = asset.get_editor_property("nanite_settings")
            nanite.set_editor_property("enabled", bool(opts["nanite"]))
            asset.set_editor_property("nanite_settings", nanite)
        lods = int(opts.get("lods") or 0)
        if lods > 1:
            _set_lods(asset, lods)
        tris = _try(lambda: lib.get_number_verts(asset, 0), 0)
        nanite_on = _try(lambda: asset.get_editor_property("nanite_settings").enabled, False)
        complexity = str(_try(lambda: lib.get_collision_complexity(asset), "")).split(".")[-1].split(":")[0]
        simple = _try(lambda: lib.get_simple_collision_count(asset), 0) + _try(lambda: lib.get_convex_collision_count(asset), 0)
        box = _try(lambda: asset.get_bounding_box())
        size = f", {box.max.x - box.min.x:.0f} x {box.max.y - box.min.y:.0f} x {box.max.z - box.min.z:.0f} cm" if box else ""
        return (f"{tris} vertices, {asset.get_num_lods()} LOD(s)" + (", Nanite" if nanite_on else "") + size
                + f", collision {simple} shape(s)" + (" + complex as simple" if "COMPLEX_AS_SIMPLE" in complexity else ""))
    if isinstance(asset, unreal.Texture2D):
        group = opts.get("texture_group")
        if group:
            key = "TEXTUREGROUP_" + re.sub(r"(?<!^)(?=[A-Z])", "_", group).upper().replace("TEXTUREGROUP__", "")
            value = getattr(unreal.TextureGroup, key, None)
            if value is None:
                raise RuntimeError(f"no texture group '{group}' (World, WorldNormalMap, Character, UI, Effects, ...)")
            asset.set_editor_property("lod_group", value)
        # the name says packed or single-channel data; the importer guesses from pixels (blue-ish reads as normal map)
        mask = bool(_MASK_SUFFIX.search(asset.get_name())) and asset.get_editor_property("compression_settings") in (
            unreal.TextureCompressionSettings.TC_DEFAULT, unreal.TextureCompressionSettings.TC_NORMALMAP)
        if mask:  # packed or single-channel data: linear, not colour
            asset.set_editor_property("srgb", False)
            asset.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
        compression = str(asset.get_editor_property("compression_settings")).split(".")[-1].split(":")[0]
        lod_group = str(asset.get_editor_property("lod_group")).split(".")[-1].split(":")[0].replace("TEXTUREGROUP_", "")
        return (f"{_texture_dims(asset)}, {compression}, sRGB "
                f"{asset.get_editor_property('srgb')}, group {lod_group}" + (" (mask: set linear)" if mask else ""))
    if isinstance(asset, unreal.SoundWave):
        return f"{asset.get_editor_property('duration'):.2f} s, {asset.get_editor_property('num_channels')} channel(s)"
    return ""


# `ue perf load`: each frame's wall-clock time and length, and whether World Partition streaming is done.
_FRAMES = getattr(sys, "_tether_frames", None) or {"handle": None}
sys._tether_frames = _FRAMES


def _frame_tick(_delta):
    state = _FRAMES
    now = time.time()
    game = _game_world()
    # The time since this callback last ran, not the engine's delta: that is measured as a frame starts, so a
    # stall would show up one frame late, after the loads that caused it.
    last, state["last"] = state.get("last") or now, now
    if len(state["frames"]) < 200000:
        state["frames"].append([round(now, 4), round(now - last, 5), game is not None])
    if game is not None and now - state["last_stream"] >= 0.25:  # cheap, but no need to ask every frame
        state["last_stream"] = now
        subsystem = state.get("subsystem")
        if subsystem is None or _try(lambda: subsystem.get_world()) != game:
            subsystem = state["subsystem"] = next((s for s in unreal.ObjectIterator(unreal.WorldPartitionSubsystem)
                                                   if _try(lambda: s.get_world()) == game), None)
        done = _try(lambda: subsystem.is_all_streaming_completed()) if subsystem else None
        state["streaming"].append([round(now, 3), done])


_COMPARE = re.compile(r"^(?P<lhs>\S+)\s*(?P<op>==|!=|<=|>=|=|<|>|~)\s*(?P<rhs>.*)$")


_AXES = {"x", "y", "z", "pitch", "yaw", "roll"}


def _as_fields(value):
    """A vector or rotator as {x, y, z} / {pitch, yaw, roll}; anything else as it is."""
    if isinstance(value, unreal.Vector):
        return {"x": value.x, "y": value.y, "z": value.z}
    if isinstance(value, unreal.Rotator):
        return {"pitch": value.pitch, "yaw": value.yaw, "roll": value.roll}
    return value


def _expect_value(lhs, pawn, controller):
    """The value `expect`/`until` compare: what `pie watch` would sample for the same expression (a property,
    hidden ones included, a getter call like player.Movement.GetSpeedKmh(), Actor.Mesh@bone, an action), plus
    a part of a vector or rotator: player.location.z, player.GetActorRotation().yaw."""
    target, _, prop = lhs.rpartition(".")
    if not target:
        raise RuntimeError(f"'{lhs}': give target.property, e.g. player.Health or Door_1.bIsOpen")
    if prop.lower() in _AXES and "." in target:  # a part of a vector or rotator, unless it is a property called x
        try:
            base = _as_fields(_expect_value(target, pawn, controller))
        except RuntimeError:
            base = None
        if isinstance(base, dict) and prop.lower() in base:
            return base[prop.lower()]
    if prop.lower() in ("location", "rotation") and not prop.endswith(")"):
        obj = _pie_object(target, pawn, controller)
        return _actor_pose(obj if isinstance(obj, unreal.Actor) else obj.get_owner())[prop.lower()]
    return _as_fields(_watch_reader(lhs, pawn, controller)())


def _pie_expect(expr, pawn, controller):
    """'<target>.<prop> <op> <value>': (ok, actual text)."""
    match = _COMPARE.match(expr.strip())
    if not match:
        raise RuntimeError(f"expected '<target>.<property> <op> <value>' (op: = != < > <= >= ~), got '{expr}'")
    lhs, op, rhs = match.group("lhs"), match.group("op"), match.group("rhs").strip()
    value = _expect_value(lhs, pawn, controller)
    actual = str(value).lower() if isinstance(value, bool) else _compact(value)
    if op == "~":
        return bool(re.search(rhs, actual, re.I)), actual
    nums = _numbers(rhs)
    if isinstance(value, bool) or rhs.lower() in ("true", "false"):
        want = rhs.lower() in ("true", "1", "yes")
        ok = {"=": value == want, "==": value == want, "!=": value != want}.get(op)
        if ok is None:
            raise RuntimeError(f"'{op}' does not apply to true/false")
        return ok, actual
    if isinstance(value, (int, float)) and nums and len(nums) == 1:
        a, b = float(value), float(nums[0])
        ok = {"=": abs(a - b) < 1e-3, "==": abs(a - b) < 1e-3, "!=": abs(a - b) >= 1e-3,
              "<": a < b, ">": a > b, "<=": a <= b + 1e-3, ">=": a >= b - 1e-3}[op]
        return ok, actual
    if op in ("=", "==", "!="):
        same = actual.lower() == rhs.lower() or _short(value).lower() == rhs.lower()
        return same == (op != "!="), actual
    raise RuntimeError(f"'{op}' needs a number on both sides (value is {actual})")


def _settings(method, *args):
    """Call Epic's ConfigSettingsToolset (the editor's Project/Editor Settings registry) and decode its JSON."""
    tool = getattr(unreal, "ConfigSettingsToolset", None)
    if tool is None:
        raise RuntimeError("needs Epic's ConfigSettingsToolset plugin (UE 5.8+; ue init enables it, then restart the editor)")
    result = unreal.get_default_object(tool).call_method(method, args)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


_SECTIONS = getattr(sys, "_tether_sections", None) or []  # [(container, category, section)], once per session
sys._tether_sections = _SECTIONS


def _all_sections():
    if not _SECTIONS:
        for container in _settings("ListContainers") or []:
            for category in _settings("ListCategories", container) or []:
                for section in _settings("ListSections", container, category) or []:
                    _SECTIONS.append((container, category, section))
    return _SECTIONS


def _find_section(query):
    squash = lambda t: re.sub(r"[\s_]", "", t).lower()
    want = squash(query)
    sections = _all_sections()
    exact = [s for s in sections if want in (squash("/".join(s)), squash(s[2]), squash(f"{s[1]}/{s[2]}"))]
    found = exact or [s for s in sections if want in squash("/".join(s))]
    if not found:
        raise RuntimeError(f"no settings section matches '{query}' (ue config lists them)")
    if len(found) > 1:
        raise RuntimeError(f"'{query}' matches {len(found)} sections: " + ", ".join("/".join(s) for s in found[:10])
                           + " (give Container/Category/Section)")
    return found[0]


def _package_file(package, is_map=False):
    """A package's file on disk (existing, or where a new one will be written)."""
    base = unreal.PackageTools.package_name_to_filename(package)
    for ext in (".umap", ".uasset"):
        if base and os.path.isfile(base + ext):
            return base + ext
    return base + (".umap" if is_map else ".uasset") if base else ""


def _clean_copy(asset):
    """A /Temp copy of a saved asset, loaded from its file, for work that marks an asset modified (recompiling a
    material does). An asset with unsaved changes is returned as is: its file is not what the editor has."""
    package = asset.get_outermost().get_name()
    dirty = {p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    path = _package_file(package)
    if package in dirty or not package.startswith("/Game/") or not os.path.isfile(path):
        return asset
    import hashlib
    with open(path, "rb") as f:
        data = f.read()
    tag, name = hashlib.sha1(data).hexdigest()[:12], package.rsplit("/", 1)[-1]
    folder = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                          "Tether", "diff", tag)  # /Temp is mounted on Saved/
    if not os.path.isfile(os.path.join(folder, name + ".uasset")):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name + ".uasset"), "wb") as f:
            f.write(data)
    return unreal.load_asset(f"/Temp/Tether/diff/{tag}/{name}.{name}") or asset


def _norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def _holder(state):
    """Who has a file checked out: Perforce reports 'bob @ ' or 'bob @ bob_ws'."""
    return re.sub(r"\s*@\s*$", "", str(getattr(state, "checked_out_other", "") or "").strip()) or "someone else"


def _bulk_limit():
    try:
        path = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir()),
                            "Tether", "vcs.json")
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("bulk_limit", 20))
    except (OSError, ValueError, TypeError, AttributeError):
        return 20


def _package_label(package):
    """A package as people know it: a World Partition / One File Per Actor package by its actor's label."""
    if "/__ExternalActors__/" not in package:
        return package
    for actor in _try(_authored_actors, []):
        if actor.get_outermost().get_name() == package:
            return f"actor {_actor_label(actor)} ({package.rsplit('/', 1)[-1]})"
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    for data in _try(lambda: registry.get_assets_by_package_name(package), []) or []:
        label = _try(lambda: str(data.get_tag_value("ActorLabel") or ""), "")
        if label:
            return f"actor {label} ({package.rsplit('/', 1)[-1]})"
    return package


class _Vcs:
    """Check-outs through the editor's revision control provider (Perforce, Plastic, ...), silently: the
    editor's own prompts are modal dialogs. Git needs no check-outs, and new files are left for the user to
    stage. Without a provider, read-only files (an unconnected Perforce workspace) are refused up front,
    because saving them would ask in a dialog."""

    def __init__(self, sc=None):
        self.sc = sc or getattr(unreal, "SourceControl", None)
        try:
            self.provider = str(self.sc.current_provider()) if self.sc and self.sc.is_enabled() and \
                self.sc.is_available() else ""
        except Exception:
            self.provider = ""
        self.checkouts = self.provider not in ("", "None", "Git")
        self.bulk, self.refused_all = _GUARD["bulk"], False

    def error(self):
        return str(_try(lambda: self.sc.last_error_msg(), "")) or "see ue log"

    def states(self, files):
        """{normalized path: state} for files, in one query to the server."""
        got = self.sc.query_file_states(list(files), True) if files else []
        return {_norm_path(s.filename): s for s in got or []}

    def over_limit(self, count):
        """The refusal for checking out `count` more files in this call, or '' (the limit is
        Config/Tether/vcs.json {"bulk_limit": N}, 20 by default; --bulk lifts it)."""
        limit, total = _bulk_limit(), _GUARD["claimed"] + count
        if total <= limit or self.bulk:
            return ""
        return (f"this would check out {total} files, more than the bulk limit of {limit} (Config/Tether/vcs.json): "
                "they would be locked for the whole team. Say so to the team, then rerun with --bulk")

    def check_out(self, files):
        if files and not self.sc.check_out_files(files, True):
            raise RuntimeError(f"could not check out {len(files)} file(s): {self.error()}")
        _GUARD["claimed"] += len(files)

    def judge(self, packages):
        """(problems, files to check out) for editing these packages: a file checked out by someone else or
        behind the depot is a problem, and so are more files than the bulk limit."""
        files = {}
        for package in packages:
            path = _package_file(package) if not package.startswith("/Temp/") else ""
            if path and os.path.isfile(path):  # a new package is marked for add when it is saved
                files[package] = os.path.abspath(path)
        states, problems, want = self.states(files.values()), [], []
        for package, path in files.items():
            state = states.get(_norm_path(path))
            if state is None or not getattr(state, "is_source_controlled", False):
                continue
            if getattr(state, "is_checked_out_other", False):
                problems.append(f"{_package_label(package)} is checked out by {_holder(state)}")
            elif not getattr(state, "is_current", True):
                problems.append(f"{_package_label(package)} is behind the depot (someone submitted a newer revision: "
                                "sync it, then make the change on that)")
            elif getattr(state, "can_check_out", False) and not getattr(state, "is_checked_out", False):
                want.append(path)
        if not problems and self.over_limit(len(want)):
            problems.append(self.over_limit(len(want)))
        if len(problems) > 5:
            problems = problems[:5] + [f"+{len(problems) - 5} more"]
        return problems, want

    def reserve(self, packages):
        """Before editing known packages: raise if they can't be edited (see judge), else check them out now."""
        if self.checkouts:
            problems, want = self.judge(packages)
            if problems:
                raise RuntimeError("; ".join(problems) + ". Nothing was changed")
            self.check_out(want)

    def snapshot(self):
        """What is unsaved and how deep the undo history is, before a tool runs (see claim)."""
        maps, content = _dirty_packages()
        undo = None if _LEGACY else unreal.ToolsetLibrary.get_active_undo_count()
        return set(maps) | set(content), undo, len(_UNDO)

    def claim(self, before):
        """After a tool ran: check out the files of the packages it newly modified, or undo the change and
        raise if it can't be kept (see judge)."""
        maps, content = _dirty_packages()
        packages = sorted((set(maps) | set(content)) - before[0])
        if not packages:
            return
        problems, want = self.judge(packages)
        if problems:
            raise RuntimeError("; ".join(problems).replace("would check out", "changed") + ". " + self.revert(before))
        try:
            self.check_out(want)
        except RuntimeError as exc:
            raise RuntimeError(f"the change was made, but {exc}") from None

    def revert(self, before):
        """Undo what a tool changed since snapshot(): its editor transactions, then a reload from disk of the
        content packages it modified. Says what is left."""
        dirty, undo, mine = before
        undone = 0
        if undo is not None:
            for _ in range(max(0, unreal.ToolsetLibrary.get_active_undo_count() - undo)):
                if not unreal.ToolsetLibrary.undo_transaction(False):
                    break
                undone += 1
        del _UNDO[mine:]
        maps, content = _dirty_packages()
        stale = [unreal.find_package(p) for p in content if p not in dirty and not p.startswith("/Temp/")
                 and os.path.isfile(_package_file(p))]
        if stale:  # undone or never transacted, they are still marked modified: load them back from disk
            unreal.EditorLoadingAndSavingUtils.reload_packages(
                [p for p in stale if p], unreal.ReloadPackagesInteractionMode.ASSUME_POSITIVE)
        maps, content = _dirty_packages()
        left = sorted(p for p in set(maps) | set(content) if p not in dirty and not p.startswith("/Temp/"))
        if not left:
            return "Nothing was changed (the edit was undone)"
        if undone:
            return (f"The edit was undone ({undone} editor transaction(s)); still marked unsaved: {', '.join(left[:5])}. "
                    "Do not save those")
        return f"It could not be undone: {', '.join(left[:5])} changed in memory. Do not save them (reload instead)"

    def prepare(self, files, writable=False):
        """Make existing files writable (check them out; with `writable` and no check-out provider, clear the
        read-only flag). Returns (problems, new files, notes)."""
        problems, new, notes = [], [], []
        states = {}
        if self.checkouts:
            existing = [p for p in files if p and os.path.isfile(p)]
            states = self.states(existing)
            need = [p for p in existing if getattr(states.get(_norm_path(p)), "can_check_out", False)
                    and not getattr(states.get(_norm_path(p)), "is_checked_out", False)]
            if self.over_limit(len(need)):
                self.refused_all = True
                return [self.over_limit(len(need))], [], []
            _GUARD["claimed"] += len(need)
        for path in files:
            if not path:
                continue
            if not os.path.isfile(path):
                new.append(path)
                continue
            if self.checkouts:
                state = states.get(_norm_path(path)) or self.sc.query_file_state(path, True)
                if getattr(state, "is_checked_out_other", False):
                    problems.append(f"{os.path.basename(path)} is checked out by {_holder(state)}")
                    continue
                if getattr(state, "is_source_controlled", False) and not getattr(state, "is_current", True):
                    notes.append(f"{os.path.basename(path)} is not at the latest revision (sync it)")
                if getattr(state, "can_check_out", False) and not getattr(state, "is_checked_out", False):
                    if self.sc.check_out_file(path, True):
                        notes.append(f"checked out {os.path.basename(path)}")
                    else:
                        problems.append(f"could not check out {os.path.basename(path)}: {self.error()}")
                        continue
            if not os.access(path, os.W_OK) and writable and not self.checkouts:
                try:
                    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
                    notes.append(f"made {os.path.basename(path)} writable")
                except OSError as exc:
                    problems.append(f"{os.path.basename(path)} is read-only and could not be made writable: {exc}")
                    continue
            if not os.access(path, os.W_OK):
                problems.append(f"{os.path.basename(path)} is read-only" + (
                    "" if self.checkouts else " (git-lfs leaves lockable files read-only after a commit: `ue save "
                    "--writable` makes them writable, or `git lfs lock` it; in a Perforce workspace, check it out)"))
        return problems, new, notes

    def added(self, files):
        """Mark files created since prepare() for add (providers that need it)."""
        if not self.checkouts:
            return []
        made = [f for f in files if os.path.isfile(f)]
        if made and self.sc.mark_files_for_add(made, True):
            return [f"marked for add: {', '.join(os.path.basename(f) for f in made)}"]
        return [f"could not mark for add: {self.error()}"] if made else []

    def deleted(self, files):
        """Mark source-controlled files that are gone from disk for delete."""
        if not self.checkouts:
            return []
        notes = []
        for path in files:
            state = self.sc.query_file_state(path, True)
            if not os.path.isfile(path) and getattr(state, "is_source_controlled", False) and \
                    not getattr(state, "is_deleted", False):
                ok = self.sc.mark_file_for_delete(path, True)
                notes.append(f"marked for delete: {os.path.basename(path)}" if ok else
                             f"could not mark {os.path.basename(path)} for delete: {self.error()}")
        return notes


_HIT_FIELDS = ("blocking_hit", "initial_overlap", "time", "distance", "location", "impact_point", "normal",
               "impact_normal", "phys_mat", "hit_actor", "hit_component", "hit_bone_name", "bone_name", "hit_item",
               "element_index", "face_index", "trace_start", "trace_end")


def _ground_actor(actor, align=False):
    """Move an actor straight down (or up, if it is buried) so the bottom of its bounds rests on the first
    blocking surface below it. Returns (old z, new z, what it landed on) or raises if there is no ground."""
    world = actor.get_world()
    origin, extent = actor.get_actor_bounds(False)
    bottom = origin.z - extent.z

    def trace(start_z):
        hit = unreal.SystemLibrary.line_trace_single(
            world, unreal.Vector(origin.x, origin.y, start_z), unreal.Vector(origin.x, origin.y, origin.z - 1000000),
            unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [actor], unreal.DrawDebugTrace.NONE, True)
        if not hit:
            return None
        fields = _try(lambda: hit.to_dict()) or dict(zip(_HIT_FIELDS, hit.to_tuple()))  # no to_dict before 5.8
        return fields if fields.get("blocking_hit") else None

    # From just above the actor first (so a roof or canopy above it is not taken for the ground), then from high
    # above, for an actor sunk below the surface.
    hit = trace(origin.z + extent.z + 1) or trace(origin.z + 200000)
    if hit is None:
        raise RuntimeError(f"no ground below {_actor_label(actor)} (nothing with collision under it)")
    loc = actor.get_actor_location()
    old_z = loc.z
    loc.z += hit["impact_point"].z - bottom
    actor.set_actor_location(loc, False, True)
    if align:
        normal = hit["impact_normal"]
        rot = actor.get_actor_rotation()
        aligned = unreal.MathLibrary.make_rot_from_zx(normal, unreal.MathLibrary.get_forward_vector(rot))
        actor.set_actor_rotation(aligned, True)
    landed = hit.get("hit_actor")
    return old_z, loc.z, (_actor_label(landed) if landed else "?")


def _ground_group(actors, rest="lowest"):
    """Move actors as one unit (a hut of separate wall pieces): sample the ground under every piece's footprint,
    ignoring the group itself, and shift all of them by one offset so the group's lowest point rests on the lowest
    (or highest, or mean) ground under it. Attached children move with their parents. Returns (dz, ground spread
    in cm, samples)."""
    world = actors[0].get_world()
    members = set(actors)
    bottom, grounds = None, []
    for actor in actors:
        origin, extent = actor.get_actor_bounds(False)
        bottom = origin.z - extent.z if bottom is None else min(bottom, origin.z - extent.z)
        for dx, dy in ((0, 0), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            hit = _trace_down(world, origin.x + dx * extent.x * 0.9, origin.y + dy * extent.y * 0.9, ignore=actors)
            if hit is not None:
                grounds.append(hit["impact_point"].z)
    if not grounds:
        raise RuntimeError("no ground below the group (nothing with collision under it)")
    target = {"lowest": min(grounds), "highest": max(grounds), "mean": sum(grounds) / len(grounds)}[rest]
    dz = target - bottom
    for actor in actors:
        parent = _try(lambda a=actor: a.get_attach_parent_actor())
        if parent in members:
            continue  # follows its parent
        loc = actor.get_actor_location()
        loc.z += dz
        actor.set_actor_location(loc, False, True)
    return dz, max(grounds) - min(grounds), len(grounds)


def _with_attached(actors):
    """The actors plus everything attached to them, recursively, without duplicates."""
    out = []
    for actor in actors:
        for a in [actor] + list(_try(lambda: actor.get_attached_actors(True, True)) or []):
            if a not in out:
                out.append(a)
    return out


def _trace_down(world, x, y, ignore=(), top=200000.0):
    """The first blocking surface below (x, y) from high above, as a hit dict, or None."""
    hit = unreal.SystemLibrary.line_trace_single(
        world, unreal.Vector(x, y, top), unreal.Vector(x, y, -1000000), unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True,
        list(ignore), unreal.DrawDebugTrace.NONE, True)
    if not hit:
        return None
    fields = _try(lambda: hit.to_dict()) or dict(zip(_HIT_FIELDS, hit.to_tuple()))
    return fields if fields.get("blocking_hit") else None


def _circle_points(rng, cx, cy, radius, count, spacing=0.0):
    """Up to count random points in a circle, at least spacing apart."""
    import math

    def sample():
        r, a = radius * math.sqrt(rng.random()), rng.random() * 2 * math.pi
        return cx + r * math.cos(a), cy + r * math.sin(a)
    return _spaced_points(sample, count, spacing)


def _rect_points(rng, x0, y0, x1, y1, count, spacing=0.0):
    """Up to count random points in a rectangle, at least spacing apart."""
    return _spaced_points(lambda: (rng.uniform(x0, x1), rng.uniform(y0, y1)), count, spacing)


def _spaced_points(sample, count, spacing):
    points, tries = [], 0
    cell = {}  # spacing grid, so dense fills stay fast
    size = spacing or 1.0
    while len(points) < count and tries < count * 30:
        tries += 1
        p = sample()
        if spacing:
            gx, gy = int(p[0] // size), int(p[1] // size)
            near = [q for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in cell.get((gx + dx, gy + dy), ())]
            if any((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 < spacing ** 2 for q in near):
                continue
            cell.setdefault((gx, gy), []).append(p)
        points.append(p)
    return points


_PATH_KEY = re.compile(r"^(\w+)(?:\[(\d+)\])?\.(\w+)$")


def _text_field(text, field, new=None):
    """A top-level Field=value of a struct's export text '(A=1,B=(X=1,Y=2),C="s")': its value, or with new, the
    text with it replaced (or added)."""
    parts = _text_parts(text)
    for i, part in enumerate(parts):
        key, _, value = part.partition("=")
        if key.strip().lower() == field.lower():
            if new is None:
                return value
            parts[i] = f"{key.strip()}={new}"
            return "(" + ",".join(parts) + ")"
    if new is None:
        return None
    return "(" + ",".join(parts + [f"{field}={new}"]) + ")"


def _text_took(wanted, now):
    """Whether a field's text reads back as asked: numbers as numbers (4000 = 4000.000000), a struct by the fields
    given ('(Default=15000)' against '(Default=15000,PerPlatform=())')."""
    wanted, now = wanted.strip().strip('"'), now.strip().strip('"')
    if wanted.startswith("(") and now.startswith("("):
        return all(_text_took(v, _text_field(now, k.strip()) or "")
                   for k, _, v in (p.partition("=") for p in _text_parts(wanted)))
    try:
        return abs(float(wanted) - float(now)) < 1e-4
    except ValueError:
        return wanted.lower() == now.lower()


def _text_parts(text):
    """The top-level 'Field=value' parts of a struct's export text."""
    body = text.strip()
    if not (body.startswith("(") and body.endswith(")")):
        raise RuntimeError(f"not a struct's text: {text[:60]}")
    parts, depth, cur, quoted = [], 0, "", False
    for ch in body[1:-1]:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "([":
            depth += 1
        elif not quoted and ch in ")]":
            depth -= 1
        elif not quoted and ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        parts.append(cur)
    return parts


def _python_name(obj, name):
    """The editor-Python name of a reflected property given its C++ or display name (GrassVarieties ->
    grass_varieties, bInOrder -> order; Blueprint variables with spaces go as they are)."""
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    for candidate in (name, snake, re.sub(r"^b_", "", snake), re.sub(r"^(b_)?in_", "", snake)):
        if _try(lambda c=candidate: obj.get_editor_property(c), _MISSING) is not _MISSING:
            return candidate
    raise RuntimeError(f"{_label(obj)} has no property '{name}' (ue props {_label(obj)})")


_MISSING = object()


def _set_paths(obj, items, pie):
    """`Name[i].Field=value` / `Name.Field=value` for struct fields, including ones editor Python cannot set
    (uint32 and the like): the struct's export text is edited and imported back, then the field is read back."""
    rows = []
    changes = {}  # (property, index): (container, element), so several fields of one element edit one copy
    containers = {}
    for key, raw in items:
        m = _PATH_KEY.match(key)
        if not m:
            raise RuntimeError(f"'{key}': a path is Name[index].Field or Name.Field")
        name, index, field = m.group(1), m.group(2), m.group(3)
        prop = _python_name(obj, name)
        if (prop, index) in changes:
            container, element = changes[(prop, index)]
        else:
            container = containers.setdefault(prop, obj.get_editor_property(prop))  # one copy per property
            if index is not None and int(index) >= len(container):
                raise RuntimeError(f"{name} has {len(container)} element(s); [{index}] is not one")
            element = container[int(index)] if index is not None else container
        if not isinstance(element, unreal.StructBase):
            raise RuntimeError(f"{name}{f'[{index}]' if index is not None else ''} is not a struct")
        text = element.export_text()
        old = _text_field(text, field)
        if old is None:
            raise RuntimeError(f"{type(element).__name__} has no field '{field}' (its fields: "
                               + ", ".join(p.partition("=")[0] for p in _text_parts(text)) + ")")
        value = raw if isinstance(raw, str) else json.dumps(raw)
        element.import_text(_text_field(text, field, value))
        now = _text_field(element.export_text(), field)
        if now is None or not _text_took(value, now):
            raise RuntimeError(f"{field} did not take '{value}' (it reads {now})")
        changes[(prop, index)] = (container, element)
        rows.append(f"{name}{f'[{index}]' if index is not None else ''}.{field}: {old} -> {now}")

    def apply():
        for (prop, index), (container, element) in changes.items():
            if index is not None:
                container[int(index)] = element
            else:
                containers[prop] = element
        for prop, value in containers.items():
            obj.set_editor_property(prop, value)
    if pie:
        apply()
    else:
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        with unreal.ScopedEditorTransaction(f"ue set {_label(obj)}"):
            obj.modify()
            apply()
        _record_undo(before, f"{_label(obj)}: " + ", ".join(k for k, _ in items))
    return rows


def _landscape_rect(world):
    """(x0, y0, x1, y1) covering the level's landscapes, or None without one."""
    boxes = [p.get_actor_bounds(False) for p in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy)]
    if not boxes:
        return None
    return (min(o.x - e.x for o, e in boxes), min(o.y - e.y for o, e in boxes),
            max(o.x + e.x for o, e in boxes), max(o.y + e.y for o, e in boxes))


def _mask_sampler(world, mask):
    """mask: JSON {w, h, data (base64, one byte per pixel, rows along +Y, columns along +X), rect?}: a function
    (x, y) -> 0..1, and the rectangle it covers (default: the landscape's)."""
    import base64
    grid = json.loads(mask)
    w, h, pixels = int(grid["w"]), int(grid["h"]), base64.b64decode(grid["data"])
    rect = grid.get("rect") or _landscape_rect(world)
    if rect is None:
        raise RuntimeError("the mask needs an area: no landscape here, so pass --mask-area x0,y0,x1,y1")
    x0, y0, x1, y1 = rect

    def value(x, y):
        col, row = int((x - x0) / (x1 - x0) * w), int((y - y0) / (y1 - y0) * h)
        return pixels[row * w + col] / 255.0 if 0 <= col < w and 0 <= row < h else 0.0
    return value, rect


def _avoid_points(world, avoid):
    """avoid: JSON [[regex, radius cm], ...] over actor labels, classes and tags: [(x, y, radius)]."""
    rules = [(re.compile(rx, re.I), float(r)) for rx, r in json.loads(avoid)] if avoid else []
    points = []
    if not rules:
        return points
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        names = [_actor_label(actor), actor.get_class().get_name()] + [str(t) for t in (actor.tags or [])]
        for rx, radius in rules:
            if any(rx.search(n) for n in names):
                loc = actor.get_actor_location()
                points.append((loc.x, loc.y, radius))
                break
    return points


_FOLIAGE_SMALL = 60.0  # cm bounds radius under which shadows and collision on thousands of instances are flagged


def _mesh_facts(mesh):
    """LOD triangle counts, Nanite, bounds radius, whether a material moves vertices (WPO), billboard last LOD."""
    lods = _try(lambda: mesh.get_num_lods(), 1) or 1
    tris = [_try(lambda i=i: mesh.get_num_triangles(i), 0) for i in range(lods)]
    nanite = bool(_try(lambda: mesh.get_editor_property("nanite_settings").enabled, False))
    radius = _try(lambda: mesh.get_bounds().sphere_radius, 0.0) or 0.0
    sizes = _try(lambda: unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem).get_lod_screen_sizes(mesh), []) or []
    wpo = None
    for slot in _try(lambda: mesh.get_editor_property("static_materials"), []) or []:
        material = _try(lambda: slot.material_interface.get_base_material())
        if material is None:
            continue
        node = _try(lambda: unreal.MaterialEditingLibrary.get_material_property_input_node(
            material, unreal.MaterialProperty.MP_WORLD_POSITION_OFFSET), "?")
        if node == "?":
            continue
        wpo = bool(wpo) or node is not None
    # Megascans/Fab plants end on a 4-12 triangle impostor card; a screen size of 0 means it is already hidden
    billboard = lods > 1 and 0 < tris[-1] <= 12 and not nanite and not (len(sizes) == lods and sizes[-1] <= 0)
    return {"lods": lods, "tris": tris, "nanite": nanite, "radius": radius, "wpo": wpo, "billboard": billboard}


def _instanced_groups(world, match):
    """{mesh path: group} for the level's instanced meshes (foliage and other ISM/HISM components) and its
    StaticMeshActors, each group with its components, actor count and mesh."""
    rx = re.compile(match, re.I) if match not in ("", "-") else None
    groups = {}
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        foliage = isinstance(actor, unreal.InstancedFoliageActor)
        for comp in actor.get_components_by_class(unreal.InstancedStaticMeshComponent):
            mesh = comp.get_editor_property("static_mesh")
            if mesh is None or (rx and not rx.search(mesh.get_name())):
                continue
            g = groups.setdefault(mesh.get_path_name(), {"mesh": mesh, "comps": [], "actors": [], "kinds": set()})
            g["comps"].append(comp)
            g["kinds"].add("foliage" if foliage else f"instanced in {_actor_label(actor)}")
        if isinstance(actor, unreal.StaticMeshActor):
            mesh = _try(lambda: actor.static_mesh_component.get_editor_property("static_mesh"))
            if mesh is not None and not (rx and not rx.search(mesh.get_name())):
                g = groups.setdefault(mesh.get_path_name(), {"mesh": mesh, "comps": [], "actors": [], "kinds": set()})
                g["actors"].append(actor)
    return groups


def _material_warnings(materials):
    """Known-broken material setups: Fab's M_MS_Foliage with "use Distance Based WPO Fade" on (that permutation does
    not compile, "Missing function input 'Function Distance Input'", and instances fall back to the default
    material)."""
    notes = []
    for mi in materials:
        if not isinstance(mi, unreal.MaterialInstance):
            continue
        base = _try(lambda: mi.get_base_material())
        if base is None or not base.get_name().lower().startswith("m_ms_foliage"):
            continue
        L = unreal.MaterialEditingLibrary
        for name in [str(n) for n in (_try(lambda: L.get_static_switch_parameter_names(base), []) or [])]:
            if "distance based wpo fade" in name.lower() and \
                    _try(lambda n=name: L.get_material_instance_static_switch_parameter_value(mi, n), False):
                notes.append(f"{mi.get_name()}: '{name}' is on, and that permutation of {base.get_name()} fails to "
                             "compile (instances fall back to the grey default material): turn it off and cap wind "
                             "with a WPO disable distance instead")
    return notes


def _mesh_materials(mesh, comp=None):
    """A mesh's materials as a component (or foliage type) shows them: overrides first, then the mesh's own."""
    own = [s.material_interface for s in (_try(lambda: mesh.get_editor_property("static_materials"), []) or [])]
    overrides = list(_try(lambda: comp.get_editor_property("override_materials"), []) or []) if comp else []
    return [overrides[i] if i < len(overrides) and overrides[i] else m for i, m in enumerate(own)]


def _grass_types(world):
    """The LandscapeGrassType assets the level's landscape materials use (through instances and functions)."""
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    options = unreal.AssetRegistryDependencyOptions()
    queue, seen, found = [], set(), []
    for proxy in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy):
        material = _try(lambda p=proxy: p.get_editor_property("landscape_material"))
        if material is not None:
            queue.append(material.get_outermost().get_name())
    while queue and len(seen) < 300:
        package = queue.pop()
        if package in seen or package.startswith("/Script/"):
            continue
        seen.add(package)
        for data in reg.get_assets_by_package_name(package) or []:
            kind = str(_try(lambda d=data: d.asset_class_path.asset_name, ""))
            if kind == "LandscapeGrassType":
                asset = unreal.load_asset(package)
                if asset is not None and asset not in found:
                    found.append(asset)
            elif kind.startswith("Material"):
                queue += [str(d) for d in (_try(lambda p=package: reg.get_dependencies(p, options), []) or [])]
    return found


def _grass_rows(world):
    """Rows and an issue count for the landscape grass the level's landscape materials spawn. GrassDensity is per
    100 m^2 (LandscapeGrass.cpp: extent^2 in cm x density / 1000 / 1000), although its header says per 10 m^2."""
    rows, issues = [], 0
    span = lambda values, unit="": (f"{min(values):g}" if min(values) == max(values)  # noqa: E731
                                    else f"{min(values):g}-{max(values):g}") + unit
    for grass in _grass_types(world):
        varieties, problems = [], []
        for i, variety in enumerate(grass.get_editor_property("grass_varieties") or []):
            text = variety.export_text()
            get = lambda f: _text_field(text, f) or ""  # noqa: E731
            number = lambda f: float(re.search(r"Default=(-?[\d.]+)", get(f)).group(1)) if "Default=" in get(f) \
                else float(get(f) or 0)  # noqa: E731
            mesh = variety.get_editor_property("grass_mesh")
            end, wpo = number("EndCullDistance"), number("InstanceWorldPositionOffsetDisableDistance")
            varieties.append((number("GrassDensity"), number("StartCullDistance") / 100, end / 100, wpo / 100))
            name = f"[{i}] {mesh.get_name() if mesh else 'no mesh'}"
            if end <= 0:
                problems.append(f"{name}: never culled (EndCullDistance 0)")
            if mesh and _mesh_facts(mesh).get("wpo") and wpo <= 0:
                problems.append(f"{name}: wind (WPO) at every distance: ue set {grass.get_name()} "
                                f"\"GrassVarieties[{i}].InstanceWorldPositionOffsetDisableDistance=4000\"")
            problems += [f"{name}: {n}" for n in _material_warnings(list(variety.get_editor_property("override_materials") or [])
                                                                    + (_mesh_materials(mesh) if mesh else []))]
        if not varieties:
            continue
        issues += bool(problems)
        densities, starts, ends, wpos = zip(*varieties)
        rows.append(f"{'!' if problems else ' '} grass {grass.get_name()}: {len(varieties)} variet{'y' if len(varieties) == 1 else 'ies'}, "
                    f"{span(densities)} per 100 m^2 each ({sum(densities):g} in all), "
                    + (f"cull {starts[0]:g}-{ends[0]:g} m" if len(set(starts)) == 1 and len(set(ends)) == 1
                       else f"cull from {span(starts, ' m')} to {span(ends, ' m')}")
                    + (f", WPO off beyond {span(wpos, ' m')}" if min(wpos) > 0 else ""))
        rows += [f"    - {p}" for p in problems[:6]] + ([f"    - ... +{len(problems) - 6} more"] if len(problems) > 6 else [])
    if rows:
        rows.append("  (grass density is per 100 m^2, as LandscapeGrass.cpp uses it; the header's 'per 10 square "
                    "meters' is wrong by 10x. Grass instances live on the GPU: counts read 0 from Python)")
    return rows, issues


def _skinned_instance_rows(world):
    """InstancedSkinnedMeshComponents (Megaplants and the like) without a TransformProvider: no bone transforms,
    so every instance's Nanite assemblies collapse onto the root."""
    cls = getattr(unreal, "InstancedSkinnedMeshComponent", None)
    rows, issues, fine = [], 0, []
    if cls is None:
        return rows, issues
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        for comp in actor.get_components_by_class(cls):
            mesh = _try(lambda c=comp: c.get_editor_property("skinned_asset") or c.get_editor_property("skeletal_mesh"))
            provider = _try(lambda c=comp: c.get_editor_property("transform_provider"))
            count = _try(lambda c=comp: c.get_instance_count(), None)
            bad = provider is None
            issues += bad
            if not bad:
                fine.append((count or 0, provider.get_name()))
                continue
            rows.append(f"{'!' if bad else ' '} skinned {mesh.get_name() if mesh else '?'} in {_actor_label(actor)}"
                        + (f": {count} instance(s)" if count is not None else "")
                        + (f", transform provider {provider.get_name()}" if provider else ""))
            rows.append("    - no TransformProvider (a DynamicWind data asset for Megaplants): instances get no "
                        "bone transforms, so their Nanite branch assemblies collapse onto the root")
    if fine:
        rows.insert(0, f"  {len(fine)} instanced skinned mesh component(s), {sum(n for n, _ in fine)} instance(s), with a "
                       f"transform provider ({', '.join(sorted({p for _, p in fine}))})")
    return rows, issues


def _foliage_check(world, match, limit):
    """Per instanced or repeated mesh: count, culling, LODs (and the impostor billboard), Nanite, wind (WPO)
    distance, shadows and collision on small instances, and meshes placed as many separate actors."""
    rows, flagged = [], 0
    groups = _instanced_groups(world, match)
    for path, g in sorted(groups.items(), key=lambda kv: -(sum(c.get_instance_count() for c in kv[1]["comps"])
                                                           + len(kv[1]["actors"]))):
        instances = sum(c.get_instance_count() for c in g["comps"])
        separate = len(g["actors"])
        if not instances and separate < 20:
            continue  # a few plain actors of a mesh are not scatter
        facts = _mesh_facts(g["mesh"])
        # Components that are never drawn (invisible collision proxies, e.g. tree-trunk colliders) only cost physics:
        # leave them out of the render checks
        drawn = [c for c in g["comps"] if _try(lambda c=c: c.is_visible(), True)
                 and not _try(lambda c=c: c.get_editor_property("hidden_in_game"), False)]
        hidden = len(g["comps"]) - len(drawn)
        comps = drawn
        instances_drawn = sum(c.get_instance_count() for c in comps)
        issues = []
        if facts["billboard"]:
            issues.append(f"last LOD is a {facts['tris'][-1]}-triangle billboard (renders oversized far away; "
                          "ue foliage fix billboards)")
        if comps and instances_drawn >= 200 and any(c.get_editor_property("instance_end_cull_distance") <= 0 for c in comps):
            issues.append("never culled (no end cull distance; ue foliage fix cull --distance N)")
        if comps and facts["wpo"] and instances_drawn >= 200 and any(
                _try(lambda c=c: c.get_editor_property("world_position_offset_disable_distance"), 1) <= 0 for c in comps):
            issues.append("wind (WPO) runs at every distance (ue foliage fix wpo --distance N)")
        if facts["lods"] == 1 and not facts["nanite"] and facts["tris"][0] >= 2000 and instances + separate >= 100:
            issues.append(f"one LOD of {facts['tris'][0]} triangles and not Nanite")
        small = facts["radius"] < _FOLIAGE_SMALL
        if small and instances_drawn >= 1000 and any(c.get_editor_property("cast_shadow") for c in comps):
            issues.append(f"{instances_drawn} small instances cast shadows")
        if small and instances >= 500 and any(c.get_collision_enabled() != unreal.CollisionEnabled.NO_COLLISION
                                              for c in g["comps"]):
            issues.append(f"collision on {instances} small instances")
        if separate >= 20:
            issues.append(f"{separate} separate actors (ue foliage convert {g['mesh'].get_name()} makes them instances)")
        materials = [m for c in comps[:4] for m in _mesh_materials(g["mesh"], c)] or _mesh_materials(g["mesh"])
        issues += list(dict.fromkeys(_material_warnings(materials)))
        flagged += bool(issues)
        cull = max((c.get_editor_property("instance_end_cull_distance") for c in comps), default=0)
        where = ", ".join(sorted(g["kinds"])) or "actors"
        rows.append(f"{'!' if issues else ' '} {g['mesh'].get_name()}: {instances} instance(s)"
                    + (f" + {separate} actor(s)" if separate else "") + f" ({where}"
                    + (", not drawn: collision only" if hidden and not comps else "") + "); LODs "
                    + "/".join(str(t) for t in facts["tris"]) + (" Nanite" if facts["nanite"] else "")
                    + (f"; cull {cull / 100:.0f} m" if cull > 0 else ""))
        rows += [f"    - {i}" for i in issues]
    grass, grass_issues = _grass_rows(world) if match in ("", "-") else ([], 0)
    skinned, skinned_issues = _skinned_instance_rows(world)
    head = (f"{len(groups)} instanced or repeated mesh(es), {flagged} with issues"
            + (f"; landscape grass: {grass_issues} with issues" if grass else "")
            + (f"; instanced skinned meshes: {skinned_issues} with issues" if skinned else ""))
    return _capped(head, rows + skinned + grass, limit)


def _foliage_fix(world, kind, match, value, scope, by_mesh=None):
    """`fix billboards`: hide the impostor last LOD (screen size 0, so it is never chosen; the authored LODs stay)
    on the level's instanced meshes, or every mesh under scope. `fix cull|wpo --distance`: set the end cull or WPO
    disable distance on the level's instanced components and their foliage types."""
    if kind == "billboards":
        if scope not in ("", "-"):
            meshes = [unreal.load_asset(p.split(".")[0]) for p in unreal.EditorAssetLibrary.list_assets(scope, True, False)]
            meshes = [m for m in meshes if isinstance(m, unreal.StaticMesh)]
        else:
            meshes = [g["mesh"] for g in _instanced_groups(world, match).values()]
        subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        fixed = []
        for mesh in meshes:
            if not _mesh_facts(mesh)["billboard"]:
                continue
            sizes = list(subsystem.get_lod_screen_sizes(mesh))
            mesh.modify()
            subsystem.set_lod_screen_sizes(mesh, sizes[:-1] + [0.0])
            fixed.append(mesh.get_name())
        return (f"hid the billboard LOD of {len(fixed)} mesh(es): {', '.join(fixed[:12])}" + (" ..." if len(fixed) > 12 else "")
                + "; unsaved (ue save, or ue changes)") if fixed else "no billboard last LODs found"
    if kind not in ("cull", "wpo") or value <= 0:
        raise RuntimeError("fix billboards | fix cull --distance <cm> | fix wpo --distance <cm>")
    prop = "instance_end_cull_distance" if kind == "cull" else "world_position_offset_disable_distance"
    comps = [c for g in _instanced_groups(world, match).values() for c in g["comps"]]
    types = set()
    for comp in comps:
        comp.modify()
        comp.set_editor_property(prop, int(value))
        mesh = comp.get_editor_property("static_mesh")
        types.update((by_mesh or {}).get(mesh.get_path_name(), []) if mesh else [])
    for ft in types:  # so new instances painted later get it too
        ft.modify()
        if kind == "cull":
            _try(lambda f=ft: f.set_editor_property("cull_distance", unreal.Int32Interval(0, int(value))))
        else:
            _try(lambda f=ft: f.set_editor_property("world_position_offset_disable_distance", int(value)))
    return (f"set {prop} = {value:g} cm on {len(comps)} component(s)" + (f" and {len(types)} foliage type(s)" if types else "")
            + "; unsaved")


def _foliage_convert(world, match, folder, resolve):
    """StaticMeshActors whose label, tag or mesh matches -> foliage instances of their mesh's foliage type (made in
    folder when missing; actors with override materials get a foliage type with those materials), the actors
    deleted. Actors with attached actors or several components are left alone."""
    rx = re.compile(match, re.I)
    by_mesh, skipped = {}, []
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.StaticMeshActor):
        comp = actor.static_mesh_component
        mesh = comp.get_editor_property("static_mesh") if comp else None
        names = [_actor_label(actor)] + [str(t) for t in (actor.tags or [])] + ([mesh.get_name()] if mesh else [])
        if mesh is None or not any(rx.search(n) for n in names):
            continue
        if _try(lambda: actor.get_attached_actors(True, False)) or len(actor.get_components_by_class(unreal.PrimitiveComponent)) > 1:
            skipped.append(f"{_actor_label(actor)} (attached actors or more components)")
            continue
        overrides = tuple(m.get_path_name() if m else "" for m in (comp.get_editor_property("override_materials") or []))
        overrides = overrides if any(overrides) else ()
        by_mesh.setdefault((mesh.get_path_name(), overrides), (mesh, []))[1].append(actor)
    if not by_mesh:
        return "nothing to convert" + (f"; skipped {', '.join(skipped[:8])}" if skipped else "")
    notes, done = [], []
    with unreal.ScopedEditorTransaction("ue foliage convert"):
        for (_, overrides), (mesh, actors) in by_mesh.items():
            ft, note = resolve(mesh.get_path_name(), True) if not overrides else (None, None)
            if overrides:  # a foliage type of its own, carrying those materials
                name = "FT_" + re.sub(r"^SM_", "", mesh.get_name()) + "_" + overrides[0].rsplit(".", 1)[-1]
                path = f"{folder.rstrip('/')}/{name}"
                ft = _try(lambda: unreal.load_asset(path)) if unreal.EditorAssetLibrary.does_asset_exist(path) else None
                ft = ft or unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                    name, folder.rstrip("/"), unreal.FoliageType_InstancedStaticMesh,
                    unreal.FoliageType_InstancedStaticMeshFactory())
                if ft is None:
                    raise RuntimeError(f"could not load or create {path}")
                ft.set_editor_property("mesh", mesh)
                ft.set_editor_property("override_materials", [unreal.load_asset(p) if p else None for p in overrides])
                unreal.EditorAssetLibrary.save_loaded_asset(ft, False)
                note = f"{_short_path(ft.get_path_name())} with {mesh.get_name()}'s override materials"
            if note:
                notes.append(note)
            unreal.InstancedFoliageActor.add_instances(world, ft, [a.static_mesh_component.get_world_transform() for a in actors])
            unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(actors)
            done.append(f"{len(actors)} {mesh.get_name()} -> {ft.get_name()}")
    return ("converted " + ", ".join(done) + " (labels and tags are not kept by instances)"
            + (f"; {'; '.join(notes)}" if notes else "") + (f"; skipped {len(skipped)}: {', '.join(skipped[:6])}" if skipped else ""))


def _read_square(world, rt, width, height, x0, y0, x1, y1):
    """Pixels of a square of a render target, row by row. In 5.8 read_render_target_raw_pixel_area takes its
    'max' arguments as the width and height (clamped to the target), not as the far corner; try that, then the
    documented corner form, and trust only a read of exactly the right size."""
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    for args in ((x0, y0, cols, rows), (x0, y0, x1 + 1, y1 + 1), (x0, y0, x1, y1)):
        pixels = unreal.RenderingLibrary.read_render_target_raw_pixel_area(world, rt, *args, True)
        if len(pixels) == cols * rows:
            return list(pixels)
    raise RuntimeError(f"could not read a {cols} x {rows} square of the render target exactly")


def _read_all_heights(world, rt, width, height, band=64):
    """Every 16-bit height of an exported heightmap render target, row by row, read in bands."""
    values = []
    for y0 in range(0, height, band):
        values += _rg16(_read_square(world, rt, width, height, 0, y0, width - 1, min(height, y0 + band) - 1))
    return values


def _write_r16(path, values):
    import array
    data = array.array("H", values)
    if sys.byteorder == "big":
        data.byteswap()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        data.tofile(f)


def _read_r16(path):
    import array
    data = array.array("H")
    with open(path, "rb") as f:
        data.frombytes(f.read())
    if sys.byteorder == "big":
        data.byteswap()
    return list(data)


def _rg16(pixels):
    """16-bit heights from an RGBA8 render target's R (high byte) and G (low byte). The raw read returns bytes as
    0-255 or 0-1 depending on the format and engine version, so look at the values."""
    scale = 1.0 if any(p.r > 1.0 or p.g > 1.0 for p in pixels) else 255.0
    return [int(round(p.r * scale)) * 256 + int(round(p.g * scale)) for p in pixels]


def _png_rgba8(width, height, rgba):
    """An 8-bit RGBA PNG in memory (for import_buffer_as_texture2d)."""
    import struct
    import zlib
    rows = b"".join(b"\x00" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


_MAT_LINK = re.compile(r"Expression=[^,()]*?(\w+)'?\"?,OutputIndex=(\d+)")


_MAT_OUTPUTS = ("BASE_COLOR", "METALLIC", "SPECULAR", "ROUGHNESS", "ANISOTROPY", "EMISSIVE_COLOR", "OPACITY",
                "OPACITY_MASK", "NORMAL", "TANGENT", "WORLD_POSITION_OFFSET", "SUBSURFACE_COLOR", "AMBIENT_OCCLUSION",
                "REFRACTION", "PIXEL_DEPTH_OFFSET", "SHADING_MODEL", "SURFACE_THICKNESS", "DISPLACEMENT",
                "FRONT_MATERIAL")  # a material's outputs (MaterialProperty values with an input pin)


def _mat_input_links(expr):
    """[(source object name, output index)] of an expression's connected inputs, in property order. Python cannot
    read FExpressionInput.OutputIndex, so this comes from the engine's own export (`obj dump`)."""
    text = "\n".join(str(v) for _, _, v in _dump_props(expr))
    return [(m.group(1), int(m.group(2))) for m in _MAT_LINK.finditer(text)]


def _mat_output_names(expr):
    """An expression's output names in order ('' for an unnamed one), from its export; [] if not exported."""
    outputs = next((v for _, n, v in _dump_props(expr) if n == "Outputs"), "")
    return re.findall(r'OutputName="?([^",)]*)"?', str(outputs))


def _mat_unused(asset):
    """(nodes, unused nodes) of a Material or MaterialFunction: unused ones reach no output (comments aside)."""
    lib = unreal.MaterialEditingLibrary
    is_fn = isinstance(asset, unreal.MaterialFunction)
    nodes = list((lib.get_material_function_expressions if is_fn else lib.get_material_expressions)(asset))
    getter = lib.get_inputs_for_material_function_expression if is_fn else lib.get_inputs_for_material_expression
    used, todo = set(), []
    if is_fn:
        todo = [e for e in nodes if e.get_class().get_name() == "MaterialExpressionFunctionOutput"]
    else:
        for name in _MAT_OUTPUTS:  # not every MaterialProperty value: MP_MAX and the like crash the editor
            prop = getattr(unreal.MaterialProperty, "MP_" + name, None)
            node = _try(lambda: lib.get_material_property_input_node(asset, prop)) if prop is not None else None
            if node:
                todo.append(node)
    while todo:
        expr = todo.pop()
        if expr.get_path_name() in used:
            continue
        used.add(expr.get_path_name())
        todo += [i for i in (_try(lambda: list(getter(asset, expr)), []) or []) if i]
    unused = [e for e in nodes if e.get_path_name() not in used and e.get_class().get_name() != "MaterialExpressionComment"]
    return nodes, unused


def _mat_node_text(expr, kind):
    """One material node in a few words: its kind, parameter name and the setting that matters."""
    name = _try(lambda: str(expr.get_editor_property("parameter_name")))
    text = kind + (f" '{name}'" if name and name != "None" else "")
    for prop, fmt in (("default_value", None), ("r", None), ("constant", None), ("texture", "asset"),
                      ("material_function", "asset"), ("coordinate_index", "uv"), ("text", None)):
        value = _try(lambda: expr.get_editor_property(prop), None)
        if value is None:
            continue
        if fmt == "asset":
            text += f" = {value.get_name()}" if value else " = none"
        elif fmt == "uv":
            text += f" UV{value}"
        elif isinstance(value, unreal.LinearColor):
            text += f" = ({value.r:g}, {value.g:g}, {value.b:g}, {value.a:g})"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            text += f" {prop}={value:g}" if prop.startswith("const_") else f" = {value:g}"
        elif isinstance(value, str) and value:
            text += f" '{value[:40]}'"
        if prop in ("default_value", "r", "constant", "texture", "material_function"):
            break
    group = _try(lambda: str(expr.get_editor_property("group")))
    return text + (f" [group {group}]" if group and group != "None" else "")


def _mat_parse_value(text, old):
    """A typed value for a material expression property, shaped like its current value."""
    if isinstance(old, bool):
        return text.lower() in ("1", "true", "yes", "on")
    if isinstance(old, (int, float)):
        return type(old)(float(text))
    if isinstance(old, unreal.LinearColor) or (old is None and "," in text):
        nums = (_numbers(text) or []) + [1.0] * 4
        return unreal.LinearColor(*nums[:4])
    if isinstance(old, unreal.Object) or text.startswith("/"):
        asset = unreal.load_asset(text.split(".")[0])
        if asset is None:
            raise RuntimeError(f"no asset at {text}")
        return asset
    return text


def _mat_set_default(expr, text):
    """Set a parameter's or constant's value, whichever property it has."""
    for prop in ("default_value", "texture", "r", "constant"):
        old = _try(lambda: expr.get_editor_property(prop), _MISSING)
        if old is not _MISSING:
            expr.set_editor_property(prop, _mat_parse_value(text, old))
            return
    raise RuntimeError(f"{expr.get_class().get_name()} has no value to set; use set <node> prop=value")


_MISSING = object()


def _enum_name(value):
    """An enum value's name in one spelling: 'GOTO_STATE', 'GotoState' and 'EStateTreeTransitionType::GotoState'
    all become 'gotostate'."""
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        text = str(value).strip("<> ").split(":")[0] if str(value).startswith("<") else str(value)
        name = text.split("::")[-1].split(".")[-1]
    return name.replace("_", "").lower()


def _statetree_lines(tree):
    """A StateTree as indented text: evaluators, global tasks, then each state with its enter conditions,
    tasks and transitions (Epic's StateTreeEditorData, described the way the editor describes nodes)."""
    ed = unreal.StateTreeEditorData.get_editor_data(tree)
    if ed is None:
        raise RuntimeError(f"{tree.get_name()} has no editor data")

    def describe(node):
        text = _try(lambda: str(ed.get_node_description(node)), "") or ""
        text = re.sub(r"<[^>]+>", "", text).strip()  # the editor's rich-text markup
        if not text:
            inner = _try(lambda: node.get_editor_property("node"))
            text = _try(lambda: inner.get_editor_property("name"), "") or _try(lambda: str(inner.get_script_struct().get_name()), "?")
        return text

    def target(link):
        kind = _enum_name(_try(lambda: link.get_editor_property("link_type"), ""))
        name = str(_try(lambda: link.get_editor_property("name"), "") or "")
        return {"gotostate": name or "?", "nextstate": "the next state", "nextselectablestate": "the next selectable state",
                "succeeded": "succeed", "failed": "fail", "none": "nothing"}.get(kind, f"{kind} {name}".strip())

    def tag_name(tag):
        name = str(_try(lambda: tag.get_editor_property("tag_name"), "") or "")
        return "" if name in ("None", "") else name

    lines = [f"{tree.get_name()} (schema {_try(lambda: ed.get_editor_property('schema').get_class().get_name(), '?')})"]
    for label, nodes in (("evaluators", _try(lambda: list(ed.get_editor_property("evaluators")), [])),
                         ("global tasks", _try(lambda: list(ed.get_editor_property("global_tasks")), []))):
        if nodes:
            lines.append(f"{label}: " + "; ".join(describe(n) for n in nodes))
    count = [0]

    def walk(state, depth):
        count[0] += 1
        pad = "  " * depth
        kind = _enum_name(_try(lambda: state.get_editor_property("type"), ""))
        select = _enum_name(_try(lambda: state.get_editor_property("selection_behavior"), ""))
        enabled = _try(lambda: state.get_editor_property("enabled"), True)
        head = f"{pad}{state.get_editor_property('name')}" + ("" if kind in ("", "state") else f" ({kind})")
        if select and select not in ("tryenterstate", "tryselectchildreninorder"):
            head += f" [select: {select}]"
        lines.append(head + ("" if enabled else "  (disabled)"))
        conditions = _try(lambda: list(state.get_editor_property("enter_conditions")), [])
        if conditions:
            lines.append(f"{pad}  enter if: " + " and ".join(describe(c) for c in conditions))
        tasks = _try(lambda: list(state.get_editor_property("tasks")), [])
        if tasks:
            lines.append(f"{pad}  tasks: " + "; ".join(describe(t) for t in tasks))
        linked = _try(lambda: state.get_editor_property("linked_subtree"))
        linked_name = str(_try(lambda: linked.get_editor_property("name"), "") or "") if linked else ""
        if linked_name and linked_name != "None":
            lines.append(f"{pad}  runs subtree: {linked_name}")
        asset = _try(lambda: state.get_editor_property("linked_asset"))
        if asset:
            lines.append(f"{pad}  runs: {asset.get_name()}")
        for tr in _try(lambda: list(state.get_editor_property("transitions")), []):
            trigger = {"onstatecompleted": "when done", "onstatesucceeded": "on success", "onstatefailed": "on failure",
                       "ontick": "every tick", "onevent": "on event", "ondelegate": "on delegate"}.get(
                _enum_name(_try(lambda: tr.get_editor_property("trigger"), "")), "")
            event = tag_name(_try(lambda: tr.get_editor_property("required_event").get_editor_property("tag"))) or tag_name(
                _try(lambda: tr.get_editor_property("event_tag")))
            if event:
                trigger += f" {event}"
            conds = _try(lambda: list(tr.get_editor_property("conditions")), [])
            delay = _try(lambda: tr.get_editor_property("delay_duration"), 0.0) if _try(lambda: tr.get_editor_property("delay_transition"), False) else 0
            on = _try(lambda: tr.get_editor_property("enabled"), True)
            lines.append(f"{pad}  -> {target(tr.get_editor_property('state'))} {trigger.strip()}"
                         + (f" if {' and '.join(describe(c) for c in conds)}" if conds else "")
                         + (f" after {delay:g}s" if delay else "") + ("" if on else " (disabled)"))
        for child in _try(lambda: list(state.get_editor_property("children")), []):
            walk(child, depth + 1)
    for root in list(ed.get_editor_property("sub_trees")):
        walk(root, 0)
    lines[0] += f", {count[0]} state(s)"
    return lines


def _node_title(node):
    title = _try(lambda: str(node.get_node_title()), "") or node.get_name()
    return re.sub(r"\s+", " ", title).strip()


def _box_row(label, cls, corners):
    """A `map --footprints` geometry row from a box's 8 world corners: label, class, kind, centre x/y, top z,
    half extents x/y, bottom z, and the outline (the 4 bottom corners)."""
    xs, ys, zs = [c.x for c in corners], [c.y for c in corners], [c.z for c in corners]
    low = sorted(corners, key=lambda c: c.z)[:4]
    centre = (sum(c.x for c in low) / 4, sum(c.y for c in low) / 4)
    low.sort(key=lambda c: math.atan2(c.y - centre[1], c.x - centre[0]))  # around the centre, in order
    return [label, cls, "geometry", round((min(xs) + max(xs)) / 2, 1), round((min(ys) + max(ys)) / 2, 1),
            round(max(zs), 1), round((max(xs) - min(xs)) / 2, 1), round((max(ys) - min(ys)) / 2, 1), round(min(zs), 1),
            [[round(c.x, 1), round(c.y, 1)] for c in low]]


def _built_parts(actor, want=None, cap=4000):
    """Footprint rows for an actor built from procedural meshes or instanced meshes (a city builder's roads and
    junctions, HISM buildings): one per procedural section's component and one per instance, each the
    transformed box of its mesh, within `want` (x, y, radius) when given. [] for other actors."""
    rows, label = [], _label(actor)

    def corners(lo, hi, transform):
        return [unreal.MathLibrary.transform_location(transform, unreal.Vector(x, y, z))
                for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]

    def near(row):
        return not want or len(want) != 3 or \
            max(abs(row[3] - want[0]) - row[6], 0) ** 2 + max(abs(row[4] - want[1]) - row[7], 0) ** 2 <= want[2] ** 2
    for comp in _try(lambda: actor.get_components_by_class(unreal.PrimitiveComponent), []) or []:
        if len(rows) >= cap:
            break
        name = comp.get_class().get_name()
        if isinstance(comp, unreal.InstancedStaticMeshComponent) and not isinstance(comp, unreal.FoliageInstancedStaticMeshComponent):
            mesh = _try(lambda: comp.get_editor_property("static_mesh"))
            box = _try(lambda: mesh.get_bounding_box()) if mesh else None
            if box is None:
                continue
            for i in range(min(_try(lambda: comp.get_instance_count(), 0) or 0, cap - len(rows))):
                transform = _try(lambda: comp.get_instance_transform(i, True))
                if transform is not None:
                    row = _box_row(f"{label}#{i}", mesh.get_name(), corners(box.min, box.max, transform))
                    if near(row):
                        rows.append(row)
        elif name == "ProceduralMeshComponent" or isinstance(comp, unreal.DynamicMeshComponent if hasattr(
                unreal, "DynamicMeshComponent") else ()):
            local = _try(lambda: comp.get_local_bounds())
            if local:
                row = _box_row(f"{label}.{comp.get_name()}", name, corners(local[0], local[1], comp.get_world_transform()))
                if near(row):
                    rows.append(row)
    return rows


def _collision_profiles():
    """{profile name: collision enabled, lower case without separators ('queryandphysics')} from the engine's and
    the project's config (+Profiles=(Name="BlockAll",CollisionEnabled=QueryAndPhysics,...))."""
    out = {}
    for folder, name in ((unreal.Paths.engine_config_dir(), "BaseEngine.ini"),
                         (unreal.Paths.project_config_dir(), "DefaultEngine.ini")):
        path = os.path.join(unreal.Paths.convert_relative_path_to_full(folder), name)
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r'^\+Profiles=\(Name="([^"]+)",\s*CollisionEnabled=(\w+)', text, re.M):
            out[m.group(1)] = m.group(2).lower().replace("_", "")
    return out


def _pin_type(pin):
    """A pin's type from its exported text: 'float', 'Vector', 'Actor', 'array of Name'..."""
    category = (re.search(r'PinType\.PinCategory="(\w*)"', pin) or [None, ""])[1]
    sub = re.search(r"PinType\.PinSubCategoryObject=[^']*'\"?[^\"']*\.(\w+)", pin)
    name = sub.group(1) if sub else {"real": "float", "int": "int", "bool": "bool", "name": "Name", "string": "String",
                                     "text": "Text", "byte": "byte", "int64": "int64"}.get(category, category or "?")
    container = (re.search(r"PinType\.ContainerType=(\w+)", pin) or [None, ""])[1]
    if container in ("Array", "Set"):
        name = f"{container.lower()} of {name}"
    elif container == "Map":
        name = f"map of {name}"
    return name + (" (by ref)" if "PinType.bIsReference=True" in pin and not container else "")


def _bp_signature(bp, name):
    """'Name(Speed: float, Target: Actor) -> (Hit: bool)' for a Blueprint function: its entry node's outputs are
    the parameters, its result node's inputs the return values (read from the function graph's text export).
    Just the name when the graph cannot be read."""
    graph = next((g for g in unreal.BlueprintEditorLibrary.list_graphs(bp) if g.get_name() == name), None)
    nodes = _try(lambda: _t3d_graph_nodes(graph)) if graph is not None else None
    if not nodes:
        return name
    params, results = [], []
    for node in nodes.values():
        if node["class"] == "K2Node_FunctionEntry":
            params = [f"{p['name']}: {p['type']}" for p in node["pins"] if p["output"] and p["type"] != "exec"]
        elif node["class"] == "K2Node_FunctionResult" and not results:
            results = [f"{p['name']}: {p['type']}" for p in node["pins"] if not p["output"] and p["type"] != "exec"]
        elif node["class"] == "K2Node_Tunnel":  # a macro: the entry tunnel's outputs, the exit tunnel's inputs
            outs = [f"{p['name']}: {p['type']}" for p in node["pins"] if p["output"] and p["type"] != "exec"]
            ins = [f"{p['name']}: {p['type']}" for p in node["pins"] if not p["output"] and p["type"] != "exec"]
            params, results = (params or outs), (results or ins)
    return f"{name}({', '.join(params)})" + (f" -> ({', '.join(results)})" if results else "")


def _t3d_graph_nodes(graph):
    """{node name: {class, props, pins: [{name, output, links: [node name]}]}} for a graph's own nodes, from a
    text export. State machine nodes have no pins in Python, but their copy-paste text has every link."""
    path = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "Tether",
                        "graph-export.t3d")
    task = unreal.AssetExportTask()
    for key, value in (("object", graph), ("filename", path), ("automated", True), ("prompt", False),
                       ("replace_identical", True)):
        task.set_editor_property(key, value)
    if not unreal.Exporter.run_asset_export_task(task):
        raise RuntimeError(f"the editor could not export {graph.get_name()} as text")
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    classes = {name: cls for cls, name in re.findall(r'^   Begin Object Class=(\S+) Name="([^"]+)"', text, re.M)}
    nodes = {}
    # The export lists the nodes twice: declared (with Class=), then defined (properties and pins).
    for block in re.finditer(r'^   Begin Object Name="([^"]+)"[^\n]*\n(.*?)^   End Object', text, re.M | re.S):
        name, body = block.group(1), block.group(2)
        props = dict(re.findall(r"^      (\w+)=(.*)$", body, re.M))
        pins = []
        for pin in re.findall(r"^      CustomProperties Pin \((.*)\)$", body, re.M):
            pin_name = re.search(r'PinName="([^"]*)"', pin)
            links = re.search(r"LinkedTo=\(([^)]*)\)", pin)
            pins.append({"name": pin_name.group(1) if pin_name else "", "output": 'Direction="EGPD_Output"' in pin,
                         "links": [l.split()[0] for l in links.group(1).split(",") if l.strip()] if links else [],
                         "type": _pin_type(pin)})
        nodes[name] = {"class": classes.get(name, "").rsplit(".", 1)[-1], "props": props, "pins": pins}
    return nodes


def _anim_graph_lines(bp):
    """An Animation Blueprint's AnimGraph and state machines as text. Graph nodes are not listed by the
    Blueprint graph editor for state machine graphs, so they are gathered by owner (each graph's nodes, each
    state's and transition's inner graph)."""
    by_outer = {}
    for node in unreal.ObjectIterator(unreal.EdGraphNode):
        outer = _try(lambda: node.get_outer())
        if outer is not None and node.get_outermost() == bp.get_outermost():
            by_outer.setdefault(outer.get_path_name(), []).append(node)
    graphs_of = {}
    for graph in unreal.ObjectIterator(unreal.EdGraph):
        outer = _try(lambda: graph.get_outer())
        if outer is not None and graph.get_outermost() == bp.get_outermost():
            graphs_of.setdefault(outer.get_path_name(), []).append(graph)

    def nodes(graph):
        return by_outer.get(graph.get_path_name(), [])

    def inputs(node):
        """(pin name, source node or None, literal) for each input pin."""
        out = []
        for pin in _try(lambda: list(node.list_all_pins()), []) or []:
            if pin.get_pin_direction() != unreal.EdGraphPinDirection.EGPD_INPUT:
                continue
            linked = _try(lambda: list(pin.list_connected_pins()), []) or []
            out.append((str(pin.get_pin_name()), linked[0].get_owning_node() if linked else None,
                        _try(lambda: str(pin.get_pin_value()), "")))
        return out

    def expr(node, depth=0):
        """What feeds a pin, as a small expression: Title(input, input)."""
        title = _node_title(node)
        if depth > 6:
            return title
        parts = []
        for name, source, literal in inputs(node):
            if source is not None:
                parts.append(expr(source, depth + 1))
            elif literal and literal not in ("", "0", "0.0", "false", "None", "()") and name not in ("self", "execute"):
                parts.append(f"{name}={literal}")
        return title + (f"({', '.join(parts)})" if parts else "")

    def result_expr(graph, result_kind):
        result = next((n for n in nodes(graph) if result_kind in n.get_class().get_name()), None)
        if result is None:
            return "?"
        fed = [expr(src) for _, src, _ in inputs(result) if src is not None]
        literal = [lit for name, src, lit in inputs(result) if src is None and lit]
        return " , ".join(fed) or (f"always {literal[0]}" if literal else "nothing")

    skeleton = _try(lambda: bp.get_editor_property("target_skeleton"))
    lines = [f"{bp.get_name()} (Animation Blueprint" + (f", skeleton {skeleton.get_name()}" if skeleton else "") + ")"]
    machines = []
    for graph in unreal.BlueprintEditorLibrary.list_graphs(bp):
        if graph.get_class().get_name() != "AnimationGraph":
            continue
        lines.append(f"{graph.get_name()}: Output Pose <- {result_expr(graph, 'AnimGraphNode_Root')}")
        machines += [n for n in nodes(graph) if n.get_class().get_name() == "AnimGraphNode_StateMachine"]
    for machine in machines:
        inner = graphs_of.get(machine.get_path_name(), [])
        if not inner:
            continue
        graph = inner[0]
        exported = _t3d_graph_nodes(graph)
        live = {n.get_name(): n for n in nodes(graph)}

        def name_of(node_name):
            info = exported.get(node_name, {})
            bound = re.search(r"'([^']+)'", info.get("props", {}).get("BoundGraph", ""))
            alias = info.get("props", {}).get("StateAliasName", "").strip('"')
            return (bound.group(1) if bound else "") or alias or node_name

        def linked(info, output):
            return [l for p in info["pins"] if p["output"] == output for l in p["links"]]
        states = [n for n, info in exported.items() if info["class"] in ("AnimStateNode", "AnimStateConduitNode",
                                                                             "AnimStateAliasNode")]
        entry = next((info for info in exported.values() if info["class"] == "AnimStateEntryNode"), None)
        first = name_of(linked(entry, True)[0]) if entry and linked(entry, True) else "?"
        lines.append(f"state machine {graph.get_name()} ({len(states)} states, starts in {first}):")
        leaving = {}
        for name, info in exported.items():
            if info["class"] != "AnimStateTransitionNode":
                continue
            sources, targets = linked(info, False), linked(info, True)
            node = live.get(name)
            rule_graph = (graphs_of.get(node.get_path_name(), []) or [None])[0] if node else None
            rule = result_expr(rule_graph, "TransitionResult") if rule_graph else "?"
            if info["props"].get("bAutomaticRuleBasedOnSequencePlayerInState") == "True":
                rule = "its animation ends (automatic rule)"
            blend = info["props"].get("CrossfadeDuration")
            for source in sources:
                leaving.setdefault(source, []).append(
                    f"-> {name_of(targets[0]) if targets else '?'} when {rule}" + (f", blend {float(blend):g}s" if blend else ""))
        for name in states:
            info = exported[name]
            kind = {"AnimStateConduitNode": "conduit", "AnimStateAliasNode": "alias"}.get(info["class"], "")
            node = live.get(name)
            bound = graphs_of.get(node.get_path_name(), []) if node else []
            if kind == "alias":
                covers = "any state" if info["props"].get("bGlobalAlias") == "True" else ", ".join(
                    name_of(m) for m in re.findall(r"AnimStateNode'[^']*\.([^'.]+)'", info["props"].get("AliasedStateNodes", "")))
                lines.append(f"  {name_of(name)} (alias for {covers or 'no states'})")
            else:
                plays = result_expr(bound[0], "TransitionResult" if kind == "conduit" else "StateResult") if bound else ""
                lines.append(f"  {name_of(name)}" + (f" ({kind})" if kind else "") + (f": {plays}" if plays else ""))
            for line in leaving.get(name, []):
                lines.append(f"    {line}")
    return lines


_ENGINE_CHANNELS = [("ECC_WORLD_STATIC", "WorldStatic", False), ("ECC_WORLD_DYNAMIC", "WorldDynamic", False),
                    ("ECC_PAWN", "Pawn", False), ("ECC_VISIBILITY", "Visibility", True), ("ECC_CAMERA", "Camera", True),
                    ("ECC_PHYSICS_BODY", "PhysicsBody", False), ("ECC_VEHICLE", "Vehicle", False),
                    ("ECC_DESTRUCTIBLE", "Destructible", False)]


def _collision_channels():
    """[(CollisionChannel enum name, display name, is a trace channel)]: the engine's, then the project's own
    from [/Script/Engine.CollisionProfile] in DefaultEngine.ini."""
    path = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir()), "DefaultEngine.ini")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return _parse_channels(f.read())
    except OSError:
        return list(_ENGINE_CHANNELS)


def _parse_channels(text):
    """The engine's channels plus those a DefaultEngine.ini declares, in channel order."""
    channels = list(_ENGINE_CHANNELS)
    custom = []
    for line in re.findall(r"^\+DefaultChannelResponses=\((.*)\)\s*$", text, re.M):
        channel = re.search(r"Channel=ECC_GameTraceChannel(\d+)", line)
        name = re.search(r'Name="([^"]+)"', line)
        if channel and name:
            custom.append((int(channel.group(1)), name.group(1), "bTraceType=True" in line))
    for number, name, trace in sorted(custom):
        channels.append((f"ECC_GAME_TRACE_CHANNEL{number}", name, trace))
    return channels


def _trace_type(channel_name):
    """The TraceTypeQuery for a trace channel's name: Visibility, Camera, then the project's trace channels in
    channel order, as the engine numbers them."""
    traces = [c for c in _collision_channels() if c[2]]
    for index, (_, name, _) in enumerate(traces):
        if name.lower() == channel_name.lower():
            return getattr(unreal.TraceTypeQuery, f"TRACE_TYPE_QUERY{index + 1}"), name
    raise RuntimeError(f"no trace channel '{channel_name}' (trace channels: {', '.join(c[1] for c in traces)})")


def _responses(component):
    """'Block: Pawn, Visibility; Overlap: ...; Ignore: ...' for a primitive component."""
    groups = {}
    for enum_name, name, _ in _collision_channels():
        channel = getattr(unreal.CollisionChannel, enum_name, None)
        if channel is None:
            continue
        response = _enum_name(_try(lambda: component.get_collision_response_to_channel(channel), "?"))
        groups.setdefault(response.replace("ecr", ""), []).append(name)
    return "; ".join(f"{kind.title()}: {', '.join(names)}" for kind, names in groups.items() if names)


def _rot_tuple(rot):
    return rot.pitch, rot.yaw, rot.roll


def _vec_tuple(vec):
    return vec.x, vec.y, vec.z


def _fmt3(values):
    return "(" + ", ".join(f"{round(v, 2) + 0.0:g}" for v in values) + ")"  # + 0.0: no "-0"


def _slope_degrees(normal):
    import math
    return math.degrees(math.acos(max(-1.0, min(1.0, normal.z))))


def _legacy_key(name):
    """Match 'bCastShadows', 'CastShadows' and 'cast_shadows' to the Python wrapper's 'cast_shadows'."""
    key = name.replace("_", "").lower()
    return key[1:] if key.startswith("b") and len(key) > 1 and name[:1] == "b" and name[1:2].isupper() else key


def _struct_fields(value):
    """A struct's fields as a dict, in the order positional values fill them (r,g,b,a for colours)."""
    if isinstance(value, unreal.Color):
        return {k: value.get_editor_property(k) for k in ("r", "g", "b", "a")}
    fields = _try(lambda: value.to_dict())
    if fields is None:
        fields = {n: value.get_editor_property(n) for n in dict(_DOC_PROPERTY.findall(type(value).__doc__ or ""))}
    return fields


def _legacy_value(raw, old, name):
    """Parse compact text into a value of the same Python wrapper type as the property's current value."""
    text = raw.strip()
    if isinstance(old, bool):
        flag = {"true": True, "1": True, "yes": True, "on": True,
                "false": False, "0": False, "no": False, "off": False}.get(text.lower())
        if flag is None:
            raise ValueError(f"expected true/false, got '{text}'")
        return flag
    if isinstance(old, (int, float)):
        nums = _numbers(text)
        if not nums or len(nums) != 1:
            raise ValueError(f"expected a number, got '{text}'")
        return type(old)(nums[0])
    if isinstance(old, unreal.EnumBase):
        member = text.split("::")[-1].split(".")[-1].upper()
        if not hasattr(type(old), member):
            names = [n for n in dir(type(old)) if n.isupper()]
            raise ValueError(f"expected one of {', '.join(names[:12])}, got '{text}'")
        return getattr(type(old), member)
    if isinstance(old, unreal.StructBase):
        fields = _struct_fields(old)
        nums = _numbers(text)
        if nums is not None:
            if len(nums) > len(fields):
                raise ValueError(f"{len(nums)} values for {len(fields)} fields ({', '.join(fields)})")
            if isinstance(old, unreal.Color) and all(0 <= v <= 1 for v in nums) and any(isinstance(v, float) for v in nums):
                nums = [round(v * 255) for v in nums]  # 0-1 colour values, as for LinearColor
            update = dict(zip(fields, nums))
        else:
            try:
                update = json.loads(text)
            except ValueError:
                raise ValueError(f"expected {','.join(fields)} (or JSON), got '{text}'") from None
        value = old.copy()
        for field, v in update.items():
            value.set_editor_property(field, v)
        return value
    if isinstance(old, unreal.Name):
        return unreal.Name(text)
    if isinstance(old, unreal.Text):
        return unreal.Text(text)
    if isinstance(old, str):
        return raw
    if old is None or isinstance(old, unreal.Object):
        if text.lower() in ("", "none", "null"):
            return None
        ref = text.split("'")[1] if "'" in text else text
        found = unreal.find_object(None, ref) or _try(lambda: unreal.load_asset(ref.split(".")[0])) or \
            _try(lambda: unreal.load_object(None, ref))
        if found is None:
            raise ValueError(f"no object at '{ref}'")
        return found
    raise ValueError(f"{type(old).__name__} values are not supported here; use ue py with set_editor_property")


def _legacy_text(value):
    if isinstance(value, unreal.StructBase):
        return _compact(_struct_fields(value))
    if isinstance(value, unreal.EnumBase):
        return value.name
    if isinstance(value, unreal.Object):
        return _label(value)
    return "None" if value is None else str(value).lower() if isinstance(value, bool) else _compact(value)


def _legacy_same(a, b):
    if isinstance(a, unreal.StructBase) and isinstance(b, unreal.StructBase):
        return _same(_round(_struct_fields(a)), _round(_struct_fields(b)))
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= 1e-4 * max(1.0, abs(a))
    return a == b


def _legacy_set(obj, requested):
    """set_props without Epic's ToolsetLibrary (UE 5.4-5.7): set_editor_property in one editor transaction, values
    parsed by the type of the current value, read back, and put back by hand if anything fails."""
    schema = dict(_DOC_PROPERTY.findall(type(obj).__doc__ or ""))
    by_key = {_legacy_key(n): n for n in schema}
    is_actor = isinstance(obj, unreal.Actor)
    plan = []
    for key, raw in requested.items():
        relative = key.endswith("+")
        base = key[:-1] if relative else key
        if is_actor and base.lower() in _ACTOR_POSE:
            plan.append((base.lower(), relative, raw))
            continue
        name = by_key.get(_legacy_key(base))
        if name is None:
            import difflib
            close = difflib.get_close_matches(_legacy_key(base), list(by_key), n=4, cutoff=0.6)
            owners = []
            if is_actor:  # actors usually keep the interesting properties on a component
                for comp in obj.get_components_by_class(unreal.ActorComponent):
                    names = {_legacy_key(n) for n in dict(_DOC_PROPERTY.findall(type(comp).__doc__ or ""))}
                    if _legacy_key(base) in names:
                        owners.append(f"{_actor_label(obj)}.{comp.get_name()}")
            raise RuntimeError(f"{_label(obj)} has no property '{base}'"
                               + (f"; it is on {', '.join(owners[:3])}" if owners else "")
                               + (f" (did you mean: {', '.join(by_key[c] for c in close)})" if close and not owners else ""))
        plan.append((name, relative, raw))
    pose_get = {"location": obj.get_actor_location, "rotation": obj.get_actor_rotation,
                "scale": obj.get_actor_scale3d} if is_actor else {}
    snap = lambda v: v.copy() if isinstance(v, unreal.StructBase) else v  # struct reads can be live views
    old = {n: snap(pose_get[n]() if n in pose_get else obj.get_editor_property(n)) for n, _, _ in plan}
    new = {}
    for name, relative, raw in plan:
        try:
            value = _legacy_value(raw if isinstance(raw, str) else json.dumps(raw), old[name], name)
            if relative:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = old[name] + value
                elif isinstance(value, unreal.StructBase):  # a delta: unspecified fields are 0, not the current value
                    current = _struct_fields(old[name])
                    value = old[name].copy()
                    for field, v in _struct_fields(_legacy_value(raw, type(old[name])(), name)).items():
                        if isinstance(v, (int, float)):
                            value.set_editor_property(field, current[field] + v)
                else:
                    raise ValueError("+= needs a number or a numeric struct")
            new[name] = value
        except ValueError as exc:
            raise RuntimeError(f"{name}: {exc}") from None
    new = {n: v for n, v in new.items() if not _legacy_same(v, old[n])}
    if not new:
        return [], old, new

    def write(values):
        for n, v in values.items():
            if n == "location":
                obj.set_actor_location(v, False, True)
            elif n == "rotation":
                obj.set_actor_rotation(v, True)
            elif n == "scale":
                obj.set_actor_scale3d(v)
            else:
                obj.set_editor_property(n, v)

    with unreal.ScopedEditorTransaction(f"ue set {_label(obj)}"):
        obj.modify()
        if is_actor and obj.root_component:
            obj.root_component.modify()
        try:
            write(new)
        except Exception as exc:
            _try(lambda: write({n: old[n] for n in new}))
            raise RuntimeError(f"could not set: {exc} (put back)") from None
    after = {n: snap(pose_get[n]() if n in pose_get else obj.get_editor_property(n)) for n in new}
    ignored = [n for n in new if not _legacy_same(new[n], after[n])]
    if ignored:
        _try(lambda: write({n: old[n] for n in new}))
        raise RuntimeError("did not take: " + ", ".join(f"{n} stayed {_legacy_text(after[n])}" for n in ignored)
                           + " (put back)")
    rows = [f"{n}: {_legacy_text(old[n])} -> {_legacy_text(after[n])}" for n in new]
    return rows, old, new


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


def _console_output(world, commands):
    """Run console commands and return the log lines they wrote (timestamps stripped). Commands print to the
    log, which Python cannot hook, so read this editor's own log file across a flush."""
    from tether import beacon
    path = beacon._find_log()
    start = os.path.getsize(path) if path and os.path.isfile(path) else 0
    for command in commands:
        unreal.SystemLibrary.execute_console_command(world, command)
    unreal.SystemLibrary.execute_console_command(None, "FLUSHLOG")
    if not path:
        return []
    with open(path, "rb") as f:
        f.seek(start)
        text = f.read().decode("utf-8", "replace")
    return [re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", line) for line in text.splitlines()]


_CVAR_LAST = re.compile(r'^(\S+) = "(.*)"\s+LastSetBy: (\w+)')


def _cvar_histories(names):
    """{name: {setby: value}} from querying each variable (the engine logs a HISTORY block, then the value)."""
    out, pending = {}, {}
    for line in _console_output(None, list(names)):
        last = _CVAR_LAST.match(line)
        if last:
            out[last.group(1)] = pending
            pending = {}
            continue
        m = re.match(r"^(\w+): (.*)$", line)
        if m and not m.group(1).startswith("Log") and m.group(1) not in ("Cmd", "HISTORY"):
            pending[m.group(1)] = m.group(2)
    return out


def _cvar_same(a, b):
    """'160' and '160.000000' (or 'true' and '1') are the same value."""
    norm = {"true": "1", "false": "0"}
    a, b = norm.get(str(a).lower(), str(a)), norm.get(str(b).lower(), str(b))
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _cvar_dump(prefix=""):
    """[{name, value, setby}] for every console variable (or those starting with prefix), via DumpCVars -csv."""
    import csv
    path = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "Tether",
                        f"cvars-{os.getpid()}.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    unreal.SystemLibrary.execute_console_command(None, f"DumpCVars {prefix} -csv={path.replace(os.sep, '/')}".replace("  ", " "))
    if not os.path.exists(path):
        raise RuntimeError("DumpCVars wrote no file")
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [{"name": r["NAME"], "value": r["VALUE"], "setby": r["SETBY"]} for r in csv.DictReader(f)]
    os.remove(path)
    return rows


_DEFER_PATH = "/Temp/Tether/BP_TetherDeferredExec"


def _deferred_exec_class():
    """A transient Blueprint class (in /Temp, never saved) whose Run function executes one console command.

    Editor Python calls every UFunction inside FEditorScriptExecutionGuard, which makes each RPC run locally
    (AActor::GetFunctionCallspace): a client's Server RPC never reaches the server. Run is called by a timer on the
    target world's own tick, in the Blueprint VM, outside that guard, so commands sent through it replicate. Built
    with the native graph API: Epic's Blueprint tools resolve their arguments by path, which /Temp assets lack."""
    cls = getattr(sys, "_tether_defer_cls2", None)  # once per editor session (stashed on sys: survives reloads)
    if cls is not None and _try(lambda: cls.get_name()):
        return cls
    lib = unreal.BlueprintEditorLibrary
    bp, attempt = None, 0
    while bp is None and attempt < 20:  # a failed earlier build can leave its package name taken
        bp = lib.create_blueprint_asset_with_parent(_DEFER_PATH + (f"_{attempt}" if attempt else ""), unreal.Object)
        attempt += 1
    if bp is None:
        raise RuntimeError("could not create the deferred-command helper Blueprint in /Temp")
    lib.add_member_variable(bp, "Command", lib.get_basic_type_by_name("string"))
    lib.add_member_variable(bp, "Target", lib.get_object_reference_type(unreal.PlayerController.static_class()))
    for name in ("Command", "Target"):  # set from Python per command
        lib.set_blueprint_variable_instance_editable(bp, name, True)
    graph = unreal.BlueprintGraphEditor.create_and_edit_function_graph(bp, "Run")

    def pin(node, name, direction):
        found = [p for p in node.list_all_pins() if p.get_pin_direction() == direction
                 and str(p.get_pin_name()).lower() == name.lower()]
        if not found:
            raise RuntimeError(f"{node.get_node_title()} has no {name} pin "
                               f"({', '.join(str(p.get_pin_name()) for p in node.list_all_pins())})")
        return found[0]
    out, inp = unreal.EdGraphPinDirection.EGPD_OUTPUT, unreal.EdGraphPinDirection.EGPD_INPUT
    run = graph.create_node_from_name("Development|ExecuteConsoleCommand", unreal.Vector2D(300, 0), [])
    get_cmd = graph.add_get_member_variable_node("Command")
    get_target = graph.add_get_member_variable_node("Target")
    target = pin(get_target, "Target", out)
    links = [(graph.find_graph_entry_pin(), pin(run, "execute", inp)), (pin(get_cmd, "Command", out), pin(run, "Command", inp)),
             (target, pin(run, "SpecificPlayer", inp))]
    world_pin = [p for p in run.list_all_pins() if str(p.get_pin_name()) == "WorldContextObject"]
    links += [(target, world_pin[0])] if world_pin else []  # shown on a plain Object, which has no world of its own
    for a, b in links:
        if not a.try_create_connection(b):
            raise RuntimeError(f"could not connect {a.get_pin_name()} -> {b.get_pin_name()}")
    lib.compile_blueprint(bp)
    cls = lib.generated_class(bp)
    if cls is None:
        raise RuntimeError("could not build the deferred-command helper Blueprint")
    sys._tether_defer_cls2 = cls
    return cls


def _networked(world):
    """True for a client or server PIE world (multiplayer PIE), where RPCs matter."""
    return bool(world) and not _try(lambda: unreal.SystemLibrary.is_standalone(world), True)


def _queue_command(world, command, delay=0.0):
    """Run a console command in a PIE world on its next tick (or `delay` seconds of game time later), as that
    world's player typed it (see _deferred_exec_class). Returns at once; its output reaches the log later."""
    controller = unreal.GameplayStatics.get_player_controller(world, 0)
    if controller is None:
        raise RuntimeError(f"{world.get_name()} has no player controller to run '{command}' for")
    runner = unreal.new_object(_deferred_exec_class(), outer=controller)  # its world: the controller's
    runner.set_editor_property("Command", command)
    runner.set_editor_property("Target", controller)
    pending = getattr(sys, "_tether_deferred", None) or []  # keep it alive until its timer has fired
    sys._tether_deferred = pending[-20:] + [runner]
    if delay > 0:
        unreal.SystemLibrary.set_timer(runner, "Run", delay, False)
    else:
        unreal.SystemLibrary.set_timer_for_next_tick(runner, "Run")


def _mapped_keys():
    """Every key some input mapping context in the project maps (cached per session)."""
    keys = getattr(sys, "_tether_mapped_keys", None)
    if keys is None:
        keys = set()
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputMappingContext")):
            imc = _try(lambda: data.get_asset())
            for row in (_mapping_rows(imc) or []) if imc else []:
                key = _try(lambda: row.get_editor_property("key"))
                name = _try(lambda: str(key.get_editor_property("key_name"))) if key else None
                if name:
                    keys.add(name)
        sys._tether_mapped_keys = keys
    return keys


_MODIFIERS = {"ctrl": "LeftControl", "control": "LeftControl", "shift": "LeftShift", "alt": "LeftAlt",
              "cmd": "LeftCommand", "win": "LeftCommand"}


# Queued `_tap` plans, run one after another from the Slate post-tick. Stashed on sys like _HELD.
_TAPS = getattr(sys, "_tether_taps", None) or {"queue": [], "handle": None}
sys._tether_taps = _TAPS


def _tap_tick(_delta):
    if not _TAPS["queue"]:
        unreal.unregister_slate_post_tick_callback(_TAPS["handle"])
        _TAPS["handle"] = None
        return
    world, plan = _TAPS["queue"][0]
    _TAPS["frame"] += 1
    for when, command in plan:
        if when == _TAPS["frame"] and command:
            _try(lambda: unreal.SystemLibrary.execute_console_command(world, command))
    if _TAPS["frame"] >= plan[-1][0]:
        _TAPS["queue"].pop(0)
        _TAPS["frame"] = -1


def _tap(world, chord, value=""):
    """Tap a key or chord ("G", "Ctrl+G") in whole engine frames. A forced key sends a new IE_Pressed on every frame
    it is held, so a legacy BindKey(IE_Pressed) fires once per frame: force it for exactly one frame (two when an
    Enhanced Input mapping uses the key, which misses a press and release processed in the same frame). Modifiers
    go down two frames before the key and up after it. Sequenced at the end of each frame (Slate post-tick), so
    the frame rate does not matter. Returns a note when an Enhanced mapping may consume a chord's key."""
    parts = [p for p in re.split(r"\s*\+\s*", chord.strip()) if p]
    mods = [_MODIFIERS.get(p.lower(), p) for p in parts[:-1]]
    key = parts[-1]
    mapped = key in _mapped_keys()
    plan, frame = [], 0
    for mod in mods:
        plan.append((frame, f"Input.+key {mod}"))
    frame += 2 if mods else 0
    plan.append((frame, f"Input.+key {key} {value}".strip()))
    # Releasing a forced key also flushes every key that is down (UPlayerInput::FlushPressedKeys), modifiers
    # included: with a one-frame key, its press and the modifier's release land in the same frame and the chord
    # sees no modifier. Two frames: the first press has the modifier down, the second comes after the flush (the
    # modifier up), so a chord binding still fires once.
    frame += 2 if mapped or mods else 1
    plan.append((frame, f"Input.-key {key}"))
    for mod in mods:
        plan.append((frame + 1, f"Input.-key {mod}"))
    plan.append((plan[-1][0] + 1, ""))  # a frame with everything up, so a tap queued next is a new press
    # Taps queue behind each other: two `key Down tap` steps sent in one frame would otherwise overlap and
    # read as a single press
    _TAPS["queue"].append((world, plan))
    if _TAPS.get("handle") is None:
        _TAPS["frame"] = -1
        _TAPS["handle"] = unreal.register_slate_post_tick_callback(_tap_tick)
    if mods and mapped:
        return (f"{key} is also mapped in Enhanced Input: its mapping usually consumes the key, so a legacy BindKey chord "
                f"on {chord} may never fire (the engine's input stack, not this tap)")
    return ""


def _raw_input(world, verb, rest):
    """`pie do` raw input: keys go in as if pressed (Enhanced Input's forced keys), so key mappings, triggers and
    modifiers (dead zones, scalars, swizzles) apply, unlike `hold IA_x`, which injects the action's value.
      key <Key> down [value] | up | tap      mouse <dx> <dy>      stick <Key> x,y [seconds]"""
    def force(key, value=""):
        unreal.SystemLibrary.execute_console_command(world, f"Input.+key {key} {value}".strip())

    def release(key, delay=0.0):
        _queue_command(world, f"Input.-key {key}", delay)
    parts = rest.split()
    if verb == "key":
        if len(parts) < 2 or parts[1].lower() not in ("down", "up", "tap"):
            raise RuntimeError("key <Key> down [value] | up | tap   (Key as Unreal names it: SpaceBar, LeftShift, E)")
        key, action = parts[0], parts[1].lower()
        if action == "tap":  # whole frames, chords included (Ctrl+G): one IE_Pressed per tap
            note = _tap(world, key, " ".join(parts[2:]))
            return f"{key} tap" + (f" ({note})" if note else "")
        if action == "up":
            unreal.SystemLibrary.execute_console_command(world, f"Input.-key {key}")
        else:
            force(key, " ".join(parts[2:]))
        return f"{key} {action}"
    if verb == "mouse":
        nums = _numbers(",".join(parts))
        if not nums or len(nums) != 2:
            raise RuntimeError("mouse <dx> <dy>: mouse movement in counts, for one frame")
        dx, dy = nums
        if "Mouse2D" in _mapped_keys() or not ({"MouseX", "MouseY"} & _mapped_keys()):
            force("Mouse2D", f"X={dx:g} Y={dy:g}")
            release("Mouse2D")  # one frame: a mouse delta is per frame
            return f"Mouse2D ({dx:g}, {dy:g}) for one frame"
        for key, value in (("MouseX", dx), ("MouseY", dy)):
            if value:
                force(key, f"{value:g}")
                release(key)
        return f"MouseX {dx:g}, MouseY {dy:g} for one frame"
    if verb == "stick":
        if len(parts) < 2:
            raise RuntimeError("stick <Key> x,y [seconds]   e.g. stick Gamepad_Right2D 0.25,0 1")
        key, nums = parts[0], _numbers(parts[1]) or []
        value = f"X={nums[0]:g} Y={nums[1]:g}" if len(nums) == 2 else f"{nums[0]:g}" if nums else ""
        force(key, value)
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        if seconds:
            release(key, seconds)
        return f"{key} at {parts[1]}" + (f" for {seconds:g}s" if seconds else " until `key {key} up`")
    raise RuntimeError(verb)


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
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

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
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            Which world the command ran in.
        """
        _authorize(token)
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        game = _game_world()  # checked first: get_editor_world() logs an error during PIE
        if pie and game is None:
            raise RuntimeError("PIE is not running")
        world = game if game is not None else editor.get_editor_world()
        if world is None:
            raise RuntimeError("no editor world")
        if _networked(game):  # from Python every RPC would run locally; run it from the world's own tick
            _queue_command(game, command)
            return f"PIE world {world.get_name()} (queued for its next tick)"
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
        obj = _player_target(target) or _resolve(target, pie)
        if _LEGACY:
            return _legacy_props(obj, match, limit)
        cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
        if not unreal.ToolsetLibrary.list_struct_properties(cls):  # Epic's schema gave up on a property type
            return _legacy_props(obj, match, limit)
        schema = _reflect(unreal.ToolsetLibrary.list_struct_properties(cls), obj)
        names = list(schema)
        if match:
            rx = re.compile(match, re.I)
            names = [n for n in names if rx.search(n) or rx.search(_type_name(schema[n]))]
        values = _reflect(unreal.ToolsetLibrary.get_object_properties(obj, names), obj) if names else {}
        rows = [f"{n}: {_type_name(schema[n])} = {_short(_masked(n, _round(values.get(n))))}" for n in names]
        hidden = []
        if not isinstance(obj, unreal.Struct):  # UPROPERTYs that are not Blueprint-visible: the engine's export
            seen = {n.lower() for n in schema}
            rx = re.compile(match, re.I) if match else None
            hidden = [(owner, name, value) for owner, name, value in _try(lambda: _dump_props(obj), []) or []
                      if name.lower() not in seen and (not rx or rx.search(name))]
            if hidden:
                rows.append(f"not Blueprint-visible ({len(hidden)}, read through the engine's export; "
                            "pie watch reads them too):")
                rows += [f"  {name} = {_short(_masked(name, value))}  ({owner})" for owner, name, value in hidden]
        head = (f"{_label(obj)} ({cls.get_name()}, {len(schema)} props" + (f" + {len(hidden)} hidden" if hidden else "")
                + (f", {len(names) + len(hidden)} match" if match else "") + ")")
        return _capped(head, rows, limit)

    @_tool
    @staticmethod
    def set_props(target: str, values: str, pie: bool = False, token: str = "", force: bool = False) -> str:
        """Set reflected properties from compact text, as one undoable editor transaction.

        Values are JSON, true/false, numbers, enum names, object paths, or positional tuples for
        structs ('0,0,200' for a Vector; missing trailing fields keep their values). A key ending
        in '+' adds to the current value. Actors also accept location, rotation and scale. If any
        property fails, the whole change is rolled back.

        Args:
            target: Object or asset path, actor label or name, or 'ActorLabel.ComponentName'.
            values: JSON object of property name to value text, e.g. {"Intensity": "10", "location+": "0,0,200"}.
            pie: Change the PIE world instead of the editor level (not undoable, not saved).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).
            force: Set values that are already set too, so the change notification fires (an actor's construction
                script reruns).

        Returns:
            One 'name: old -> new' line per property.
        """
        _authorize(token)
        requested_all = json.loads(values)
        paths = [(k, v) for k, v in requested_all.items() if isinstance(requested_all, dict) and ("[" in k or "." in k)]
        if paths:  # struct fields (Name[i].Field): through the struct's text, which also reaches uint32 fields
            obj = _resolve(target, pie)
            rows = _set_paths(obj, paths, pie)
            rest = {k: v for k, v in requested_all.items() if (k, v) not in paths}
            head = _label(obj) + (" (PIE)" if pie else "")
            if not rest:
                return "\n".join([head] + rows)
            more = TetherCore.set_props(target, json.dumps(rest), pie, token, force).split("\n")[1:]
            return "\n".join([head] + rows + more)
        legacy_obj = _resolve(target, pie) if not _LEGACY else None
        legacy_cls = legacy_obj and (legacy_obj if isinstance(legacy_obj, unreal.Struct) else legacy_obj.get_class())
        if _LEGACY or (legacy_cls and not unreal.ToolsetLibrary.list_struct_properties(legacy_cls)):
            # Without Epic's ToolsetLibrary, or when its schema gives up on one of the class's property types
            obj = _resolve(target, pie)
            requested = json.loads(values)
            if not isinstance(requested, dict) or not requested:
                raise RuntimeError('values must be a non-empty JSON object, e.g. {"Intensity": "10"}')
            rows, old, new = _legacy_set(obj, requested)
            if not rows:
                return _label(obj) + (" (PIE)" if pie else "") + "\n(no change: already set)"
            if not pie:  # `ue undo` puts these values back (there is no undo-history depth to check before 5.8)
                _UNDO.append({"legacy": True, "target": obj.get_path_name(), "old": {n: old[n] for n in new},
                              "new": new, "label": f"{_label(obj)}: " + ", ".join(new)})
            return "\n".join([_label(obj) + (" (PIE)" if pie else "")] + rows)
        obj = _resolve(target, pie)
        cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
        schema = _reflect(unreal.ToolsetLibrary.list_struct_properties(cls), obj)
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
                            owners.append(f"{_actor_label(obj)}.{comp.get_name()}")
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
        # Skip values that are already set: a write that changes nothing opens an empty transaction, which the
        # editor drops, and `ue undo` would then line up with the wrong entry.
        unchanged = [n for n in new if n in old and isinstance(new[n], (int, float, bool, dict)) and _took(new[n], old[n])
                     and (not isinstance(new[n], dict) or all(isinstance(v, (int, float, bool)) for v in new[n].values()))
                     and not force]
        for name in unchanged:
            del new[name]
        if not new:
            return (_label(obj) + (" (PIE)" if pie else "") + "\n(no change: " + ", ".join(unchanged) + " already set; "
                    "--force sets it anyway, firing the change notification: `ue rerun` reruns a construction script)")

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
        count = unreal.ToolsetLibrary.get_active_undo_count()
        if not pie and count > before:  # remember where the undo history stands after our change, for `ue undo`
            _UNDO.append({"count": count, "label": f"{_label(obj)}: " + ", ".join(new)})
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
        simulate = pie and _simulating(game)
        if pie and not simulate:
            loc, rotation = cam.get_camera_location(), cam.get_camera_rotation()
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
                           "scale": {"x": 1, "y": 1, "z": 1}, "pie": pie, "simulate": simulate})

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
            label, cls, name = _actor_label(actor), actor.get_class().get_name(), actor.get_name()
            # log lines name actors by object name (NRRoadNode_1031), the outliner by label (N_X_2_4): match both
            if rx and not (rx.search(label) or rx.search(cls) or rx.search(name)):
                continue
            loc = actor.get_actor_location()
            rows.append(f"{label}" + (f" ({name})" if name != label else "")
                        + f"  {cls}  ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})")
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
    def spawn(what: str, at: str = "0,0,0", rot: str = "", label: str = "", ground: bool = False, align: bool = False,
              token: str = "") -> str:
        """Place an actor in the edited level (undoable): from a class (PointLight, /Script/Engine.TargetPoint,
        a Blueprint's generated class) or an asset (a StaticMesh, a Blueprint, ...).

        Args:
            what: Class name, /Script/... class path, or an asset path (/Game/..., /Engine/..., a plugin's /Niagara/...).
            at: Location 'x,y,z'.
            rot: Rotation 'pitch,yaw,roll'.
            label: Actor label to give it.
            ground: Drop it onto the first surface below `at` (its bounds' bottom on the ground).
            align: With ground: tilt it to the surface normal.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            The new actor's label, class and location.
        """
        _authorize(token)
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        nums = _numbers(at) or [0, 0, 0]
        if len(nums) != 3:
            raise RuntimeError(f"at: expected x,y,z, got '{at}'")
        location = unreal.Vector(*nums)
        r = _numbers(rot) if rot else [0, 0, 0]
        rotation = unreal.Rotator(roll=r[2], pitch=r[0], yaw=r[1])
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        if what.startswith("/") and not what.startswith("/Script/"):  # an asset (mesh, Blueprint, Niagara system...)
            asset, cls = unreal.load_asset(what), None
            if asset is None:
                raise RuntimeError(f"no asset at {what} (ue find)")
        else:
            asset = None
            cls = unreal.load_class(None, what) if what.startswith("/Script/") else getattr(unreal, what, None)
            if cls is None:
                raise RuntimeError(f"no class '{what}' (try ue api {what}, or a /Script/Module.Class path)")
        with unreal.ScopedEditorTransaction(f"ue spawn {label or what}"):  # spawn + label = one undo step
            actor = (subsystem.spawn_actor_from_object(asset, location, rotation) if asset is not None
                     else subsystem.spawn_actor_from_class(cls, location, rotation))
            if actor is not None and label:
                actor.set_actor_label(label)
            landed = None
            if actor is not None and ground:
                try:
                    landed = _ground_actor(actor, align)[2]
                except RuntimeError as exc:
                    landed = f"not grounded: {exc}"
        if actor is None:
            raise RuntimeError(f"could not spawn {what} (see ue log)")
        _record_undo(before, f"spawn {_actor_label(actor)}")
        loc = actor.get_actor_location()
        return f"spawned {_actor_label(actor)} ({actor.get_class().get_name()}) at ({loc.x:g}, {loc.y:g}, {loc.z:.6g})" + \
            (f" on {landed}" if landed and not landed.startswith("not grounded") else f" ({landed})" if landed else "")

    @_tool
    @staticmethod
    def pie_view(pose: str = "", token: str = "") -> str:
        """Show the PIE player's screen from another point (a spectator shot), or give the view back.

        Uses the engine's debug camera (the player's pawn and controller are left alone, and the game keeps
        running): it is turned on, its help text hidden, and its camera moved to the pose. Python cannot spawn
        actors in the PIE world, so this is the one free camera there is. While it is on, console commands go
        to the debug camera, so capture through the Slate inspector. An empty pose turns it off again.

        Args:
            pose: {"location": {x,y,z}, "rotation": {pitch,yaw,roll}} as from shot_pose, or "" to restore.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was done.
        """
        _authorize(token)
        game = _game_world()
        if game is None:
            raise RuntimeError("PIE is not running")
        current = unreal.GameplayStatics.get_player_controller(game, 0)
        debug = current if isinstance(current, unreal.DebugCameraController) else None
        if not pose:
            if debug is None:
                return "no spectator view to restore"
            debug.get_editor_property("cheat_manager").disable_debug_camera()
            return "view restored"
        if debug is None:
            cheats = current.get_editor_property("cheat_manager") if current else None
            if cheats is None:
                raise RuntimeError("no cheat manager on the player controller (cheats are off), so no debug camera")
            cheats.enable_debug_camera()
            debug = unreal.GameplayStatics.get_player_controller(game, 0)
            if not isinstance(debug, unreal.DebugCameraController):
                raise RuntimeError("the debug camera did not start")
        hud = _try(lambda: debug.get_hud())
        if hud:
            _try(lambda: hud.set_editor_property("show_hud", False))  # its help text and hit info
        data = json.loads(pose)
        loc, rot = data["location"], data["rotation"]
        location = unreal.Vector(loc["x"], loc["y"], loc["z"])
        rotation = unreal.Rotator(roll=rot["roll"], pitch=rot["pitch"], yaw=rot["yaw"])
        pawn = debug.get_spectator_pawn()
        if pawn:
            pawn.set_actor_location_and_rotation(location, rotation, False, True)
        debug.set_control_rotation(rotation)
        return "spectator view"

    @_tool
    @staticmethod
    def drop(targets: str, align: bool = False, group: bool = False, rest: str = "lowest", token: str = "") -> str:
        """Put actors on the ground: move each straight down (or up, if buried) so the bottom of its bounds rests
        on the first blocking surface below it. With group, move them (and what is attached to them) as one unit,
        so a structure built of separate pieces keeps its shape on a slope. One undoable step.

        Args:
            targets: Comma-separated actor labels or names.
            align: Also tilt each to the surface normal (not with group).
            group: Move all of them, and their attached actors, by one offset.
            rest: With group: rest the group's lowest point on the lowest, highest or mean ground under it.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One line per actor (or one for the group): the height change and what it landed on.
        """
        _authorize(token)
        actors = []
        for ref in [t.strip() for t in targets.split(",") if t.strip()]:
            obj = _resolve(ref)
            if not isinstance(obj, unreal.Actor):
                raise RuntimeError(f"'{ref}' is not an actor")
            actors.append(obj)
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        rows = []
        if group:
            if rest not in ("lowest", "highest", "mean"):
                raise RuntimeError("rest must be lowest, highest or mean")
            members = _with_attached(actors)
            with unreal.ScopedEditorTransaction("ue drop --group"):
                for actor in members:
                    actor.modify()
                    if actor.root_component:
                        actor.root_component.modify()
                dz, spread, samples = _ground_group(members, rest)
            _record_undo(before, "drop group " + ", ".join(_actor_label(a) for a in actors))
            extra = len(members) - len(actors)
            return (f"group of {len(members)} actor(s)" + (f" ({extra} attached)" if extra else "") +
                    f" moved {dz:+.0f} cm as one, resting on the {rest} ground under it; the ground varies "
                    f"{spread:.0f} cm over {samples} samples" +
                    (" (on a slope: the uphill side sits that much into it, or try flatter ground)"
                     if rest == "lowest" and spread > 50 else ""))
        with unreal.ScopedEditorTransaction("ue drop"):
            for actor in actors:
                actor.modify()
                if actor.root_component:
                    actor.root_component.modify()
                try:
                    old_z, new_z, landed = _ground_actor(actor, align)
                    rows.append(f"{_actor_label(actor)}: z {old_z:.6g} -> {new_z:.6g} (on {landed})")
                except RuntimeError as exc:
                    rows.append(f"{_actor_label(actor)}: {exc}")
        _record_undo(before, "drop " + ", ".join(_actor_label(a) for a in actors))
        return "\n".join(rows)

    @_tool
    @staticmethod
    def scatter(what: str, count: int, area: str = "", line: str = "", yaw: str = "0,360", scale: str = "1,1",
                spacing: float = 0.0, seed: int = 0, align: bool = False, label: str = "", token: str = "") -> str:
        """Place many copies of an asset or class on the ground: random points in a circle (area 'x,y,radius') or
        evenly along a line (line 'x,y,z x,y,z'), each dropped onto the first surface below it, with random yaw
        and uniform scale ranges and a minimum spacing. One undoable step.

        Args:
            what: Asset path (mesh, Blueprint, ...) or class name, as for spawn.
            count: How many to place.
            area: 'x,y,radius' circle to scatter in.
            line: 'x,y,z x,y,z' segment to place along (evenly spaced).
            yaw: 'min,max' degrees.
            scale: 'min,max' uniform scale.
            spacing: Minimum distance between copies (cm).
            seed: Random seed (same seed, same layout).
            align: Tilt each to the surface.
            label: Label prefix (default: the asset's name).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            How many were placed, and why any were not.
        """
        _authorize(token)
        import math
        import random
        rng = random.Random(seed)
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if what.startswith(("/Game/", "/Engine/")):
            asset = unreal.load_asset(what.split(".")[0])
            if asset is None:
                raise RuntimeError(f"no asset at {what} (ue find)")
            cls = unreal.BlueprintEditorLibrary.generated_class(asset) if isinstance(asset, unreal.Blueprint) else None
        else:
            asset = None
            cls = unreal.load_class(None, what) if what.startswith("/Script/") else getattr(unreal, what, None)
            if cls is None:
                raise RuntimeError(f"no class '{what}'")
        yaw_lo, yaw_hi = (_numbers(yaw) or [0, 360])[:2]
        scale_lo, scale_hi = ((_numbers(scale) or [1, 1]) * 2)[:2]
        points = []
        if line:
            ends = [(_numbers(p) or []) for p in line.split()]
            if len(ends) != 2 or any(len(e) != 3 for e in ends):
                raise RuntimeError("line: expected 'x,y,z x,y,z'")
            (ax, ay, _), (bx, by, _) = ends
            points = [(ax + (bx - ax) * i / max(count - 1, 1), ay + (by - ay) * i / max(count - 1, 1))
                      for i in range(count)]
        else:
            nums = _numbers(area) or []
            if len(nums) != 3:
                raise RuntimeError("area: expected 'x,y,radius' (or use line)")
            cx, cy, radius = nums
            tries = 0
            while len(points) < count and tries < count * 50:
                tries += 1
                r, a = radius * math.sqrt(rng.random()), rng.random() * 2 * math.pi
                p = (cx + r * math.cos(a), cy + r * math.sin(a))
                if spacing and any((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 < spacing ** 2 for q in points):
                    continue
                points.append(p)
        prefix = label or (asset.get_name() if asset else what.rsplit(".", 1)[-1])
        placed, no_ground = [], 0
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        with unreal.ScopedEditorTransaction(f"ue scatter {prefix} x{len(points)}"):
            for i, (x, y) in enumerate(points):
                rotation = unreal.Rotator(roll=0, pitch=0, yaw=rng.uniform(yaw_lo, yaw_hi))
                spot = unreal.Vector(x, y, 200000)  # dropped from high above onto the first surface below
                actor = (subsystem.spawn_actor_from_class(cls, spot, rotation) if cls is not None
                         else subsystem.spawn_actor_from_object(asset, spot, rotation))
                if actor is None:
                    continue
                s = rng.uniform(scale_lo, scale_hi)
                actor.set_actor_scale3d(unreal.Vector(s, s, s))
                try:
                    _ground_actor(actor, align)
                except RuntimeError:
                    subsystem.destroy_actor(actor)
                    no_ground += 1
                    continue
                actor.set_actor_label(f"{prefix}_{i + 1:02d}")
                placed.append(actor)
        if placed:
            _record_undo(before, f"scatter {prefix} x{len(placed)}")
        notes = []
        if len(points) < count:
            notes.append(f"{count - len(points)} did not fit with spacing {spacing:g}")
        if no_ground:
            notes.append(f"{no_ground} had no ground below")
        zs = [a.get_actor_location().z for a in placed]
        return (f"placed {len(placed)} of {count} {prefix}" + (f" (z {min(zs):.0f} to {max(zs):.0f})" if zs else "")
                + (": " + "; ".join(notes) if notes else "") + (f"; labels {prefix}_01.." if placed else ""))

    @_tool
    @staticmethod
    def wp(action: str = "info", match: str = "", area: str = "-", limit: int = 60, token: str = "") -> str:
        """World Partition in the editor: a summary (actors in total and loaded, data layers, runtime grids), every
        actor including unloaded ones (from actor descriptors), or loading/unloading actors by area or name so
        other commands can see them.

        Args:
            action: info, actors, load or unload.
            match: Regex on actor label, name or class (actors, load, unload).
            area: 'x,y,radius' for load/unload (and to narrow actors).
            limit: Maximum rows.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it; load/unload).

        Returns:
            Text.
        """
        lib = unreal.WorldPartitionBlueprintLibrary
        descs = _try(lambda: list(lib.get_actor_descs() or []), None)
        if not descs:
            raise RuntimeError("the edited level is not a World Partition level (or has no actors)")
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        key = lambda guid: _try(lambda: guid.export_text(), "") if guid is not None else ""  # noqa: E731
        loaded = {key(_try(lambda: a.get_editor_property("actor_guid"))) for a in
                  unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()}
        rx = re.compile(match, re.I) if match else None
        circle = _numbers(area) if area not in ("", "-") else None

        def info(d):
            return {k: _try(lambda: d.get_editor_property(k)) for k in
                    ("label", "name", "class_", "native_class", "guid", "bounds", "is_spatially_loaded", "runtime_grid",
                     "data_layer_assets", "actor_is_editor_only")}

        def centre(i):
            b = i["bounds"]
            return ((b.min.x + b.max.x) / 2, (b.min.y + b.max.y) / 2, (b.min.z + b.max.z) / 2) if b else (0, 0, 0)

        def class_name(i):
            for c in (i["class_"], i["native_class"]):
                text = _try(lambda: c.export_text(), "") if c is not None else ""
                if text and text != "None":
                    return text.strip("'\"").rsplit(".", 1)[-1]
            return "?"
        rows = [info(d) for d in descs]

        def selected(i):
            if not rx and (class_name(i).startswith("WorldPartitionHLOD") or str(i["label"] or "").startswith("HLOD")):
                return False  # generated HLOD proxies: only when asked for by name
            if rx and not (rx.search(str(i["label"] or "")) or rx.search(str(i["name"] or "")) or rx.search(class_name(i))):
                return False
            if circle:
                x, y, _ = centre(i)
                return (x - circle[0]) ** 2 + (y - circle[1]) ** 2 <= circle[2] ** 2
            return True
        if action == "info":
            in_memory = sum(1 for i in rows if key(i["guid"]) in loaded)
            always = sum(1 for i in rows if not i["is_spatially_loaded"])
            layers, grids = {}, {}
            for i in rows:
                for layer in i["data_layer_assets"] or []:
                    layers[_try(lambda: layer.get_name(), str(layer))] = layers.get(_try(lambda: layer.get_name(), str(layer)), 0) + 1
                grid = str(i["runtime_grid"] or "")
                if grid and grid != "None":
                    grids[grid] = grids.get(grid, 0) + 1
            bounds = _try(lambda: lib.get_editor_world_bounds())
            size = f", {(bounds.max.x - bounds.min.x) / 100:.0f} x {(bounds.max.y - bounds.min.y) / 100:.0f} m" if bounds else ""
            lines = [f"{world.get_outermost().get_name()}: {len(rows)} actors{size}, {in_memory} loaded in the editor, "
                     f"{always} always loaded (not spatially loaded)"]
            if layers:
                lines.append("data layers: " + ", ".join(f"{n} ({c})" for n, c in sorted(layers.items())))
            if grids:
                lines.append("runtime grids: " + ", ".join(f"{n} ({c})" for n, c in sorted(grids.items())))
            unloaded = len(rows) - in_memory
            if unloaded:
                lines.append(f"{unloaded} actor(s) are not loaded: other ue commands do not see them until "
                             "`ue wp load x,y,r` or `ue wp load <regex>`")
            return "\n".join(lines)
        if action == "actors":
            picked = [i for i in rows if selected(i)]
            out = []
            for i in sorted(picked, key=lambda i: str(i["label"] or i["name"]))[:limit]:
                x, y, z = centre(i)
                out.append(f"{'*' if key(i['guid']) in loaded else ' '} {i['label'] or i['name']}  {class_name(i)}  "
                           f"({x:.0f}, {y:.0f}, {z:.0f})" + ("" if i["is_spatially_loaded"] else "  always loaded"))
            return _capped(f"{len(picked)} actor(s) (* = loaded in the editor)", out, limit) if len(out) == len(picked) \
                else "\n".join([f"{len(picked)} actor(s) (* = loaded in the editor)"] + out + [f"... +{len(picked) - limit} more"])
        if action in ("load", "unload"):
            _authorize(token)
            if not (rx or circle):
                raise RuntimeError(f"{action} what? an area 'x,y,radius' or a name regex")
            picked = [i for i in rows if selected(i)]
            guids = [i["guid"] for i in picked if (key(i["guid"]) in loaded) == (action == "unload")]
            if guids:
                (lib.load_actors if action == "load" else lib.unload_actors)(guids)
            now = {key(_try(lambda: a.get_editor_property("actor_guid"))) for a in
                   unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()}
            changed = sum(1 for g in guids if (key(g) in now) == (action == "load"))
            text = f"{action}ed {changed} actor(s)" + (f" ({len(picked) - len(guids)} already {action}ed)"
                                                      if len(picked) > len(guids) else "")
            if action == "unload" and changed < len(guids):
                text += (f"; {len(guids) - changed} stay loaded: the editor's own loaded regions hold them "
                         "(unload releases what `ue wp load` loaded)")
            return text
        raise RuntimeError("action: info, actors, load or unload")

    @_tool
    @staticmethod
    def post_process(at: str = "", limit: int = 80) -> str:
        """The post-process volumes in effect at a point and the settings they add up to: unbound volumes, the
        volumes the point is inside (or within the blend radius of, at a reduced weight), and in PIE the view
        camera's own post-process settings, merged by priority as the renderer does.

        Args:
            at: 'x,y,z' or an actor label/name; empty: the camera (the player's in PIE, else the level viewport's).
            limit: Maximum setting rows.

        Returns:
            Text.
        """
        game = _game_world()
        world = game or unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        camera_comp, where = None, ""
        if at:
            nums = re.findall(r"-?[\d.]+", at)
            if len(nums) == 3 and re.fullmatch(r"[-\d.,\s]+", at):
                point = unreal.Vector(*map(float, nums))
            else:
                actor = _resolve(at, pie=game is not None)
                point, where = actor.get_actor_location(), f" ({_label(actor)})"
        elif game is not None:
            manager = unreal.GameplayStatics.get_player_camera_manager(game, 0)
            point, where = manager.get_camera_location(), " (the player's camera)"
            target = _try(lambda: unreal.GameplayStatics.get_player_controller(game, 0).get_view_target())
            camera_comp = _try(lambda: target.get_component_by_class(unreal.CameraComponent)) if target else None
        else:
            info = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_level_viewport_camera_info()
            point, where = next(x for x in info if isinstance(x, unreal.Vector)), " (the level viewport's camera)"
        get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731

        def overrides(settings):
            return [n[len("override_"):] for n in dir(settings) if n.startswith("override_") and get(settings, n)]

        def show(value):
            if isinstance(value, float):
                return f"{value:.4g}"
            if isinstance(value, (unreal.LinearColor, unreal.Vector4)):
                parts = [getattr(value, k) for k in ("r", "g", "b", "a") if hasattr(value, k)] or \
                    [getattr(value, k) for k in ("x", "y", "z", "w")]
                return "(" + ", ".join(f"{v:.3g}" for v in parts) + ")"
            if isinstance(value, unreal.Object):
                return _short_path(value.get_path_name())
            if hasattr(value, "name") and not isinstance(value, str):
                return _enum_name(value)
            text = str(value)
            return text if len(text) < 80 else text[:77] + "..."
        active, idle = [], []
        for volume in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PostProcessVolume):
            if not get(volume, "enabled", True):
                idle.append((_label(volume), "disabled"))
                continue
            weight, radius = get(volume, "blend_weight", 1.0), get(volume, "blend_radius", 0.0)
            if get(volume, "unbound", False):
                how, scale = "unbound", 1.0
            else:
                brush = (_try(lambda: volume.get_components_by_class(unreal.BrushComponent), []) or [None])[0]
                found = _try(lambda: brush.get_closest_point_on_collision(point)) if brush else None
                distance = found[0] if isinstance(found, tuple) else -1.0
                if distance < 0:  # no collision to measure against: its bounds
                    origin, extent = volume.get_actor_bounds(False)
                    d = [max(0.0, abs(getattr(point, k) - getattr(origin, k)) - getattr(extent, k)) for k in "xyz"]
                    distance = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
                if distance <= 0:
                    how, scale = "inside", 1.0
                elif distance < radius:
                    how, scale = f"{distance:.0f} cm outside, in its {radius:.0f} cm blend", 1.0 - distance / radius
                else:
                    idle.append((_label(volume), f"{distance / 100:.0f} m away"))
                    continue
            settings = get(volume, "settings")
            active.append({"name": _label(volume), "priority": get(volume, "priority", 0.0), "weight": weight * scale,
                           "how": how, "settings": settings, "keys": overrides(settings)})
        active.sort(key=lambda v: v["priority"])  # the renderer blends lowest priority first
        if camera_comp is not None and get(camera_comp, "post_process_blend_weight", 0.0) > 0:
            settings = get(camera_comp, "post_process_settings")
            keys = overrides(settings)
            if keys:
                active.append({"name": f"camera {_label(camera_comp)}", "priority": float("inf"),
                               "weight": get(camera_comp, "post_process_blend_weight", 1.0), "how": "the view camera",
                               "settings": settings, "keys": keys})
        lines = [f"post process at ({point.x:.0f}, {point.y:.0f}, {point.z:.0f}){where}:"]
        if not active:
            lines.append("  no post-process volume in effect here: engine defaults and the project's settings")
        for v in reversed(active):
            prio = "camera, last" if v["priority"] == float("inf") else f"priority {v['priority']:g}"
            lines.append(f"  {v['name']}: {v['how']}, {prio}, weight {v['weight']:.2f}, {len(v['keys'])} setting(s)")
        if idle:
            lines.append("  not here: " + ", ".join(f"{n} ({why})" for n, why in idle[:8])
                         + (f" (+{len(idle) - 8} more)" if len(idle) > 8 else ""))
        merged = {}
        for v in active:  # later (higher priority) wins; a weight below 1 blends into what is under it
            for key in v["keys"]:
                merged[key] = (show(get(v["settings"], key)), v["name"], v["weight"], merged.get(key))
        if merged:
            lines.append(f"settings in effect ({len(merged)}):")
            for key in sorted(merged)[:limit]:
                value, source, weight, under = merged[key]
                blend = ""
                if weight < 0.999:
                    blend = f", weight {weight:.2f} over " + (f"{under[0]} from {under[1]}" if under else "the default")
                lines.append(f"  {key} = {value}  ({source}{blend})")
            if len(merged) > limit:
                lines.append(f"  ... {len(merged) - limit} more (--limit)")
        return "\n".join(lines)

    @_tool
    @staticmethod
    def lights(limit: int = 60) -> str:
        """The edited level's lighting in one view: every light (type, mobility, intensity, colour, radius,
        shadows, IES, light function), sky light, height fog, post-process exposure, and notes on what commonly
        goes wrong (auto exposure lifting intended darkness, huge radii, costly shadowing lights).

        Args:
            limit: Maximum light rows.

        Returns:
            Text.
        """
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
        get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731

        def colour(c):
            if not c:
                return "?"
            if isinstance(c, unreal.Color):  # 0-255 bytes
                return f"rgb({c.r}, {c.g}, {c.b})"
            return f"({c.r:.2f}, {c.g:.2f}, {c.b:.2f})"
        rows, notes, counts = [], [], {}
        movable_shadows = 0
        # Light components on any actor: Blueprints such as Ultra Dynamic Sky carry their sun, moon and sky light
        found = []
        for actor in actors:
            for comp in _try(lambda: actor.get_components_by_class(unreal.LightComponentBase), []) or []:
                found.append((actor, comp))
        for actor, comp in found:
            own = isinstance(actor, (unreal.Light, unreal.SkyLight))
            name = _label(actor) if own else f"{_label(actor)}.{comp.get_name()}"
            kind = comp.get_class().get_name().replace("Component", "").replace("Light", " light").strip().lower()
            mobility = _enum_name(get(comp, "mobility", "?"))
            counts[mobility] = counts.get(mobility, 0) + 1
            visible = get(comp, "visible", True) and not get(actor, "hidden", False)
            shadows = get(comp, "cast_shadows", False)
            if shadows and mobility == "movable" and not isinstance(comp, unreal.SkyLightComponent):
                movable_shadows += 1
            parts = [f"{kind:<15} {name:<24} {mobility}"]
            intensity = get(comp, "intensity")
            units = _enum_name(get(comp, "intensity_units", "")) if not isinstance(comp, (unreal.DirectionalLightComponent,
                                                                                          unreal.SkyLightComponent)) else ""
            unit_text = {"candelas": "cd", "lumens": "lm", "unitless": "", "ev": "EV"}.get(units, units)
            if isinstance(comp, unreal.DirectionalLightComponent):
                unit_text = "lux"
            if intensity is not None:
                parts.append(f"intensity {intensity:g}" if isinstance(comp, unreal.SkyLightComponent)
                             else f"{intensity:g} {unit_text}".strip())
            if not isinstance(comp, unreal.SkyLightComponent):
                parts.append(colour(get(comp, "light_color")))
                if get(comp, "use_temperature", False):
                    parts.append(f"{get(comp, 'temperature', 6500):.0f} K")
            radius = get(comp, "attenuation_radius")
            if radius is not None:
                parts.append(f"radius {radius / 100:g} m")
                if radius > 5000:
                    notes.append(f"{name}: {radius / 100:g} m radius (cost grows with what it reaches)")
            if isinstance(comp, unreal.SpotLightComponent):
                parts.append(f"cone {get(comp, 'outer_cone_angle', 0):g} deg")
            parts.append("shadows" if shadows else "no shadows")
            if get(comp, "ies_texture"):
                parts.append(f"IES {get(comp, 'ies_texture').get_name()}")
            if get(comp, "light_function_material"):
                parts.append(f"light function {get(comp, 'light_function_material').get_name()}")
            if isinstance(comp, unreal.SkyLightComponent):
                parts.append("real-time capture" if get(comp, "real_time_capture", False) else
                             _enum_name(get(comp, "source_type", "")).replace("sls", "source: "))
            if isinstance(comp, unreal.DirectionalLightComponent) and get(comp, "atmosphere_sun_light", False):
                parts.append("atmosphere sun")
            volumetric = get(comp, "volumetric_scattering_intensity", 1.0)
            if volumetric not in (None, 1.0):
                parts.append(f"volumetric x{volumetric:g}")
            rows.append("  ".join(parts) + ("" if visible else "  (hidden)"))
        lines = [f"{len(rows)} light(s): " + ", ".join(f"{n} {m}" for m, n in sorted(counts.items()))
                 + (f"; {movable_shadows} movable shadow-casting" if movable_shadows else "")]
        lines += rows[:limit] + ([f"... +{len(rows) - limit} more"] if len(rows) > limit else [])
        for fog in [a for a in actors if isinstance(a, unreal.ExponentialHeightFog)]:
            comp = get(fog, "component")
            if comp:
                lines.append(f"fog {_label(fog)}: density {get(comp, 'fog_density', 0):g}, height falloff "
                             f"{get(comp, 'fog_height_falloff', 0):g}, inscattering {colour(get(comp, 'fog_inscattering_luminance') or get(comp, 'fog_inscattering_color'))}"
                             + (", volumetric fog" if get(comp, "volumetric_fog", False) else ""))
        auto_exposure = None
        # Post process volumes and post process components (a sky Blueprint's own): the highest priority among
        # the unbound ones wins for each setting it overrides
        sources = [(a, _label(a)) for a in actors if isinstance(a, unreal.PostProcessVolume)]
        for actor in actors:
            for comp in _try(lambda: actor.get_components_by_class(unreal.PostProcessComponent), []) or []:
                sources.append((comp, f"{_label(actor)}.{comp.get_name()}"))
        exposure_owners = []
        for volume, name in sources:
            settings = get(volume, "settings")
            if settings is None:
                continue
            bits = []
            if (get(settings, "override_auto_exposure_method", False) or get(settings, "override_auto_exposure_bias", False)) \
                    and get(volume, "unbound", False) and get(volume, "enabled", True) is not False:
                exposure_owners.append((get(volume, "priority", 0), name))
            if get(settings, "override_auto_exposure_method", False):
                method = _enum_name(get(settings, "auto_exposure_method", "")).replace("aem", "")
                bits.append(f"exposure {method}")
                auto_exposure = method != "manual"
            if get(settings, "override_auto_exposure_min_brightness", False) or get(settings, "override_auto_exposure_max_brightness", False):
                bits.append(f"EV100 {get(settings, 'auto_exposure_min_brightness', 0):g}..{get(settings, 'auto_exposure_max_brightness', 0):g}")
            if get(settings, "override_auto_exposure_bias", False):
                bits.append(f"compensation {get(settings, 'auto_exposure_bias', 0):g}")
            if get(settings, "override_bloom_intensity", False):
                bits.append(f"bloom {get(settings, 'bloom_intensity', 0):g}")
            scope = "unbound" if get(volume, "unbound", False) else "bounded"
            lines.append(f"post process {name} ({scope}, priority {get(volume, 'priority', 0):g}): "
                         + (", ".join(bits) if bits else "no exposure overrides"))
        if len(exposure_owners) > 1:
            exposure_owners.sort(reverse=True)
            notes.append(f"exposure set by {len(exposure_owners)} unbound post processes; {exposure_owners[0][1]} "
                         f"(priority {exposure_owners[0][0]:g}) wins over "
                         + ", ".join(f"{n} ({p:g})" for p, n in exposure_owners[1:])
                         + ": give the one you edit a higher priority")
        # Lumen gives primitives kilometres across no reflections (nor surface-cache lighting)
        for actor in actors:
            if isinstance(actor, unreal.Landscape) or re.search(r"sky|dome|atmos|cloud", _label(actor), re.I):
                continue
            if not _try(lambda: actor.get_components_by_class(unreal.StaticMeshComponent)):
                continue
            extent = _try(lambda: actor.get_actor_bounds(False)[1])
            if extent is None:
                continue
            size = sorted((extent.x * 2, extent.y * 2, extent.z * 2), reverse=True)
            if size[1] > 200000:  # two axes over 2 km: a slab or a wall (sky spheres are skipped above)
                notes.append(f"{_label(actor)}: {size[0] / 100000:.1f} x {size[1] / 100000:.1f} km; Lumen gives "
                             "primitives this large no reflections: split it into tiles of ~1 km")
        project_auto = True
        config = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir()), "DefaultEngine.ini")
        try:
            with open(config, encoding="utf-8-sig", errors="replace") as f:
                m = re.search(r"^r\.DefaultFeature\.AutoExposure\s*=\s*(\w+)", f.read(), re.M | re.I)
                if m:
                    project_auto = m.group(1).lower() in ("1", "true")
        except OSError:
            pass
        lines.append(f"project default: auto exposure {'on' if project_auto else 'off'} (r.DefaultFeature.AutoExposure)")
        if auto_exposure or (auto_exposure is None and project_auto):
            notes.append("auto exposure is on: the camera brightens dark areas toward middle grey, so intended darkness "
                         "fades in; for a dark game use Manual exposure or raise the minimum EV100 in an unbound post "
                         "process volume")
        if movable_shadows > 6:
            notes.append(f"{movable_shadows} movable lights cast shadows (each costs shadow depth passes every frame)")
        if notes:
            lines.append("notes:")
            lines += [f"  - {n}" for n in notes[:12]]
        return "\n".join(lines)

    @_tool
    @staticmethod
    def class_info(name: str, limit: int = 40, section: str = "-", grep: str = "-") -> str:
        """A class at a glance, C++ or Blueprint: parent chain, its own properties (with Blueprint access) and
        functions, a Blueprint's variables, components, functions and events, the classes and Blueprints that
        derive from it, and (for a Blueprint) what references it.

        Args:
            name: A class name (Character, AMyEnemy, UMyComponent), /Script/Module.Class, a Blueprint asset path,
                or a Blueprint's name (BP_Door).
            limit: Maximum rows per list.
            section: '-' for the overview; props, functions, vars, components, events, derived or refs for that
                list alone, one item per line (functions with their parameters and types).
            grep: Regex the listed items must match ('-' for all).

        Returns:
            Text.
        """
        want = section.lower() if section not in ("", "-") else "all"
        rx = re.compile(grep, re.I) if grep not in ("", "-") else None
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        bp, cls = None, None
        if name.startswith("/Script/"):
            cls = unreal.load_class(None, name)
        elif name.startswith("/"):
            bp = unreal.load_asset(name.split(".")[0])
        else:
            for candidate in (name, name[1:] if name[:1] in "AUF" and name[1:2].isupper() else None):
                py_type = getattr(unreal, candidate, None) if candidate else None
                if isinstance(py_type, type) and hasattr(py_type, "static_class"):
                    cls = py_type.static_class()
                    break
            if cls is None:
                stem = name[:-2] if name.endswith("_C") else name
                found = [d for d in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "Blueprint"), True)
                         if str(d.asset_name) == stem]
                if found:
                    bp = unreal.load_asset(str(found[0].package_name))
        if bp is not None:
            if not isinstance(bp, unreal.Blueprint):
                raise RuntimeError(f"{name} is a {bp.get_class().get_name()}, not a Blueprint or class")
            cls = unreal.BlueprintEditorLibrary.generated_class(bp)
        if cls is None:
            raise RuntimeError(f"no class or Blueprint '{name}' (ue find {name}, ue api {name})")

        def py_type_of(c):
            return getattr(unreal, c.get_name(), None)

        def doc_props(py_type):
            return {m.group(1): m.group(2) for m in _DOC_PROPERTY.finditer(py_type.__doc__ or "")} if py_type else {}
        chain, current, parent_bp = [], bp, None
        while current is not None:  # Blueprint parents first, then the native chain
            parent = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(current)
            if parent is None:
                break
            if parent.get_path_name().startswith("/Script/"):
                chain.append(f"{parent.get_name()} (C++)")
                native = py_type_of(parent)
                chain += [t.__name__ for t in (native.__mro__[1:] if native else ()) if t.__name__ not in ("_ObjectBase", "_WrapperBase", "object")]
                break
            parent_bp = unreal.load_asset(parent.get_path_name().split(".")[0])
            chain.append(f"{parent_bp.get_name()} (Blueprint)" if parent_bp else parent.get_name())
            current = parent_bp
        if bp is None:
            native = py_type_of(cls)
            chain = [t.__name__ for t in (native.__mro__[1:] if native else ()) if t.__name__ not in ("_ObjectBase", "_WrapperBase", "object")]
        where = bp.get_path_name().split(".")[0] if bp else cls.get_path_name().rsplit(".", 1)[0]
        lines = [f"{bp.get_name() if bp else cls.get_name()} ({'Blueprint ' + where if bp else 'C++, ' + where})"
                 + (" : " + " : ".join(chain) if chain else "")]
        native = None if bp else py_type_of(cls)
        def emit(key, title, items):
            """One list: in the overview a comma-joined line; with section=key, one item per line."""
            if want not in ("all", key):
                return
            items = [i for i in items if not rx or rx.search(i)]
            if not items:
                if want == key:
                    lines.append(f"{title}: none" + (" matching" if rx else ""))
                return
            more = len(items) - limit
            if want == "all":
                lines.append(f"{title} ({len(items)}): " + ", ".join(items[:limit]) + (f" +{more}" if more > 0 else ""))
            else:
                lines.append(f"{title} ({len(items)}):")
                lines.extend("  " + i for i in items[:limit])
                if more > 0:
                    lines.append(f"  ... +{more} more (--limit)")
        if native is not None:
            summary = (native.__doc__ or "").strip().splitlines()
            if summary and want == "all":
                lines.append("  " + summary[0][:200])
            own = doc_props(native)
            for base in native.__mro__[1:2]:
                for key in doc_props(base):
                    own.pop(key, None)
            emit("props", "own properties", [f"{k} ({v})" for k, v in own.items()])
            methods = sorted(k for k, v in vars(native).items() if callable(v) and not k.startswith("_")
                             and k not in ("static_class", "cast", "get_default_object"))
            if want == "functions":  # the Python signature: the doc line that shows the call
                methods = [next((l.strip() for l in (getattr(native, m).__doc__ or "").splitlines()
                                 if re.match(r"\s*\w+\.\w+\(", l)), m)[:200] for m in methods]
            emit("functions", "own functions", methods)
        if bp is not None:
            parts = _bp_parts(bp)
            comps = [k.split(" ", 1)[1] + f" ({v.get_class().get_name()})" for k, v in parts.items() if k.startswith("component ")]
            emit("components", "components", comps)
            mine = _try(lambda: _reflect(unreal.ToolsetLibrary.list_struct_properties(cls), None), {}) or {}
            types = {k.lower(): _type_name(v) for k, v in mine.items()}
            comp_names = {k.split(" ", 1)[1].lower() for k in parts if k.startswith("component ")}
            names = _try(lambda: list(unreal.BlueprintEditorLibrary.list_member_variable_names(bp, False)), []) or []
            variables = [f"{n} ({types.get(str(n).lower(), '?')})" for n in map(str, names) if n.lower() not in comp_names]
            emit("vars", "variables", variables)
            graphs = list(unreal.BlueprintEditorLibrary.list_graphs(bp))
            functions = [g.get_name() for g in graphs if g.get_class().get_name() == "EdGraph"
                         and g.get_name() not in ("EventGraph", "UserConstructionScript")]
            events = []
            for g in graphs:
                if g.get_name() == "EventGraph":
                    for node in _try(lambda: list(unreal.BlueprintGraphEditor.get_graph_editor(g).list_all_nodes()), []) or []:
                        if node.get_class().get_name() in ("K2Node_Event", "K2Node_CustomEvent", "K2Node_ComponentBoundEvent",
                                                           "K2Node_InputAction", "K2Node_EnhancedInputAction"):
                            events.append(_node_title(node))
            if want == "functions":  # what a call by reflection needs: each parameter with its type
                picked = [f for f in functions if not rx or rx.search(f)]
                functions = [_bp_signature(bp, f) for f in picked[:limit]] + picked[limit:]
            emit("functions", "functions", functions)
            emit("events", "events", events)
        derived = {(str(p.package_name), str(p.asset_name)) for p in _try(lambda: list(registry.get_derived_class_names(
            [unreal.TopLevelAssetPath(cls.get_path_name())], [])), []) or []}
        own_path = cls.get_path_name()
        derived_native = [n for pkg, n in derived if pkg.startswith("/Script/") and f"{pkg}.{n}" != own_path]
        derived_bp = sorted(pkg for pkg, n in derived if not pkg.startswith("/Script/") and pkg != where)
        emit("derived", "derived C++ classes", sorted(derived_native))
        emit("derived", "derived Blueprints", derived_bp)
        if bp is not None and want in ("all", "refs"):
            refs = sorted({str(n) for n in registry.get_referencers(where, _dependency_options()) or []} - {where})
            maps = [r for r in refs if "/__ExternalActors__/" in r or r.endswith(("Map", "Lvl")) or "/Maps/" in r]
            if refs:
                lines.append(f"referenced by {len(refs)} package(s)" + (f", {len(maps)} in maps" if maps else "")
                             + ": " + ", ".join(r.rsplit("/", 1)[-1] for r in refs[:12]) + (" ..." if len(refs) > 12 else ""))
        return "\n".join(lines)

    @_tool
    @staticmethod
    def trace(start: str = "player", end: str = "look", distance: float = 5000.0, channel: str = "Visibility",
              sphere: float = 0.0, pie: bool = True, complex: bool = True) -> str:
        """Trace like gameplay code does and say what it hits: every hit along the way and the first blocking one,
        with each hit component's collision profile and response to the channel.

        Args:
            start: 'player' (the PIE player's camera), 'camera' (the editor viewport), an actor, or 'x,y,z'.
            end: 'look' (along the view, distance cm), an actor, or 'x,y,z'.
            distance: With end 'look': how far, cm.
            channel: Trace channel name: Visibility, Camera, or one of the project's (e.g. Interact).
            sphere: Sweep a sphere of this radius instead of a line.
            pie: Trace in the PIE world (else the edited level).
            complex: Trace against complex (per-triangle) collision.

        Returns:
            JSON {start, end, channel, hits: [{actor, component, distance, blocking, profile, response, phys_mat}]}.
        """
        world = _game_world() if pie else unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if world is None:
            raise RuntimeError("PIE is not running (ue pie start, or --editor for the edited level)")
        trace_type, channel_name = _trace_type(channel)
        ignore, forward = [], None
        if start in ("player", "camera"):
            if start == "player":
                if not pie:
                    raise RuntimeError("'player' needs PIE; use 'camera' for the editor viewport")
                _, pawn, _ = _pie_player()
                camera = unreal.GameplayStatics.get_player_camera_manager(world, 0)
                origin, rotation = camera.get_camera_location(), camera.get_camera_rotation()
                ignore.append(pawn)
            else:
                info = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_level_viewport_camera_info()
                if not info:
                    raise RuntimeError("no level viewport camera (a -nullrhi editor has none)")
                origin, rotation = info
            forward = rotation.get_forward_vector()
        else:
            origin = _point(start, pie)
            if not _numbers(start):
                ignore.append(_resolve(start, pie))
        if end == "look":
            if forward is None:
                raise RuntimeError("'look' needs start 'player' or 'camera'; give an end point or actor")
            target = origin + forward * distance
        else:
            target = _point(end, pie)
        args = (world, origin, target) + ((sphere,) if sphere > 0 else ()) + (trace_type, complex, ignore,
                                                                               unreal.DrawDebugTrace.NONE, True)
        hits = (unreal.SystemLibrary.sphere_trace_multi if sphere > 0 else unreal.SystemLibrary.line_trace_multi)(*args) or []
        channel_enum = next((getattr(unreal.CollisionChannel, e, None) for e, n, _ in _collision_channels()
                             if n == channel_name), None)
        rows = []
        for hit in hits:
            fields = _try(lambda: hit.to_dict()) or dict(zip(_HIT_FIELDS, hit.to_tuple()))
            comp, actor = fields.get("hit_component"), fields.get("hit_actor")
            phys = fields.get("phys_mat")
            rows.append({"actor": _label(actor) if actor else None, "component": comp.get_name() if comp else None,
                         "distance": round(fields.get("distance") or 0.0, 1), "blocking": bool(fields.get("blocking_hit")),
                         "point": [round(v) for v in _vec_tuple(fields["impact_point"])],
                         "profile": str(_try(lambda: comp.get_collision_profile_name(), "")) if comp else "",
                         "response": _enum_name(_try(lambda: comp.get_collision_response_to_channel(channel_enum), "")).replace("ecr", "")
                         if comp and channel_enum is not None else "",
                         "phys_mat": phys.get_name() if phys else None})
        return json.dumps({"start": [round(v) for v in _vec_tuple(origin)], "end": [round(v) for v in _vec_tuple(target)],
                           "channel": channel_name, "sphere": sphere, "hits": rows})

    @_tool
    @staticmethod
    def niagara_components(match: str = "", pie: bool = False, limit: int = 60) -> str:
        """Niagara components in the edited level or PIE: actor, component, system, whether it is active and
        auto-activates. (Live particle counts are not reachable from Python.)

        Args:
            match: Case-insensitive regex on actor label or system name.
            pie: Read the PIE world.
            limit: Maximum rows.

        Returns:
            JSON list of {actor, component, path, system, active, auto_activate, visible}.
        """
        rx = re.compile(match, re.I) if match else None
        rows = []
        for actor in _actors(pie):
            for comp in actor.get_components_by_class(unreal.NiagaraComponent) or []:
                system = _try(lambda: comp.get_asset())
                row = {"actor": _label(actor), "component": comp.get_name(),
                       "system": _short_path(system.get_path_name()) if system else None,
                       "active": bool(_try(lambda: comp.is_active(), False)),
                       "auto_activate": bool(_try(lambda: comp.get_editor_property("auto_activate"), False)),
                       "visible": bool(_try(lambda: comp.get_editor_property("visible"), True))}
                if rx and not rx.search(row["actor"]) and not rx.search(row["system"] or ""):
                    continue
                row["path"] = comp.get_path_name()
                rows.append(row)
        return json.dumps(rows[:limit])

    @_tool
    @staticmethod
    def set_niagara(target: str, values: str, pie: bool = False, token: str = "") -> str:
        """Set user parameters on a placed Niagara component (type taken from the parameter itself: float, int,
        bool, vector 2/3/4, colour or position). In the edited level this is undoable and marks the level dirty.

        Args:
            target: Actor label (its first Niagara component) or 'Actor.Component'.
            values: JSON object of parameter name ('Color' or 'User.Color') to value text ('2.5', 'true', '1,0,0,1').
            pie: Change the PIE world's component (not undoable, not saved).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One 'name (type): old -> new' line per parameter.
        """
        _authorize(token)
        obj = _resolve(target, pie)
        comp = obj if isinstance(obj, unreal.NiagaraComponent) else \
            next(iter(obj.get_components_by_class(unreal.NiagaraComponent) or []), None)
        if comp is None:
            raise RuntimeError(f"{target} has no Niagara component")
        kinds = [("float", "get_variable_float", "set_niagara_variable_float", lambda n: float(n[0])),
                 ("int", "get_variable_int", "set_niagara_variable_int", lambda n: int(n[0])),
                 ("vec2", "get_variable_vec2", "set_niagara_variable_vec2", lambda n: unreal.Vector2D(*n[:2])),
                 ("vec3", "get_variable_vec3", "set_niagara_variable_vec3", lambda n: unreal.Vector(*n[:3])),
                 ("position", "get_variable_position", "set_niagara_variable_position", lambda n: unreal.Vector(*n[:3])),
                 ("vec4", "get_variable_vec4", "set_niagara_variable_vec4", lambda n: unreal.Vector4(*n[:4])),
                 ("colour", "get_variable_color", "set_niagara_variable_linear_color",
                  lambda n: unreal.LinearColor(*(list(n) + [1.0])[:4]))]

        def read(name):
            for kind, getter, setter, make in kinds:
                result = _try(lambda: getattr(comp, getter)(name))
                if isinstance(result, tuple) and len(result) == 2 and result[1]:
                    return kind, result[0], setter, make
            value = _try(lambda: comp.get_variable_bool(name))
            if value is not None:
                return "bool", value, "set_niagara_variable_bool", None
            return None

        def text(value):
            if isinstance(value, float):
                return f"{value:g}"
            fields = [f for f in ("x", "y", "z", "w", "r", "g", "b", "a") if hasattr(value, f)]
            return "(" + ", ".join(f"{getattr(value, f):g}" for f in fields) + ")" if fields else str(value)
        rows = []
        context = unreal.ScopedEditorTransaction("ue fx set") if not pie else contextlib.nullcontext()
        with context:
            if not pie:
                comp.modify()
            for name, raw in json.loads(values).items():
                found = read(name) or (read(f"User.{name}") if not name.startswith("User.") else None)
                if found is None:
                    raise RuntimeError(f"{comp.get_asset().get_name() if comp.get_asset() else 'the system'} has no "
                                       f"user parameter '{name}' (ue fx {_short_path(comp.get_asset().get_path_name())})")
                kind, old, setter, make = found
                key = name if read(name) else f"User.{name}"
                if kind == "bool":
                    value = str(raw).strip().lower() in ("1", "true", "yes", "on")
                else:
                    nums = _numbers(str(raw))
                    if not nums:
                        raise RuntimeError(f"{name}: expected numbers, got '{raw}'")
                    value = make(nums)
                getattr(comp, setter)(key.replace("User.", "", 1), value)
                new = read(key)
                rows.append(f"{key} ({kind}): {text(old)} -> {text(new[1] if new else value)}")
        return "\n".join(rows)

    @_tool
    @staticmethod
    def pcg(action: str = "list", target: str = "", seed: int = -1, limit: int = 60, token: str = "") -> str:
        """PCG components in the edited level: list them (graph, seed, trigger, generated state and what they
        spawned), generate one (optionally with a new seed; generation finishes on later ticks, so poll 'status'),
        check one's status, or clean one up.

        Args:
            action: list, status, generate, cleanup, or meshes (target = a graph path: its spawners' meshes).
            target: Actor label (its first PCG component) or 'Actor.Component'; a regex with list.
            seed: With generate: set this seed first (undoable); -1 keeps it.
            limit: Maximum rows with list.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it; generate and cleanup).

        Returns:
            JSON: list of {actor, component, graph, seed, trigger, generated, generating, partitioned,
            instances: {mesh: count}, actors: n}.
        """
        get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731

        def spawned(comp):
            owner = comp.get_owner()
            holders = [owner]
            if get(comp, "is_component_partitioned", False):  # the instances live on the partition actors
                holders += [a for a in _actors() if a.get_class().get_name() == "PCGPartitionActor"]
            meshes, actors = {}, 0
            for holder in holders:
                for ism in holder.get_components_by_class(unreal.InstancedStaticMeshComponent) or []:
                    if not any("PCG Generated" in str(t) for t in ism.component_tags):
                        continue
                    mesh = get(ism, "static_mesh")
                    name = mesh.get_name() if mesh else ism.get_name()
                    meshes[name] = meshes.get(name, 0) + ism.get_instance_count()
            for actor in _actors():
                if actor != owner and any("PCG Generated" in str(t) for t in actor.tags) \
                        and actor.get_attach_parent_actor() == owner:
                    actors += 1
            return meshes, actors

        def row(comp):
            graph = _try(lambda: comp.get_graph())
            meshes, actors = spawned(comp)
            return {"actor": _label(comp.get_owner()), "component": comp.get_name(),
                    "graph": _short_path(graph.get_path_name()) if graph else None, "seed": get(comp, "seed"),
                    "trigger": _enum_name(get(comp, "generation_trigger", "")),
                    "generated": bool(get(comp, "generated", False)),
                    "generating": bool(get(comp, "generation_in_progress", False)),
                    "partitioned": bool(get(comp, "is_component_partitioned", False)),
                    "instances": meshes, "actors": actors}

        if action == "meshes":  # a graph's Static Mesh Spawner nodes and the meshes they pick from
            graph = unreal.load_asset(target)
            out = {}
            for node in _try(lambda: graph.get_editor_property("nodes"), []) or []:
                settings = _try(lambda: node.get_settings())
                selector = get(settings, "mesh_selector_parameters") if settings else None
                entries = get(selector, "mesh_entries", []) if selector else []
                if entries:
                    out[str(node.get_name())] = [_try(lambda: e.get_editor_property("descriptor").get_editor_property(
                        "static_mesh").get_name(), "?") for e in entries]
            return json.dumps(out)
        if action == "list":
            rx = re.compile(target, re.I) if target else None
            rows = []
            for actor in _actors():
                for comp in actor.get_components_by_class(unreal.PCGComponent) or []:
                    if rx and not rx.search(_label(actor)):
                        continue
                    rows.append(row(comp))
            return json.dumps(rows[:limit])
        obj = _resolve(target)
        comp = obj if isinstance(obj, unreal.PCGComponent) else \
            next(iter(obj.get_components_by_class(unreal.PCGComponent) or []), None)
        if comp is None:
            raise RuntimeError(f"{target} has no PCG component")
        if action == "status":
            return json.dumps([row(comp)])
        _authorize(token)
        if action == "generate":
            if seed >= 0:
                with unreal.ScopedEditorTransaction(f"ue pcg seed {seed}"):
                    comp.modify()
                    comp.set_editor_property("seed", seed)
            comp.generate(True)
        elif action == "cleanup":
            comp.cleanup(True)
        else:
            raise RuntimeError("action must be list, status, generate or cleanup")
        return json.dumps([row(comp)])

    @_tool
    @staticmethod
    def net_info(target: str, pie: bool = False) -> str:
        """Replication facts the editor knows about a class, Blueprint or actor: the class chain (C++ classes with
        their module, so the CLI can read their headers), each Blueprint's replicated variables, and for an actor
        its live network state in every PIE world it is found in (role, owner, update rate, relevancy, dormancy).

        Args:
            target: An actor label or name ('player' for the PIE pawn), a class name, or a Blueprint path.
            pie: Look the actor up in the PIE world (the one `ue pie target` picked).

        Returns:
            JSON: {name, chain: [{name, native, module, blueprint}], bp_vars: [{blueprint, name, replication}],
            actors: [{world, index, net_mode, ...}]}.
        """
        get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731
        actor, bp, cls = None, None, None
        if target.lower() in ("player", "pawn"):
            actor = _pie_player()[1]
        elif "/" in target:
            bp = unreal.load_asset(target.split(".")[0])
            if not isinstance(bp, unreal.Blueprint):
                raise RuntimeError(f"{target} is not a Blueprint")
        else:
            found = _try(lambda: _resolve(target, pie))
            if isinstance(found, unreal.Actor):
                actor = found
            elif found is not None and isinstance(found, unreal.ActorComponent):
                actor = found.get_owner()
            else:
                py_type = getattr(unreal, target, None) or getattr(unreal, target[1:], None)
                cls = py_type.static_class() if py_type and hasattr(py_type, "static_class") else None
                if cls is None:
                    raise RuntimeError(f"no actor, class or Blueprint '{target}'")
        if actor is not None:
            cls = actor.get_class()
        chain, bp_vars = [], []
        current = bp or (unreal.load_asset(cls.get_path_name().rsplit("_C", 1)[0].split(".")[0])
                         if cls is not None and not cls.get_path_name().startswith("/Script/") else None)
        if current is not None and not isinstance(current, unreal.Blueprint):
            current = None
        from editor_toolset.toolsets import blueprint as epic
        while current is not None:  # Blueprint parents first
            chain.append({"name": current.get_name(), "native": False, "blueprint": current.get_path_name().split(".")[0]})
            for name in _try(lambda: unreal.BlueprintEditorLibrary.list_member_variable_names(current), []) or []:
                if "/" in str(name) or "." in str(name):  # inherited native members come as paths
                    continue
                mode = _try(lambda: epic.BlueprintTools.get_variable_replication(current, str(name)))
                if mode is not None and "none" not in str(mode).lower():
                    bp_vars.append({"blueprint": current.get_name(), "name": str(name),
                                    "replication": str(mode).split(".")[-1].split(":")[0].lower()})
            parent = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(current)
            if parent is None:
                current = None
                break
            if parent.get_path_name().startswith("/Script/"):
                cls = parent
                current = None
            else:
                current = unreal.load_asset(parent.get_path_name().split(".")[0])
        native = getattr(unreal, cls.get_name(), None) if cls is not None else None
        for t in ([native] + list(native.__mro__[1:]) if native else []):
            name = t.__name__
            if name in ("_ObjectBase", "_WrapperBase", "object", "Object"):
                continue
            uclass = _try(lambda: t.static_class())
            module = uclass.get_path_name().split(".")[0].replace("/Script/", "") if uclass else ""
            chain.append({"name": name, "native": True, "module": module})
        rows = []
        if actor is not None:  # this actor in each PIE world: the same name, else the nearest of its class
            loc = actor.get_actor_location()
            worlds = _pie_worlds() if _game_world() is not None else {}
            for index, world in sorted(worlds.items()) or [(-1, actor.get_world())]:
                same = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, actor.get_class())]
                match = next((a for a in same if a.get_name() == actor.get_name()), None) if world == actor.get_world() \
                    else min(same, key=lambda a: (a.get_actor_location() - loc).length(), default=None)
                if match is None:
                    rows.append({"world": world.get_name(), "index": index, "missing": True})
                    continue
                lib = unreal.SystemLibrary
                mode = "standalone" if _try(lambda: lib.is_standalone(world), True) else \
                    "dedicated server" if _try(lambda: lib.is_dedicated_server(world), False) else \
                    "listen server" if _try(lambda: lib.is_server(world), False) else "client"
                owner = _try(lambda: match.get_owner())
                rows.append({"world": world.get_name(), "index": index, "net_mode": mode, "name": match.get_name(),
                             "distance": round((match.get_actor_location() - loc).length(), 1),
                             "role": _enum_name(_try(lambda: match.get_local_role(), "")),
                             "remote_role": _enum_name(_try(lambda: match.get_remote_role(), "")),
                             "owner": _label(owner) if owner else None,
                             "replicates": bool(get(match, "replicates", False)),
                             "replicate_movement": bool(get(match, "replicate_movement", False)),
                             "update_hz": get(match, "net_update_frequency"),
                             "min_update_hz": get(match, "min_net_update_frequency"),
                             "priority": get(match, "net_priority"),
                             "cull_m": round((get(match, "net_cull_distance_squared", 0) or 0) ** 0.5 / 100, 1),
                             "always_relevant": bool(get(match, "always_relevant", False)),
                             "only_owner": bool(get(match, "only_relevant_to_owner", False)),
                             "dormancy": _enum_name(get(match, "net_dormancy", ""))})
        return json.dumps({"name": (actor and _label(actor)) or (bp and bp.get_name()) or (cls and cls.get_name()),
                           "actor": actor is not None, "chain": chain, "bp_vars": bp_vars, "actors": rows})

    @_tool
    @staticmethod
    def net_compare(target: str, prop: str) -> str:
        """One property of a replicated actor in every PIE world: the actor of the same name in the world it was
        named in (the one `ue pie target` picked), and the nearest actor of its class in the others.

        Args:
            target: Actor label or name, or 'player' (the targeted world's pawn).
            prop: Property name (as `ue props` lists it), or location/rotation.

        Returns:
            JSON list of {world, index, net_mode, name, value, distance}.
        """
        worlds = _pie_worlds()
        if not worlds:
            raise RuntimeError("PIE is not running (ue pie start --clients 2)")
        base = _pie_player()[1] if target.lower() in ("player", "pawn") else _resolve(target, True)
        if isinstance(base, unreal.ActorComponent):
            base = base.get_owner()
        loc = base.get_actor_location()

        def value(obj):
            if prop.lower() == "location":
                return _fmt3(_vec_tuple(obj.get_actor_location()))
            if prop.lower() == "rotation":
                return _fmt3(_rot_tuple(obj.get_actor_rotation()))
            text = unreal.ToolsetLibrary.get_object_properties(obj, [prop])
            data = json.loads(text) if isinstance(text, str) else text
            if isinstance(data, dict) and data:
                return _short(_round(next(iter(data.values()))), 120)
            raise RuntimeError(f"{_label(obj)} has no property '{prop}' (ue props {_label(obj)} --pie)")
        rows = []
        for index, world in sorted(worlds.items()):
            same = list(unreal.GameplayStatics.get_all_actors_of_class(world, base.get_class()))
            match = base if world == base.get_world() else \
                min(same, key=lambda a: (a.get_actor_location() - loc).length(), default=None)
            lib = unreal.SystemLibrary
            mode = "dedicated server" if _try(lambda: lib.is_dedicated_server(world), False) else \
                "listen server" if _try(lambda: lib.is_server(world), False) and not _try(lambda: lib.is_standalone(world), False) \
                else "standalone" if _try(lambda: lib.is_standalone(world), False) else "client"
            rows.append({"world": world.get_name(), "index": index, "net_mode": mode,
                         "name": _label(match) if match else None,
                         "value": value(match) if match else None,
                         "distance": round((match.get_actor_location() - loc).length(), 1) if match else None})
        return json.dumps(rows)

    @_tool
    @staticmethod
    def skel_info(target: str) -> str:
        """A skeleton or skeletal mesh: bones with parents (in hierarchy order), sockets with their bone and offset,
        virtual bones, LODs, materials, morph targets, its physics asset, and the IK rigs / retargeters that use it.

        Args:
            target: A SkeletalMesh or Skeleton asset path.

        Returns:
            JSON: {mesh, skeleton, bones: [[name, parent]], sockets: [...], virtual_bones, lods, materials,
            morphs, physics_asset, rigs: [[path, class]]}.
        """
        asset = unreal.load_asset(target.split(".")[0])
        if isinstance(asset, unreal.Skeleton):
            skeleton, mesh = asset, _try(lambda: asset.get_skeleton_preview_mesh(True))
        elif isinstance(asset, unreal.SkeletalMesh):
            mesh, skeleton = asset, asset.get_editor_property("skeleton")
        else:
            raise RuntimeError(f"{target} is not a SkeletalMesh or Skeleton")
        names = [str(n) for n in unreal.AnimPoseExtensions.get_bone_names(skeleton.get_reference_pose())] if skeleton else []
        bones = []
        for name in names:
            parent = str(_try(lambda: mesh.get_bone_parent(name), "")) if mesh else ""
            bones.append([name, "" if parent in ("None", name) else parent])
        sockets = []
        for i in range(_try(lambda: mesh.num_sockets(), 0) if mesh else 0):
            sock = mesh.get_socket_by_index(i)
            get = lambda p, d=None: _try(lambda: sock.get_editor_property(p), d)  # noqa: E731
            loc, rot = get("relative_location"), get("relative_rotation")
            sockets.append({"name": str(get("socket_name")), "bone": str(get("bone_name")), "on": "mesh",
                            "at": _fmt3(_vec_tuple(loc)) if loc else "", "rot": _fmt3(_rot_tuple(rot)) if rot else ""})
        dumped = _try(lambda: _dump_props(skeleton), []) if skeleton else []
        for owner, name, value in dumped:
            if name == "Sockets":  # skeleton sockets (not Python-visible): their paths; read each socket object
                for path in re.findall(r"'([^']+)'", value):
                    sock = _try(lambda: unreal.find_object(None, path))
                    if sock is not None and not any(s["name"] == str(sock.get_editor_property("socket_name")) for s in sockets):
                        loc = sock.get_editor_property("relative_location")
                        sockets.append({"name": str(sock.get_editor_property("socket_name")),
                                        "bone": str(sock.get_editor_property("bone_name")), "on": "skeleton",
                                        "at": _fmt3(_vec_tuple(loc)), "rot": ""})
        virtual = [m.group(1) for owner, name, value in dumped if name == "VirtualBones"
                   for m in re.finditer(r"VirtualBoneName=\"?(\w+)", value)]
        materials = _try(lambda: mesh.get_editor_property("materials"), []) if mesh else []
        slots = [[str(_try(lambda: m.get_editor_property("material_slot_name"), "")),
                  _short_path(_try(lambda: m.get_editor_property("material_interface").get_path_name(), "") or "")]
                 for m in materials]
        physics = _try(lambda: mesh.get_editor_property("physics_asset")) if mesh else None
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        rigs = []
        for source in [p for p in (mesh, skeleton) if p is not None]:
            package = source.get_outermost().get_name()
            for ref in registry.get_referencers(package, _dependency_options()) or []:
                for data in registry.get_assets_by_package_name(str(ref)) or []:
                    cls = str(data.asset_class_path.asset_name)
                    if cls in ("IKRigDefinition", "IKRetargeter", "ControlRigBlueprint", "PhysicsAsset", "PoseAsset") \
                            and [str(ref), cls] not in rigs:
                        rigs.append([str(ref), cls])
        lods = _try(lambda: unreal.EditorSkeletalMeshLibrary.get_lod_count(mesh), None) if mesh else None
        if lods is None and mesh:
            lods = _try(lambda: len(mesh.get_editor_property("lod_info")), None)
        return json.dumps({"mesh": _short_path(mesh.get_path_name()) if mesh else None,
                           "skeleton": _short_path(skeleton.get_path_name()) if skeleton else None,
                           "bones": bones, "sockets": sockets, "virtual_bones": virtual, "lods": lods,
                           "materials": slots,
                           "morphs": len(_try(lambda: mesh.get_editor_property("morph_targets"), []) or []) if mesh else 0,
                           "physics_asset": _short_path(physics.get_path_name()) if physics else None,
                           "rigs": rigs})

    @_tool
    @staticmethod
    def mesh_check(target: str, height: float = -1.0) -> str:
        """Sanity checks for a static or skeletal mesh, aimed at meshes made in Blender and other DCC tools:
        size (against an expected height when given), where the pivot sits, UV channels and the lightmap UV,
        faces pointing inward, collision, sockets, empty material slots, LODs and Nanite.

        Args:
            target: A StaticMesh or SkeletalMesh asset path.
            height: Expected height in cm (e.g. 200 for a robot), or -1.

        Returns:
            JSON: {mesh, class, size: [x, y, z], pivot, facts: [...], issues: [...]}.
        """
        mesh = unreal.load_asset(target.split(".")[0])
        if not isinstance(mesh, (unreal.StaticMesh, unreal.SkeletalMesh)):
            raise RuntimeError(f"{target} is not a StaticMesh or SkeletalMesh")
        facts, issues = [], []
        bounds = mesh.get_bounds()
        o, e = bounds.origin, bounds.box_extent
        size = [round(e.x * 2, 1), round(e.y * 2, 1), round(e.z * 2, 1)]
        biggest = max(size)
        # Size: Blender works in metres; an export at the wrong scale comes in 100x too small or too large.
        if height > 0:
            ratio = size[2] / height if size[2] else 0
            if ratio and (0.005 < ratio < 0.02 or 50 < ratio < 200):
                issues.append(f"{size[2]:g} cm tall, expected about {height:g}: {round(1 / ratio) if ratio < 1 else round(ratio)}x off "
                              "(metres vs centimetres: set the export or import scale)")
            elif max(size[0], size[1]) > 1.5 * size[2] and 0.5 < max(size[0], size[1]) / height < 2:
                issues.append(f"lies on its side: {size[0]:g} x {size[1]:g} x {size[2]:g} cm, the expected height is along "
                              "X or Y (Y-up export? rotate 90 degrees on import, or export Z-up)")
            elif ratio and not 0.5 < ratio < 2:
                issues.append(f"{size[2]:g} cm tall, expected about {height:g}")
        elif biggest and biggest < 2:
            issues.append(f"only {biggest:g} cm across: exported in metres with a 0.01 scale? (import with a scale of 100)")
        elif biggest > 50000:
            issues.append(f"{biggest / 100:g} m across: exported in centimetres twice? (import with a scale of 0.01)")
        # Pivot: the mesh's local origin against its bounds.
        bottom = o.z - e.z
        centred = abs(o.x) <= max(1.0, 0.05 * e.x * 2) and abs(o.y) <= max(1.0, 0.05 * e.y * 2)
        if centred and abs(bottom) <= max(1.0, 0.02 * size[2]):
            pivot = "at the base, centred"
        elif centred and abs(o.z) <= max(1.0, 0.05 * size[2]):
            pivot = "at the centre"
        else:
            off = (o.x ** 2 + o.y ** 2 + bottom ** 2) ** 0.5
            pivot = f"{off:.0f} cm from the base centre (bounds centre at {_fmt3((o.x, o.y, o.z))})"
            if off > max(size) * 0.5:
                issues.append(f"pivot is {off:.0f} cm away from the mesh: it rotates and places around a far point "
                              "(apply transforms / set the origin in the DCC)")
        get = lambda p, d=None: _try(lambda: mesh.get_editor_property(p), d)  # noqa: E731
        if isinstance(mesh, unreal.StaticMesh):
            lib = unreal.EditorStaticMeshLibrary
            lods, uvs = lib.get_lod_count(mesh), lib.get_num_uv_channels(mesh, 0)
            verts = lib.get_number_verts(mesh, 0)
            nanite = bool(_try(lambda: get("nanite_settings").get_editor_property("enabled"), False))
            lightmap = get("light_map_coordinate_index", 0)
            facts.append(f"{verts} vertices, {lods} LOD(s), {uvs} UV channel(s), lightmap UV {lightmap}"
                         + (", Nanite" if nanite else ""))
            generated = bool(_try(lambda: lib.get_lod_build_settings(mesh, 0).get_editor_property("generate_lightmap_u_vs"), False))
            if generated and lightmap >= uvs:
                uvs += 1  # the build adds the lightmap channel
            if lightmap >= uvs:
                issues.append(f"lightmap UV channel {lightmap} does not exist ({uvs} channel(s)): static lighting will fail; "
                              "turn on Generate Lightmap UVs in the build settings")
            elif uvs < 2 and not nanite:
                facts.append("one UV channel: the lightmap shares the texture UVs (overlapping UVs bake badly)")
            simple = lib.get_simple_collision_count(mesh) + lib.get_convex_collision_count(mesh)
            complexity = _enum_name(_try(lambda: lib.get_collision_complexity(mesh), ""))
            facts.append(f"collision: {simple} simple shape(s)" + (", complex as simple" if "complexassimple" in complexity else ""))
            if not simple and "complexassimple" not in complexity:
                issues.append("no simple collision: characters and physics pass through it (UCX_ shapes from Blender, "
                              "or ue lint --fix)")
            sockets = [str(s.get_editor_property("socket_name")) for s in get("sockets", []) or []]
            slots = [(str(m.get_editor_property("material_slot_name")), m.get_editor_property("material_interface"))
                     for m in get("static_materials", []) or []]
            sections = _try(lambda: mesh.get_num_sections(0))
            if sections == 1 and len(slots) > 1:
                issues.append(f"1 section in LOD 0 but {len(slots)} material slots: every triangle draws with "
                              f"'{slots[0][0]}'. GeometryScript's create_new_static_mesh_asset_from_mesh ignores material "
                              "IDs; copy_mesh_to_static_mesh with replace_materials splits them")
            # Faces turned inward: the signed volume of a closed mesh (Unreal's winding gives a negative one).
            desc = _try(lambda: mesh.get_static_mesh_description(0))
            if desc is not None:
                count = desc.get_triangle_count()
                stride = max(1, count // 30000)
                volume = 0.0
                for i in range(0, count, stride):
                    tid = unreal.TriangleID()
                    tid.set_editor_property("id_value", i)
                    a, b, c = (desc.get_vertex_position(desc.get_vertex_instance_vertex(desc.get_triangle_vertex_instance(tid, k)))
                               for k in range(3))
                    volume += a.x * (b.y * c.z - b.z * c.y) - a.y * (b.x * c.z - b.z * c.x) + a.z * (b.x * c.y - b.y * c.x)
                volume = volume * stride / 6
                box = size[0] * size[1] * size[2]
                if box and volume > 0.1 * box:
                    issues.append("faces point inward (inside-out): flip the normals / recalculate outside in the DCC")
        else:
            sub = unreal.get_editor_subsystem(unreal.SkeletalMeshEditorSubsystem) if hasattr(unreal, "SkeletalMeshEditorSubsystem") else None
            lods = _try(lambda: sub.get_lod_count(mesh), 0) if sub else 0
            verts = _try(lambda: sub.get_num_verts(mesh, 0), 0) if sub else 0
            skeleton = get("skeleton")
            bones = len(unreal.AnimPoseExtensions.get_bone_names(skeleton.get_reference_pose())) if skeleton else 0
            facts.append(f"{verts} vertices, {lods} LOD(s), {bones} bones" + ("" if get("physics_asset") else ", no physics asset"))
            sockets = [str(mesh.get_socket_by_index(i).get_editor_property("socket_name")) for i in range(mesh.num_sockets())]
            slots = [(str(m.get_editor_property("material_slot_name")), m.get_editor_property("material_interface"))
                     for m in get("materials", []) or []]
        if sockets:
            facts.append("sockets: " + ", ".join(sockets[:12]) + (f" +{len(sockets) - 12}" if len(sockets) > 12 else ""))
        empty = [name or "(unnamed)" for name, material in slots if material is None]
        facts.append(f"{len(slots)} material slot(s)")
        if empty:
            issues.append(f"empty material slot(s): {', '.join(empty)} (renders with the default material)")
        return json.dumps({"mesh": _short_path(mesh.get_path_name()), "class": mesh.get_class().get_name(),
                           "size": size, "pivot": pivot, "facts": facts, "issues": issues})

    @_tool
    @staticmethod
    def sound_info(target: str) -> str:
        """A sound asset (SoundWave, SoundCue, MetaSound source): duration, channels, sample rate, compression,
        looping, attenuation (with its falloff), concurrency, sound class and submix routing, the waves a cue
        plays, and how many assets use it.

        Args:
            target: The sound asset path.

        Returns:
            JSON with those fields.
        """
        sound = unreal.load_asset(target.split(".")[0])
        if not isinstance(sound, unreal.SoundBase):
            raise RuntimeError(f"{target} is not a sound")
        get = lambda obj, p, d=None: _try(lambda: obj.get_editor_property(p), d)  # noqa: E731
        name = lambda o: _short_path(o.get_path_name()) if o else None  # noqa: E731
        info = {"sound": name(sound), "class": sound.get_class().get_name(), "duration": get(sound, "duration"),
                "looping": bool(get(sound, "looping", False)) or (get(sound, "duration", 0) or 0) > 1e5}
        if isinstance(sound, unreal.SoundWave):
            info.update(channels=get(sound, "num_channels"), sample_rate=get(sound, "sample_rate"),
                        compression=_enum_name(get(sound, "sound_asset_compression_type", "")),
                        quality=get(sound, "compression_quality"),
                        streaming=_enum_name(get(sound, "loading_behavior", "")))
        attenuation = get(sound, "attenuation_settings")
        if attenuation is not None:
            settings = get(attenuation, "attenuation")
            falloff = _try(lambda: settings.get_editor_property("falloff_distance"))
            inner = _try(lambda: settings.get_editor_property("attenuation_shape_extents").x)
            info["attenuation"] = {"asset": name(attenuation), "falloff_m": round(falloff / 100, 1) if falloff else None,
                                   "inner_m": round(inner / 100, 1) if inner else None,
                                   "spatialized": bool(_try(lambda: settings.get_editor_property("spatialize"), True))}
        override = get(sound, "attenuation_overrides") if get(sound, "override_attenuation", False) else None
        if override is not None and attenuation is None:
            info["attenuation"] = {"asset": "(set on the asset)", "falloff_m": round((get(override, "falloff_distance", 0) or 0) / 100, 1)}
        concurrency = [name(c) for c in get(sound, "concurrency_set", []) or []]
        if get(sound, "override_concurrency", False):
            max_count = _try(lambda: get(sound, "concurrency_overrides").get_editor_property("max_count"))
            concurrency.append(f"override: max {max_count}")
        info["concurrency"] = concurrency
        sound_class = get(sound, "sound_class_object")
        chain = []
        while sound_class is not None and len(chain) < 8:
            chain.append(sound_class.get_name())
            sound_class = get(sound_class, "parent_class")
        info["sound_class"] = chain
        info["submix"] = name(get(sound, "sound_submix_object"))
        if isinstance(sound, unreal.SoundCue):  # the waves it plays
            registry = unreal.AssetRegistryHelpers.get_asset_registry()
            waves = []
            for dep in registry.get_dependencies(sound.get_outermost().get_name(), _dependency_options()) or []:
                for data in registry.get_assets_by_package_name(str(dep)) or []:
                    if str(data.asset_class_path.asset_name) == "SoundWave":
                        waves.append(str(dep))
            info["waves"] = waves
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        info["used_by"] = len(registry.get_referencers(sound.get_outermost().get_name(), _dependency_options()) or [])
        return json.dumps(info)

    @_tool
    @staticmethod
    def sound_usage(targets: str, depth: int = 4, limit: int = 4000) -> str:
        """Every SoundWave a data asset, Blueprint, cue or folder reaches through hard and soft references (through
        cues, MetaSounds and data assets; maps, meshes, textures and materials are not followed), with the assets
        that reference each one. Read from the asset registry: nothing is loaded.

        Args:
            targets: JSON list of asset paths or folders ('/Game/Audio', '/Game/SD/Audio/DA_SDSounds').
            depth: How many references deep to follow.
            limit: Most packages to visit.

        Returns:
            JSON {roots, waves: [{path, channels, duration, sample_rate, looping, users}], visited}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        roots = []
        for target in json.loads(targets):
            path = target.split(".")[0].rstrip("/")
            if registry.get_assets_by_package_name(path):
                roots.append(path)
            else:
                found = registry.get_assets_by_path(path, recursive=True) or []
                if not found:
                    raise RuntimeError(f"no asset or folder {path}")
                roots += sorted({str(a.package_name) for a in found})
        waves, seen, parents, sound_like = {}, set(), {}, set()
        queue = [(root, 0, None) for root in roots]
        while queue and len(seen) < limit:
            package, level, user = queue.pop(0)
            if user:
                parents.setdefault(package, set()).add(user)
            row = _wave_row(registry, package)
            if row:
                entry = waves.setdefault(package, dict(row, path=package, users=[]))
                if user and user not in entry["users"]:
                    entry["users"].append(user)
                continue
            if package in seen:
                continue
            seen.add(package)
            classes = {str(a.asset_class_path.asset_name) for a in registry.get_assets_by_package_name(package) or []}
            if any(c.startswith(("Sound", "MetaSound")) for c in classes):
                sound_like.add(package)
            if classes & _SOUND_SKIP or level >= depth:
                continue
            for dep in registry.get_dependencies(package, _dependency_options()) or []:
                dep = str(dep)
                if dep.startswith("/Script/") or (dep.startswith("/Engine/") and not _wave_row(registry, dep)):
                    continue
                queue.append((dep, level + 1, package))
        # Where each sound (wave or cue) is referenced inside the data assets and Blueprints that use it.
        targets = set(waves) | sound_like
        users = sorted({u for t in targets for u in parents.get(t, ()) if u not in sound_like})[:60]
        refs, gains = {}, {}
        for user in users:
            for target, found in _referencing_props(user, targets).items():
                refs.setdefault(target, []).extend(f"{user.rsplit('/', 1)[-1]}: {p}" for p, _ in found[:6])
                gains.setdefault(target, []).extend(g for _, g in found if g is not None)
        for path, entry in waves.items():
            entry["refs"] = refs.get(path, [])[:6]
            entry["gains"] = gains.get(path, [])[:6]
            for cue in entry["users"]:
                if cue in sound_like:
                    entry["refs"] += [f"via {cue.rsplit('/', 1)[-1]} ({r})" for r in refs.get(cue, [])[:3]]
                    entry["gains"] += gains.get(cue, [])[:3]
        return json.dumps({"roots": roots, "waves": sorted(waves.values(), key=lambda w: w["path"]),
                           "visited": len(seen)})

    @_tool
    @staticmethod
    def export_waves(waves: str, folder: str) -> str:
        """Export SoundWaves as .wav files (the engine's WAV exporter), for loudness analysis outside the editor.

        Args:
            waves: JSON {package path: file name} to write into folder.
            folder: Output folder.

        Returns:
            JSON {package path: file path, or 'error: ...'}.
        """
        os.makedirs(folder, exist_ok=True)
        out = {}
        for package, file_name in json.loads(waves).items():
            wave = unreal.load_asset(package)
            if not isinstance(wave, unreal.SoundWave):
                out[package] = "error: not a SoundWave"
                continue
            task = unreal.AssetExportTask()
            task.object = wave
            task.filename = os.path.join(folder, file_name)
            task.automated = True
            task.prompt = False
            task.replace_identical = True
            task.exporter = unreal.SoundExporterWAV()
            ok = unreal.Exporter.run_asset_export_task(task)
            out[package] = task.filename if ok and os.path.isfile(task.filename) else "error: export failed"
        return json.dumps(out)

    @_tool
    @staticmethod
    def audio_log(action: str = "status", every: float = 0.05) -> str:
        """Record the sounds that start on audio components in PIE, with what the mixer resolves for each:
        spatialized or not, effective attenuation (component override, then the component's asset, then the
        sound's), effective sound class (SoundClassOverride, then the sound's), the waves and their channels,
        owner and distance. Polled on the editor tick, so a sound shorter than `every` may be missed.

        Args:
            action: 'start' (replaces a running log), 'stop' (ends it and returns the starts), or 'status'.
            every: Seconds between polls.

        Returns:
            JSON: status, or {starts: [...], seconds, ended}.
        """
        state = _AUDIO_LOG
        if action == "start":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
            game = _game_world()
            if game is None:
                raise RuntimeError("PIE is not running (ue pie start)")
            state.clear()
            state.update(world=game, start=time.time(), last=0.0, every=max(0.0, every), starts=[], playing={},
                         live={}, ended=None)
            state["handle"] = unreal.register_slate_post_tick_callback(_audio_log_tick)
            return json.dumps({"recording": True})
        if action == "stop":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
                state["handle"] = None
            return json.dumps({"starts": state.get("starts", []), "ended": state.get("ended"),
                               "seconds": round(time.time() - state.get("start", time.time()), 1)})
        return json.dumps({"recording": state.get("handle") is not None, "starts": len(state.get("starts", [])),
                           "ended": state.get("ended")})

    @_tool
    @staticmethod
    def audio_mix() -> str:
        """The project's sound classes and submixes as trees: each class with volume, pitch and children; each
        submix with its output volume, effect count and children.

        Returns:
            JSON: {classes: [[name, parent, volume, pitch]], submixes: [[name, parent, volume, effects]]}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        get = lambda obj, p, d=None: _try(lambda: obj.get_editor_property(p), d)  # noqa: E731
        classes, submixes = [], []
        for cls_name, out in (("SoundClass", classes), ("SoundSubmix", submixes)):
            for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", cls_name)):
                package = str(data.package_name)
                if not package.startswith("/Game/"):
                    continue
                asset = data.get_asset()
                if cls_name == "SoundClass":
                    props = get(asset, "properties")
                    parent = get(asset, "parent_class")
                    out.append([package, _short_path(parent.get_path_name()) if parent else "",
                                _try(lambda: props.get_editor_property("volume")), _try(lambda: props.get_editor_property("pitch"))])
                else:
                    parent = get(asset, "parent_submix")
                    out.append([package, _short_path(parent.get_path_name()) if parent else "",
                                get(asset, "output_volume"), len(get(asset, "submix_effect_chain", []) or [])])
        return json.dumps({"classes": classes, "submixes": submixes})

    @_tool
    @staticmethod
    def data_table(query: str, limit: int = 200) -> str:
        """Property values across many assets, for balancing: the data assets of a class, the class defaults of
        the Blueprints deriving from a class, or the assets (Blueprints: their class defaults) in a folder or
        matching a path glob (/Game/Robots/BP_*).

        Args:
            query: A class name (C++ or Blueprint), a folder, or a /Game/... glob.
            limit: Maximum rows.

        Returns:
            JSON: {rows: [{asset, object, values: {prop: value}}]} (values compact; references to the row's own
            asset written as ~).
        """
        import fnmatch
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        objects = []

        def add(asset_path, obj):
            if obj is not None and len(objects) < limit:
                objects.append((asset_path, obj))

        def cdo_of(bp):
            cls = unreal.BlueprintEditorLibrary.generated_class(bp)
            return unreal.get_default_object(cls) if cls else None
        if query.startswith("/"):
            folder, glob = query, "*"
            if any(ch in query.rsplit("/", 1)[-1] for ch in "*?["):
                folder, glob = query.rsplit("/", 1)
            for data in sorted(registry.get_assets_by_path(folder.rstrip("/"), recursive=True), key=lambda d: str(d.package_name)):
                package = str(data.package_name)
                if "/__External" in package or not fnmatch.fnmatch(package.rsplit("/", 1)[-1], glob):
                    continue
                asset = data.get_asset()
                add(package, cdo_of(asset) if isinstance(asset, unreal.Blueprint) else asset)
        else:
            py_type = getattr(unreal, query, None) or getattr(unreal, query[1:], None)
            base = py_type.static_class() if py_type and hasattr(py_type, "static_class") else None
            if base is None:  # a Blueprint class by asset name
                hits = [d for d in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "Blueprint"))
                        if str(d.asset_name).lower() == query.lower()]
                bp = hits[0].get_asset() if hits else None
                base = unreal.BlueprintEditorLibrary.generated_class(bp) if bp else None
            if base is None:
                raise RuntimeError(f"no class or Blueprint '{query}' (ue class {query})")
            if unreal.MathLibrary.class_is_child_of(base, unreal.DataAsset.static_class()):  # its instances
                flt = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath(base.get_path_name().rsplit(".", 1)[0],
                                                                            base.get_name())],
                                      recursive_classes=True)
                for data in sorted(registry.get_assets(flt), key=lambda d: str(d.package_name)):
                    add(str(data.package_name), data.get_asset())
            for path in unreal.ToolsetLibrary.get_derived_classes(base) or []:  # Blueprints deriving from it
                text = str(path.export_text()).strip('"')
                if text.startswith("/Game/"):
                    package = text.split(".")[0]
                    bp = unreal.load_asset(package)
                    if isinstance(bp, unreal.Blueprint):
                        add(package, cdo_of(bp))
        rows = []
        for package, obj in objects:
            values = _try(lambda: _props_json(obj), {}) or {}
            own, name = obj.get_path_name().split(".")[0], package.rsplit("/", 1)[-1]
            # References into the row's own asset (its components) would differ by name alone: write them as ~.
            text = json.dumps({k: _rebase(_round(v), own, "~") for k, v in values.items()}).replace(name, "~")
            rows.append({"asset": package, "object": obj.get_path_name(), "values": json.loads(text)})
        return json.dumps({"rows": rows})

    @_tool
    @staticmethod
    def map_data(area: str = "-", pie: bool = False, limit: int = 20000, footprints: bool = False) -> str:
        """The level from above, as data for `ue map`: every actor's footprint (axis-aligned bounds) and kind:
        geometry (meshes, blocking volumes), marker (gameplay actors: pawns, starts, triggers, Blueprints...), or
        landscape. Lights, sky, fog, post-process and other invisible infrastructure are left out.

        Args:
            area: 'x,y,radius' to keep only what overlaps it, or '-' for the whole level.
            pie: Read the PIE world.
            limit: Maximum actors.
            footprints: Also each geometry row's bottom z and its outline: the four XY corners of its mesh's own
                box turned with the actor (a rotated wall is not its axis-aligned bounds).

        Returns:
            JSON: {actors: [[label, class, kind, x, y, z_top, half_x, half_y(, z_bottom, corners)]], landscape: bool}.
        """
        skip = ("WorldSettings", "Light", "SkyAtmosphere", "SkyLight", "ExponentialHeightFog", "VolumetricCloud",
                "PostProcessVolume", "NavMeshBoundsVolume", "RecastNavMesh", "AbstractNavData", "NavigationData",
                "WorldPartitionHLOD", "WorldPartitionMiniMap", "LevelBounds", "GameplayDebugger", "Note",
                "ReflectionCapture", "LightmassImportanceVolume", "PrecomputedVisibilityVolume", "AudioVolume",
                "WorldDataLayers", "LevelInstance", "SceneCapture", "CameraActor", "DefaultPhysicsVolume", "Brush",
                "ChaosDebugDraw", "PlayerCameraManager", "GameMode", "GameState", "PlayerState", "HUD", "GameSession",
                "GameNetworkManager", "ParticleEventManager")
        geometry = ("StaticMeshActor", "BlockingVolume", "InstancedFoliageActor", "Brush")
        want = _numbers(area) if area and area != "-" else None
        rows, landscape = [], False
        for actor in _actors(pie):
            cls = actor.get_class().get_name()
            if cls.startswith(("Landscape",)):
                landscape = True
                continue
            if footprints and not isinstance(actor, unreal.StaticMeshActor) and cls != "InstancedFoliageActor":
                parts = _built_parts(actor, want)  # a builder's roads and buildings, one row per piece
                if parts:
                    rows.extend(parts[: max(0, limit - len(rows))])
                    if len(rows) >= limit:
                        break
                    continue
            if any(cls == s or (cls.endswith(s) and s not in ("Brush",)) for s in skip) or cls in ("Brush",) and not \
                    isinstance(actor, unreal.BlockingVolume):
                continue
            if isinstance(actor, (unreal.Controller, unreal.Info)) and not isinstance(actor, unreal.PlayerStart):
                continue
            origin, extent = actor.get_actor_bounds(False)
            if extent.x > 500000 or extent.y > 500000:
                continue
            if not extent.x and not extent.y:  # a point actor (no visible component): use its location
                origin = actor.get_actor_location()
            if want and len(want) == 3:
                dx, dy = abs(origin.x - want[0]) - extent.x, abs(origin.y - want[1]) - extent.y
                if max(dx, 0) ** 2 + max(dy, 0) ** 2 > want[2] ** 2:
                    continue
            kind = "geometry" if cls in geometry or (isinstance(actor, unreal.StaticMeshActor)) else "marker"
            if kind == "geometry" and cls == "InstancedFoliageActor":
                continue  # foliage: thousands of instances; drawn as nothing rather than one huge box
            row = [_label(actor), cls, kind, round(origin.x, 1), round(origin.y, 1), round(origin.z + extent.z, 1),
                   round(extent.x, 1), round(extent.y, 1)]
            if footprints and kind == "geometry":
                row += [round(origin.z - extent.z, 1), _mesh_outline(actor) or
                        [[round(origin.x + sx * extent.x, 1), round(origin.y + sy * extent.y, 1)]
                         for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]]
            rows.append(row)
            if len(rows) >= limit:
                break
        return json.dumps({"actors": rows, "landscape": landscape})

    @_tool
    @staticmethod
    def component_tree(target: str, pie: bool = False, limit: int = 120) -> str:
        """An actor's (or a Blueprint's) components as an indented tree: class, attach socket, relative transform
        (when not identity), mobility, mesh or other asset, and visibility/shadow/collision flags; components that
        are not scene components (movement, AI, custom logic) are listed after the tree.

        Args:
            target: Actor label or name, 'player' (the PIE pawn), or a Blueprint asset path.
            pie: Read the PIE world's actor.
            limit: Maximum lines.

        Returns:
            Text.
        """
        get = lambda obj, prop, default=None: _try(lambda: obj.get_editor_property(prop), default)  # noqa: E731

        def describe(comp, socket=""):
            bits = [comp.get_class().get_name().replace("_C", "")]
            asset = None
            for prop in ("static_mesh", "skeletal_mesh_asset", "skinned_asset", "skeletal_mesh", "asset", "sound",
                         "template", "child_actor_class", "widget_class"):
                asset = get(comp, prop)
                if asset is not None:
                    break
            if asset is not None:
                bits.append(asset.get_name())
            if socket and socket != "None":
                bits.append(f"socket {socket}")
            if isinstance(comp, unreal.SceneComponent):
                loc, rot, scale = (get(comp, p) for p in ("relative_location", "relative_rotation", "relative_scale3d"))
                if loc is not None and any(abs(v) > 1e-3 for v in (loc.x, loc.y, loc.z)):
                    bits.append(f"at {_fmt3(_vec_tuple(loc))}")
                if rot is not None and any(abs(v) > 1e-3 for v in (rot.pitch, rot.yaw, rot.roll)):
                    bits.append(f"rot {_fmt3(_rot_tuple(rot))}")
                if scale is not None and any(abs(v - 1) > 1e-3 for v in (scale.x, scale.y, scale.z)):
                    bits.append(f"scale {_fmt3(_vec_tuple(scale))}")
                bits.append(_enum_name(get(comp, "mobility", "")) or "?")
                if get(comp, "visible", True) is False:
                    bits.append("invisible")
                if get(comp, "hidden_in_game", False):
                    bits.append("hidden in game")
            if isinstance(comp, unreal.PrimitiveComponent):
                profile = str(_try(lambda: comp.get_collision_profile_name(), "") or "")
                enabled = _enum_name(_try(lambda: comp.get_collision_enabled(), ""))
                bits.append("no collision" if enabled == "nocollision" else f"collision {profile}")
                if not get(comp, "cast_shadow", True):
                    bits.append("no shadow")
            if _try(lambda: comp.get_editor_property("is_editor_only"), False):
                bits.append("editor only")
            return ", ".join(b for b in bits if b)

        lines = []
        if "/" in target and not pie:
            bp = unreal.load_asset(target)
            if not isinstance(bp, unreal.Blueprint):
                raise RuntimeError(f"{target} is not a Blueprint")
            subsystem = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            lib = unreal.SubobjectDataBlueprintFunctionLibrary
            nodes, order = {}, []
            for handle in subsystem.k2_gather_subobject_data_for_blueprint(bp) or []:
                data = lib.get_data(handle)
                if lib.is_actor(data):
                    continue
                name = str(lib.get_variable_name(data))
                parent = lib.get_data(lib.get_parent_handle(data))
                parent_name = str(lib.get_variable_name(parent)) if lib.is_valid(parent) and not lib.is_actor(parent) else ""
                obj = lib.get_object(data)
                if obj is None:
                    continue
                origin = "inherited" if lib.is_inherited_component(data) else "native" if lib.is_native_component(data) else ""
                nodes[name] = {"obj": obj, "parent": parent_name, "origin": origin,
                               "socket": str(_try(lambda: obj.get_editor_property("attach_socket_name"), "") or "")}
                order.append(name)
            parent_class = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(bp)
            lines.append(f"{bp.get_name()} (Blueprint of {parent_class.get_name() if parent_class else '?'}), "
                         f"{len(order)} component(s)")
            scene = [n for n in order if isinstance(nodes[n]["obj"], unreal.SceneComponent)]

            def walk(name, depth):
                node = nodes[name]
                lines.append("  " * (depth + 1) + f"{name}: {describe(node['obj'], node['socket'])}"
                             + (f" ({node['origin']})" if node["origin"] else ""))
                for child in scene:
                    if nodes[child]["parent"] == name:
                        walk(child, depth + 1)
            for root in [n for n in scene if nodes[n]["parent"] not in nodes]:
                walk(root, 0)
            others = [n for n in order if n not in scene]
        else:
            if target.lower() in ("player", "pawn") or pie and target.lower() in ("player", "pawn"):
                actor = _pie_player()[1]
            else:
                obj = _resolve(target, pie)
                actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
            comps = actor.get_components_by_class(unreal.ActorComponent)
            lines.append(f"{_label(actor)} ({actor.get_class().get_name()}), {len(comps)} component(s)")
            root = actor.get_editor_property("root_component")
            seen = set()

            def walk(comp, depth):
                seen.add(comp.get_name())
                socket = str(_try(lambda: comp.get_attach_socket_name(), "") or "")
                lines.append("  " * (depth + 1) + f"{comp.get_name()}: {describe(comp, socket)}")
                for child in _try(lambda: comp.get_children_components(False), []) or []:
                    if child.get_owner() == actor:
                        walk(child, depth + 1)
                    else:
                        lines.append("  " * (depth + 2) + f"(attached actor {_label(child.get_owner())})")
            if root is not None:
                walk(root, 0)
            attached = [a for a in _try(lambda: actor.get_attached_actors(), []) or []]
            if attached:
                lines.append("attached actors: " + ", ".join(_label(a) for a in attached))
            nodes = {c.get_name(): {"obj": c} for c in comps}
            others = [c.get_name() for c in comps if c.get_name() not in seen]
        if others:
            lines.append("not in the tree: " + "; ".join(f"{n} ({describe(nodes[n]['obj'])}"
                                                         + (f", {nodes[n]['origin']})" if nodes[n].get("origin") else ")")
                                                         for n in others))
        return _capped(lines[0], lines[1:], limit)

    @_tool
    @staticmethod
    def collision(target: str, pie: bool = False) -> str:
        """An actor's collision, component by component: profile, enabled state, object type, the response to
        every channel (the project's own included), overlap events, physics, and for static meshes whether they
        have simple collision and how complex collision is used.

        Args:
            target: Actor label or name.
            pie: Read the PIE world's actor.

        Returns:
            Text.
        """
        obj = _resolve(target, pie)
        actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
        comps = actor.get_components_by_class(unreal.PrimitiveComponent)
        lines = [f"{_label(actor)} ({actor.get_class().get_name()}), {len(comps)} primitive component(s)"
                 + ("" if actor.get_actor_enable_collision() else "; collision disabled on the actor")]
        for comp in comps:
            enabled = _enum_name(_try(lambda: comp.get_collision_enabled(), "?"))
            obj_enum = _enum_name(_try(lambda: comp.get_collision_object_type(), "?"))
            obj_type = next((n for e, n, _ in _collision_channels() if _enum_name(e) == obj_enum), obj_enum)
            state = {"queryandphysics": "query and physics", "queryonly": "query only", "physicsonly": "physics only",
                     "nocollision": "no collision", "probeonly": "probe only", "queryandprobe": "query and probe"}
            head = (f"  {comp.get_name()} ({comp.get_class().get_name()}): profile {comp.get_collision_profile_name()}, "
                    f"{state.get(enabled, enabled)}, object type {obj_type}")
            extra = []
            if _try(lambda: comp.get_editor_property("generate_overlap_events"), False):
                extra.append("overlap events")
            if _try(lambda: comp.is_simulating_physics(), False):
                extra.append("simulates physics")
            if isinstance(comp, unreal.StaticMeshComponent) and comp.get_editor_property("static_mesh"):
                mesh = comp.get_editor_property("static_mesh")
                simple = _try(lambda: unreal.EditorStaticMeshLibrary.get_simple_collision_count(mesh), 0) + \
                    _try(lambda: unreal.EditorStaticMeshLibrary.get_convex_collision_count(mesh), 0)
                flag = _enum_name(_try(lambda: unreal.EditorStaticMeshLibrary.get_collision_complexity(mesh), ""))
                extra.append(f"{simple} simple shape(s)" + (", complex as simple" if "complexassimple" in flag else
                                                            ", simple as complex" if "simpleascomplex" in flag else ""))
            lines.append(head + (f" ({', '.join(extra)})" if extra else ""))
            if enabled not in ("nocollision", "?"):
                lines.append(f"    {_responses(comp)}")
        return "\n".join(lines)

    @_tool
    @staticmethod
    def editor_undo(action: str = "status", count: int = 1, token: str = "") -> str:
        """The editor's own undo history (every transacted edit, not only Tether's): undo or redo the last N
        entries. Their titles go to the log (LogEditorTransaction: Undo/Redo <title>); the ue CLI prints them.

        Args:
            action: status, undo or redo.
            count: How many entries.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {undoable, done}.
        """
        lib = unreal.ToolsetLibrary
        if action == "status":
            return json.dumps({"undoable": lib.get_active_undo_count(), "done": 0})
        _authorize(token)
        if _game_world() is not None:
            raise RuntimeError("PIE is running; stop it first (ue pie stop): undo during play is not safe")
        done = 0
        for _ in range(max(1, count)):
            if action == "undo":
                if lib.get_active_undo_count() == 0 or not lib.undo_transaction(True):
                    break
            elif action == "redo":
                before = lib.get_active_undo_count()
                unreal.SystemLibrary.execute_console_command(None, "TRANSACTION REDO")
                if lib.get_active_undo_count() <= before:
                    break
            else:
                raise RuntimeError("action: status, undo or redo")
            done += 1
        _UNDO.clear()  # Tether's own undo records no longer match the level
        return json.dumps({"undoable": lib.get_active_undo_count(), "done": done})

    @_tool
    @staticmethod
    def anim_graph(target: str) -> str:
        """An Animation Blueprint's AnimGraph and state machines as text: what feeds the output pose, each state
        machine's entry state, each state with what it plays, aliases, and every transition with its rule and blend.

        Args:
            target: AnimBlueprint path.

        Returns:
            Text.
        """
        bp = unreal.load_asset(target.split(".")[0])
        if not isinstance(bp, unreal.AnimBlueprint):
            raise RuntimeError(f"{target} is not an Animation Blueprint (ue find --class AnimBlueprint)")
        return "\n".join(_anim_graph_lines(bp))

    @_tool
    @staticmethod
    def statetree(target: str = "-", pie: bool = False) -> str:
        """A StateTree asset as text (states, enter conditions, tasks, transitions, evaluators, global tasks), the
        project's StateTree assets, or in PIE each StateTree component with its tree and run status.

        Args:
            target: A StateTree asset path; '-' lists the assets (or, with pie, the components).
            pie: List the PIE world's StateTree components instead.

        Returns:
            Text.
        """
        if getattr(unreal, "StateTreeEditorData", None) is None:
            raise RuntimeError("needs the StateTree plugin (enable StateTree / GameplayStateTree)")
        if pie:
            game = _game_world()
            if game is None:
                raise RuntimeError("PIE is not running (ue pie start)")
            component_class = getattr(unreal, "StateTreeComponent", None)
            if component_class is None:
                raise RuntimeError("no StateTree components: GameplayStateTree is not enabled")
            rows = []
            for actor in unreal.GameplayStatics.get_all_actors_of_class(game, unreal.Actor):
                for comp in actor.get_components_by_class(component_class):
                    ref = _try(lambda: comp.get_editor_property("state_tree_ref"))
                    tree = _try(lambda: ref.get_editor_property("state_tree")) if ref else None
                    tree = tree or _try(lambda: comp.get_editor_property("state_tree"))
                    status = _enum_name(_try(lambda: comp.get_state_tree_run_status(), "?"))
                    rows.append(f"{_label(actor)}: {tree.get_name() if tree else 'no tree'}, {status}"
                                + ("" if _try(lambda: comp.is_active(), True) else ", inactive"))
            return _capped(f"{len(rows)} StateTree component(s) in PIE (Python cannot read the active state; "
                           "`ue statetree <asset>` shows the states)", rows, 60)
        if target in ("", "-"):
            registry = unreal.AssetRegistryHelpers.get_asset_registry()
            found = registry.get_assets(unreal.ARFilter(
                class_paths=[unreal.TopLevelAssetPath("/Script/StateTreeModule", "StateTree")], recursive_classes=True))
            names = sorted(str(d.package_name) for d in found if str(d.package_name).startswith("/Game"))
            return _capped(f"{len(names)} StateTree asset(s)", names, 60)
        tree = unreal.load_asset(target.split(".")[0])
        if not isinstance(tree, unreal.StateTree):
            raise RuntimeError(f"{target} is not a StateTree (ue statetree lists them)")
        return "\n".join(_statetree_lines(tree))

    @_tool
    @staticmethod
    def mat_graph(target: str, op: str = "-", args: str = "[]", token: str = "") -> str:
        """A material's (or material function's) node graph as text, or a small edit to it.

        Args:
            target: Material or MaterialFunction path.
            op: '-' to read; add <ExpressionClass> [name] [value]; connect <from>[.Output] <to>.<Input> | <Property>;
                set <node> prop=value ...; delete <node>. Nodes are '#n' from the last read, a parameter name, or a
                class name used once (e.g. Multiply).
            args: JSON list of the op's arguments.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it; edits only).

        Returns:
            The graph as text (after an edit too, with a line saying what changed).
        """
        lib = unreal.MaterialEditingLibrary
        asset = unreal.load_asset(target.split(".")[0])
        if not isinstance(asset, (unreal.Material, unreal.MaterialFunction)):
            raise RuntimeError(f"{target} is not a Material or MaterialFunction (ue mat lists an instance's parameters)")
        is_fn = isinstance(asset, unreal.MaterialFunction)
        exprs = lambda: list((lib.get_material_function_expressions if is_fn else lib.get_material_expressions)(asset))  # noqa: E731

        def short(expr):
            return expr.get_class().get_name().replace("MaterialExpression", "")

        def resolve(ref, nodes):
            ref = ref.strip()
            if ref.startswith("#") and ref[1:].isdigit() and 0 < int(ref[1:]) <= len(nodes):
                return nodes[int(ref[1:]) - 1]
            named = [e for e in nodes if str(_try(lambda: e.get_editor_property("parameter_name"), "")) == ref]
            if len(named) == 1:
                return named[0]
            by_class = [e for e in nodes if short(e).lower() == ref.lower()]
            if len(by_class) == 1:
                return by_class[0]
            raise RuntimeError(f"no single node '{ref}' (use #n from `ue mat graph`, a parameter name, or a class used once)")

        def mat_graph_text():
            nodes = exprs()
            ids = {e.get_path_name(): i + 1 for i, e in enumerate(nodes)}
            ref = lambda e, out="": f"#{ids.get(e.get_path_name(), '?')}" + (f".{out}" if out else "")  # noqa: E731
            lines = []
            if is_fn:
                lines.append(f"{asset.get_name()} (MaterialFunction), {len(nodes)} node(s)")
            else:
                info = [str(_try(lambda: asset.get_editor_property(p), "")).split(".")[-1].split(":")[0]
                        for p in ("material_domain", "blend_mode", "shading_model")]
                two_sided = _try(lambda: asset.get_editor_property("two_sided"), False)
                lines.append(f"{asset.get_name()} (Material: {', '.join(i.replace('MD_', '').replace('BLEND_', '').replace('MSM_', '') for i in info if i)}"
                             + (", two-sided" if two_sided else "") + f"), {len(nodes)} node(s)")
            used = set()

            def walk(expr):
                key = expr.get_path_name()
                if key in used:
                    return
                used.add(key)
                getter = lib.get_inputs_for_material_function_expression if is_fn else lib.get_inputs_for_material_expression
                for inp in _try(lambda: list(getter(asset, expr)), []) or []:
                    if inp:
                        walk(inp)
            if not is_fn:
                outputs = []
                for name in _MAT_OUTPUTS:
                    prop = getattr(unreal.MaterialProperty, "MP_" + name, None)
                    node = _try(lambda: lib.get_material_property_input_node(asset, prop)) if prop is not None else None
                    if node:
                        out = _try(lambda: lib.get_material_property_input_node_output_name(asset, prop), "") or ""
                        outputs.append(f"  {name.title().replace('_', '')} <- {ref(node, out)} {_mat_node_text(node, short(node))}")
                        walk(node)
                lines.append("outputs:" if outputs else "outputs: none connected")
                lines += outputs
            else:
                for expr in nodes:
                    if short(expr) == "FunctionOutput":
                        walk(expr)
            lines.append("nodes:")
            getter = lib.get_inputs_for_material_function_expression if is_fn else lib.get_inputs_for_material_expression
            for i, expr in enumerate(nodes):
                if expr.get_path_name() not in used:
                    continue
                names = list(_try(lambda: lib.get_material_expression_input_names(expr), []) or [])
                inputs = list(_try(lambda: getter(asset, expr), []) or [])
                wired = []
                sources = [i.get_path_name() for i in inputs if i]
                # Two inputs from one node: the library names the first link's output for both (FO <- #23.RGB and
                # FOA <- #23.RGB for .RGB and .A), so read each input's own OutputIndex from the export.
                links = _try(lambda: _mat_input_links(expr), []) if len(sources) != len(set(sources)) else []
                outs = {}
                for name, inp in zip(names, inputs):
                    if inp:
                        out = _try(lambda: lib.get_input_node_output_name_for_material_expression(expr, inp), "") or ""
                        if sources.count(inp.get_path_name()) > 1:
                            link = next((l for l in links if l[0] == inp.get_name()), None)
                            if link:
                                links.remove(link)
                                key = inp.get_path_name()
                                outs[key] = outs.get(key) or _try(lambda: _mat_output_names(inp), []) or []
                                out = outs[key][link[1]] if link[1] < len(outs[key]) else f"output {link[1]}"
                        wired.append(f"{name or 'In'} <- {ref(inp, out)}")
                lines.append(f"  #{i + 1} {_mat_node_text(expr, short(expr))}" + (f" ({', '.join(wired)})" if wired else ""))
            unused = [f"#{i + 1} {_mat_node_text(e, short(e))}" for i, e in enumerate(nodes)
                      if e.get_path_name() not in used and short(e) != "Comment"]
            if unused:
                lines.append(f"not connected to an output ({len(unused)}; `ue mat graph {asset.get_name()} prune` deletes "
                             "them): " + "; ".join(unused[:12]) + (f" +{len(unused) - 12}" if len(unused) > 12 else ""))
            return "\n".join(lines)

        note = ""
        if op not in ("", "-"):
            _authorize(token)
            argv = json.loads(args)
            nodes = exprs()
            if op == "add":
                if not argv:
                    raise RuntimeError("add <ExpressionClass> [parameter name] [value]")
                cls_name = argv[0] if argv[0].startswith("MaterialExpression") else "MaterialExpression" + argv[0]
                cls = getattr(unreal, cls_name, None)
                if cls is None:
                    raise RuntimeError(f"no expression class {cls_name}")
                xs = [lib.get_material_expression_node_position(e)[0] for e in nodes] or [0]
                create = lib.create_material_expression_in_function if is_fn else lib.create_material_expression
                expr = create(asset, cls, min(xs) - 300, 200 * len(nodes) % 1200)
                if len(argv) > 1 and _try(lambda: expr.get_editor_property("parameter_name"), None) is not None:
                    expr.set_editor_property("parameter_name", argv[1])
                if len(argv) > 2:
                    _mat_set_default(expr, argv[2])
                note = f"added #{len(nodes) + 1} {short(expr)}" + (f" '{argv[1]}'" if len(argv) > 1 else "")
            elif op == "connect":
                if len(argv) != 2:
                    raise RuntimeError("connect <from>[.Output] <to>.<Input> | <MaterialProperty>")
                source, _, out_pin = argv[0].partition(".")
                src = resolve(source, nodes)
                dest, _, in_pin = argv[1].partition(".")
                prop = getattr(unreal.MaterialProperty, "MP_" + re.sub(r"(?<!^)(?=[A-Z])", "_", dest).upper(), None)
                if prop is not None and not in_pin and not is_fn:
                    if not lib.connect_material_property(src, out_pin, prop):
                        raise RuntimeError(f"could not connect {argv[0]} to {dest}")
                    note = f"connected {argv[0]} -> {dest}"
                else:
                    dst = resolve(dest, nodes)
                    if not lib.connect_material_expressions(src, out_pin, dst, in_pin):
                        names = list(lib.get_material_expression_input_names(dst))
                        raise RuntimeError(f"could not connect {argv[0]} to {argv[1]} (inputs of {short(dst)}: "
                                           f"{', '.join(names) or 'none'})")
                    note = f"connected {argv[0]} -> {argv[1]}"
            elif op == "set":
                if len(argv) < 2:
                    raise RuntimeError("set <node> prop=value ...")
                expr = resolve(argv[0], nodes)
                for pair in argv[1:]:
                    name, _, value = pair.partition("=")
                    if name in ("value", "default"):
                        _mat_set_default(expr, value)
                    else:
                        old = expr.get_editor_property(name)
                        expr.set_editor_property(name, _mat_parse_value(value, old))
                note = f"set {argv[0]}: {', '.join(argv[1:])}"
            elif op == "delete":
                expr = resolve(argv[0], nodes)
                (lib.delete_material_expression_in_function if is_fn else lib.delete_material_expression)(asset, expr)
                note = f"deleted {argv[0]} ({short(expr)})"
            elif op == "prune":  # every node that reaches no output, in one undo step
                _, unused = _mat_unused(asset)
                if not unused:
                    return "nothing to prune: every node reaches an output\n" + mat_graph_text()
                with unreal.ScopedEditorTransaction(f"Prune {asset.get_name()}"):
                    asset.modify()
                    for expr in unused:
                        (lib.delete_material_expression_in_function if is_fn else lib.delete_material_expression)(asset, expr)
                note = f"pruned {len(unused)} node(s) that reached no output: " + ", ".join(
                    sorted({short(e) for e in unused}))[:200] + " (`ue undo --editor` brings them back)"
            else:
                raise RuntimeError("op: add, connect, set, delete or prune")
            if is_fn:
                lib.update_material_function(asset, None)
            else:
                lib.recompile_material(asset)
            _try(lambda: (lib.refresh_material_function_editor if is_fn else lib.refresh_material_editor)(asset))

        return (note + "\n" if note else "") + mat_graph_text()

    @_tool
    @staticmethod
    def level_check(gap: float = 5.0, limit: int = 400) -> str:
        """Problems with placed actors in the edited level that Map Check does not report: props floating above
        or buried under the ground, identical actors stacked in the same place, actors below Kill Z or far outside
        the landscape, hidden actors that still block, and more than 4 shadowing stationary lights overlapping.

        Args:
            gap: How far above the surface below counts as floating, cm.
            limit: Maximum issues to return.

        Returns:
            JSON {actors: n checked, issues: [{kind, actor, detail}]}.
        """
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        actors = [a for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
                  if a and not isinstance(a, (unreal.Brush, unreal.WorldSettings, unreal.LandscapeProxy))]
        issues = []

        def report(kind, actor, detail):
            if len(issues) < limit:
                issues.append({"kind": kind, "actor": _label(actor), "detail": detail})

        settings = world.get_world_settings()
        kill_z = _try(lambda: settings.get_editor_property("kill_z"), -1e9) if settings else -1e9
        lands = list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy))
        land_box = None
        if lands:
            boxes = [l.get_actor_bounds(False) for l in lands]
            land_box = (min(o.x - e.x for o, e in boxes), min(o.y - e.y for o, e in boxes),
                        max(o.x + e.x for o, e in boxes), max(o.y + e.y for o, e in boxes))
        seen = {}
        for actor in actors:
            meshes = [c for c in actor.get_components_by_class(unreal.StaticMeshComponent)
                      if c.get_editor_property("static_mesh") and not isinstance(c, unreal.InstancedStaticMeshComponent)]
            loc = actor.get_actor_location()
            if loc.z < kill_z:
                report("below kill z", actor, f"z {loc.z:.0f} is under Kill Z {kill_z:.0f}: it is destroyed when play starts")
            if land_box and not isinstance(actor, (unreal.Light, unreal.SkyLight, unreal.SkyAtmosphere)) and (
                    loc.x < land_box[0] - 20000 or loc.y < land_box[1] - 20000 or loc.x > land_box[2] + 20000
                    or loc.y > land_box[3] + 20000):
                report("outside", actor, f"at ({loc.x:.0f}, {loc.y:.0f}), more than 200 m beyond the landscape")
            if not meshes:
                continue
            hidden = bool(_try(lambda: actor.get_editor_property("hidden"), False)) or all(
                _try(lambda: c.get_editor_property("hidden_in_game"), False) or not _try(lambda: c.is_visible(), True)
                for c in meshes)
            blocking = any(c.get_collision_enabled() != unreal.CollisionEnabled.NO_COLLISION and
                           c.get_collision_response_to_channel(unreal.CollisionChannel.ECC_PAWN) == unreal.CollisionResponseType.ECR_BLOCK
                           for c in meshes)
            if hidden and blocking and not isinstance(actor, unreal.Volume):
                report("invisible blocker", actor, "hidden in game but blocks pawns: an invisible wall")
            key = (tuple(sorted(c.get_editor_property("static_mesh").get_path_name() for c in meshes)),
                   round(loc.x), round(loc.y), round(loc.z), tuple(round(v) for v in _rot_tuple(actor.get_actor_rotation())),
                   tuple(round(v, 2) for v in _vec_tuple(actor.get_actor_scale3d())))
            if key in seen:
                report("stacked", actor, f"same mesh, place, rotation and scale as {_label(seen[key])}")
            else:
                seen[key] = actor
            if actor.get_attach_parent_actor() is not None or _try(lambda: meshes[0].is_simulating_physics(), False):
                continue
            mobility = str(_try(lambda: meshes[0].get_editor_property("mobility"), "")).split(".")[-1].split(":")[0]
            origin, extent = actor.get_actor_bounds(False)
            if extent.z < 1 or extent.x > 5000 or extent.y > 5000:  # flat decals and huge pieces (floors, terrain)
                continue
            top, bottom = origin.z + extent.z, origin.z - extent.z
            # From inside the actor's top, so a prop sunk partly into the ground reads as resting on it.
            ground = _trace_down(world, origin.x, origin.y, ignore=[actor], top=top - 1)
            above = _trace_down(world, origin.x, origin.y, ignore=[actor], top=origin.z + 200000)
            if ground is None and above is None:
                continue
            if ground is not None:
                height = bottom - ground["impact_point"].z
                mounted = unreal.SystemLibrary.line_trace_single(
                    world, unreal.Vector(origin.x, origin.y, top - 1), unreal.Vector(origin.x, origin.y, top + 15),
                    unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [actor], unreal.DrawDebugTrace.NONE, True)
                if gap < height < 2000 and not mounted and mobility != "MOVABLE":
                    report("floating", actor, f"bottom is {height:.0f} cm above {_label(ground.get('hit_actor')) if ground.get('hit_actor') else 'the surface'} below")
            if above is not None and isinstance(above.get("hit_actor"), unreal.LandscapeProxy) and \
                    above["impact_point"].z > top + 1:
                report("buried", actor, f"top is {above['impact_point'].z - top:.0f} cm under the landscape")
        lights = [a for a in actors if isinstance(a, unreal.Light)]
        stationary = []
        for light in lights:
            comp = _try(lambda: light.get_editor_property("light_component"))
            if comp is None or isinstance(comp, unreal.DirectionalLightComponent):
                continue
            mobility = str(_try(lambda: comp.get_editor_property("mobility"), "")).split(".")[-1].split(":")[0]
            if mobility == "STATIONARY" and _try(lambda: comp.get_editor_property("cast_shadows"), True):
                radius = _try(lambda: comp.get_editor_property("attenuation_radius"), 1000.0)
                stationary.append((light, light.get_actor_location(), radius))
        for light, loc, radius in stationary:
            overlapping = [o for o, oloc, orad in stationary if o != light and loc.distance(oloc) < radius + orad]
            if len(overlapping) >= 4:
                report("stationary lights", light, f"overlaps {len(overlapping)} other shadowing stationary lights "
                                                   "(only 4 get shadow channels; the rest lose shadows)")
        # A component whose Collision Enabled disagrees with its (non-Custom) profile: the next property change
        # re-applies the profile (Python's set_collision_enabled is undone by the following PostEditChange)
        profiles = _collision_profiles()
        for actor in actors:
            for comp in _try(lambda: actor.get_components_by_class(unreal.PrimitiveComponent), []) or []:
                profile = str(_try(lambda: comp.get_collision_profile_name(), "") or "")
                expected = profiles.get(profile)
                if not expected or profile == "Custom":
                    continue
                actual = _enum_name(_try(lambda: comp.get_collision_enabled(), "")).replace("_", "")
                if actual and actual != expected:
                    report("collision profile", actor,
                           f"{comp.get_name()}: Collision Enabled {actual} but profile {profile} says {expected}; the "
                           "next edit re-applies the profile (set the profile to Custom, or pick one that fits)")
        return json.dumps({"actors": len(actors), "issues": issues, "capped": len(issues) >= limit})

    @_tool
    @staticmethod
    def landscape(action: str = "info", x: float = 0.0, y: float = 0.0, size: float = 0.0, px: int = 64) -> str:
        """Landscapes in the edited level: a summary, the surface at a point, or a height/layer grid of an area.

        Args:
            action: info; at (height, slope and paint-layer weights at x,y); grid (px x px samples of a size x size
                cm square centred on x,y: heights and the dominant paint layer, for a picture).
            x: World x.
            y: World y.
            size: With grid: side of the square, cm (default: the whole landscape).
            px: With grid: samples per side (max 256).

        Returns:
            info: text. at / grid: JSON.
        """
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        proxies = list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy))
        if not proxies:
            raise RuntimeError("no landscape in the edited level")
        landscapes = [p for p in proxies if isinstance(p, unreal.Landscape)] or proxies
        components = [c for p in proxies for c in p.get_components_by_class(unreal.LandscapeComponent)]
        layers = []
        for land in landscapes:
            for name in _try(lambda: list(land.get_target_layer_names()), []) or []:
                if str(name) not in layers:
                    layers.append(str(name))

        def component_at(px_, py_):
            """The landscape component covering a point (weights are read per component)."""
            for comp in components:
                origin, extent, _ = unreal.SystemLibrary.get_component_bounds(comp)
                if abs(px_ - origin.x) <= extent.x and abs(py_ - origin.y) <= extent.y:
                    return comp
            return None

        def surface(px_, py_):
            hits = unreal.SystemLibrary.line_trace_multi(
                world, unreal.Vector(px_, py_, 1e6), unreal.Vector(px_, py_, -1e6), unreal.TraceTypeQuery.TRACE_TYPE_QUERY1,
                True, [], unreal.DrawDebugTrace.NONE, True) or []
            for hit in hits:
                fields = _try(lambda: hit.to_dict()) or dict(zip(_HIT_FIELDS, hit.to_tuple()))
                if isinstance(fields.get("hit_actor"), unreal.LandscapeProxy):
                    return fields
            return None

        def weights(comp, point):
            out = {}
            for name in layers:
                w = _try(lambda: comp.editor_get_paint_layer_weight_by_name_at_location(point, name))
                if w is not None:
                    out[name] = round(w, 3)
            return out

        if action == "info":
            lines = []
            for land in landscapes:
                origin, extent = land.get_actor_bounds(False)
                scale = land.get_actor_scale3d()
                mine = [c for c in components if c.get_owner() == land or
                        _try(lambda: c.get_owner().get_landscape_actor()) == land]
                quads = round(unreal.SystemLibrary.get_component_bounds(mine[0])[1].x * 2 / scale.x) if mine else 0
                material = _try(lambda: land.get_editor_property("landscape_material"))
                lines.append(f"{_label(land)}: {extent.x * 2 / 100:.0f} x {extent.y * 2 / 100:.0f} m, z {origin.z - extent.z:.0f} "
                             f"to {origin.z + extent.z:.0f}, {len(mine)} component(s) of {quads} quads, "
                             f"{scale.x / 100:g} m per quad, material {material.get_name() if material else 'none'}")
            proxies_only = len(proxies) - len(landscapes)
            if proxies_only > 0:
                lines.append(f"{proxies_only} streaming proxies (World Partition)")
            lines.append("paint layers: " + (", ".join(layers) if layers else "none"))
            return "\n".join(lines)
        if action == "at":
            hit = surface(x, y)
            if hit is None:
                raise RuntimeError(f"no landscape below {x:.0f}, {y:.0f}")
            point, normal = hit["impact_point"], hit["impact_normal"]
            comp = component_at(x, y)
            return json.dumps({"x": round(x), "y": round(y), "z": round(point.z, 1),
                               "slope": round(_slope_degrees(normal), 1),
                               "normal": [round(normal.x, 3), round(normal.y, 3), round(normal.z, 3)],
                               "landscape": _label(hit["hit_actor"]), "weights": weights(comp, point) if comp else {}})
        if action == "grid":
            px = max(2, min(256, int(px)))
            if size <= 0:  # the whole (first) landscape
                origin, extent = landscapes[0].get_actor_bounds(False)
                x, y, size = origin.x, origin.y, 2 * max(extent.x, extent.y)
            step = size / (px - 1)
            heights, dominant = [], []
            for row in range(px):
                hrow, drow = [], []
                for col in range(px):
                    sx, sy = x - size / 2 + col * step, y - size / 2 + row * step
                    hit = surface(sx, sy)
                    if hit is None:
                        hrow.append(None)
                        drow.append(None)
                        continue
                    hrow.append(round(hit["impact_point"].z, 1))
                    comp = component_at(sx, sy) if layers else None
                    w = weights(comp, hit["impact_point"]) if comp else {}
                    drow.append(max(w, key=w.get) if w and max(w.values()) > 0 else None)
                heights.append(hrow)
                dominant.append(drow)
            return json.dumps({"x": x, "y": y, "size": size, "px": px, "heights": heights, "layers": layers,
                               "dominant": dominant})
        raise RuntimeError("action: info, at or grid")

    @_tool
    @staticmethod
    def landscape_edit(step: str, action: str = "-", x: float = 0.0, y: float = 0.0, radius: float = 0.0,
                       value: float = 0.0, strength: float = 1.0, backup: str = "-", file: str = "", layer: str = "",
                       token: str = "") -> str:
        """Sculpt the landscape under a circle: flatten (to value, or the height at the centre), raise (by value cm,
        negative lowers), smooth, or restore a backup. Python cannot sculpt, so this goes through the heightmap:
        export it to a render target, rewrite just the circle's square, check it reads back exactly, then import.
        GPU work lands a frame later, so the ue CLI calls the steps in turn: begin, apply, commit. The same path
        exports the whole heightmap to a .r16 file (export: begin, apply), imports one (import: begin, apply,
        commit; the old heights go to a .r16 backup), and imports a paint layer's weights from a picture (paint:
        begin, commit).

        Args:
            step: begin (export), apply (edit the square; writes a backup), commit (verify and import), or cancel.
            action: With begin: flatten, raise, smooth, restore, export, import or paint.
            x: Circle centre x.
            y: Circle centre y.
            radius: Circle radius, cm (the outer quarter fades out).
            value: flatten: target height (below -1e29: the centre's); raise: cm.
            strength: 0..1, how far toward the result (smooth: passes).
            backup: With restore: the backup file a previous edit wrote.
            file: export: the .r16 to write; import: a .r16 of exactly the landscape's vertex grid; paint: a PNG
                whose red channel is the layer's weight, stretched over the whole landscape.
            layer: With paint: the paint (target) layer's name.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {step, ...}.
        """
        _authorize(token)
        import math
        state = getattr(sys, "_tether_land_edit", None) or {}
        sys._tether_land_edit = state
        R = unreal.RenderingLibrary
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if step == "cancel":
            state.clear()
            return json.dumps({"step": "cancel"})
        if step == "begin":
            if _game_world() is not None:
                raise RuntimeError("PIE is running (ue pie stop)")
            lands = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Landscape)]
            if not lands:
                raise RuntimeError("no landscape in the edited level")
            land = lands[0]
            comps = [c for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy)
                     for c in a.get_components_by_class(unreal.LandscapeComponent)]
            boxes = [unreal.SystemLibrary.get_component_bounds(c) for c in comps]
            min_x = min(o.x - e.x for o, e, _ in boxes)
            min_y = min(o.y - e.y for o, e, _ in boxes)
            max_x = max(o.x + e.x for o, e, _ in boxes)
            max_y = max(o.y + e.y for o, e, _ in boxes)
            scale = land.get_actor_scale3d()
            w = int(round((max_x - min_x) / scale.x)) + 1
            h = int(round((max_y - min_y) / scale.y)) + 1
            rt = R.create_render_target2d(world, w, h, unreal.TextureRenderTargetFormat.RTF_RGBA8)
            if action == "paint":
                names = [str(n) for n in (_try(lambda: list(land.get_target_layer_names()), []) or [])]
                if layer not in names:
                    raise RuntimeError(f"no paint layer '{layer}' on {_actor_label(land)}"
                                       + (f" (it has {', '.join(names)})" if names else " (it has no paint layers)"))
                with open(file, "rb") as f:
                    texture = R.import_buffer_as_texture2d(world, list(f.read()))
                if texture is None:
                    raise RuntimeError(f"could not read {file} as a texture")
                texture.set_editor_property("srgb", False)
                canvas, _, context = R.begin_draw_canvas_to_render_target(world, rt)
                canvas.draw_texture(texture, unreal.Vector2D(0, 0), unreal.Vector2D(w, h), unreal.Vector2D(0, 0),
                                    unreal.Vector2D(1, 1), unreal.LinearColor(1, 1, 1, 1), unreal.BlendMode.BLEND_OPAQUE)
                R.end_draw_canvas_to_render_target(world, context)
                # The layer's weights as they are, for a backup (the editor's undo does not always keep paint)
                before_rt = R.create_render_target2d(world, w, h, unreal.TextureRenderTargetFormat.RTF_RGBA8)
                saved = _try(lambda: land.render_weightmap(unreal.Transform(), unreal.Box2D(), layer, before_rt), False)
                state.clear()
                state.update(land=land, rt=rt, w=w, h=h, action="paint", layer=layer, texture=texture,
                             scale=(scale.x, scale.y, scale.z), z0=land.get_actor_location().z,
                             before_rt=before_rt if saved else None)
                return json.dumps({"step": "begin", "vertices": [w, h]})
            if not land.landscape_export_heightmap_to_render_target(rt, True, True):
                raise RuntimeError("the heightmap export failed")
            state.clear()
            state.update(land=land, rt=rt, w=w, h=h, min_x=min_x, min_y=min_y, scale=(scale.x, scale.y, scale.z),
                         z0=land.get_actor_location().z, action=action, args=(x, y, radius, value, strength, backup),
                         file=file)
            return json.dumps({"step": "begin", "vertices": [w, h]})
        if not state.get("rt"):
            raise RuntimeError("no landscape edit in progress (begin first)")
        w, h, sx, sy, sz = state["w"], state["h"], *state["scale"]
        to_raw = lambda z: 32768.0 + (z - state["z0"]) / sz * 128.0  # noqa: E731
        to_z = lambda raw: state["z0"] + (raw - 32768.0) / 128.0 * sz  # noqa: E731
        if step == "apply" and state["action"] == "export":
            values = _read_all_heights(world, state["rt"], w, h)
            path = state["file"]
            _write_r16(path, values)
            result = {"step": "apply", "file": path, "vertices": [w, h],
                      "z_min": round(to_z(min(values)), 1), "z_max": round(to_z(max(values)), 1)}
            state.clear()
            return json.dumps(result)
        if step == "apply":
            x, y, radius, value, strength, backup_file = state["args"]
            if state["action"] == "import":
                patch = _read_r16(state["file"])
                if len(patch) != w * h:
                    raise RuntimeError(f"the heightmap has {len(patch)} samples; the landscape is {w} x {h} vertices "
                                       f"({w * h}): resample it to that size first")
                old = _read_all_heights(world, state["rt"], w, h)
                folder = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                                      "Tether", "landscape")
                backup_file = os.path.normpath(os.path.join(folder, time.strftime("backup-%Y%m%d-%H%M%S.r16")))
                _write_r16(backup_file, old)
                x0, y0, x1, y1 = 0, 0, w - 1, h - 1
                state["before"] = None
                state["range"] = (to_z(min(patch)), to_z(max(patch)))
            elif state["action"] == "restore":
                with open(backup_file, encoding="utf-8") as f:
                    saved = json.load(f)
                x0, y0, x1, y1 = saved["square"]
                patch = saved["raw"]
            else:
                cx, cy = (x - state["min_x"]) / sx, (y - state["min_y"]) / sy
                r = radius / sx
                x0, y0 = max(0, int(math.floor(cx - r)) - 1), max(0, int(math.floor(cy - r)) - 1)
                x1, y1 = min(w - 1, int(math.ceil(cx + r)) + 1), min(h - 1, int(math.ceil(cy + r)) + 1)
                if x0 > x1 or y0 > y1:
                    raise RuntimeError("the circle is outside the landscape")
                pixels = _read_square(world, state["rt"], w, h, x0, y0, x1, y1)
                cols = x1 - x0 + 1
                raw = _rg16(pixels)
                old = list(raw)
                centre = raw[min(len(raw) - 1, int(round(cy - y0)) * cols + int(round(cx - x0)))]
                target = to_raw(value) if state["action"] == "flatten" and value > -1e29 else centre
                passes = max(1, int(round(strength))) if state["action"] == "smooth" else 1
                for _ in range(passes):
                    src = list(raw)
                    for j in range(y1 - y0 + 1):
                        for i in range(cols):
                            d = math.hypot(x0 + i - cx, y0 + j - cy)
                            if d > r:
                                continue
                            fade = 1.0 if d <= 0.75 * r else max(0.0, (r - d) / (0.25 * r))
                            k = j * cols + i
                            if state["action"] == "flatten":
                                goal = target
                            elif state["action"] == "raise":
                                goal = src[k] + value / sz * 128.0
                            elif state["action"] == "smooth":
                                near = [src[(j + dj) * cols + i + di] for dj in (-1, 0, 1) for di in (-1, 0, 1)
                                        if 0 <= j + dj <= y1 - y0 and 0 <= i + di < cols]
                                goal = sum(near) / len(near)
                            else:
                                raise RuntimeError("action: flatten, raise, smooth or restore")
                            amount = fade * (1.0 if state["action"] == "smooth" else max(0.0, min(1.0, strength)))
                            raw[k] = int(round(max(0, min(65535, src[k] + (goal - src[k]) * amount))))
                patch = raw
                folder = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                                      "Tether", "landscape")
                os.makedirs(folder, exist_ok=True)
                backup_file = os.path.normpath(os.path.join(folder, time.strftime("backup-%Y%m%d-%H%M%S.json")))
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump({"landscape": state["land"].get_path_name(), "square": [x0, y0, x1, y1], "raw": old}, f)
                state["before"] = (to_z(centre), max(abs(a - b) for a, b in zip(old, raw)) / 128.0 * sz)
            cols, rows = x1 - x0 + 1, y1 - y0 + 1
            rgba = bytearray(b"\x00\x00\x00\xff" * len(patch))  # height in R (high byte) and G (low byte)
            rgba[0::4] = bytes(v >> 8 for v in patch)
            rgba[1::4] = bytes(v & 255 for v in patch)
            texture =R.import_buffer_as_texture2d(world, list(_png_rgba8(cols, rows, rgba)))
            if texture is None:
                raise RuntimeError("could not make the patch texture")
            texture.set_editor_property("srgb", False)
            texture.set_editor_property("filter", unreal.TextureFilter.TF_NEAREST)
            canvas, _, context = R.begin_draw_canvas_to_render_target(world, state["rt"])
            canvas.draw_texture(texture, unreal.Vector2D(x0, y0), unreal.Vector2D(cols, rows), unreal.Vector2D(0, 0),
                                unreal.Vector2D(1, 1), unreal.LinearColor(1, 1, 1, 1), unreal.BlendMode.BLEND_OPAQUE)
            R.end_draw_canvas_to_render_target(world, context)
            state.update(square=(x0, y0, x1, y1), patch=patch, texture=texture, backup=backup_file)
            return json.dumps({"step": "apply", "square": [x0, y0, x1, y1], "backup": backup_file})
        if step == "commit" and state["action"] == "paint":
            backup_file = None
            if state.get("before_rt"):
                weights = bytearray()
                for y0 in range(0, h, 64):
                    pixels = _read_square(world, state["before_rt"], w, h, 0, y0, w - 1, min(h, y0 + 64) - 1)
                    scale255 = 1.0 if any(p.r > 1.0 for p in pixels) else 255.0
                    weights += bytes(int(round(min(255.0, p.r * scale255))) for p in pixels)
                rgba = bytearray(b"\x00\x00\x00\xff" * len(weights))
                rgba[0::4], rgba[1::4], rgba[2::4] = weights, weights, weights
                folder = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                                      "Tether", "landscape")
                os.makedirs(folder, exist_ok=True)
                backup_file = os.path.normpath(os.path.join(folder, time.strftime(
                    f"paint-{re.sub(r'[^A-Za-z0-9_-]', '_', state['layer'])}-%Y%m%d-%H%M%S.png")))
                with open(backup_file, "wb") as f:
                    f.write(_png_rgba8(w, h, rgba))
            land = state["land"]
            before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
            with unreal.ScopedEditorTransaction(f"ue landscape paint {state['layer']}"):
                land.modify()
                ok = land.landscape_import_weightmap_from_render_target(state["rt"], state["layer"], 0)
            _record_undo(before, f"landscape paint {state['layer']}")
            result = {"step": "commit", "ok": bool(ok), "action": "paint", "layer": state["layer"], "vertices": w * h,
                      "backup": backup_file}
            state.clear()
            return json.dumps(result)
        if step == "commit":
            x0, y0, x1, y1 = state["square"]
            if state["action"] == "import":
                back = _read_all_heights(world, state["rt"], w, h)
            else:
                back = _rg16(_read_square(world, state["rt"], w, h, x0, y0, x1, y1))
            wrong = sum(1 for a, b in zip(back, state["patch"]) if a != b)
            if wrong or len(back) != len(state["patch"]):
                state.clear()
                raise RuntimeError(f"the patch did not read back exactly ({wrong} of {len(back)} vertices differ); "
                                   "nothing was changed")
            land = state["land"]
            before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
            with unreal.ScopedEditorTransaction(f"ue landscape {state['action']}"):
                land.modify()
                ok = land.landscape_import_heightmap_from_render_target(state["rt"], True, 0)
            _record_undo(before, f"landscape {state['action']}")
            result = {"step": "commit", "ok": bool(ok), "action": state["action"], "backup": state.get("backup"),
                      "vertices": len(back)}
            if state.get("before"):
                result.update(centre_before=round(state["before"][0], 1), max_change_cm=round(state["before"][1], 1))
            if state.get("range"):
                result.update(z_min=round(state["range"][0], 1), z_max=round(state["range"][1], 1),
                              grid={"min_x": state["min_x"], "min_y": state["min_y"], "scale": list(state["scale"]),
                                    "z0": state["z0"], "w": w, "h": h})
            state.clear()
            return json.dumps(result)
        raise RuntimeError("step: begin, apply, commit or cancel")

    @_tool
    @staticmethod
    def foliage(action: str = "list", what: str = "-", area: str = "-", density: float = -1.0, count: int = -1,
                spacing: float = -1.0, slope: str = "-", height: str = "-", scale: str = "-", align: bool = False,
                seed: int = 0, folder: str = "/Game/Foliage", mask: str = "", avoid: str = "", value: float = -1.0,
                scope: str = "", token: str = "") -> str:
        """Foliage instances in the edited level: list them by type, add instances over an area (real instanced
        foliage, snapped to the ground), or remove them from an area. Unset options use the foliage type's own
        settings (density, radius, ground slope, height range, scale, random yaw, align to normal). check reports
        culling, LODs, billboards, wind and shadows per instanced mesh; convert turns StaticMeshActors into
        instances; fix billboards|cull|wpo repairs what check finds.

        Args:
            action: list, add, remove, check, convert or fix.
            what: A FoliageType asset, or a static mesh (its foliage type is found, or created in folder for add).
                '-' for every type (list, remove).
            area: 'x,y,radius'; required for add and remove, optional for list.
            density: Instances per 10 m x 10 m (the foliage tool's unit).
            count: Exact number to add instead of density.
            spacing: Minimum distance between new instances, cm.
            slope: 'min,max' ground slope in degrees.
            height: 'min,max' world z.
            scale: 'min,max' uniform scale.
            align: Tilt each instance to the ground normal.
            seed: Random seed (same seed, same layout).
            folder: Where a foliage type made for a mesh goes.
            mask: With add: JSON {w, h, data (base64 bytes, rows along +Y), rect?}: density 0..1 per pixel over rect
                (default: the landscape), which also becomes the area when none is given.
            avoid: With add: JSON [[regex, radius cm], ...]: keep away from actors whose label, class or tag matches.
            value: With fix cull|wpo: the distance in cm.
            scope: With fix: a regex over mesh names, or a content folder (billboards only) to fix every mesh in
                (default: all the level's instanced meshes).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it; add and remove).

        Returns:
            What was found, added or removed.
        """
        import math
        import random
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        circle = _numbers(area) if area not in ("", "-") else None
        if circle is not None and len(circle) != 3:
            raise RuntimeError("area: expected 'x,y,radius'")
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        types = []
        for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Foliage", "FoliageType_InstancedStaticMesh"),
                                                 True):
            ft = _try(lambda: unreal.load_asset(str(data.package_name)))
            if ft is not None:
                types.append(ft)
        by_mesh = {}
        for ft in types:
            mesh = _try(lambda: ft.get_editor_property("mesh"))
            if mesh:
                by_mesh.setdefault(mesh.get_path_name(), []).append(ft)

        def resolve(ref, create):
            asset = unreal.load_asset(ref.split(".")[0])
            if asset is None:
                raise RuntimeError(f"no asset at {ref} (a FoliageType or a StaticMesh)")
            if isinstance(asset, unreal.FoliageType):
                return asset, None
            if not isinstance(asset, unreal.StaticMesh):
                raise RuntimeError(f"{ref} is a {asset.get_class().get_name()}, not a FoliageType or StaticMesh")
            known = by_mesh.get(asset.get_path_name(), [])
            if len(known) > 1 and not create:
                raise RuntimeError(f"{len(known)} foliage types use {asset.get_name()}: name one "
                                   f"({', '.join(_short_path(k.get_path_name()) for k in known)})")
            if known:
                return known[0], None
            if not create:
                raise RuntimeError(f"no foliage type uses {asset.get_name()}")
            name = "FT_" + re.sub(r"^SM_", "", asset.get_name())
            ft = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                name, folder.rstrip("/"), unreal.FoliageType_InstancedStaticMesh,
                unreal.FoliageType_InstancedStaticMeshFactory())
            if ft is None:
                raise RuntimeError(f"could not create {folder}/{name}")
            ft.set_editor_property("mesh", asset)
            unreal.EditorAssetLibrary.save_loaded_asset(ft, False)
            return ft, f"created {_short_path(ft.get_path_name())} for {asset.get_name()}"

        components = []
        for ifa in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.InstancedFoliageActor):
            components += ifa.get_components_by_class(unreal.FoliageInstancedStaticMeshComponent)

        def in_circle(comp):
            """Indices of a component's instances inside the area (all of them without one)."""
            total = comp.get_instance_count()
            if circle is None:
                return list(range(total))
            cx, cy, r = circle
            box = unreal.Box(unreal.Vector(cx - r, cy - r, -1e7), unreal.Vector(cx + r, cy + r, 1e7))
            hits = comp.get_instances_overlapping_box(box, True)
            keep = []
            for i in hits:
                loc = comp.get_instance_transform(i, True).translation
                if (loc.x - cx) ** 2 + (loc.y - cy) ** 2 <= r * r:
                    keep.append(i)
            return keep

        if action == "list":
            rows = {}
            for comp in components:
                mesh = comp.get_editor_property("static_mesh")
                key = mesh.get_path_name() if mesh else "?"
                total, inside = rows.get(key, (0, 0))
                rows[key] = (total + comp.get_instance_count(), inside + (len(in_circle(comp)) if circle else 0))
            out = []
            for key, (total, inside) in sorted(rows.items(), key=lambda kv: -kv[1][0]):
                names = ", ".join(ft.get_name() for ft in by_mesh.get(key, [])) or "no foliage type asset"
                out.append(f"{names} ({key.rsplit('.', 1)[-1]}): {total} instance(s)"
                           + (f", {inside} in the area" if circle else ""))
            unused = [ft.get_name() for ft in types if _try(lambda: ft.get_editor_property("mesh")) and
                      ft.get_editor_property("mesh").get_path_name() not in rows]
            head = f"{sum(t for t, _ in rows.values())} foliage instance(s) of {len(rows)} mesh(es) in {len(components)} component(s)"
            if unused:
                out.append(f"foliage types with no instances here: {', '.join(sorted(unused)[:12])}"
                           + (f" +{len(unused) - 12}" if len(unused) > 12 else ""))
            return _capped(head, out, 60)
        if action == "check":
            return _foliage_check(world, what, 80)
        _authorize(token)
        if action == "fix":
            folder_scope = scope.startswith("/")  # a content folder; anything else is a regex over mesh names
            before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
            with unreal.ScopedEditorTransaction(f"ue foliage fix {what}"):
                out = _foliage_fix(world, what, "-" if folder_scope or not scope else scope, value,
                                   scope if folder_scope else "", by_mesh)
            _record_undo(before, f"foliage fix {what}")
            return out
        if action == "convert":
            if what in ("", "-"):
                raise RuntimeError("convert what? a regex over actor labels, tags or mesh names")
            before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
            out = _foliage_convert(world, what, folder, resolve)
            _record_undo(before, f"foliage convert {what}")
            return out
        sampler = _mask_sampler(world, mask) if mask and action == "add" else None
        if circle is None and sampler is None:
            raise RuntimeError(f"{action} needs an area: 'x,y,radius'" + (" (or a --mask)" if action == "add" else ""))
        if action == "add":
            if what in ("", "-"):
                raise RuntimeError("add what? a FoliageType asset or a static mesh")
            ft, note = resolve(what, create=True)
            rng = random.Random(seed)
            per_100m2 = density if density >= 0 else float(_try(lambda: ft.get_editor_property("density"), 100.0))
            if circle is not None:
                cx, cy, radius = circle
                area_cm2 = math.pi * radius * radius
            else:
                rx0, ry0, rx1, ry1 = sampler[1]
                area_cm2 = (rx1 - rx0) * (ry1 - ry0)
            wanted = count if count >= 0 else int(round(per_100m2 * area_cm2 / 1e6))
            gap = spacing if spacing >= 0 else float(_try(lambda: ft.get_editor_property("radius"), 0.0))
            slope_range = _numbers(slope) if slope not in ("", "-") else None
            if slope_range is None:
                interval = _try(lambda: ft.get_editor_property("ground_slope_angle"))
                slope_range = [interval.min, interval.max] if interval else [0, 90]
            height_range = _numbers(height) if height not in ("", "-") else None
            if height_range is None:
                interval = _try(lambda: ft.get_editor_property("height"))
                height_range = [interval.min, interval.max] if interval else [-1e9, 1e9]
            scale_range = _numbers(scale) if scale not in ("", "-") else None
            if scale_range is None:
                interval = _try(lambda: ft.get_editor_property("scale_x"))
                scale_range = [interval.min, interval.max] if interval else [1, 1]
            scale_range = (scale_range * 2)[:2]
            random_yaw = bool(_try(lambda: ft.get_editor_property("random_yaw"), True))
            tilt = align or bool(_try(lambda: ft.get_editor_property("align_to_normal"), False))
            transforms, skipped = [], {"mask": 0, "keep-out": 0, "no ground": 0, "slope": 0, "height": 0}
            keep_out = _avoid_points(world, avoid)
            points = (_circle_points(rng, cx, cy, radius, wanted, gap) if circle is not None
                      else _rect_points(rng, *sampler[1], wanted, gap))
            for x, y in points:
                if sampler is not None and rng.random() >= sampler[0](x, y):  # thinned by the mask, before tracing
                    skipped["mask"] += 1
                    continue
                if any((x - ax) ** 2 + (y - ay) ** 2 < ar * ar for ax, ay, ar in keep_out):
                    skipped["keep-out"] += 1
                    continue
                hit = _trace_down(world, x, y)
                if hit is None:
                    skipped["no ground"] += 1
                    continue
                normal, point = hit["impact_normal"], hit["impact_point"]
                if not slope_range[0] <= _slope_degrees(normal) <= slope_range[1]:
                    skipped["slope"] += 1
                    continue
                if not height_range[0] <= point.z <= height_range[1]:
                    skipped["height"] += 1
                    continue
                yaw = rng.uniform(0, 360) if random_yaw else 0.0
                rotation = unreal.Rotator(roll=0, pitch=0, yaw=yaw)
                if tilt:
                    rotation = unreal.MathLibrary.make_rot_from_zx(normal, unreal.MathLibrary.get_forward_vector(rotation))
                s = rng.uniform(*scale_range)
                transforms.append(unreal.Transform(point, rotation, unreal.Vector(s, s, s)))
            if transforms:
                before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
                with unreal.ScopedEditorTransaction(f"ue foliage add {ft.get_name()} x{len(transforms)}"):
                    unreal.InstancedFoliageActor.add_instances(world, ft, transforms)
                _record_undo(before, f"foliage add {ft.get_name()} x{len(transforms)}")
            notes = [f"{n} {why}" for why, n in skipped.items() if n]
            if len(transforms) + sum(skipped.values()) < wanted:
                notes.append(f"{wanted - len(transforms) - sum(skipped.values())} did not fit with spacing {gap:g}")
            return (f"added {len(transforms)} of {wanted} {ft.get_name()}" + (f" ({note})" if note else "")
                    + (": skipped " + ", ".join(notes) if notes else ""))
        if action == "remove":
            targets = [resolve(what, create=False)[0]] if what not in ("", "-") else types
            removed, kept_total = [], 0
            before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
            with unreal.ScopedEditorTransaction("ue foliage remove"):
                for ft in targets:
                    mesh = _try(lambda: ft.get_editor_property("mesh"))
                    comps = [c for c in components if mesh and c.get_editor_property("static_mesh") == mesh]
                    inside = {id(c): set(in_circle(c)) for c in comps}
                    if not any(inside.values()):
                        continue
                    if len(by_mesh.get(mesh.get_path_name(), [])) > 1 and what in ("", "-"):
                        continue  # can't tell which type an instance came from; name the type to remove it
                    # Python can only remove all of a type's instances: keep the rest by adding them back.
                    keep = [c.get_instance_transform(i, True) for c in comps for i in range(c.get_instance_count())
                            if i not in inside[id(c)]]
                    unreal.InstancedFoliageActor.remove_all_instances(world, ft)
                    if keep:
                        unreal.InstancedFoliageActor.add_instances(world, ft, keep)
                    removed.append(f"{sum(len(v) for v in inside.values())} {ft.get_name()}")
                    kept_total += len(keep)
            _record_undo(before, "foliage remove")
            return ("removed " + ", ".join(removed) + f" ({kept_total} outside the area kept)") if removed else \
                "no foliage instances of those types in the area"
        raise RuntimeError("action: list, add or remove")

    @_tool
    @staticmethod
    def delete(targets: str, token: str = "") -> str:
        """Delete actors from the edited level (undoable).

        Args:
            targets: Comma-separated actor labels or names.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was deleted.
        """
        _authorize(token)
        actors = []
        for ref in [t.strip() for t in targets.split(",") if t.strip()]:
            obj = _resolve(ref)
            if not isinstance(obj, unreal.Actor):
                raise RuntimeError(f"'{ref}' is not an actor")
            actors.append(obj)
        labels = [_actor_label(a) for a in actors]
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        with unreal.ScopedEditorTransaction("ue delete"):
            ok = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(actors)
        if not ok:
            raise RuntimeError("the editor refused to delete them")
        _record_undo(before, "delete " + ", ".join(labels))
        return "deleted " + ", ".join(labels)

    @_tool
    @staticmethod
    def rerun(targets: str, token: str = "") -> str:
        """Rerun actors' construction scripts (undoable), as editing a property in the Details panel does: for a
        Blueprint that applies its variables there, after they were set without a change notification.

        Args:
            targets: Comma-separated actor labels or names.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One line per actor: its class and component count before and after.
        """
        _authorize(token)
        from tether import scripting
        actors = []
        for ref in [t.strip() for t in targets.split(",") if t.strip()]:
            obj = _resolve(ref)
            if not isinstance(obj, unreal.Actor):
                raise RuntimeError(f"'{ref}' is not an actor")
            actors.append(obj)
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        rows = []
        with unreal.ScopedEditorTransaction("ue rerun"):
            for actor in actors:
                count = len(actor.get_components_by_class(unreal.ActorComponent))
                scripting.rerun(actor)
                cls = actor.get_class()
                blueprint = cls.get_name().endswith("_C")
                rows.append(f"{_actor_label(actor)} ({cls.get_name()}): reran its construction script, "
                            f"{count} -> {len(actor.get_components_by_class(unreal.ActorComponent))} components"
                            + ("" if blueprint else " (a C++ class: only OnConstruction runs again)"))
        _record_undo(before, "rerun " + ", ".join(_actor_label(a) for a in actors))
        return "\n".join(rows)

    @_tool
    @staticmethod
    def duplicate(target: str, offset: str = "100,0,0", count: int = 1, token: str = "") -> str:
        """Duplicate an actor (undoable), each copy shifted by `offset` from the previous one.

        Args:
            target: Actor label or name.
            offset: Shift per copy 'x,y,z'.
            count: Number of copies.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            The copies' labels and locations.
        """
        _authorize(token)
        source = _resolve(target)
        if not isinstance(source, unreal.Actor):
            raise RuntimeError(f"'{target}' is not an actor")
        nums = _numbers(offset) or [100, 0, 0]
        step = unreal.Vector(*nums)
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        copies, previous = [], source
        with unreal.ScopedEditorTransaction(f"ue duplicate {_actor_label(source)}"):  # all copies, one undo step
            for _ in range(max(1, count)):
                copy = subsystem.duplicate_actor(previous, None, step)
                if copy is None:
                    break
                copies.append(copy)
                previous = copy
        if not copies:
            raise RuntimeError("the editor refused to duplicate it")
        _record_undo(before, f"duplicate {_actor_label(source)} x{len(copies)}")
        return "\n".join(f"{_actor_label(c)} at ({c.get_actor_location().x:g}, {c.get_actor_location().y:g}, "
                         f"{c.get_actor_location().z:g})" for c in copies)

    @_tool
    @staticmethod
    def mv(asset: str, dest: str, token: str = "") -> str:
        """Move or rename an asset and fix up what referenced it (not undoable; version control is the undo).

        Args:
            asset: /Game/... asset path.
            dest: New /Game/... path (folder/Name), or a folder ending in '/' to keep the name.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            Old and new paths, and how many referencers were fixed up.
        """
        _authorize(token)
        source = asset.split(".")[0]
        target = (dest.rstrip("/") + "/" + source.rsplit("/", 1)[-1]) if dest.endswith("/") else dest.split(".")[0]
        library = unreal.EditorAssetLibrary
        if not library.does_asset_exist(source):
            raise RuntimeError(f"no asset at {source} (ue find)")
        if library.does_asset_exist(target):
            raise RuntimeError(f"{target} already exists")
        vcs, source_file = _Vcs(), _package_file(source)
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        vcs.reserve(sorted({str(n) for n in registry.get_referencers(source, _dependency_options()) or []} - {source}))
        problems, _, notes = vcs.prepare([source_file])
        if problems:
            raise RuntimeError("; ".join(problems))
        dirty_before = set(sum(_dirty_packages(), []))
        if not library.rename_asset(source, target):
            raise RuntimeError("the rename failed (see ue log)")
        leftover = unreal.load_asset(source) if library.does_asset_exist(source) else None
        if leftover is not None and leftover.get_class().get_name() == "ObjectRedirector":
            saved, problems = _fixup_redirector(source, vcs)  # repoint the referencers, drop the redirector
            notes.append(f"redirector fixed up, {saved} referencer(s) saved" if not problems
                         else "redirector left: " + "; ".join(problems))
        notes += vcs.added([_package_file(target, source_file.endswith(".umap"))]) + vcs.deleted([source_file])
        maps, content = _dirty_packages()
        touched = [p for p in maps + content if p != target and p not in dirty_before]
        if touched:
            notes.append(f"{len(touched)} referencing package(s) updated but unsaved: " + ", ".join(touched[:5])
                         + " (ue save)")
        return f"moved {source} -> {target}" + (" (" + "; ".join(notes) + ")" if notes else "")

    @_tool
    @staticmethod
    def move_folder(source: str, dest: str, dry_run: bool = False, token: str = "") -> str:
        """Move a content folder (a Fab or marketplace pack that landed at the content root) and everything in it,
        fixing up what referenced it: redirectors are resolved and removed, the moved assets saved, the emptied
        folder deleted (not undoable; version control is the undo).

        Args:
            source: /Game/... folder to move.
            dest: /Game/... folder it becomes (must not exist yet, or be empty).
            dry_run: Only report what would move and who references it.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {moved, maps, referencers, notes, left}.
        """
        _authorize(token)
        library = unreal.EditorAssetLibrary
        src, dst = source.rstrip("/"), dest.rstrip("/")
        if not (src.startswith("/Game/") and dst.startswith("/Game/")):
            raise RuntimeError("both folders must be under /Game")
        if (dst + "/").startswith(src + "/"):
            raise RuntimeError(f"{dst} is inside {src}")
        packages = sorted({a.split(".")[0] for a in library.list_assets(src, recursive=True, include_folder=False)})
        if not packages:
            raise RuntimeError(f"no assets under {src} (ue find {src}/*)")
        clashes = [p for p in library.list_assets(dst, recursive=True, include_folder=False)] \
            if library.does_directory_exist(dst) else []
        if clashes:
            raise RuntimeError(f"{dst} already holds {len(clashes)} asset(s): move into an empty or new folder")
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        inside, outside = set(packages), set()
        for package in packages:
            for user in registry.get_referencers(package, options) or []:
                if str(user) not in inside:
                    outside.add(str(user))
        maps = [p for p in packages if str(_try(lambda: library.find_asset_data(p).asset_class_path.asset_name, "")) == "World"]
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        open_map = world.get_outermost().get_name() if world else ""
        result = {"moved": len(packages), "maps": maps, "referencers": sorted(outside), "notes": [], "left": []}
        if dry_run:
            return json.dumps(result)
        if open_map in inside:
            raise RuntimeError(f"the open map {open_map} is in {src}: open another map first (ue level open ...)")
        vcs = _Vcs()
        vcs.reserve(sorted(outside))  # they get resaved: refuse before moving anything if one is someone else's
        files = [_package_file(p) for p in packages]
        problems, _, notes = vcs.prepare(files)
        if problems:
            raise RuntimeError("; ".join(problems[:5]))
        dirty_before = set(sum(_dirty_packages(), []))
        if not library.rename_directory(src, dst):
            raise RuntimeError("the move failed (see ue log); some assets may have moved: ue find " + dst)
        for package in sorted(a.split(".")[0] for a in library.list_assets(src, recursive=True, include_folder=False)):
            asset = _try(lambda: unreal.load_asset(package))
            if asset is not None and asset.get_class().get_name() == "ObjectRedirector":
                saved, problems = _fixup_redirector(package, vcs)
                if problems:
                    notes.append(f"redirector {package} left: " + "; ".join(problems))
        left = sorted({a.split(".")[0] for a in library.list_assets(src, recursive=True, include_folder=False)})
        if not left:
            _try(lambda: library.delete_directory(src))
        library.save_directory(dst, only_if_is_dirty=True, recursive=True)
        moved_files = [_package_file(p) for p in sorted({a.split(".")[0] for a in
                                                          library.list_assets(dst, recursive=True, include_folder=False)})]
        notes += vcs.added(moved_files) + vcs.deleted([f for f in files if not os.path.isfile(f)])
        dirty = [p for p in sum(_dirty_packages(), []) if p not in dirty_before and not p.startswith(dst + "/")]
        if dirty:
            notes.append(f"{len(dirty)} referencing package(s) updated but unsaved: " + ", ".join(dirty[:5]) + " (ue save)")
        if maps:
            notes.append("maps moved: their external actors (World Partition) and saved bookmarks follow the map; "
                         "open one to check (ue level open)")
        result.update(notes=notes, left=left)
        return json.dumps(result)

    @_tool
    @staticmethod
    def rm(asset: str, force: bool = False, token: str = "") -> str:
        """Delete an asset, refusing while anything references it (not undoable; version control is the undo).

        Args:
            asset: /Game/... asset path.
            force: Delete even if referenced (the references break).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was deleted.
        """
        _authorize(token)
        package = asset.split(".")[0]
        if not package.startswith("/Game/"):
            raise RuntimeError("only assets under /Game can be deleted")
        if not unreal.EditorAssetLibrary.does_asset_exist(package):
            raise RuntimeError(f"no asset at {package} (ue find)")
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        users = sorted({str(n) for n in registry.get_referencers(package, options) or []} - {package})
        if users and not force:
            raise RuntimeError(f"{len(users)} asset(s) still reference {package}: " + ", ".join(users[:8])
                               + (" ..." if len(users) > 8 else "") + " (ue refs; pass --force to delete anyway)")
        vcs, file = _Vcs(), _package_file(package)
        problems = [p for p in vcs.prepare([file])[0] if "checked out by" in p or not vcs.checkouts]
        if problems:
            raise RuntimeError("; ".join(problems))
        if not unreal.EditorAssetLibrary.delete_asset(package):
            raise RuntimeError("the delete failed (see ue log)")
        notes = vcs.deleted([file])
        return f"deleted {package}" + (f" ({len(users)} references now broken)" if users else "") + \
            (f"\n{vcs.provider}: " + "; ".join(notes) if notes else "")

    @_tool
    @staticmethod
    def changes(limit: int = 20) -> str:
        """Unsaved work: dirty packages, and the edited level's actors added, removed, moved or changed since
        it was loaded or last saved.

        Args:
            limit: Maximum names listed per group.

        Returns:
            A few summary lines.
        """
        if _game_world() is not None:
            raise RuntimeError("PIE is running; stop it first (changes are about the edited level)")
        maps, content = _dirty_packages()
        # World Partition saves actors as their own packages; group those under their level.
        actor_packages = [p for p in content if "/__ExternalActors__/" in p]
        assets = [p for p in content if p not in actor_packages]
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        level = world.get_path_name().split(".")[0] if world else None
        cap = lambda names: ", ".join(names[:limit]) + (f", +{len(names) - limit} more" if len(names) > limit else "")
        lines = []
        total = len(maps) + len(assets) + len(actor_packages)
        lines.append(f"{total} unsaved package{'' if total == 1 else 's'}" + (":" if total else " (nothing to save)"))
        if maps:
            lines.append("  maps: " + cap(maps))
        if assets:
            lines.append("  assets: " + cap(assets))
        if actor_packages:
            lines.append(f"  {len(actor_packages)} World Partition actor package(s)")
        base = _LEVELS.get(level)
        if level and base is not None and (maps or actor_packages):
            now = {a.get_path_name(): a for a in _authored_actors()}
            # A delete and respawn with the same label and class (a generator's rebuild) is one actor rebuilt, not
            # one added and one removed
            gone = {}
            for p in base:
                if p not in now:
                    gone.setdefault(tuple(base[p][:2]), []).append(p)
            added, rebuilt, rebuilt_moved = [], [], []
            for p in now:
                if p in base:
                    continue
                label = _actor_label(now[p])
                old = gone.get((label, now[p].get_class().get_name()))
                if old:
                    rebuilt.append(label)
                    if _pose_key(now[p]) != base[old.pop()][2]:
                        rebuilt_moved.append(label)
                else:
                    added.append(label)
            removed = [base[p][0] for paths in gone.values() for p in paths]
            moved = [_actor_label(now[p]) for p in now if p in base and _pose_key(now[p]) != base[p][2]]
            dirty_actor = {p.rsplit("/", 1)[-1] for p in actor_packages}
            changed = [_actor_label(now[p]) for p in now if p in base and _actor_label(now[p]) not in moved
                       and now[p].get_outermost().get_name().rsplit("/", 1)[-1] in dirty_actor]
            parts = [f"{n} {kind} ({cap(names)})" for kind, names in
                     (("added", added), ("removed", removed), ("moved", moved), ("changed", changed)) for n in [len(names)] if n]
            if rebuilt:
                parts.append(f"{len(rebuilt)} rebuilt (deleted and spawned again with the same label and class: "
                             f"{cap(rebuilt)}; " + (f"{len(rebuilt_moved)} of them elsewhere: {cap(rebuilt_moved)})"
                                                    if rebuilt_moved else "all in place)"))
            lines.append(f"  level {level} since load/save: " + ("; ".join(parts) if parts else
                         "no actors added, removed or moved (other edits: see the map package)"))
        elif level and base is None:
            lines.append(f"  level {level}: no baseline yet (it is taken when a map loads)")
        return "\n".join(lines)

    @_tool
    @staticmethod
    def vcs_status(paths: str = "") -> str:
        """Revision-control state of content files: who has what checked out, and what is behind the depot.

        Args:
            paths: JSON list of /Game folders or packages ('' = all of /Game). A map includes its One File Per
                Actor actor files.

        Returns:
            JSON {provider, files, rows: [{package, label, other, mine, behind, action}]}, rows only for files
            that are checked out (by anyone), behind the depot, or marked for add/delete.
        """
        vcs = _Vcs()
        if not vcs.checkouts:
            return json.dumps({"provider": vcs.provider, "files": 0, "rows": []})
        content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
        files = {}

        def walk(folder):
            for root, _, names in os.walk(folder):
                for name in names:
                    if name.endswith((".uasset", ".umap")):
                        files.setdefault(_norm_path(os.path.join(root, name)), (os.path.join(root, name), None))

        for path in json.loads(paths) if paths else ["/Game"]:
            path = path.rstrip("/").split(".")[0]
            if not path.startswith("/Game"):
                raise RuntimeError(f"{path}: give /Game folders or assets")
            rest = path[len("/Game"):].strip("/")
            folder = os.path.join(content, *rest.split("/")) if rest else content
            if os.path.isdir(folder):
                walk(folder)
                continue
            file = _package_file(path)
            if not os.path.isfile(file):
                raise RuntimeError(f"{path}: no such folder or asset")
            files.setdefault(_norm_path(file), (file, path))
            if file.endswith(".umap"):  # its World Partition / One File Per Actor actors
                walk(os.path.join(content, "__ExternalActors__", *rest.split("/")))
        states, rows = vcs.states([file for file, _ in files.values()]), []
        for norm, state in sorted(states.items()):
            other = getattr(state, "is_checked_out_other", False)
            mine = getattr(state, "is_checked_out", False)
            behind = getattr(state, "is_source_controlled", False) and not getattr(state, "is_current", True)
            action = "add" if getattr(state, "is_added", False) else "delete" if getattr(state, "is_deleted", False) else ""
            if not (other or mine or behind or action):
                continue
            file, package = files.get(norm, (state.filename, None))
            package = package or str(_try(lambda: unreal.PackageTools.filename_to_package_name(file), "")) or file
            rows.append({"package": package, "label": _package_label(package), "other": _holder(state) if other else "",
                         "mine": mine, "behind": behind, "action": action})
        return json.dumps({"provider": vcs.provider, "files": len(files), "rows": rows})

    @_tool
    @staticmethod
    def save(paths: str = "", token: str = "", writable: bool = False) -> str:
        """Save unsaved packages: the given ones, or all of them. Untitled /Temp maps are skipped (saving them
        would open a Save As dialog).

        Args:
            paths: Comma-separated package paths ('' saves every unsaved package).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).
            writable: Make read-only package files writable first (without a check-out provider: git-lfs leaves
                lockable files read-only after a commit).

        Returns:
            What was saved and what was skipped.
        """
        _authorize(token)
        maps, content = _dirty_packages()
        dirty = maps + content
        wanted = [p.strip().split(".")[0] for p in paths.split(",") if p.strip()] or dirty
        missing = [p for p in wanted if p not in dirty]
        temp = [p for p in wanted if p.startswith("/Temp/")]
        todo = [p for p in wanted if p in dirty and p not in temp]
        # Check files out first (Perforce and co.): saving a read-only file would ask in a modal dialog.
        vcs = _Vcs()
        files = {p: _package_file(p, p in maps) for p in todo}
        problems, new_files, notes = vcs.prepare(list(files.values()), writable=writable)
        blocked = todo if vcs.refused_all else \
            [p for p in todo if any(os.path.basename(files[p]) in problem for problem in problems)]
        todo = [p for p in todo if p not in blocked]
        packages = [unreal.find_package(p) for p in todo]
        saved = bool(packages) and unreal.EditorLoadingAndSavingUtils.save_packages([p for p in packages if p], False)
        if saved:
            notes += vcs.added(new_files)
        if saved and any(p in maps for p in todo):
            snapshot_level()
        lines = [f"saved {len(todo)} package{'' if len(todo) == 1 else 's'}" if saved else
                 ("nothing saved" if blocked else "nothing to save" if not todo else "save failed (see ue log)")]
        if todo and saved:
            lines[0] += ": " + ", ".join(todo[:10]) + (f", +{len(todo) - 10} more" if len(todo) > 10 else "")
        if temp:
            lines.append(f"skipped untitled {', '.join(temp)}: save it under /Game with a name first "
                         "(e.g. unreal.EditorLoadingAndSavingUtils.save_map(world, '/Game/Maps/Name'))")
        if missing:
            lines.append("not unsaved (nothing to do): " + ", ".join(missing[:10]))
        if blocked:
            lines.append(f"not saved ({len(blocked)}): " + "; ".join(problems[:10]))
        if notes:
            lines.append(f"{vcs.provider or 'files'}: " + "; ".join(notes[:10]))
        return "\n".join(lines)

    @_tool
    @staticmethod
    def undo(token: str = "") -> str:
        """Undo Tether's most recent `ue set`, but only if nothing else entered the editor's undo history
        since; it never undoes someone else's edit.

        Args:
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was undone.
        """
        _authorize(token)
        if not _UNDO:
            raise RuntimeError("nothing from Tether to undo in this editor session")
        last = _UNDO[-1]
        if last.get("legacy"):  # before 5.8: write the old values back, if nothing changed them since
            obj = unreal.find_object(None, last["target"])
            if obj is None:
                raise RuntimeError(f"{last['label']}: the object is gone")
            pose = {"location": obj.get_actor_location, "rotation": obj.get_actor_rotation,
                    "scale": obj.get_actor_scale3d} if isinstance(obj, unreal.Actor) else {}
            now = {n: (pose[n]() if n in pose else obj.get_editor_property(n)) for n in last["new"]}
            moved = [n for n in last["new"] if not _legacy_same(now[n], last["new"][n])]
            if moved:
                raise RuntimeError(f"{', '.join(moved)} changed after Tether's edit ({last['label']}), so `ue undo` will "
                                   "not touch it; use Edit > Undo in the editor")
            with unreal.ScopedEditorTransaction(f"ue undo {last['label']}"):
                obj.modify()
                if pose and obj.root_component:
                    obj.root_component.modify()
                for n, v in last["old"].items():  # the exact old values, not a text round trip
                    if n == "location":
                        obj.set_actor_location(v, False, True)
                    elif n == "rotation":
                        obj.set_actor_rotation(v, True)
                    elif n == "scale":
                        obj.set_actor_scale3d(v)
                    else:
                        obj.set_editor_property(n, v)
            _UNDO.pop()
            return f"undone: {last['label']}" + (f" ({len(_UNDO)} more Tether edit(s) can be undone)" if _UNDO else "")
        now = unreal.ToolsetLibrary.get_active_undo_count()
        if now != last["count"]:
            raise RuntimeError(f"the undo history changed after Tether's last edit ({last['label']}): another edit or "
                               "undo happened since, so `ue undo` will not touch it; use Edit > Undo in the editor")
        if not unreal.ToolsetLibrary.undo_transaction(True):
            raise RuntimeError("the editor refused the undo")
        _UNDO.pop()
        return f"undone: {last['label']}" + (f" ({len(_UNDO)} more Tether edit(s) can be undone)" if _UNDO else "")

    @_tool
    @staticmethod
    def get_props(target: str, names: str = "", pie: bool = False) -> str:
        """Property values as JSON, for exporting an asset or object (e.g. a DataAsset) to a file.

        Args:
            target: Object or asset path, actor label or name, or 'ActorLabel.ComponentName'.
            names: Comma-separated property names ('' for all).
            pie: Resolve actors in the PIE world.

        Returns:
            JSON object of property name to value.
        """
        if _LEGACY:
            raise RuntimeError("needs UE 5.8+ (Epic's ToolsetLibrary)")
        obj = _resolve(target, pie)
        cls = obj if isinstance(obj, unreal.Struct) else obj.get_class()
        wanted = [n.strip() for n in names.split(",") if n.strip()] or \
            list(json.loads(unreal.ToolsetLibrary.list_struct_properties(cls)))
        return unreal.ToolsetLibrary.get_object_properties(obj, wanted)

    @_tool
    @staticmethod
    def asset_info(path: str) -> str:
        """A few lines about an asset, by type: meshes (triangles per LOD, Nanite, materials, collision, size),
        textures (size, compression, streaming, memory), materials and instances (parameters, parent chain,
        cost), Blueprints (parent, own variables), DataTables, sounds, maps; plus disk size and references.

        Args:
            path: /Game/... (or /Engine/...) asset path.

        Returns:
            A short summary.
        """
        package = path.split(".")[0]
        asset = unreal.load_asset(package)
        if asset is None:
            raise RuntimeError(f"no asset at {package} (ue find)")
        kind = asset.get_class().get_name()
        out = [f"{package} ({kind})"]
        prop = lambda obj, name, default=None: _try(lambda: obj.get_editor_property(name), default)
        short = lambda o: _short_path(o.get_path_name()) if o else "None"
        if isinstance(asset, unreal.StaticMesh):
            lods = asset.get_num_lods()
            tris = [asset.get_num_triangles(i) for i in range(lods)]
            out.append(f"  {lods} LOD(s), triangles " + " / ".join(f"{t:,}" for t in tris)
                       + f", LOD0 {asset.get_num_vertices(0):,} vertices")
            nanite = prop(prop(asset, "nanite_settings"), "enabled", False)
            if nanite:
                out.append(f"  Nanite on: {asset.get_num_nanite_triangles():,} triangles")
            box = asset.get_bounds().box_extent
            out.append(f"  size {box.x * 2:.0f} x {box.y * 2:.0f} x {box.z * 2:.0f} cm")
            subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
            out.append(f"  collision: {subsystem.get_simple_collision_count(asset)} simple shape(s), "
                       f"{subsystem.get_convex_collision_count(asset)} convex, "
                       f"{str(subsystem.get_collision_complexity(asset)).split('.')[-1].split(':')[0]}")
            slots = prop(asset, "static_materials", []) or []
            out.append("  materials: " + ", ".join(f"{s.material_slot_name}={short(s.material_interface)}" for s in slots[:8]))
        elif isinstance(asset, unreal.SkeletalMesh):
            out.append(f"  skeleton {short(prop(asset, 'skeleton'))}, physics {short(prop(asset, 'physics_asset'))}")
            mats = prop(asset, "materials", []) or []
            out.append("  materials: " + ", ".join(f"{m.material_slot_name}={short(m.material_interface)}" for m in mats[:8]))
        elif isinstance(asset, unreal.Texture2D):
            out.append(f"  {_texture_dims(asset)}, "
                       f"{str(prop(asset, 'compression_settings')).split('.')[-1].split(':')[0]}, "
                       f"sRGB {prop(asset, 'srgb')}, LOD group {str(prop(asset, 'lod_group')).split('.')[-1].split(':')[0]}")
            memory = asset.blueprint_get_memory_size()  # 0 when no render resource exists (-nullrhi)
            out.append((f"  memory {memory / 1024 ** 2:.1f} MB, " if memory else "  ")
                       + f"never stream {prop(asset, 'never_stream')}, virtual texture {prop(asset, 'virtual_texture_streaming')}")
        elif isinstance(asset, unreal.MaterialInstance):
            chain, parent = [], prop(asset, "parent")
            while parent is not None and len(chain) < 6:
                chain.append(short(parent))
                parent = prop(parent, "parent") if isinstance(parent, unreal.MaterialInstance) else None
            out.append("  parent chain: " + " <- ".join(chain))
            for label, field, value in (("scalars", "scalar_parameter_values", "parameter_value"),
                                        ("vectors", "vector_parameter_values", "parameter_value"),
                                        ("textures", "texture_parameter_values", "parameter_value")):
                items = prop(asset, field, []) or []
                if items:
                    shown = [f"{p.parameter_info.name}={_compact(_try(lambda: getattr(p, value).to_dict(), None) or getattr(p, value)) if not isinstance(getattr(p, value), unreal.Object) else short(getattr(p, value))}"
                             for p in items[:8]]
                    out.append(f"  overrides {label}: " + ", ".join(shown))
        elif isinstance(asset, unreal.Material):
            out.append(f"  blend {str(prop(asset, 'blend_mode')).split('.')[-1].split(':')[0]}, "
                       f"shading {str(prop(asset, 'shading_model')).split('.')[-1].split(':')[0]}, "
                       f"two-sided {prop(asset, 'two_sided')}")
            lib = unreal.MaterialEditingLibrary
            for label, getter in (("scalar", lib.get_scalar_parameter_names), ("vector", lib.get_vector_parameter_names),
                                  ("texture", lib.get_texture_parameter_names),
                                  ("switch", lib.get_static_switch_parameter_names)):
                names = [str(n) for n in (_try(lambda: getter(asset), []) or [])]
                if names:
                    out.append(f"  {label} parameters: " + ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""))
            stats = {k: v for k, v in (_try(lambda: lib.get_statistics(asset).to_dict(), {}) or {}).items()
                     if isinstance(v, int) and v}  # all zero without compiled shaders (-nullrhi)
            if stats:
                out.append("  cost: " + ", ".join(f"{k.replace('num_', '').replace('_', ' ')} {v}" for k, v in stats.items()
                                                    if isinstance(v, int) and v)[:200])
        elif isinstance(asset, unreal.Blueprint):
            parent = _try(lambda: unreal.BlueprintEditorLibrary.get_blueprint_parent_class(asset))
            generated = _try(lambda: unreal.BlueprintEditorLibrary.generated_class(asset))
            parent_name = parent.get_name() if parent else None
            if not parent_name:  # no get_blueprint_parent_class before 5.8: the registry's ParentClass tag
                data = unreal.AssetRegistryHelpers.get_asset_registry().get_assets_by_package_name(package)
                tag = str(_try(lambda: data[0].get_tag_value("ParentClass"), "") or "")
                parent_name = tag.rstrip("'").rsplit(".", 1)[-1] or "?"
            out.append(f"  parent {parent_name}, class {generated.get_name() if generated else '?'}")
            if generated and parent and not _LEGACY:
                mine = set(_reflect(unreal.ToolsetLibrary.list_struct_properties(generated), asset)) - \
                    set(_reflect(unreal.ToolsetLibrary.list_struct_properties(parent), asset))
                out.append(f"  own variables/components ({len(mine)}): " + ", ".join(sorted(mine)[:15])
                           + (" ..." if len(mine) > 15 else ""))
        elif isinstance(asset, unreal.DataTable):
            names = _try(lambda: unreal.DataTableFunctionLibrary.get_data_table_row_names(asset), []) or []
            out.append(f"  row struct {short(prop(asset, 'row_struct'))}, {len(names)} rows")
        elif isinstance(asset, unreal.SoundWave):
            out.append(f"  {prop(asset, 'duration', 0):.2f} s, {prop(asset, 'num_channels', '?')} channel(s), "
                       f"looping {prop(asset, 'looping')}, compression quality {prop(asset, 'compression_quality')}")
        elif isinstance(asset, unreal.World):
            registry = unreal.AssetRegistryHelpers.get_asset_registry()
            root, rest = package.split("/", 2)[1], package.split("/", 2)[2]
            actors = registry.get_assets_by_path(f"/{root}/__ExternalActors__/{rest}", recursive=True)
            out.append(f"  map; {len(actors)} World Partition actor packages" if actors else "  map (actors stored in the map)")
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        users = {str(n) for n in registry.get_referencers(package, options) or []} - {package}
        deps = {str(n) for n in registry.get_dependencies(package, options) or [] if not str(n).startswith("/Script/")}
        disk = ""
        if package.startswith("/Game/"):
            base = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir()),
                                *package.split("/")[2:])
            for ext in (".uasset", ".umap"):
                if os.path.isfile(base + ext):
                    disk = f"{os.path.getsize(base + ext) / 1024:.0f} KB on disk, "
        out.append(f"  {disk}{len(users)} referencer(s), {len(deps)} asset dependenc{'y' if len(deps) == 1 else 'ies'}")
        return "\n".join(out)

    @_tool
    @staticmethod
    def validate(paths: str = "", dirty: bool = False, deleted: str = "") -> str:
        """Run the editor's data validation (the validators plus each asset's IsDataValid), as the Validate
        Assets menu does, on assets and folders; optionally also on unsaved packages.

        Args:
            paths: Comma-separated package paths or content folders (folders are searched recursively).
                World Partition actor packages are validated through their level.
            dirty: Also validate every unsaved package (untitled /Temp maps excluded).
            deleted: Comma-separated package paths deleted on disk: report what still references them.

        Returns:
            JSON: {"checked": N, "not_validated": N, "problems": [{"path", "result", "errors", "warnings"}],
            "broken_refs": [{"path", "referencers"}]}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        wanted = []
        for ref in [p.strip() for p in paths.split(",") if p.strip()]:
            package = ref.split(".")[0].rstrip("/")
            match = re.match(r"^(/[^/]+)/__ExternalActors__/(.+)/[^/]+/[^/]+/[^/]+$", package)
            if match:  # a World Partition actor: validate its level
                package = f"{match.group(1)}/{match.group(2)}"
            found = registry.get_assets_by_package_name(package)
            if not found:
                found = registry.get_assets_by_path(package, recursive=True)
                if not found and not unreal.EditorAssetLibrary.does_directory_exist(package):
                    raise RuntimeError(f"no asset or folder at {package} (ue find)")
            wanted.extend(found)
        if dirty:
            maps, content = _dirty_packages()
            for package in maps + content:
                if not package.startswith("/Temp/") and "/__ExternalActors__/" not in package:
                    wanted.extend(registry.get_assets_by_package_name(package))
        seen, unique = set(), []
        for data in wanted:
            key = str(data.package_name)
            if key not in seen and not key.startswith("/Script/"):
                seen.add(key)
                unique.append(data)
        subsystem = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
        problems, skipped = [], 0
        for data in unique:
            result, errors, warnings = subsystem.is_asset_valid(data, unreal.DataValidationUsecase.MANUAL)
            errors, warnings = [str(e) for e in errors], [str(w) for w in warnings]
            if result == unreal.DataValidationResult.NOT_VALIDATED and not errors and not warnings:
                skipped += 1
            elif result == unreal.DataValidationResult.INVALID or errors or warnings:
                problems.append({"path": str(data.package_name),
                                 "result": "invalid" if result == unreal.DataValidationResult.INVALID else "warnings",
                                 "errors": errors, "warnings": warnings})
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        broken = []
        for package in [p.strip().split(".")[0] for p in deleted.split(",") if p.strip()]:
            if registry.get_assets_by_package_name(package):
                continue  # still there (the registry has not caught up, or it was restored)
            users = sorted({str(n) for n in registry.get_referencers(package, options) or []} - {package})
            if users:
                broken.append({"path": package, "referencers": users[:20]})
        return json.dumps({"checked": len(unique), "not_validated": skipped, "problems": problems,
                           "broken_refs": broken})

    @_tool
    @staticmethod
    def diff(old: str, new: str, limit: int = 200) -> str:
        """What changed between two versions of an asset, e.g. an earlier revision copied under Saved/ (which
        loads as /Temp/...) and the current one. Blueprints: class defaults and components (graphs and
        variables come from Epic's Blueprint tools); DataTables: rows; maps: actors added, removed and changed;
        anything else: its reflected properties.

        Args:
            old: Object or package path of the earlier version.
            new: Object or package path of the later version.
            limit: Maximum number of lines.

        Returns:
            One line per difference ('name: old -> new'), grouped by part.
        """
        if _LEGACY:
            raise RuntimeError("needs UE 5.8+ (Epic's ToolsetLibrary)")
        a, b = _resolve(old), _resolve(new)
        roots = (a.get_outermost().get_name(), b.get_outermost().get_name())
        out = []
        if a.get_class() != b.get_class():
            out.append(f"class: {a.get_class().get_name()} -> {b.get_class().get_name()}")
        if isinstance(a, unreal.Blueprint) and isinstance(b, unreal.Blueprint):
            _diff_parts(_bp_parts(a), _bp_parts(b), roots, out)
        elif isinstance(a, unreal.DataTable) and isinstance(b, unreal.DataTable):
            ra, rb = _table_rows(a), _table_rows(b)
            out += [f"+ row {n}" for n in rb if n not in ra] + [f"- row {n}" for n in ra if n not in rb]
            for name in [n for n in rb if n in ra]:
                lines = []
                for col in list(ra[name]) + [c for c in rb[name] if c not in ra[name]]:
                    _diff_value(col, _rebase(ra[name].get(col, _MISSING), *roots), rb[name].get(col, _MISSING), lines)
                if lines:
                    out.append(f"~ row {name}: " + "; ".join(lines))
        elif isinstance(a, unreal.World) and isinstance(b, unreal.World):
            (old_actors, _), (new_actors, saved) = _level_actors(a), _level_actors(b)

            def where(actor):
                root = actor.root_component
                loc = root.get_editor_property("relative_location") if root else None  # unregistered: no world transform
                return f" at ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})" if loc else ""

            for name in [n for n in new_actors if n not in old_actors and (saved is None or n in saved)]:
                actor = new_actors[name]
                out.append(f"+ {_actor_label(actor)} ({actor.get_class().get_name()}){where(actor)}")
            for name in [n for n in old_actors if n not in new_actors]:
                actor = old_actors[name]
                out.append(f"- {_actor_label(actor)} ({actor.get_class().get_name()})")
            for name in [n for n in new_actors if n in old_actors]:
                lines = []
                _diff_parts(_actor_parts(old_actors[name]), _actor_parts(new_actors[name]), roots, lines)
                if lines:
                    out.append(f"~ {_actor_label(new_actors[name])} ({new_actors[name].get_class().get_name()})")
                    out.extend("  " + line for line in lines)
        else:
            _diff_props("properties", a, b, roots, out, limit)
        if not out:
            return ("no actors added, removed or changed in the map package itself" if isinstance(b, unreal.World)
                    else "no differences in reflected properties")
        return "\n".join(out[:limit]) + (f"\n... +{len(out) - limit} more lines" if len(out) > limit else "")

    @_tool
    @staticmethod
    def mat_unused_check(path: str = "/Game", min_unused: int = 5) -> str:
        """Materials and material functions with nodes that reach no output (left behind by edits or by a
        generator that rebuilds the graph): they cost nothing at runtime but make the graph hard to read.

        Args:
            path: Materials under this content folder.
            min_unused: Report only those with at least this many unused nodes.

        Returns:
            JSON {checked, materials: [[path, nodes, unused, [classes]]]}, most unused first.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        rows, checked = [], 0
        for cls in ("Material", "MaterialFunction"):
            for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", cls), False):
                package = str(data.package_name)
                if not package.startswith(path.rstrip("/") + "/") and package != path:
                    continue
                asset = unreal.load_asset(package)
                if not isinstance(asset, (unreal.Material, unreal.MaterialFunction)):
                    continue
                checked += 1
                nodes, unused = _try(lambda: _mat_unused(asset), ([], []))
                if len(unused) >= min_unused:
                    kinds = sorted({e.get_class().get_name().replace("MaterialExpression", "") for e in unused})
                    rows.append([package, len(nodes), len(unused), kinds[:8]])
        rows.sort(key=lambda r: -r[2])
        return json.dumps({"checked": checked, "materials": rows})

    @_tool
    @staticmethod
    def mpc_check(path: str = "/Game", fix: bool = False, token: str = "") -> str:
        """Collection Parameter nodes whose parameter their Material Parameter Collection no longer has. A node
        finds its parameter by ID; rewriting the collection's parameter arrays gives every parameter a new one,
        and those nodes then read 0 (no warning in the editor session). With fix, set each stale node's
        parameter name again (which takes the collection's current ID for that name) and recompile.

        Args:
            path: Collections under this content folder.
            fix: Re-link stale nodes by name and recompile their materials.
            token: Session token from Saved/Tether/editors/<pid>.token (needed with fix).

        Returns:
            JSON {collections, materials, stale: [{material, parameter, collection, why}], fixed}.
        """
        if fix:
            _authorize(token)
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        lib = unreal.MaterialEditingLibrary
        hard = unreal.AssetRegistryDependencyOptions(include_soft_package_references=False,
                                                     include_hard_package_references=True,
                                                     include_searchable_names=False, include_soft_management_references=False,
                                                     include_hard_management_references=False)
        found, stale, fixed, checked = 0, [], 0, set()
        for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "MaterialParameterCollection"), True):
            if not str(data.package_name).startswith(path.rstrip("/")):
                continue
            mpc = unreal.load_asset(str(data.package_name))
            if mpc is None:
                continue
            found += 1
            ids = {}
            for param in list(mpc.get_editor_property("scalar_parameters")) + list(mpc.get_editor_property("vector_parameters")):
                guid = re.search(r"Id=([0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}|"
                                 r"[0-9A-Fa-f]{32})", param.export_text())
                ids[re.sub(r"[^0-9A-F]", "", guid.group(1).upper()) if guid else ""] = \
                    str(param.get_editor_property("parameter_name"))
            names = set(ids.values())
            for user in registry.get_referencers(data.package_name, hard) or []:
                asset = unreal.load_asset(str(user))
                if not isinstance(asset, (unreal.Material, unreal.MaterialFunction)):
                    continue
                checked.add(str(user))
                getter = lib.get_material_function_expressions if isinstance(asset, unreal.MaterialFunction) \
                    else lib.get_material_expressions
                broken = False
                for expr in getter(asset) or []:
                    if not isinstance(expr, unreal.MaterialExpressionCollectionParameter) or \
                            expr.get_editor_property("collection") != mpc:
                        continue
                    name = str(expr.get_editor_property("parameter_name"))
                    own = next((v for _, n, v in _try(lambda: _dump_props(expr), []) or [] if n == "ParameterId"), "")
                    own = re.sub(r"[^0-9A-F]", "", str(own).upper())
                    if name not in names:
                        why = f"no parameter '{name}' in the collection (renamed or removed)"
                    elif own and own not in ids:
                        why = "its parameter ID is not the collection's any more (the parameter arrays were rewritten)"
                    else:
                        continue
                    stale.append({"material": str(user), "parameter": name, "collection": str(data.package_name),
                                  "why": why})
                    if fix and name in names:
                        # the same name again, with the change event forced: PostEditChange takes the current ID
                        expr.set_editor_property("parameter_name", unreal.Name(name),
                                                 unreal.PropertyAccessChangeNotifyMode.ALWAYS)
                        broken = True
                if broken:
                    if isinstance(asset, unreal.Material):
                        lib.recompile_material(asset)
                    else:
                        lib.update_material_function(asset)
                    fixed += 1  # left unsaved, as lint --fix does without --save
        return json.dumps({"collections": found, "materials": len(checked), "stale": stale, "fixed": fixed})

    @_tool
    @staticmethod
    def material(target: str, compile: bool = False, limit: int = 80) -> str:
        """A material's or material instance's parameters with their values and where each value comes from,
        or, for an actor, the materials on each of its components' slots.

        Args:
            target: Material or material instance path, or an actor or 'Actor.Component'.
            compile: Recompile the (base) material and list its compile errors.
            limit: Maximum number of parameter lines.

        Returns:
            A header (class, parent chain), one line per parameter, then compile errors if asked.
        """
        lib = unreal.MaterialEditingLibrary
        obj = _resolve(target)
        if isinstance(obj, (unreal.Actor, unreal.PrimitiveComponent)):
            rows = []
            comps = obj.get_components_by_class(unreal.PrimitiveComponent) if isinstance(obj, unreal.Actor) else [obj]
            for comp in comps:
                slots = [str(n) for n in (_try(lambda: comp.get_material_slot_names(), []) or [])]
                for i in range(_try(lambda: comp.get_num_materials(), 0) or 0):
                    mat = comp.get_material(i)
                    slot = slots[i] if i < len(slots) else str(i)
                    text = _short_path(mat.get_path_name()) if mat else "None"
                    if isinstance(mat, unreal.MaterialInstanceDynamic):
                        text = f"dynamic instance of {_short_path(mat.get_editor_property('parent').get_path_name())}"
                    rows.append(f"{comp.get_name()}[{slot}]: {text}")
            return _capped(f"{_label(obj)}: {len(rows)} material slot(s)", rows, limit)
        material = _material_of(target)
        chain, parent = [], _try(lambda: material.get_editor_property("parent"))
        while parent is not None and len(chain) < 8:
            chain.append(_short_path(parent.get_path_name()))
            parent = _try(lambda: parent.get_editor_property("parent")) if isinstance(parent, unreal.MaterialInstance) else None
        head = f"{_short_path(material.get_path_name())} ({material.get_class().get_name()})"
        if chain:
            head += " <- " + " <- ".join(chain)
        is_instance = isinstance(material, unreal.MaterialInstance)
        parent = _try(lambda: material.get_editor_property("parent")) if is_instance else None
        rows = []
        for kind, names in _mat_names(material).items():
            for name in names:
                value = _try(lambda: _mat_value(material, kind, name))
                note = ""
                if is_instance:
                    if _try(lambda: lib.is_material_instance_parameter_overridden(material, name), False):
                        inherited = _try(lambda: _mat_value(parent, kind, name)) if parent else None
                        note = "  (override" + (f"; parent {_mat_text(inherited)}" if parent else "") + ")"
                else:
                    getter = getattr(lib, f"get_{'static_switch' if kind == 'switch' else kind}_parameter_source", None)
                    source = _try(lambda: str(getter(material, name).export_text())) if getter else None
                    if source and source not in ("", "None") and material.get_path_name() not in source:
                        note = f"  (from {_short_path(source)})"
                rows.append(f"{kind:<7} {name} = {_mat_text(value)}{note}")
        out = _capped(head + f"\n{len(rows)} parameter(s)", rows, limit)
        if compile:
            base = material
            while isinstance(base, unreal.MaterialInstance) and _try(lambda: base.get_editor_property("parent")):
                base = base.get_editor_property("parent")
            # Recompiling marks the material modified; compile a copy of a saved one so it stays clean.
            target = _clean_copy(base) if isinstance(base, unreal.Material) else base
            errors = [str(e) for e in lib.recompile_material(target)] if isinstance(target, unreal.Material) else []
            out += f"\ncompiled {_short_path(base.get_path_name())}: " + \
                (f"{len(errors)} error(s)\n" + "\n".join("  " + e for e in errors) if errors else "no errors")
        return out

    @_tool
    @staticmethod
    def set_material(target: str, values: str, reset: str = "", token: str = "") -> str:
        """Set material instance parameters (or a material's parameter defaults), as one undoable transaction.

        Args:
            target: Material instance (or material) path.
            values: JSON object of parameter name to value text: numbers, 'r,g,b[,a]', true/false, texture paths.
            reset: Comma-separated parameter names to stop overriding (instances only).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One 'name: old -> new' line per parameter.
        """
        _authorize(token)
        lib = unreal.MaterialEditingLibrary
        material = _material_of(target)
        is_instance = isinstance(material, unreal.MaterialInstance)
        names = _mat_names(material)
        kind_of = {n.lower(): (k, n) for k, ns in names.items() for n in ns}
        requested = json.loads(values) if values else {}
        resets = [r.strip() for r in reset.split(",") if r.strip()]
        plan = []
        for key, raw in list(requested.items()) + [(r, None) for r in resets]:
            if key.lower() not in kind_of:
                import difflib
                close = difflib.get_close_matches(key.lower(), list(kind_of), n=4, cutoff=0.5)
                raise RuntimeError(f"{_label(material)} has no parameter '{key}'"
                                   + (f" (did you mean: {', '.join(kind_of[c][1] for c in close)})" if close else ""))
            kind, name = kind_of[key.lower()]
            try:
                plan.append((kind, name, None if raw is None else _mat_parse(kind, str(raw))))
            except ValueError as exc:
                raise RuntimeError(f"{name}: {exc}") from None
        if resets and not is_instance:
            raise RuntimeError("--reset is for material instances (a material's parameters are its defaults)")
        old = {name: _mat_value(material, kind, name) for kind, name, _ in plan}
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        with unreal.ScopedEditorTransaction(f"ue mat {_label(material)}"):
            material.modify()
            for kind, name, value in plan:
                if value is None and name in resets:
                    lib.set_material_instance_parameter_override(material, name, False)
                elif is_instance:
                    setter = {"scalar": lib.set_material_instance_scalar_parameter_value,
                              "vector": lib.set_material_instance_vector_parameter_value,
                              "texture": lib.set_material_instance_texture_parameter_value,
                              "switch": lib.set_material_instance_static_switch_parameter_value}[kind]
                    setter(material, name, value)
                else:  # a base material: change the parameter node's default value
                    expressions = [e for e in lib.get_material_expressions(material)
                                   if str(_try(lambda: e.get_editor_property("parameter_name"), "")) == name]
                    if not expressions:
                        raise RuntimeError(f"{name} is defined in a material function; edit it there")
                    for expression in expressions:
                        expression.modify()
                        expression.set_editor_property("default_value", value)
            if is_instance:
                lib.update_material_instance(material)
        if not is_instance:
            lib.recompile_material(material)
        _record_undo(before, f"mat {_label(material)}: " + ", ".join(n for _, n, _ in plan))
        rows = [f"{name}: {_mat_text(old[name])} -> {_mat_text(_mat_value(material, kind, name))}"
                + (" (parent value)" if name in resets else "") for kind, name, _ in plan]
        if not is_instance:  # a debug toggle set here once stayed on in a saved asset through a full rebuild
            back = " ".join(f'"{name}={_mat_text(old[name])}"' if " " in f"{name}{_mat_text(old[name])}"
                            else f"{name}={_mat_text(old[name])}" for _, name, _ in plan)
            rows.append("note: these are the base material's own defaults, saved with it and seen by every instance "
                        "that does not override them; a generator that rebuilds the material can keep them. Put them "
                        f"back before saving: ue mat {material.get_path_name().split('.')[0]} {back}")
        return "\n".join([_label(material)] + rows)

    @_tool
    @staticmethod
    def pie_step(step: str, token: str = "") -> str:
        """One step of a scripted play test in the running PIE session (the ue CLI's `pie do` runs a list).

        Steps: 'at <x,y,z|actor> [dx,dy,dz]' teleports the player; 'look <x,y,z|actor>' aims the camera;
        'move <x,y,z|actor>' starts walking there on the navmesh; 'near <x,y,z|actor> [radius]' checks
        distance; 'tap <action>' presses an Enhanced Input action for one frame; 'hold <action> [x[,y]]' keeps
        it pressed (or a 2D value, e.g. 'hold IA_Move 0,1') until 'release <action>'; 'call <target> <Fn>
        [json args]' calls a function; 'expect <target>.<prop> <op> <value>' checks a property (op = != < >
        <= >= ~regex); 'exists <actor>' / 'gone <actor>'; 'exec <console command>'. Targets: 'player',
        'controller', 'player.<Component>', or an actor label/name. Actions: an InputAction asset name or path,
        or a property/subobject of the pawn holding one.

        Args:
            step: The step text.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {"ok": bool, "detail": str}.
        """
        _authorize(token)
        verb, _, rest = step.strip().partition(" ")
        verb, rest = verb.lower(), rest.strip()
        game, pawn, controller = _pie_player()
        ok, detail = True, ""
        if verb == "at":
            where, _, offset = rest.partition(" ")
            base = _pie_point(where, pawn, controller)
            point = base + unreal.Vector(*(_numbers(offset) or [0, 0, 0])) if offset else base
            pawn.set_actor_location(point, False, True)
            loc = pawn.get_actor_location()
            detail = f"player at ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})" + (
                "" if _numbers(where) is not None else f"; {where} is at ({base.x:.0f}, {base.y:.0f}, {base.z:.0f})")
        elif verb == "look":
            camera = unreal.GameplayStatics.get_player_camera_manager(game, 0)
            eye = camera.get_camera_location() if camera else pawn.get_actor_location()
            target = _pie_point(rest, pawn, controller)
            rotation = unreal.MathLibrary.find_look_at_rotation(eye, target)
            controller.set_control_rotation(rotation)
            detail = (f"looking pitch {rotation.pitch:.0f} yaw {rotation.yaw:.0f} at ({target.x:.0f}, {target.y:.0f}, "
                      f"{target.z:.0f})")
        elif verb == "move":
            goal = _pie_point(rest, pawn, controller)
            path = unreal.NavigationSystemV1.find_path_to_location_synchronously(game, pawn.get_actor_location(), goal)
            if path is None or not path.is_valid() or not path.path_points:
                raise RuntimeError("no navmesh path from the player to there (ue nav --pie says why)")
            unreal.AIHelperLibrary.simple_move_to_location(controller, goal)
            detail = f"moving along {path.get_path_length():.0f} cm" + (" (partial path)" if path.is_partial() else "")
        elif verb == "near":
            where, _, radius = rest.partition(" ")
            distance = pawn.get_actor_location().distance(_pie_point(where, pawn, controller))
            ok = distance <= float(radius or 150)
            detail = f"{distance:.0f} cm away"
        elif verb in ("tap", "hold", "release"):
            name, _, value = rest.partition(" ")
            action = _input_action(name, pawn)
            subsystem = _input_subsystem(game)
            nums = _numbers(value) if value else None
            vector = unreal.Vector(*(list(nums or [1.0]) + [0.0, 0.0])[:3])
            if verb == "tap":
                subsystem.inject_input_vector_for_action(action, vector, [], [])
            elif verb == "hold":
                _hold(action, vector)
            else:
                _release(action)
            detail = f"{verb} {action.get_name()}"
            if verb != "release" and not _try(lambda: subsystem.query_keys_mapped_to_action(action), [1]):
                detail += " (warning: no active mapping context maps it, so a player could not press it; ue input)"
        elif verb == "call":
            parts = rest.split(None, 2)
            if len(parts) < 2:
                raise RuntimeError("call <target> <Function> [json args]")
            obj = _pie_object(parts[0], pawn, controller)
            args = json.loads(parts[2]) if len(parts) > 2 else []
            args = tuple(args) if isinstance(args, list) else (args,)
            args = _call_args(obj, parts[1], args)
            try:
                result = obj.call_method(parts[1], args)
            except TypeError as exc:  # a library function's WorldContext parameter: pass the PIE world
                if "world_context" not in str(exc).lower():
                    raise
                result = obj.call_method(parts[1], (game,) + args)
            if isinstance(result, unreal.StructBase):
                result = _compact(_try(lambda: result.to_dict(), str(result)))
            elif isinstance(result, unreal.Object):
                result = _label(result)
            detail = f"{_label(obj)}.{parts[1]}() -> {_short(result) if result is not None else 'done'}"
        elif verb == "expect":
            ok, actual = _pie_expect(rest, pawn, controller)
            detail = f"is {actual}"
        elif verb in ("exists", "gone"):
            found = _match_actors(rest, pie=True)
            ok = bool(found) == (verb == "exists")
            detail = f"{len(found)} match(es)"
        elif verb in ("key", "mouse", "stick"):
            detail = _raw_input(game, verb, rest)
        elif verb == "exec":
            if _networked(game):  # RPCs would run locally from Python (see _deferred_exec_class)
                _queue_command(game, rest)
                detail = "queued for the next tick"
            else:
                unreal.SystemLibrary.execute_console_command(game, rest)
                detail = "ran"
        else:
            raise RuntimeError(f"unknown step '{verb}' (at, look, move, near, tap, hold, release, key, mouse, stick, call, "
                               "expect, exists, gone, exec; the CLI adds wait, until, press, log, shot)")
        return json.dumps({"ok": ok, "detail": detail})

    @_tool
    @staticmethod
    def nav(action: str = "status", start: str = "", end: str = "", match: str = "", pie: bool = False,
            limit: int = 40, agent: str = "") -> str:
        """Navigation: whether the level has a usable navmesh (and why not), paths between points, and which
        actors stand off the navmesh.

        Args:
            action: status, path (start -> end) or check (actors matching `match`).
            start: Path start: 'x,y,z' or an actor.
            end: Path end: 'x,y,z' or an actor.
            match: With check: regex on actor label and class (default: player starts, spawns, patrol points).
            pie: Use the PIE world (runtime-generated navmesh) instead of the editor level.
            limit: Maximum number of actor lines for check.
            agent: With path: an actor (a pawn) whose navigation agent the path is for (default: the default agent).

        Returns:
            A few lines.
        """
        world = _game_world() if pie else unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if world is None:
            raise RuntimeError("PIE is not running" if pie else "no editor world")
        navsys = unreal.NavigationSystemV1
        actors = unreal.GameplayStatics.get_all_actors_of_class
        if action == "status":
            out = [f"{world.get_path_name().split('.')[0]}" + (" (PIE)" if pie else "")]
            settings = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.WorldSettings)
            enabled = _try(lambda: settings[0].get_editor_property("enable_navigation_system"), True) if settings else True
            if not enabled:
                out.append("  navigation is OFF in World Settings (Enable Navigation System): nothing can path here")
            data = [d for d in actors(world, unreal.NavigationData) if not isinstance(d, unreal.AbstractNavData)]
            for nav_data in data:
                prop = lambda n, d=None: _try(lambda: nav_data.get_editor_property(n), d)
                generation = str(prop("runtime_generation", "?")).split(".")[-1].split(":")[0].title()
                agent = f", agent radius {prop('agent_radius', 0):g} height {prop('agent_height', 0):g}" \
                    if isinstance(nav_data, unreal.RecastNavMesh) else ""
                out.append(f"  nav data {_actor_label(nav_data)} ({nav_data.get_class().get_name()}, "
                           f"runtime generation {generation}{agent})")
            if not data:
                out.append("  no navigation data actor (a NavMeshBoundsVolume creates RecastNavMesh-Default)")
            volumes = list(actors(world, unreal.NavMeshBoundsVolume))
            empty = 0
            for volume in volumes:
                centre, extent = volume.get_actor_bounds(False)
                found = navsys.get_random_point_in_navigable_radius(world, centre, max(extent.x, extent.y))
                empty += found is None
                out.append(f"  bounds {_actor_label(volume)}: {extent.x * 2 / 100:.0f} x {extent.y * 2 / 100:.0f} x "
                           f"{extent.z * 2 / 100:.0f} m, " + ("navmesh present" if found is not None else "NO navmesh inside"))
            if not volumes:
                out.append("  no NavMeshBoundsVolume: no navmesh is generated")
            if navsys.is_navigation_being_built(world):
                out.append("  navigation is being built right now; results may be incomplete")
            elif volumes and empty and enabled:
                out.append("  hint: build it (Build > Build Paths), or check the volume covers the floor; with runtime "
                           "generation Dynamic it is built in PIE (ue nav --pie)")
            return "\n".join(out)
        if action == "path":
            if not start or not end:
                raise RuntimeError("path needs a start and an end (x,y,z or actor)")
            a, b = _point(start, pie), _point(end, pie)
            straight = a.distance(b)
            context = None
            if agent:
                context = _resolve(agent, pie)
                if not isinstance(context, unreal.Actor):
                    raise RuntimeError(f"agent '{agent}' is not an actor")
            path = navsys.find_path_to_location_synchronously(world, a, b, context)
            if path is None or not path.is_valid() or not path.path_points:
                notes = []
                for label, point in (("start", a), ("end", b)):
                    if navsys.project_point_to_navigation(world, point, None, None, unreal.Vector(50, 50, 250)) is None:
                        near = navsys.project_point_to_navigation(world, point, None, None, unreal.Vector(1000, 1000, 1000))
                        notes.append(f"{label} is off the navmesh" +
                                     (f" ({point.distance(near):.0f} cm from it)" if near is not None else " (none within 10 m)"))
                return f"no path ({straight:.0f} cm straight line)" + (": " + "; ".join(notes) if notes else "")
            length = path.get_path_length()
            points = list(path.path_points)
            top = max(points, key=lambda p: p.z)
            last = points[-1]
            # A partial path stops where the navmesh stops connecting (a step too high, a gap): say where.
            return (f"{'partial path' if path.is_partial() else 'path'}: {length:.0f} cm over {len(points)} "
                    f"points (straight line {straight:.0f} cm), highest point z {top.z:.0f}" +
                    (f"; it stops at ({last.x:.0f}, {last.y:.0f}, {last.z:.0f}), {last.distance(b):.0f} cm short "
                     f"({b.z - last.z:+.0f} cm in height)" if path.is_partial() else ""))
        if action == "check":
            rx = re.compile(match or r"PlayerStart|Spawn|Patrol|Waypoint|TargetPoint", re.I)
            rows, off = [], 0
            for actor in actors(world, unreal.Actor):
                label = _actor_label(actor)
                if not (rx.search(label) or rx.search(actor.get_class().get_name())):
                    continue
                loc = actor.get_actor_location()
                if navsys.project_point_to_navigation(world, loc, None, None, unreal.Vector(50, 50, 250)) is not None:
                    rows.append(f"ok   {label}")
                    continue
                off += 1
                near = navsys.project_point_to_navigation(world, loc, None, None, unreal.Vector(1000, 1000, 1000))
                rows.append(f"OFF  {label} at ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})" +
                            (f": navmesh {loc.distance(near):.0f} cm away" if near is not None else ": no navmesh within 10 m"))
            rows.sort(key=lambda r: not r.startswith("OFF"))
            return _capped(f"{len(rows)} actors checked, {off} off the navmesh", rows, limit)
        raise RuntimeError("action must be status, path or check")

    @_tool
    @staticmethod
    def package_graph(packages: str, deps: bool = False) -> str:
        """Asset classes of packages and, with deps, everything they load with them: hard package
        dependencies, recursively (for a World Partition map, its actor packages too).

        Args:
            packages: Comma-separated package paths.
            deps: Also return the recursive hard dependencies.

        Returns:
            JSON {"classes": {package: class}, "deps": [package, ...]}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        roots = [p.strip().split(".")[0] for p in packages.split(",") if p.strip()]
        classes = {}
        for package in roots:
            found = registry.get_assets_by_package_name(package)
            classes[package] = str(found[0].asset_class_path.asset_name) if found else ""
        if not deps:
            return json.dumps({"classes": classes, "deps": []})
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=False, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        queue = list(roots)
        for package in roots:
            if classes.get(package) == "World":  # World Partition actors live in their own packages
                root, rest = package.split("/", 2)[1], package.split("/", 2)[2]
                queue += [str(a.package_name) for a in
                          registry.get_assets_by_path(f"/{root}/__ExternalActors__/{rest}", recursive=True)]
        seen = set(queue)
        while queue:
            for dep in registry.get_dependencies(queue.pop(), options) or []:
                name = str(dep)
                if name not in seen and not name.startswith("/Script/"):
                    seen.add(name)
                    queue.append(name)
        found = sorted(seen - set(roots))
        for package in found:
            data = registry.get_assets_by_package_name(package)
            classes[package] = str(data[0].asset_class_path.asset_name) if data else ""
        return json.dumps({"classes": classes, "deps": found})

    @_tool
    @staticmethod
    def config(section: str = "", match: str = "", limit: int = 80) -> str:
        """Project and editor settings (the Settings windows), read through the editor's settings registry: the
        list of sections, or one section's properties with values.

        Args:
            section: Section name ('Maps', 'Rendering', 'Engine/General') or Container/Category/Section; '' lists all.
            match: Case-insensitive regex on property names.
            limit: Maximum number of lines.

        Returns:
            Sections grouped by Container/Category, or 'name: type = value' lines.
        """
        if not section:
            groups = {}
            for container, category, name in _all_sections():
                groups.setdefault(f"{container}/{category}", []).append(name)
            return "\n".join(f"{key}: {', '.join(names)}" for key, names in sorted(groups.items()))
        container, category, name = _find_section(section)
        schema = _settings("GetSectionSchema", container, category, name) or {}
        names = list(schema)
        if match:
            rx = re.compile(match, re.I)
            names = [n for n in names if rx.search(n)]
        values = _settings("GetSectionPropertyValues", container, category, name, names) if names else {}
        values = values if isinstance(values, dict) else {}
        rows = [f"{n}: {_type_name(schema[n])} = {_short(_round(values.get(n)))}" for n in names]
        head = f"{container}/{category}/{name} ({len(schema)} settings" + (f", {len(names)} match" if match else "") + ")"
        return _capped(head, rows, limit)

    @_tool
    @staticmethod
    def set_config(section: str, values: str, token: str = "") -> str:
        """Change project or editor settings and save them where the Settings window would (not undoable).

        Args:
            section: Section name or Container/Category/Section (see `config`).
            values: JSON object of setting name to value text (numbers, true/false, enum names, paths, JSON).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One 'name: old -> new' line per setting.
        """
        _authorize(token)
        container, category, name = _find_section(section)
        schema = _settings("GetSectionSchema", container, category, name) or {}
        by_lower = {n.lower(): n for n in schema}
        requested = json.loads(values)
        plan = {}
        for key, raw in requested.items():
            prop = by_lower.get(key.lower())
            if prop is None:
                import difflib
                close = difflib.get_close_matches(key.lower(), list(by_lower), n=4, cutoff=0.6)
                raise RuntimeError(f"{name} has no setting '{key}'"
                                   + (f" (did you mean: {', '.join(by_lower[c] for c in close)})" if close else ""))
            plan[prop] = raw
        old = _settings("GetSectionPropertyValues", container, category, name, list(plan)) or {}
        new = {}
        for prop, raw in plan.items():
            try:
                new[prop] = _parse(raw if isinstance(raw, str) else json.dumps(raw), schema[prop], old.get(prop))
            except ValueError as exc:
                raise RuntimeError(f"{prop}: {exc}") from None
        _settings("SetSectionProperties", container, category, name, json.dumps(new))
        after = _settings("GetSectionPropertyValues", container, category, name, list(plan)) or {}
        ignored = [p for p in new if not _took(new[p], after.get(p))]
        if ignored:
            raise RuntimeError("did not take: " + ", ".join(f"{p} stayed {_compact(after.get(p))}" for p in ignored))
        rows = [f"{p}: {_compact(old.get(p))} -> {_compact(after.get(p))}" for p in plan]
        return "\n".join([f"{container}/{category}/{name} (saved)"] + rows)

    @_tool
    @staticmethod
    def cvars(match: str = "", changed: bool = False, limit: int = 60) -> str:
        """Console variables with their value, who set it last (Constructor, Scalability, ProjectSetting,
        SystemSettingsIni, DeviceProfile, Commandline, Code, Console, ...) and the built-in default.

        Args:
            match: Case-insensitive regex on the name ('' = all); a plain prefix like 'r.Shadow.' is fastest.
            changed: Only variables whose value differs from the built-in (constructor) default.
            limit: Maximum rows to return in detail (with history).

        Returns:
            JSON: {total, matched, rows: [{name, value, setby, default, history}]}.
        """
        prefix = match if re.fullmatch(r"[\w.]+", match or "") else ""
        rows = _cvar_dump(prefix)
        total = len(rows)
        if match and not prefix:
            rx = re.compile(match, re.I)
            rows = [r for r in rows if rx.search(r["name"])]
        elif prefix:
            rows = [r for r in rows if r["name"].lower().startswith(prefix.lower())]
        if changed:
            rows = [r for r in rows if r["setby"] != "Constructor"]
            history = _cvar_histories([r["name"] for r in rows[:3000]])
            for r in rows:
                r["history"] = history.get(r["name"], {})
                r["default"] = r["history"].get("Constructor")
            rows = [r for r in rows if r["default"] is None or not _cvar_same(r["default"], r["value"])]
        else:
            history = _cvar_histories([r["name"] for r in rows[:limit] if r["setby"] != "Constructor"])
            for r in rows[:limit]:
                r["history"] = history.get(r["name"], {})
                r["default"] = r["history"].get("Constructor", r["value"] if r["setby"] == "Constructor" else None)
        for r in rows[:limit]:
            r["same_as_default"] = r.get("default") is not None and _cvar_same(r["default"], r["value"])
        return json.dumps({"total": total, "matched": len(rows), "rows": rows[:limit]})

    @_tool
    @staticmethod
    def set_cvars(values: str = "{}", reset: str = "[]", token: str = "") -> str:
        """Set console variables (as the console does: highest priority, not saved), or reset ones set that way
        back to what the next priority level says (UnsetCVar -setby=Console).

        Args:
            values: JSON object of name to value text.
            reset: JSON list of names to reset.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON: [{name, before, after, setby_before, setby}].
        """
        _authorize(token)
        values, reset = json.loads(values), json.loads(reset)
        names = list(values) + [n for n in reset if n not in values]
        known = {r["name"].lower(): r for r in _cvar_dump()}
        missing = [n for n in names if n.lower() not in known]
        if missing:
            import difflib
            close = difflib.get_close_matches(missing[0].lower(), list(known), n=3, cutoff=0.7)
            raise RuntimeError(f"no console variable '{missing[0]}'"
                               + (f" (did you mean: {', '.join(known[c]['name'] for c in close)})" if close else ""))
        before = {n: known[n.lower()] for n in names}
        commands = [f"{before[n]['name']} {v}" for n, v in values.items()]
        commands += [f"UnsetCVar {before[n]['name']} -setby=Console" for n in reset if n not in values]
        for command in commands:
            unreal.SystemLibrary.execute_console_command(None, command)
        after = {r["name"].lower(): r for r in _cvar_dump()}
        return json.dumps([{"name": before[n]["name"], "before": before[n]["value"], "setby_before": before[n]["setby"],
                            "after": after[n.lower()]["value"], "setby": after[n.lower()]["setby"]} for n in names])

    @_tool
    @staticmethod
    def umg(click: str = "", token: str = "") -> str:
        """UMG widgets on the PIE viewport as a compact tree: texts, buttons (by their label), text boxes and
        check boxes. The fallback for `ue ui` when Epic's SlateInspectorToolset is not enabled: raw Slate
        widgets are invisible from Python. With click, fire the OnClicked of the button whose label or name
        matches (case-insensitive).

        Args:
            click: Button label or widget name to click ('' lists).
            token: Session token from Saved/Tether/editors/<pid>.token (needed only with click).

        Returns:
            One line per widget, indented by nesting.
        """
        game = _game_world()
        if game is None:
            raise RuntimeError("PIE is not running")
        tops = [u for u in unreal.WidgetLibrary.get_all_widgets_of_class(game, unreal.UserWidget, True)
                if u.is_in_viewport() and u.is_visible()]
        lib = unreal.UserWidgetFunctionLibrary
        children, owners = {}, {}
        everything = [(w, _try(lambda w=w: lib.get_outer_user_widget(w))) for w in unreal.ObjectIterator(unreal.Widget)]
        shown, grew = set(tops), True  # the user widgets on screen: the tops and those nested in them (menu rows)
        while grew:
            grew = False
            for widget, owner in everything:
                if owner in shown and isinstance(widget, unreal.UserWidget) and widget not in shown:
                    shown.add(widget)
                    grew = True
        for widget, owner in everything:
            if owner in shown:
                parent = widget.get_parent()
                if parent is None and owner not in tops:  # the root of a nested user widget's own tree
                    parent = owner
                children.setdefault(parent, []).append(widget)
                owners[widget] = owner

        def label(widget):
            if isinstance(widget, unreal.TextBlock):
                return str(widget.get_text())
            texts = [label(c) for c in children.get(widget, []) if isinstance(c, (unreal.TextBlock, unreal.PanelWidget))]
            return " ".join(t for t in texts if t)

        for parent, kids in children.items():  # layout order, not object creation order
            if parent is not None:
                kids.sort(key=lambda w: _try(lambda: parent.get_child_index(w), 0))
        rows, buttons = [], []

        def button_label(widget):
            """Its text children, else its accessible text, else the text of its siblings (a transparent button
            laid over a text row in an Overlay)."""
            text = label(widget) or str(_try(lambda: widget.get_accessible_text(), "") or "")
            if text:
                return text
            siblings = [label(c) for c in children.get(widget.get_parent(), [])
                        if c is not widget and not isinstance(c, unreal.Button)
                        and isinstance(c, (unreal.TextBlock, unreal.PanelWidget))]
            return " ".join(t for t in siblings if t)

        def walk(widget, depth):
            if not widget.is_visible():
                return
            kind = None
            if isinstance(widget, unreal.Button):
                kind, text = "button", button_label(widget)
                buttons.append((text, widget))
            elif isinstance(widget, unreal.TextBlock):
                kind, text = "text", str(widget.get_text())
            elif isinstance(widget, (unreal.EditableTextBox, unreal.EditableText)):
                kind, text = "textbox", str(widget.get_text()) or str(_try(lambda: widget.get_editor_property("hint_text"), ""))
            elif isinstance(widget, unreal.CheckBox):
                kind, text = "checkbox", ""
            if kind:
                flags = "" if widget.get_is_enabled() else " [disabled]"
                if kind == "checkbox":
                    flags += " [checked]" if widget.is_checked() else " [unchecked]"
                rows.append("  " * depth + f'{kind} "{text}"{flags}  ({widget.get_name()})')
            if kind == "button":
                return  # its label is already on the button line
            for child in children.get(widget, []):
                walk(child, depth + (1 if kind else 0))

        for top in tops:
            rows.append(f"{top.get_class().get_name()}")
            for root in [w for w in children.get(None, []) if owners[w] == top]:
                walk(root, 1)
        if click:
            _authorize(token)
            key = click.lower()
            match = [(t, b) for t, b in buttons if t.lower() == key or b.get_name().lower() == key] or \
                [(t, b) for t, b in buttons if key in t.lower()]
            if not match:
                raise RuntimeError(f"no visible button '{click}' (buttons: {', '.join(t or b.get_name() for t, b in buttons[:10])})")
            text, button = match[0]
            if not button.get_is_enabled():
                raise RuntimeError(f"'{click}' is disabled")
            button.on_clicked.broadcast()
            return f'clicked {button.get_name()}' + (f' "{text}"' if text else "") + " (its OnClicked)"
        return "\n".join(rows) if rows else "no UMG widgets on the viewport"

    @_tool
    @staticmethod
    def ai(target: str = "", limit: int = 40) -> str:
        """AI-controlled pawns in PIE: movement and path following, focus, what they perceive, their brain
        (behavior tree or StateTree), blackboard values, and the state their project classes expose through
        no-argument getters (get_*, is_*, has_*). One line per AI, or a block for one (or a single) AI.

        Args:
            target: A pawn or controller label/name ('' for all AIs).
            limit: Maximum number of AIs listed.

        Returns:
            Summary lines, or a detailed block.
        """
        game = _game_world()
        if game is None:
            raise RuntimeError("PIE is not running (ue pie start)")
        controllers = list(unreal.GameplayStatics.get_all_actors_of_class(game, unreal.AIController))
        pawn_of = lambda c: _try(lambda: c.k2_get_pawn()) or _try(lambda: c.get_controlled_pawn())
        if target:
            key = target.lower()
            controllers = [c for c in controllers if key in (_actor_label(c).lower(), c.get_name().lower())
                           or (pawn_of(c) and key in (pawn_of(c).get_actor_label().lower(), pawn_of(c).get_name().lower()))] \
                or [c for c in controllers if pawn_of(c) and key in pawn_of(c).get_actor_label().lower()]
            if not controllers:
                raise RuntimeError(f"no AI-controlled pawn '{target}' in PIE")
        if not controllers:
            return "no AI controllers in the PIE world"
        enum = lambda v: str(v).split(".")[-1].split(":")[0].lower()
        vec = lambda v: f"({v.x:.0f}, {v.y:.0f}, {v.z:.0f})"

        def facts(controller):
            pawn = pawn_of(controller)
            out = {"pawn": pawn, "controller": controller}
            status = _try(lambda: controller.get_move_status())
            out["move"] = enum(status) if status is not None else "?"
            if out["move"] == "moving" and pawn:
                dest = controller.get_immediate_move_destination()
                out["move"] += f" to {vec(dest)}, {pawn.get_actor_location().distance(dest):.0f} cm" + \
                    (" (partial path)" if _try(lambda: controller.has_partial_path(), False) else "")
            focus = _try(lambda: controller.get_focus_actor())
            out["focus"] = _label(focus) if focus else ""
            perception = _try(lambda: controller.get_ai_perception_component())
            seen = _try(lambda: perception.get_currently_perceived_actors(None), []) if perception else []
            out["perceives"] = [_label(a) for a in seen or []]
            brain = _try(lambda: controller.get_editor_property("brain_component"))
            out["brain"] = (f"{brain.get_class().get_name()} " + ("running" if _try(lambda: brain.is_running(), False)
                                                                  else "stopped")) if brain else ""
            return out

        if len(controllers) > 1 and not target:
            rows = []
            for controller in controllers[:limit]:
                f = facts(controller)
                pawn = f["pawn"]
                where = vec(pawn.get_actor_location()) if pawn else "(no pawn)"
                rows.append(f"{_label(pawn) if pawn else _actor_label(controller)} at {where}: {f['move']}"
                            + (f", focus {f['focus']}" if f["focus"] else "")
                            + (f", perceives {', '.join(f['perceives'][:3])}" if f["perceives"] else "")
                            + (f", {f['brain']}" if f["brain"] else ""))
            return _capped(f"{len(controllers)} AI(s) (ue ai <name> for one in detail)", rows, limit)

        blocks = []
        for controller in controllers[:limit]:
            f = facts(controller)
            pawn = f["pawn"]
            lines = [f"{_label(pawn) if pawn else '(no pawn)'} ({pawn.get_class().get_name() if pawn else '-'})"
                     + (f" at {vec(pawn.get_actor_location())}, speed {pawn.get_velocity().length():.0f} cm/s" if pawn else "")]
            lines.append(f"  controller {_actor_label(controller)} ({controller.get_class().get_name()})")
            lines.append(f"  move: {f['move']}")
            lines.append(f"  focus: {f['focus'] or 'none'}")
            lines.append(f"  perceives: {', '.join(f['perceives']) or 'nothing'}")
            if f["brain"]:
                lines.append(f"  brain: {f['brain']}")
            board = _try(lambda: controller.get_editor_property("blackboard"))
            data = _try(lambda: board.get_editor_property("default_blackboard_asset")) if board else None
            keys = _try(lambda: data.get_editor_property("keys"), []) if data else []
            values = []
            for entry in keys or []:
                name = str(entry.get_editor_property("entry_name"))
                kind = entry.get_editor_property("key_type").get_class().get_name().replace("BlackboardKeyType_", "")
                getter = {"Object": "get_value_as_object", "Vector": "get_value_as_vector", "Float": "get_value_as_float",
                          "Int": "get_value_as_int", "Bool": "get_value_as_bool", "Name": "get_value_as_name",
                          "String": "get_value_as_string", "Enum": "get_value_as_enum",
                          "Rotator": "get_value_as_rotator"}.get(kind)
                value = _try(lambda: getattr(board, getter)(name)) if getter else "?"
                values.append(f"{name}={_label(value) if isinstance(value, unreal.Object) else vec(value) if isinstance(value, unreal.Vector) else _short(value, 40)}")
            if values:
                lines.append("  blackboard: " + ", ".join(values))
            for owner, base in ((pawn, unreal.Character if isinstance(pawn, unreal.Character) else unreal.Pawn),
                                (controller, unreal.AIController)):
                if owner is None:
                    continue
                getters = sorted(n for n in set(dir(type(owner))) - set(dir(base))
                                 if n.startswith(("get_", "is_", "has_")) and callable(getattr(type(owner), n, None)))
                shown = []
                for name in getters:
                    value = _try(lambda: getattr(owner, name)(), _MISSING)
                    if value is _MISSING or value is None:
                        continue
                    if isinstance(value, unreal.StructBase):
                        value = _try(lambda: _compact(value.to_dict()), str(value))
                    elif isinstance(value, unreal.Object):
                        value = _label(value)
                    elif isinstance(value, unreal.EnumBase):
                        value = enum(value)
                    elif isinstance(value, bool):
                        value = str(value).lower()
                    elif isinstance(value, float):
                        value = _round(value)
                    shown.append(f"{name}={_short(value, 40)}")
                if shown:
                    lines.append(f"  {owner.get_class().get_name()} getters: " + ", ".join(shown))
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    @_tool
    @staticmethod
    def new_asset(kind: str, path: str, parent: str = "", save: bool = False, token: str = "") -> str:
        """Create an asset: blueprint (parent class), material, mi / material instance (parent material),
        datatable (row struct), dataasset (data asset class), level, or folder. Refuses to overwrite.

        Args:
            kind: blueprint, material, mi, datatable, dataasset, level or folder.
            path: New /Game/... path (folder/Name).
            parent: Blueprint: parent class (Actor, /Script/Module.Class, or a Blueprint path); mi: parent material;
                datatable: row struct (name, /Script/Module.Struct, or a struct asset); dataasset: its class.
            save: Save the new asset.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was created.
        """
        _authorize(token)
        kind = kind.lower().replace("_", "").replace("-", "")
        path = path.rstrip("/").split(".")[0]
        library = unreal.EditorAssetLibrary
        if not path.startswith("/") or path.count("/") < 2:
            raise RuntimeError(f"expected a /Game/Folder/Name path, got '{path}'")
        if kind == "folder":
            if library.does_directory_exist(path):
                raise RuntimeError(f"{path} already exists")
            library.make_directory(path)
            return f"created folder {path}"
        if library.does_asset_exist(path):
            raise RuntimeError(f"{path} already exists")
        folder, name = path.rsplit("/", 1)

        def find_class(ref, base=None):
            if not ref:
                return None
            cls = None
            if ref.startswith("/Script/"):
                cls = unreal.load_class(None, ref)
            elif ref.startswith("/"):
                asset = unreal.load_asset(ref.split(".")[0])
                cls = unreal.BlueprintEditorLibrary.generated_class(asset) if isinstance(asset, unreal.Blueprint) else asset
            else:
                wrapper = getattr(unreal, ref, None)
                cls = wrapper.static_class() if isinstance(wrapper, type) and hasattr(wrapper, "static_class") else None
            if not isinstance(cls, unreal.Class):
                raise RuntimeError(f"no class '{ref}' (a class name like Actor, /Script/Module.Class, or a Blueprint path)")
            return cls

        tools = unreal.AssetToolsHelpers.get_asset_tools()
        note = ""
        if kind in ("blueprint", "bp"):
            factory = unreal.BlueprintFactory()
            factory.set_editor_property("parent_class", find_class(parent or "Actor"))
            asset = tools.create_asset(name, folder, unreal.Blueprint, factory)
            note = f"parent {parent or 'Actor'}"
        elif kind == "material":
            asset = tools.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
        elif kind in ("mi", "materialinstance"):
            base = unreal.load_asset(parent.split(".")[0]) if parent else None
            if parent and not isinstance(base, unreal.MaterialInterface):
                raise RuntimeError(f"no material at {parent}")
            asset = tools.create_asset(name, folder, unreal.MaterialInstanceConstant,
                                       unreal.MaterialInstanceConstantFactoryNew())
            if asset and base:
                unreal.MaterialEditingLibrary.set_material_instance_parent(asset, base)
                note = f"parent {_short_path(base.get_path_name())}"
        elif kind == "datatable":
            if not parent:
                raise RuntimeError("a DataTable needs its row struct: --parent <StructName or /Script/Module.Struct>")
            struct = unreal.load_object(None, parent) if parent.startswith("/Script/") else \
                unreal.load_asset(parent.split(".")[0]) if parent.startswith("/") else None
            if struct is None:
                wrapper = getattr(unreal, parent, None)
                struct = wrapper.static_struct() if wrapper is not None and hasattr(wrapper, "static_struct") else None
            if not isinstance(struct, unreal.ScriptStruct):
                raise RuntimeError(f"no row struct '{parent}'")
            factory = unreal.DataTableFactory()
            factory.set_editor_property("struct", struct)
            asset = tools.create_asset(name, folder, unreal.DataTable, factory)
            note = f"rows {struct.get_name()}"
        elif kind == "dataasset":
            if not parent:
                raise RuntimeError("a DataAsset needs its class: --parent <DataAsset subclass>")
            factory = unreal.DataAssetFactory()
            factory.set_editor_property("data_asset_class", find_class(parent))
            asset = tools.create_asset(name, folder, unreal.DataAsset, factory)
            note = f"class {parent}"
        elif kind in ("level", "map"):
            asset = tools.create_asset(name, folder, unreal.World, unreal.WorldFactory())
        else:
            raise RuntimeError("kind must be blueprint, material, mi, datatable, dataasset, level or folder")
        if asset is None:
            raise RuntimeError(f"could not create {path} (see ue log)")
        if save:
            library.save_loaded_asset(asset, False)
        return f"created {path} ({asset.get_class().get_name()}" + (f", {note}" if note else "") + ")" + \
            ("" if save else " (unsaved: ue save, or pass --save)")

    @_tool
    @staticmethod
    def anim(target: str, pie: bool = True, limit: int = 30) -> str:
        """Animation state of an actor: per skeletal mesh component, its mode, Anim Blueprint or single animation,
        the playing montage (section, position, rate), root motion mode, active curves, and the Anim Blueprint's
        own variables (the values its state machine transitions test); plus the character's movement mode and
        speed, to tell animation from movement problems.

        Args:
            target: Actor label/name, 'player', or 'Actor.Component'.
            pie: Read the PIE world (default) instead of the editor level.
            limit: Maximum number of lines per component.

        Returns:
            A block per skeletal mesh component.
        """
        game = _game_world() if pie else None
        if pie and game is None:
            raise RuntimeError("PIE is not running (ue pie start; or --editor for the editor level)")
        if target.lower() in ("player", "pawn") and game is not None:
            obj = unreal.GameplayStatics.get_player_pawn(game, 0)
        else:
            obj = _resolve(target, pie)
        actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
        comps = [obj] if isinstance(obj, unreal.SkeletalMeshComponent) else \
            list(actor.get_components_by_class(unreal.SkeletalMeshComponent))
        enum = lambda v: str(v).split(".")[-1].split(":")[0].lower()
        out = [f"{_label(actor)} ({actor.get_class().get_name()})"]
        movement = _try(lambda: actor.get_editor_property("character_movement")) if isinstance(actor, unreal.Character) else None
        if movement:
            velocity = actor.get_velocity()
            out.append(f"  movement: {enum(movement.get_editor_property('movement_mode'))}, speed "
                       f"{velocity.length():.0f} cm/s (max walk {movement.get_editor_property('max_walk_speed'):.0f})"
                       + (", falling" if _try(lambda: movement.is_falling(), False) else ""))
        for comp in comps:
            mesh = _try(lambda: comp.get_skeletal_mesh_asset()) or _try(lambda: comp.get_editor_property("skeletal_mesh"))
            mode = enum(_try(lambda: comp.get_editor_property("animation_mode"), "?"))
            lines = [f"  {comp.get_name()}: mesh {_short_path(mesh.get_path_name()) if mesh else 'None'}, {mode}"
                     + ("" if comp.is_visible() else ", hidden")]
            instance = _try(lambda: comp.get_anim_instance())
            if mode.endswith("asset") or "single" in mode:
                anim = _try(lambda: comp.get_editor_property("animation_data").get_editor_property("anim_to_play"))
                lines.append(f"    single animation {_short_path(anim.get_path_name()) if anim else 'None'}")
            if instance is not None:
                lines.append(f"    anim instance {instance.get_class().get_name()}, root motion "
                             f"{enum(_try(lambda: instance.get_editor_property('root_motion_mode'), '?'))}")
                montage = _try(lambda: instance.get_current_active_montage())
                if montage:
                    section = str(_try(lambda: instance.montage_get_current_section(montage), "?"))
                    position = _try(lambda: instance.montage_get_position(montage), 0.0)
                    length = _try(lambda: montage.get_play_length(), 0.0)
                    lines.append(f"    montage {_short_path(montage.get_path_name())}: section {section}, "
                                 f"{position:.2f}/{length:.2f} s, rate {_try(lambda: instance.montage_get_play_rate(montage), 1.0):g}"
                                 + ("" if _try(lambda: instance.montage_is_playing(montage), True) else ", paused"))
                elif not _try(lambda: instance.is_any_montage_playing(), False):
                    lines.append("    no montage playing")
                curves = []
                for name in (_try(lambda: instance.get_all_curve_names(), []) or [])[:200]:
                    value = _try(lambda: instance.get_curve_value(name), 0.0)
                    if abs(value) > 1e-3:
                        curves.append(f"{name}={value:.2f}")
                if curves:
                    lines.append(f"    curves: {', '.join(curves[:12])}" + (f" +{len(curves) - 12}" if len(curves) > 12 else ""))
                if not _LEGACY:  # the Anim Blueprint's own variables: what its transitions read
                    cls = instance.get_class()
                    own = set(_reflect(unreal.ToolsetLibrary.list_struct_properties(cls), instance)) - \
                        set(_reflect(unreal.ToolsetLibrary.list_struct_properties(unreal.AnimInstance.static_class()), instance))
                    names = sorted(own)
                    values = _reflect(unreal.ToolsetLibrary.get_object_properties(instance, names), instance) if names else {}
                    shown = [f"{n}={_short(_round(values.get(n)), 40)}" for n in names
                             if not isinstance(values.get(n), (dict, list)) or set(values.get(n) or {}) <= {"x", "y", "z"}]
                    if shown:
                        lines.append("    variables: " + ", ".join(shown[:limit]) + (f" +{len(shown) - limit}" if len(shown) > limit else ""))
            elif _try(lambda: comp.get_editor_property("leader_pose_component")):
                leader = comp.get_editor_property("leader_pose_component")
                lines.append(f"    follows {leader.get_name()}'s pose (leader pose component)")
            elif mode.endswith("blueprint"):
                lines.append("    no anim instance (not initialised: is the component ticking and visible?)")
            out.extend(lines)
        if not comps:
            out.append("  no skeletal mesh components")
        return "\n".join(out)

    @_tool
    @staticmethod
    def audio_components(limit: int = 60) -> str:
        """Playing audio components in PIE: sound, owner, distance to the player's camera, volume multiplier and
        attenuation. (Fire-and-forget sounds have no component; `ue audio` adds those from the audio device.)

        Args:
            limit: Maximum number of entries.

        Returns:
            JSON list of {component, sound, owner, distance, volume, attenuation}.
        """
        game = _game_world()
        if game is None:
            raise RuntimeError("PIE is not running (ue pie start)")
        camera = unreal.GameplayStatics.get_player_camera_manager(game, 0)
        listener = camera.get_camera_location() if camera else None
        rows = []
        for comp in unreal.ObjectIterator(unreal.AudioComponent):
            if _try(lambda: comp.get_world()) != game or not _try(lambda: comp.is_playing(), False):
                continue
            sound = _try(lambda: comp.get_editor_property("sound"))
            owner = comp.get_owner()
            where = comp.get_world_location()
            attenuation = _try(lambda: comp.get_editor_property("attenuation_settings"))
            rows.append({"component": comp.get_name(), "sound": sound.get_name() if sound else None,
                         "owner": _label(owner) if owner else None,
                         "distance": round(where.distance(listener)) if listener else None,
                         "volume": round(_try(lambda: comp.get_editor_property("volume_multiplier"), 1.0), 3),
                         "attenuation": attenuation.get_name() if attenuation else None})
            if len(rows) >= limit:
                break
        return json.dumps(rows)

    @_tool
    @staticmethod
    def pie_watch(action: str = "status", targets: str = "[]", every: float = 0.1) -> str:
        """Sample properties of PIE objects on every editor tick (at most every `every` seconds) until stopped.

        Args:
            action: 'start' (replaces a running watch), 'stop' (ends it and returns the samples), or 'status'.
            targets: With start: JSON list like ["player.Health", "BP_Enemy_C_1.State", "BP_Door_2"] (an actor
                alone means its location; 'player', 'controller' and 'player.<Component>' work as in pie_step).
            every: With start: seconds between samples (0.1).

        Returns:
            start/status: a line of text. stop: JSON {targets, columns, samples: [[real s, game s, values...]],
            ended, missing}.
        """
        state = _WATCH
        if action == "start":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
            specs = json.loads(targets) if targets.strip().startswith("[") else [t for t in targets.split(",") if t]
            if not specs:
                raise RuntimeError("give properties to watch, e.g. player.Health BP_Enemy_C_1.State")
            game = _game_world()
            if game is None:
                raise RuntimeError("PIE is not running (ue pie start)")
            pawn = unreal.GameplayStatics.get_player_pawn(game, 0)  # may come later: bound on the tick then
            controller = unreal.GameplayStatics.get_player_controller(game, 0)
            readers, missing = [], []
            for spec in specs:
                try:
                    if pawn is None and spec.split(".", 1)[0].lower() in ("player", "pawn", "controller"):
                        raise RuntimeError("no player pawn yet")
                    readers.append(_watch_reader(spec, pawn, controller))
                except RuntimeError as exc:
                    if "has no property" in str(exc) or "not readable" in str(exc):
                        raise
                    readers.append(None)  # an actor (or the pawn) that may spawn later
                    missing.append(f"{spec}: {exc}")
            now = time.time()
            state.update(world=game, specs=specs, readers=readers, retry=[now] * len(specs), every=max(0.0, every),
                         samples=[], start=now, last=0.0, ended=None, missing=missing, rebind=now, kinds={},
                         bound=[pawn if r is not None else None for r in readers])
            state["handle"] = unreal.register_slate_post_tick_callback(_watch_tick)
            return f"watching {len(specs)} value(s) every {every:g}s" + (f"; not found yet: {'; '.join(missing)}"
                                                                        if missing else "")
        if action == "stop":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
                state["handle"] = None
            if "specs" not in state:
                raise RuntimeError("no watch was started (ue pie watch)")
            return json.dumps({"targets": state["specs"], "columns": ["real", "game"] + state["specs"],
                               "samples": state["samples"], "ended": state.get("ended"),
                               "kinds": [state.get("kinds", {}).get(i) for i in range(len(state["specs"]))],
                               "missing": state.get("missing", []), "capped": len(state["samples"]) >= _WATCH_LIMIT})
        running = state.get("handle") is not None
        return (f"watching {len(state['specs'])} value(s), {len(state['samples'])} sample(s)" if running
                else "no watch running")

    @_tool
    @staticmethod
    def pie_loop(action: str = "status", script: str = "", seconds: float = 0.0, every: float = 0.0,
                 every_script: str = "", token: str = "") -> str:
        """Console commands and waits run from the editor tick in the PIE game ('stat unit; wait 5; ...'), looped
        for `seconds` on a schedule kept from the start (no drift), with `every_script` every `every` seconds.

        Args:
            action: 'start' (replaces a running loop), 'stop', or 'status'.
            script: With start: commands and 'wait N' separated by ';' or newlines.
            seconds: With start: how long to loop (0: one pass through the script).
            every: With start: seconds between runs of every_script (0: none).
            every_script: With start: commands (';'-separated) run every `every` seconds.
            token: Session token from Saved/Tether/editors/<pid>.token (needed with start).

        Returns:
            JSON {ran, passes, every_runs, errors, ended, elapsed, seconds, running}.
        """
        state = _LOOP
        if action == "start":
            _authorize(token)
            if state.get("handle") is not None:
                _loop_end(state, "replaced")
            game = _game_world()
            if game is None:
                raise RuntimeError("PIE is not running")
            steps = _loop_steps(script)
            if not steps:
                raise RuntimeError("give commands, e.g. 'stat unit; wait 5; SD.Spawn 10; wait 30'")
            if seconds > 0 and not any(k == "wait" and v > 0 for k, v in steps):
                raise RuntimeError("a looped script needs a wait (it would run every frame): add 'wait N'")
            every_steps = [s for s in _loop_steps(every_script) if s[0] == "cmd"] if every > 0 else []
            now = time.time()
            state.clear()
            state.update(handle=None, world=game, steps=steps, index=0, next=now, start=now, seconds=seconds,
                         end=now + seconds if seconds > 0 else 0, every=every, every_steps=every_steps,
                         every_at=now + every, ran=0, passes=0, every_runs=0, errors=[], ended=None)
            state["handle"] = unreal.register_slate_post_tick_callback(_loop_tick)
            return json.dumps(_loop_status(state))
        if "start" not in state:
            raise RuntimeError("no loop was started (ue pie loop)")
        if action == "stop" and state.get("handle") is not None:
            _loop_end(state, "stopped")
        return json.dumps(_loop_status(state))

    @_tool
    @staticmethod
    def unused(path: str = "/Game", roots: str = "[]") -> str:
        """Assets nothing uses: everything not reachable through the asset registry's dependencies (hard and
        soft) from the roots. Roots: every map under /Game, Primary Asset Labels, editor utilities, and the
        packages or 'dir:/Game/Folder' prefixes given (the CLI adds what the config and code name).

        Args:
            path: Content folder to report on.
            roots: JSON list of extra root packages, or 'dir:/Game/X' for every asset under a folder.

        Returns:
            JSON {unused: [[package, class]], redirectors: [package], roots: {kind: count}, total: int}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        classes = {}
        for data in registry.get_assets_by_path("/Game", recursive=True):
            classes.setdefault(str(data.package_name), str(data.asset_class_path.asset_name))
        extra = json.loads(roots or "[]")
        dirs = [r[4:].rstrip("/") + "/" for r in extra if r.startswith("dir:")]
        named = {r.split(".")[0] for r in extra if not r.startswith("dir:")}
        counts = {"maps": 0, "labels and editor tools": 0, "named in config/code": 0, "always-cook folders": 0}
        queue = []
        for package, cls in classes.items():
            kind = ("maps" if cls == "World" else
                    "labels and editor tools" if cls == "PrimaryAssetLabel" or cls.startswith("EditorUtility") else
                    "named in config/code" if package in named else
                    "always-cook folders" if any(package.startswith(d) for d in dirs) else None)
            if kind:
                counts[kind] += 1
                queue.append(package)
        reached, options = set(queue), _dependency_options()
        while queue:
            package = queue.pop()
            for dep in registry.get_dependencies(package, options) or []:
                dep = str(dep)
                if dep not in reached and dep.startswith("/Game/"):
                    reached.add(dep)
                    queue.append(dep)
        prefix = path.rstrip("/") + "/"
        skip = ("/Game/Developers/", "/Game/Collections/", "/Game/__ExternalActors__/", "/Game/__ExternalObjects__/")
        unused, redirectors = [], []
        for package, cls in sorted(classes.items()):
            if not package.startswith(prefix) or package.startswith(skip) or "/__External" in package:
                continue
            if cls == "ObjectRedirector":
                redirectors.append(package)
            elif package not in reached:
                unused.append([package, cls])
        return json.dumps({"unused": unused, "redirectors": redirectors, "roots": counts,
                           "total": sum(1 for p in classes if p.startswith(prefix))})

    @_tool
    @staticmethod
    def lint_data(path: str = "/Game") -> str:
        """Every asset under a folder with its class and the asset registry tags `ue lint` checks (no loading).

        Args:
            path: Content folder.

        Returns:
            JSON list of [package, class, {tag: value}] (tags for textures and static meshes only; `ue tex` uses it too).
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        texture = ("Dimensions", "CompressionSettings", "SRGB", "LODGroup", "Format", "NeverStream", "MipGenSettings",
                   "VirtualTextureStreaming", "HasAlphaChannel", "MaxTextureSize")
        wanted = {"Texture2D": texture, "TextureCube": texture, "Texture2DArray": texture, "VolumeTexture": texture,
                  "StaticMesh": ("Triangles", "LODs", "CollisionPrims", "CollisionComplexity", "NaniteEnabled",
                                 "DefaultCollision"),
                  "SoundWave": ("NumChannels", "Duration", "SampleRate", "bLooping")}
        rows, seen, classes = [], set(), {}
        options = _dependency_options()

        def class_of(package):  # the asset class in a package, or None when no such package exists
            if package not in classes:
                found = registry.get_assets_by_package_name(package) or []
                classes[package] = str(found[0].asset_class_path.asset_name) if found else None
            return classes[package]
        for data in registry.get_assets_by_path(path.rstrip("/") or "/Game", recursive=True):
            package, cls = str(data.package_name), str(data.asset_class_path.asset_name)
            if package in seen or "/__External" in package:
                continue
            seen.add(package)
            tags = {t: data.get_tag_value(t) for t in wanted.get(cls, ())}
            tags = {k: v for k, v in tags.items() if v is not None}
            deps = [str(d) for d in registry.get_dependencies(package, options) or []]
            deps = [d for d in deps if d.startswith("/") and not d.startswith(("/Script/", "/Temp/", "/Memory/"))]
            missing = [d for d in deps if class_of(d) is None]
            if missing:  # e.g. a material slot pointing at a deleted asset: it loads as None
                tags["_missing"] = missing[:10]
            if cls in ("StaticMesh", "SkeletalMesh"):
                tags["_materials"] = sum(1 for d in deps if "Material" in (class_of(d) or ""))
            if cls == "SoundWave":
                tags["_attenuated"] = any(class_of(d) == "SoundAttenuation" for d in deps)
            rows.append([package, cls, tags])
        return json.dumps(rows)

    @_tool
    @staticmethod
    def texture_streaming(limit: int = 15) -> str:
        """Texture memory as the engine holds it now (ListTextures and ListStreamingRenderAssets): the streaming
        pool size, what is resident (streaming and not), the largest textures, and the ones that want more mips
        than they have (pool too small, or still streaming in). Covers PIE too: textures are shared.

        Args:
            limit: How many of the largest textures to return.

        Returns:
            JSON: {pool_mb, in_mem_mb, streaming_mb, non_streaming_mb, count, largest: [...], wanting: [...]}.
        """
        lines = _console_output(None, ["ListTextures", "ListStreamingRenderAssets"])
        row = re.compile(r"^(\d+)x(\d+) \((\d+) KB, -?\d+\), (\d+)x(\d+) \((\d+) KB\), (?:PF_)?(\w+), TEXTUREGROUP_(\w+), "
                         r"(\S+), (YES|NO), (?:YES|NO), (YES|NO), \d+, (\d+)")
        textures, wanting, current = {}, [], None
        for line in lines:
            m = row.match(line)
            if m:
                textures[m.group(9).split(".")[0]] = {
                    "path": m.group(9).split(".")[0], "max": f"{m.group(1)}x{m.group(2)}", "max_kb": int(m.group(3)),
                    "current": f"{m.group(4)}x{m.group(5)}", "kb": int(m.group(6)), "format": m.group(7),
                    "group": m.group(8), "streaming": m.group(10) == "YES", "vt": m.group(11) == "YES",
                    "mips": int(m.group(12))}
                continue
            head = re.search(r"Texture \[\d+\] : \w+ (\S+)", line)
            if head:
                current = head.group(1).split(".")[0]
                continue
            dims = re.search(r"Current=(\d+)x(\d+) Wanted=(\d+)x(\d+) MaxAllowed=(\d+)x(\d+)", line)
            if dims and current:
                cw, ch, ww, wh = (int(dims.group(i)) for i in range(1, 5))
                if ww * wh > cw * ch:
                    wanting.append({"path": current, "current": f"{cw}x{ch}", "wanted": f"{ww}x{wh}",
                                    "ratio": (ww * wh) / max(1, cw * ch)})
                current = None
        for w in wanting:
            kb = textures.get(w["path"], {}).get("kb")
            w["missing_kb"] = int(kb * (w.pop("ratio") - 1)) if kb else None
        wanting.sort(key=lambda w: -(w["missing_kb"] or 0))
        rows = sorted(textures.values(), key=lambda t: -t["kb"])
        streaming = sum(t["kb"] for t in rows if t["streaming"])
        pool = _try(lambda: unreal.SystemLibrary.get_console_variable_int_value("r.Streaming.PoolSize"), 0)
        return json.dumps({"pool_mb": pool, "count": len(rows), "in_mem_mb": round(sum(t["kb"] for t in rows) / 1024, 1),
                           "streaming_mb": round(streaming / 1024, 1),
                           "non_streaming_mb": round(sum(t["kb"] for t in rows if not t["streaming"]) / 1024, 1),
                           "largest": rows[:limit], "wanting_count": len(wanting),
                           "wanting_mb": round(sum(w["missing_kb"] or 0 for w in wanting) / 1024, 1),
                           "wanting": wanting[:limit]})

    @_tool
    @staticmethod
    def set_assets(folder: str, values: str, cls: str = "", match: str = "", dry_run: bool = False, save: bool = False,
                   limit: int = 500, token: str = "") -> str:
        """Set the same properties on many assets: every asset under a folder whose class and name match. Each
        asset is changed like `ue set` does (one undoable step each); a dry run shows the current values instead.

        Args:
            folder: Content folder, searched recursively.
            values: JSON object of property name to value text, as for set_props.
            cls: Regex on the asset class (e.g. 'Texture2D$').
            match: fnmatch pattern on the asset name (e.g. '*_N').
            dry_run: Show what would change, change nothing.
            save: Save each changed asset.
            limit: At most this many assets.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {matched, rows: [{asset, ok, detail}]}.
        """
        import fnmatch
        _authorize(token)
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        cls_rx = re.compile(cls, re.I) if cls else None
        found = []
        for data in registry.get_assets_by_path(folder.rstrip("/"), recursive=True):
            name, kind = str(data.asset_name), str(data.asset_class_path.asset_name)
            if (cls_rx and not cls_rx.search(kind)) or (match and not fnmatch.fnmatch(name, match)):
                continue
            found.append(str(data.package_name))
        found = sorted(set(found))
        requested = json.loads(values)
        rows = []
        if not dry_run:  # all or nothing: refuse before the first edit if a file is someone else's
            _Vcs().reserve(found[:limit])
        for package in found[:limit]:
            try:
                if dry_run:
                    obj = unreal.load_asset(package)
                    schema = _reflect(unreal.ToolsetLibrary.list_struct_properties(obj.get_class()), obj)
                    by_lower = {n.lower(): n for n in schema}
                    wanted = [by_lower.get(k.rstrip("+").lower()) for k in requested]
                    missing = [k for k, w in zip(requested, wanted) if w is None]
                    if missing:
                        raise RuntimeError(f"no property {', '.join(missing)}")
                    current = _reflect(unreal.ToolsetLibrary.get_object_properties(obj, wanted), obj)
                    rows.append({"asset": package, "ok": True, "detail": ", ".join(
                        f"{k} is {_short(_compact(v), 40)}" for k, v in current.items())})
                else:
                    out = _RAW["set_props"](package, values, False, token)
                    if save:
                        unreal.EditorAssetLibrary.save_asset(package, only_if_is_dirty=True)
                    rows.append({"asset": package, "ok": True, "detail": "; ".join(out.splitlines()[1:])})
            except Exception as exc:  # one asset without the property must not stop the rest
                rows.append({"asset": package, "ok": False, "detail": str(exc)})
        return json.dumps({"matched": len(found), "rows": rows})

    @_tool
    @staticmethod
    def lint_fix(plan: str, save: bool = False, token: str = "") -> str:
        """Apply `ue lint --fix` fixes: rename (references fixed up, through revision control), normal-map
        compression, sRGB off for masks, a box collision for meshes without simple collision.

        Args:
            plan: JSON list of {rule, package, fix}: fix is 'rename:<NewName>', 'normalmap', 'linear', 'box',
                'nanite' or 'lods:<N>'.
            save: Save each changed asset (renames save their referencers as ue mv does).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON list of {package, fix, ok, detail}.
        """
        _authorize(token)
        results = []
        _Vcs().reserve(sorted({item["package"] for item in json.loads(plan)}))  # all or nothing, before any fix
        for item in json.loads(plan):
            package, fix = item["package"], item["fix"]
            try:
                if fix.startswith("rename:"):
                    folder = package.rsplit("/", 1)[0]
                    target = f"{folder}/{fix.split(':', 1)[1]}"
                    if unreal.EditorAssetLibrary.does_asset_exist(target):
                        raise RuntimeError(f"{target} already exists")
                    detail = _RAW["mv"](package, target, token)
                else:
                    asset = unreal.load_asset(package)
                    if asset is None:
                        raise RuntimeError("not found")
                    if fix == "normalmap":
                        asset.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
                        asset.set_editor_property("srgb", False)
                        detail = "normal-map compression, sRGB off"
                    elif fix == "linear":
                        asset.set_editor_property("srgb", False)
                        if asset.get_editor_property("compression_settings") == unreal.TextureCompressionSettings.TC_DEFAULT:
                            asset.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
                        detail = "sRGB off" + (", mask compression" if asset.get_editor_property(
                            "compression_settings") == unreal.TextureCompressionSettings.TC_MASKS else "")
                    elif fix == "box":
                        unreal.EditorStaticMeshLibrary.add_simple_collisions(asset, unreal.ScriptCollisionShapeType.BOX)
                        detail = "added a box collision"
                    elif fix == "nanite":
                        nanite = asset.get_editor_property("nanite_settings")
                        nanite.set_editor_property("enabled", True)
                        asset.set_editor_property("nanite_settings", nanite)
                        detail = "Nanite on"
                    elif fix.startswith("lods:"):
                        _set_lods(asset, int(fix.split(":", 1)[1]))
                        detail = f"{asset.get_num_lods()} LODs generated (each half the triangles of the one before)"
                    else:
                        raise RuntimeError(f"unknown fix '{fix}'")
                    if save:
                        unreal.EditorAssetLibrary.save_loaded_asset(asset, False)
                results.append({"package": package, "fix": fix, "ok": True, "detail": detail})
            except Exception as exc:  # one bad asset must not stop the rest
                results.append({"package": package, "fix": fix, "ok": False, "detail": str(exc)})
        return json.dumps(results)

    @_tool
    @staticmethod
    def fix_redirectors(paths: str, token: str = "") -> str:
        """Fix up redirectors: resave what references each one (now pointing at the real asset), then delete it.

        Args:
            paths: JSON list of redirector packages.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One line per redirector.
        """
        _authorize(token)
        vcs, lines = _Vcs(), []
        for package in json.loads(paths):
            saved, problems = _fixup_redirector(package, vcs)
            lines.append(f"{package}: " + ("; ".join(problems) if problems else f"fixed, {saved} referencer(s) resaved"))
        return "\n".join(lines)

    @_tool
    @staticmethod
    def import_files(files: str, dest: str, options: str = "{}", token: str = "") -> str:
        """Import files (meshes, textures, sounds, anything the editor imports) without dialogs, then apply the
        settings given: collision, LODs and Nanite for static meshes; texture group and mask handling for textures.

        Args:
            files: JSON list of absolute file paths.
            dest: Content folder, e.g. /Game/Props/Crates.
            options: JSON {name: str (single file), collision: none|box|sphere|capsule|convex|complex,
                lods: int, nanite: bool, texture_group: e.g. World|UI|Character, replace: bool, save: bool}.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON list of {file, assets: [{path, class, detail}], error}.
        """
        _authorize(token)
        if _game_world() is not None:
            raise RuntimeError("PIE is running, and the editor builds no meshes in play mode (ue pie stop)")
        paths, opts = json.loads(files), json.loads(options or "{}")
        tasks = []
        for path in paths:
            if not os.path.isfile(path):
                raise RuntimeError(f"no such file: {path}")
            task = unreal.AssetImportTask()
            task.set_editor_property("filename", path)
            task.set_editor_property("destination_path", dest.rstrip("/"))
            if opts.get("name") and len(paths) == 1:
                task.set_editor_property("destination_name", opts["name"])
            task.set_editor_property("automated", True)  # no option dialogs
            task.set_editor_property("replace_existing", bool(opts.get("replace", True)))
            task.set_editor_property("save", False)
            tasks.append(task)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)
        results = []
        for task in tasks:
            assets = []
            for object_path in task.get_editor_property("imported_object_paths") or []:
                asset = unreal.load_asset(str(object_path))
                if asset is None:
                    continue
                detail = _after_import(asset, opts)
                if opts.get("save"):
                    unreal.EditorAssetLibrary.save_loaded_asset(asset, False)
                assets.append({"path": asset.get_path_name().split(".")[0], "class": asset.get_class().get_name(),
                               "detail": detail})
            results.append({"file": task.get_editor_property("filename"), "assets": assets,
                            "error": None if assets else "nothing imported (ue log says why)"})
        return json.dumps(results)

    @_tool
    @staticmethod
    def reimport_assets(paths: str, force: bool = False, save: bool = False, token: str = "") -> str:
        """Reimport assets from the files they were imported from, when those changed since the asset was saved.

        Args:
            paths: JSON list of asset paths or content folders (searched recursively).
            force: Reimport even when the source file is not newer than the saved asset.
            save: Save the reimported assets.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON list of {path, source, state: reimported|unchanged|source missing|no source|failed, detail}.
        """
        _authorize(token)
        if _game_world() is not None:
            raise RuntimeError("PIE is running, and the editor builds no meshes in play mode (ue pie stop)")
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        packages = []
        for path in json.loads(paths):
            path = path.split(".")[0].rstrip("/")
            found = registry.get_assets_by_package_name(path)
            if found:
                packages.append(path)
            else:
                packages += sorted({str(d.package_name) for d in registry.get_assets_by_path(path, recursive=True)})
        if not packages:
            raise RuntimeError("no assets at those paths (ue find)")
        rows, by_source = [], {}
        for package in packages:
            asset = unreal.load_asset(package)
            data = _try(lambda: asset.get_editor_property("asset_import_data")) if asset else None
            source = _try(lambda: data.get_first_filename(), "") if data else ""
            row = {"path": package, "source": source, "detail": ""}
            rows.append(row)
            saved = _package_file(package)
            if not source:
                row["state"] = "no source"
            elif not os.path.isfile(source):
                row["state"] = "source missing"
            elif not os.path.isfile(saved) and not force:
                row["state"] = "unsaved"  # nothing on disk to be newer than: it was just imported
            elif not force and os.path.getmtime(source) <= os.path.getmtime(saved):
                row["state"] = "unchanged"
            else:
                by_source.setdefault(os.path.normcase(source), []).append(row)
        for group in by_source.values():
            # One import per file: a mesh file also brings its materials, so reimport through the asset named
            # after the file (or the first), and let the others ride along.
            stem = os.path.splitext(os.path.basename(group[0]["source"]))[0].lower()
            lead = next((r for r in group if r["path"].rsplit("/", 1)[1].lower() == stem), group[0])
            task = unreal.AssetImportTask()
            task.set_editor_property("filename", lead["source"])
            task.set_editor_property("destination_path", lead["path"].rsplit("/", 1)[0])
            task.set_editor_property("destination_name", lead["path"].rsplit("/", 1)[1])
            task.set_editor_property("automated", True)
            task.set_editor_property("replace_existing", True)
            task.set_editor_property("replace_existing_settings", False)  # keep the asset's own settings
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
            done = bool(task.get_editor_property("imported_object_paths"))
            for row in group:
                row["state"] = "reimported" if done else "failed"
                fresh = unreal.load_asset(row["path"]) if done else None
                if fresh:
                    tag = unreal.EditorAssetLibrary.get_metadata_tag(fresh, _IMPORT_TAG)
                    row["detail"] = _after_import(fresh, json.loads(tag) if tag else {})
                    if save:
                        unreal.EditorAssetLibrary.save_loaded_asset(fresh, False)
        return json.dumps(rows)

    @_tool
    @staticmethod
    def frame_log(action: str = "status") -> str:
        """Record every editor frame's wall-clock time and length, with package loading logged verbosely, so the
        ue CLI's `perf load` can tell which loads landed in which hitch.

        Args:
            action: 'start' (logs a marker line tying the log's clock to the wall clock), 'stop' or 'status'.

        Returns:
            start/status: a line of text. stop: JSON {frames: [[wall time, seconds, in PIE]], mark: wall time, streaming:
            [[wall time, all streaming completed]]}.
        """
        state = _FRAMES
        if action == "start":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
            state.update(frames=[], streaming=[], last_stream=0.0, mark=time.time(), subsystem=None, last=None)
            unreal.SystemLibrary.execute_console_command(None, "log LogStreaming Verbose")
            unreal.log(f"TetherFrameLog mark {state['mark']:.3f}")
            state["handle"] = unreal.register_slate_post_tick_callback(_frame_tick)
            return "recording frames"
        if action == "stop":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
                state["handle"] = None
            unreal.SystemLibrary.execute_console_command(None, "log LogStreaming Log")
            if "frames" not in state:
                raise RuntimeError("no frame log was started")
            return json.dumps({"frames": state["frames"], "mark": state["mark"], "streaming": state["streaming"]})
        return (f"recording, {len(state.get('frames', []))} frame(s)" if state.get("handle") is not None
                else "not recording")

    @_tool
    @staticmethod
    def pie_record(action: str = "status", every: float = 0.25) -> str:
        """Record a play session in PIE: key presses for every key a mapping context uses, analog sticks, and
        the player's position and view. The ue CLI's `pie record` turns this into a `pie do` step file.

        Args:
            action: 'start' (replaces a running recording), 'stop' (ends it and returns the events), or 'status'.
            every: With start: seconds between position/view samples (0.25).

        Returns:
            start/status: a line of text. stop: JSON {mappings: [{context, action, type, key, modifiers}],
            active: [context names], events: [[t, 'down'|'up', key] | [t, 'axis', key, [x, y]] |
            [t, 'pose', [x, y, z], [pitch, yaw], [camera x, y, z]]], ended, seconds}.
        """
        state = _REC
        if action == "start":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
            game, pawn, controller = _pie_player()
            subsystem = _try(lambda: _input_subsystem(game))
            registry = unreal.AssetRegistryHelpers.get_asset_registry()
            mappings, keys, active = [], {}, []
            for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputMappingContext")):
                imc = _try(lambda: unreal.load_asset(str(data.package_name)))
                if not isinstance(imc, unreal.InputMappingContext):
                    continue
                if subsystem and _try(lambda: subsystem.has_mapping_context(imc)) not in (None, False):
                    active.append(imc.get_name())
                for m in _mapping_rows(imc):
                    act, key = m.get_editor_property("action"), m.get_editor_property("key")
                    name = key.export_text()
                    if not act or not name or name == "None":
                        continue
                    value_type = str(_try(lambda: act.get_editor_property("value_type"), "")).split(".")[-1].split(":")[0]
                    mappings.append({"context": imc.get_name(), "action": act.get_name(), "type": value_type.lower(),
                                     "key": name, "modifiers": [_modifier_text(x) for x in m.get_editor_property("modifiers") if x]})
                    if not _LOOK_KEYS.match(name) and name not in keys:
                        analog = ("2d" if _try(lambda: unreal.InputLibrary.key_is_axis2d(key), False) else
                                  "1d" if _try(lambda: unreal.InputLibrary.key_is_analog(key), False) else "")
                        keys[name] = (name, key, analog)
            if not keys:
                raise RuntimeError("no keys to record: no Enhanced Input mapping contexts in the project (ue input)")
            state.update(world=game, controller=controller, keys=list(keys.values()), mappings=mappings, active=active,
                         down=set(), analog={}, events=[], start=time.time(), last_pose=-1.0, every=max(0.05, every),
                         ended=None)
            state["handle"] = unreal.register_slate_post_tick_callback(_rec_tick)
            return f"recording {len(keys)} key(s) from {len({m['context'] for m in mappings})} mapping context(s)"
        if action == "stop":
            if state.get("handle") is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
                state["handle"] = None
            if "events" not in state:
                raise RuntimeError("nothing was recorded (ue pie record)")
            return json.dumps({"mappings": state["mappings"], "active": state["active"],
                               "events": state["events"], "ended": state.get("ended"),
                               "seconds": round(time.time() - state["start"], 2)})
        running = state.get("handle") is not None
        return (f"recording, {len(state['events'])} event(s) so far" if running else "not recording")

    @_tool
    @staticmethod
    def input_map(limit: int = 400) -> str:
        """Enhanced Input: actions (value type, triggers), mapping contexts with their key mappings and what
        references them, and in PIE which contexts are active and which keys reach each action.

        Args:
            limit: Maximum number of mappings per context.

        Returns:
            JSON {actions: [{name, path, type, triggers}], contexts: [{name, path, mappings: [{action, key,
            modifiers, triggers}], referencers, active}], pie: bool, live: {action path: [keys]}}.
        """
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        actions, contexts = {}, []

        def kinds(objs, prefix):
            return [o.get_class().get_name().replace(prefix, "").lower().replace("swizzleaxis", "swizzle")
                    for o in (objs or []) if o]

        def add_action(action):
            path = action.get_path_name()
            if path not in actions:
                value_type = str(_try(lambda: action.get_editor_property("value_type"), "")).split(".")[-1].split(":")[0]
                actions[path] = {"name": action.get_name(), "path": path, "type": value_type.title().replace("_", ""),
                                 "triggers": kinds(_try(lambda: action.get_editor_property("triggers")), "InputTrigger")}
        for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputAction")):
            action = _try(lambda: unreal.load_asset(str(data.package_name)))
            if isinstance(action, unreal.InputAction):
                add_action(action)
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True, include_searchable_names=False,
            include_soft_management_references=False, include_hard_management_references=False)
        game = _game_world()
        subsystem = _try(lambda: _input_subsystem(game)) if game else None
        for data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputMappingContext")):
            imc = _try(lambda: unreal.load_asset(str(data.package_name)))
            if not isinstance(imc, unreal.InputMappingContext):
                continue
            holder = _try(lambda: imc.get_editor_property("default_key_mappings"))
            mappings = _try(lambda: holder.get_editor_property("mappings"), None) if holder else None
            if mappings is None:  # before 5.6 the mappings sat on the context itself
                mappings = _try(lambda: imc.get_editor_property("mappings"), [])
            rows = []
            for m in list(mappings)[:limit]:
                action = m.get_editor_property("action")
                if action:
                    add_action(action)
                rows.append({"action": action.get_name() if action else None,
                             "key": m.get_editor_property("key").export_text(),
                             "modifiers": kinds(m.get_editor_property("modifiers"), "InputModifier"),
                             "triggers": kinds(m.get_editor_property("triggers"), "InputTrigger")})
            package = str(data.package_name)
            refs = sorted({str(n) for n in registry.get_referencers(package, options) or []} - {package})
            active = _try(lambda: subsystem.has_mapping_context(imc)) if subsystem else None
            contexts.append({"name": imc.get_name(), "path": package, "mappings": rows, "referencers": refs,
                             "active": active is not None and active is not False, "priority": active})
        live = {}
        if subsystem:
            for path in actions:
                action = unreal.find_object(None, path)
                if action:
                    live[path] = [k.export_text() for k in _try(lambda: subsystem.query_keys_mapped_to_action(action), [])]
        return json.dumps({"actions": sorted(actions.values(), key=lambda a: a["name"].lower()),
                           "contexts": sorted(contexts, key=lambda c: c["name"].lower()),
                           "pie": bool(subsystem), "live": live})

    @_tool
    @staticmethod
    def pie_net(action: str = "status", clients: int = 1, net: str = "listen", target: int = -1, game: str = "-",
                token: str = "") -> str:
        """Multiplayer PIE: configure the next session's clients and net mode (restorable), pick which PIE world
        the other tools act on, or list the running PIE worlds with their net mode and players.

        Args:
            action: status, configure, restore, target, or game (the next PIE's game mode, in memory only).
            clients: With configure: number of clients.
            net: With configure: standalone, listen (listen server + clients) or client (dedicated server + clients).
            target: With target: the PIE instance index to act on (0 = the first world), or -1 for the default.
            game: With game: a game mode class path (/Script/Module.Class or a Blueprint's _C) for the next PIE.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was done, or one line per PIE world.
        """
        settings = unreal.find_object(None, "/Script/UnrealEd.Default__LevelEditorPlaySettings")
        keys = ["playNumberOfClients", "playNetMode", "runUnderOneProcess"]
        if action == "configure":
            _authorize(token)
            if _LEGACY:
                raise RuntimeError("needs UE 5.8+ (Epic's ToolsetLibrary)")
            mode = {"standalone": "PIE_Standalone", "listen": "PIE_ListenServer", "client": "PIE_Client"}.get(net.lower())
            if mode is None:
                raise RuntimeError("net must be standalone, listen or client")
            # What to put back afterwards, also on disk: these settings are saved to the user's per-project config,
            # so an editor killed before `pie stop` would otherwise keep starting multiplayer sessions.
            if not os.path.exists(_play_saved()):
                original = unreal.ToolsetLibrary.get_object_properties(settings, keys)
                os.makedirs(os.path.dirname(_play_saved()), exist_ok=True)
                with open(_play_saved(), "w", encoding="utf-8") as f:
                    f.write(original if isinstance(original, str) else json.dumps(original))
            unreal.ToolsetLibrary.set_object_properties(settings, json.dumps(
                {"playNumberOfClients": max(1, clients), "playNetMode": mode, "runUnderOneProcess": True}))
            return f"next PIE: {clients} client(s), {mode}"
        if action == "game":
            _authorize(token)
            maps = unreal.find_object(None, "/Script/EngineSettings.Default__GameMapsSettings")
            cls = unreal.load_class(None, game) if game.startswith("/") else None
            if cls is None:
                raise RuntimeError(f"no game mode class '{game}' (/Script/Module.Class, or /Game/BP_X.BP_X_C)")
            if getattr(sys, "_tether_game_mode", None) is None:  # the project's own default, put back by restore
                sys._tether_game_mode = str(maps.get_editor_property("global_default_game_mode").export_text())
            maps.set_editor_property("global_default_game_mode", unreal.SoftClassPath(cls.get_path_name()))
            world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
            override = _try(lambda: world.get_world_settings().get_editor_property("default_game_mode"))
            return f"next PIE: game mode {cls.get_name()}" + (
                f" (but this map's World Settings override it with {override.get_name()})" if override else "")
        if action == "restore":
            _authorize(token)
            sys._tether_pie_target = None
            saved_mode = getattr(sys, "_tether_game_mode", None)
            if saved_mode is not None:
                maps = unreal.find_object(None, "/Script/EngineSettings.Default__GameMapsSettings")
                maps.set_editor_property("global_default_game_mode", unreal.SoftClassPath(saved_mode.strip('"')))
                sys._tether_game_mode = None
                if not os.path.exists(_play_saved()):
                    return "game mode restored"
            if not os.path.exists(_play_saved()):
                return "nothing to restore"
            with open(_play_saved(), encoding="utf-8") as f:
                saved = f.read()
            # Through the settings registry, which also writes the user's config: setting the object alone left the
            # multiplayer values on disk (saved when PIE started) for an editor closed without its own config flush.
            try:
                _settings("SetSectionProperties", "Editor", "LevelEditor", "PlayIn", saved)
            except Exception:  # noqa: BLE001 - no settings section: at least fix the running editor
                unreal.ToolsetLibrary.set_object_properties(settings, saved)
            os.remove(_play_saved())
            return "play settings restored"
        if action == "target":
            sys._tether_pie_target = None if target < 0 else target
            if target >= 0 and target not in _pie_worlds():
                sys._tether_pie_target = None
                raise RuntimeError(f"no PIE world {target} (running: {sorted(_pie_worlds()) or 'none'})")
            return "tools act on " + (f"PIE world {target}" if target >= 0 else "the first PIE world")
        worlds = _pie_worlds()
        if not worlds:
            return "PIE is not running"
        current = getattr(sys, "_tether_pie_target", None)
        rows = []
        for index, world in sorted(worlds.items()):
            lib = unreal.SystemLibrary
            mode = "dedicated server" if _try(lambda: lib.is_dedicated_server(world), False) else \
                "listen server" if _try(lambda: lib.is_server(world), False) and not _try(lambda: lib.is_standalone(world), False) else \
                "standalone" if _try(lambda: lib.is_standalone(world), False) else "client"
            state = _try(lambda: unreal.GameplayStatics.get_game_state(world))
            players = len(_try(lambda: state.get_editor_property("player_array"), []) or []) if state else 0
            pawn = _try(lambda: unreal.GameplayStatics.get_player_pawn(world, 0))
            rows.append(f"{'*' if index == (current if current is not None else min(worlds)) else ' '} {index}: {mode}, "
                        f"{players} player(s)" + (f", pawn {_label(pawn)}" if pawn else ""))
        return "\n".join(rows) + "\n(* = the world ue props/actors/pie do --pie act on; ue pie target <n> to switch)"

    @_tool
    @staticmethod
    def look_stage(asset: str, action: str = "place", token: str = "") -> str:
        """Stage an asset for `ue look`: place it (a mesh, a Blueprint, or a material on a sphere) as transient
        actors high above the level with a light, or remove them again. Transient actors are never saved.

        Args:
            asset: /Game/... (or /Engine/...) asset path.
            action: place or remove.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            JSON {"center": [x, y, z], "radius": r} after place.
        """
        _authorize(token)
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        staged = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
                  if _actor_label(a).startswith("TetherLook_")]
        if staged:
            subsystem.destroy_actors(staged)
        for comp in getattr(sys, "_tether_look_hidden", None) or []:  # the level's fog, hidden while staging
            _try(lambda: comp.set_visibility(True))
        sys._tether_look_hidden = []
        if action == "remove":
            return json.dumps({"removed": len(staged)})
        obj = unreal.load_asset(asset.split(".")[0])
        if obj is None:
            raise RuntimeError(f"no asset at {asset} (ue find)")
        spot = unreal.Vector(0, 0, 500000)  # far above the level: nothing else in the shot
        if isinstance(obj, unreal.MaterialInterface):
            actor = subsystem.spawn_actor_from_object(unreal.load_asset("/Engine/BasicShapes/Sphere"), spot,
                                                      unreal.Rotator(), transient=True)
            actor.static_mesh_component.set_material(0, obj)
        elif isinstance(obj, unreal.Blueprint):
            actor = subsystem.spawn_actor_from_class(unreal.BlueprintEditorLibrary.generated_class(obj), spot,
                                                     unreal.Rotator(), transient=True)
        else:
            actor = subsystem.spawn_actor_from_object(obj, spot, unreal.Rotator(), transient=True)
        if actor is None:
            raise RuntimeError(f"cannot place a {obj.get_class().get_name()} in a level (meshes, Blueprints, materials)")
        actor.set_actor_label("TetherLook_Subject")
        center, extent = actor.get_actor_bounds(False)
        radius = max(extent.length(), 10.0)
        # A key light and a weaker one from the opposite side, so no view (front, back, sides) is left black.
        for label, offset, share in (("TetherLook_Light", (-2, -2, 2), 1.0), ("TetherLook_Fill", (2, 2, 1), 0.5)):
            light = subsystem.spawn_actor_from_class(
                unreal.PointLight, center + unreal.Vector(*(radius * o for o in offset)), unreal.Rotator(), transient=True)
            light.set_actor_label(label)
            comp = light.get_component_by_class(unreal.PointLightComponent)
            comp.set_editor_property("attenuation_radius", radius * 20)
            comp.set_editor_property("intensity", share * 150.0 * max(1.0, (radius / 100.0) ** 2))  # a fill, not a spot
            for sprite in light.get_components_by_class(unreal.BillboardComponent):  # the editor's light icon
                sprite.set_visibility(False)
        # The subject is seen inside the user's level, and dense height fog washed it out: hide the fog until
        # `remove` (not a transaction: nothing for undo, nothing saved).
        hidden = []
        for fog in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.ExponentialHeightFog):
            for comp in fog.get_components_by_class(unreal.ExponentialHeightFogComponent):
                if comp.is_visible():
                    comp.set_visibility(False)
                    hidden.append(comp)
        sys._tether_look_hidden = hidden
        return json.dumps({"center": [center.x, center.y, center.z], "radius": radius, "fog_hidden": len(hidden),
                           "class": obj.get_class().get_name()})

    @_tool
    @staticmethod
    def bp_component(blueprint: str, name: str, cls: str, parent: str = "", values: str = "{}", token: str = "") -> str:
        """Add a component to a Blueprint: of a class, attached under a parent component (default: the root),
        renamed, and with property values set on its template. Compiles the Blueprint.

        Args:
            blueprint: /Game/... Blueprint path.
            name: Component (variable) name.
            cls: Component class: a name (StaticMeshComponent, PointLightComponent) or /Script/Module.Class.
            parent: Name of the component to attach under ('' = the root component).
            values: JSON object of property name to value text, as for ue set (e.g. {"StaticMesh": "/Game/X"}).
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            What was added, and the values set.
        """
        _authorize(token)
        bp = unreal.load_asset(blueprint.split(".")[0])
        if not isinstance(bp, unreal.Blueprint):
            raise RuntimeError(f"no Blueprint at {blueprint}")
        # 'StaticMesh' names the asset class too: try the component spelling first.
        klass = unreal.load_class(None, cls) if cls.startswith("/Script/") else \
            (getattr(unreal, cls + "Component", None) if not cls.endswith("Component") else None) or getattr(unreal, cls, None)
        klass = klass.static_class() if isinstance(klass, type) and hasattr(klass, "static_class") else klass
        if not isinstance(klass, unreal.Class) or not unreal.MathLibrary.class_is_child_of(klass, unreal.ActorComponent.static_class()):
            raise RuntimeError(f"no component class '{cls}' (e.g. StaticMeshComponent, /Script/Module.MyComponent)")
        subsystem = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
        lib = unreal.SubobjectDataBlueprintFunctionLibrary
        handles = subsystem.k2_gather_subobject_data_for_blueprint(bp)
        by_name = {str(lib.get_variable_name(lib.get_data(h))): h for h in handles}
        if name in by_name:
            raise RuntimeError(f"{bp.get_name()} already has a component '{name}'")
        if parent:
            if parent not in by_name:
                raise RuntimeError(f"no component '{parent}' (have: {', '.join(n for n in by_name if n != 'None')})")
            parent_handle = by_name[parent]
        else:  # the root component, or the actor itself when there is none
            parent_handle = next((h for h in handles if lib.is_root_component(lib.get_data(h))), handles[0])
        params = unreal.AddNewSubobjectParams(parent_handle=parent_handle, new_class=klass, blueprint_context=bp)
        handle, failure = subsystem.add_new_subobject(params)
        if not lib.is_handle_valid(handle):
            raise RuntimeError(f"could not add the component: {failure}")
        subsystem.rename_subobject(handle, unreal.Text(name))
        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        rows = [f"added {name} ({klass.get_name()}) under {parent or 'the root'}"]
        requested = json.loads(values or "{}")
        # A bad object path makes the engine log a script error, which fails the whole MCP call even though the
        # component was added; check paths first and leave those out.
        for key, text in list(requested.items()):
            ref = str(text)
            # (find_object alone is not enough: a failed load leaves an empty package object behind)
            found = unreal.find_object(None, ref) if ":" in ref else _try(lambda: unreal.load_asset(ref.split(".")[0]))
            if ref.startswith("/") and (found is None or isinstance(found, unreal.Package)):
                rows.append(f"skipped {key}: nothing at {ref}")
                del requested[key]
        if requested:
            template = lib.get_object(lib.get_data(handle))
            try:
                rows.append(_RAW["set_props"](template.get_path_name(), json.dumps(requested), False, token))
            except Exception as exc:  # the component is there; say which values did not go in
                rows.append(f"but its values were not set: {exc}")
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        return "\n".join(rows)

    @_tool
    @staticmethod
    def savegame(slot: str, user: int = 0, match: str = "", limit: int = 80) -> str:
        """A save slot's SaveGame object, loaded in the editor, as 'name: Type = value' lines.

        Args:
            slot: Save slot name (Saved/SaveGames/<slot>.sav).
            user: User index.
            match: Case-insensitive regex on property names.
            limit: Maximum number of lines.

        Returns:
            A header with the SaveGame class, then its properties.
        """
        if not unreal.GameplayStatics.does_save_game_exist(slot, user):
            raise RuntimeError(f"no save slot '{slot}' (user {user})")
        save = unreal.GameplayStatics.load_game_from_slot(slot, user)
        if save is None:
            raise RuntimeError(f"slot '{slot}' did not load (a SaveGame class that no longer exists?)")
        # A text export lists every saved property, also plain UPROPERTY()s that neither Python nor Epic's
        # reflection tools can read.
        path = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()),
                            "Tether", f"savegame-{slot}.t3d")
        task = unreal.AssetExportTask()
        for key, value in (("object", save), ("filename", path), ("automated", True), ("prompt", False),
                           ("replace_identical", True)):
            task.set_editor_property(key, value)
        if not unreal.Exporter.run_asset_export_task(task):
            raise RuntimeError("the editor could not export the SaveGame as text")
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
        values, arrays = {}, {}
        for line in lines[1:]:
            m = re.match(r"^ {3}(\w+)(?:\((\d+)\))?=(.*)$", line)  # top-level properties only
            if not m:
                continue
            if m.group(2) is not None:
                arrays.setdefault(m.group(1), []).append(m.group(3))
            else:
                values[m.group(1)] = m.group(3)
        rx = re.compile(match, re.I) if match else None
        rows = [f"{k} = {_short(v, 100)}" for k, v in values.items() if not rx or rx.search(k)]
        for k, items in arrays.items():
            if not rx or rx.search(k):
                rows.append(f"{k}: {len(items)} item(s)" + (f", first {_short(items[0], 80)}" if items else ""))
        return _capped(f"slot {slot}: {save.get_class().get_name()} ({len(values) + len(arrays)} saved properties)",
                       rows, limit)

    @_tool
    @staticmethod
    def set_savegame(slot: str, values: str, user: int = 0, token: str = "") -> str:
        """Change properties of a save slot's SaveGame and write it back (the old file is kept as a .bak).

        Args:
            slot: Save slot name.
            values: JSON object of property name to value text, as for ue set.
            user: User index.
            token: Session token from Saved/Tether/editors/<pid>.token (the ue CLI sends it).

        Returns:
            One 'name: old -> new' line per property, and the backup's name.
        """
        _authorize(token)
        if not unreal.GameplayStatics.does_save_game_exist(slot, user):
            raise RuntimeError(f"no save slot '{slot}' (user {user})")
        save = unreal.GameplayStatics.load_game_from_slot(slot, user)
        if save is None:
            raise RuntimeError(f"slot '{slot}' did not load")
        try:
            result = _RAW["set_props"](save.get_path_name(), values, True, token)  # pie=True: no editor transaction
        except RuntimeError as exc:
            if "has no property" in str(exc):
                raise RuntimeError(f"{exc}. Only properties exposed to Blueprints or the editor can be set from Python; "
                                   "a plain UPROPERTY() is read-only here (ue savegame shows it)") from None
            raise
        folder = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "SaveGames")
        source = os.path.join(folder, f"{slot}.sav")
        backup = None
        if os.path.isfile(source):  # only now that the change is made in memory
            import shutil
            import time as _time
            backup = os.path.join(folder, f"{slot}.sav.bak-{_time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(source, backup)
        if not unreal.GameplayStatics.save_game_to_slot(save, slot, user):
            raise RuntimeError(f"could not write slot '{slot}'" + (f" (the old file is in {os.path.basename(backup)})" if backup else ""))
        lines = result.splitlines()[1:]
        return "\n".join([f"{slot} ({save.get_class().get_name()}) saved"] + lines
                         + ([f"backup: Saved/SaveGames/{os.path.basename(backup)}"] if backup else []))

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
        # Compile first: a reload of a broken file fails on the next tick, where nobody sees it, and leaves the
        # old code running. Report the error now instead and keep the current code.
        folder = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    try:
                        compile(f.read(), name, "exec")
                    except SyntaxError as exc:
                        raise RuntimeError(f"not reloaded: {name}:{exc.lineno}: {exc.msg}") from None
        _schedule_reload()
        return "reload scheduled"


registration = Registration([TetherCore]) if toolset_registry else None
