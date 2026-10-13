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

# Tether's own edits, newest last, with the undo-history depth right after each; stashed on sys like below.
_UNDO = getattr(sys, "_tether_undo", None) or []
sys._tether_undo = _UNDO

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
    if not path:
        return "None"
    package, dot, name = path.rpartition(".")
    if dot and ":" not in name and package.rsplit("/", 1)[-1] == name:  # /Game/X/A.A -> /Game/X/A
        return package
    return path.split(":PersistentLevel.", 1)[-1]


def _label(obj):
    if obj.get_name().startswith("Default__"):  # a class default object: its "actor label" is meaningless
        return _short_path(obj.get_path_name())
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
    _LEVELS[level] = {a.get_path_name(): (a.get_actor_label(), a.get_class().get_name(), _pose_key(a))
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


def _pie_point(ref, pawn, controller):
    nums = _numbers(ref)
    if nums is not None:
        if len(nums) != 3:
            raise RuntimeError(f"expected x,y,z, got '{ref}'")
        return unreal.Vector(*nums)
    obj = _pie_object(ref, pawn, controller)
    actor = obj if isinstance(obj, unreal.Actor) else obj.get_owner()
    return actor.get_actor_bounds(False)[0]


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
    # Actions can also be created in C++ as subobjects of the character, e.g. CreateDefaultSubobject<UInputAction>.
    action = _try(lambda: pawn.get_editor_property(name))
    if isinstance(action, unreal.InputAction):
        return action
    action = unreal.find_object(pawn, name)
    if isinstance(action, unreal.InputAction):
        return action
    names = sorted(str(a.asset_name) for a in
                   registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/EnhancedInput", "InputAction")))
    raise RuntimeError(f"no InputAction '{name}' (assets: {', '.join(names[:12])}; or a property/subobject of the pawn)")


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


_COMPARE = re.compile(r"^(?P<lhs>\S+)\s*(?P<op>==|!=|<=|>=|=|<|>|~)\s*(?P<rhs>.*)$")


def _pie_expect(expr, pawn, controller):
    """'<target>.<prop> <op> <value>': (ok, actual text)."""
    match = _COMPARE.match(expr.strip())
    if not match:
        raise RuntimeError(f"expected '<target>.<property> <op> <value>' (op: = != < > <= >= ~), got '{expr}'")
    lhs, op, rhs = match.group("lhs"), match.group("op"), match.group("rhs").strip()
    target, _, prop = lhs.rpartition(".")
    if not target:
        raise RuntimeError(f"'{lhs}': give target.property, e.g. player.Health or Door_1.bIsOpen")
    obj = _pie_object(target, pawn, controller)
    if prop.lower() in ("location", "rotation"):
        value = _actor_pose(obj if isinstance(obj, unreal.Actor) else obj.get_owner())[prop.lower()]
    else:
        schema = _reflect(unreal.ToolsetLibrary.list_struct_properties(obj.get_class()), obj)
        name = {n.lower(): n for n in schema}.get(prop.lower())
        if name is None:
            raise RuntimeError(f"{_label(obj)} has no property '{prop}'")
        value = _reflect(unreal.ToolsetLibrary.get_object_properties(obj, [name]), obj).get(name)
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
        raise RuntimeError("needs Epic's ConfigSettingsToolset (UE 5.8+)")
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

    def error(self):
        return str(_try(lambda: self.sc.last_error_msg(), "")) or "see ue log"

    def prepare(self, files):
        """Make existing files writable (check them out). Returns (problems, new files, notes)."""
        problems, new, notes = [], [], []
        for path in files:
            if not path:
                continue
            if not os.path.isfile(path):
                new.append(path)
                continue
            if self.checkouts:
                state = self.sc.query_file_state(path, True)
                if getattr(state, "is_checked_out_other", False):
                    problems.append(f"{os.path.basename(path)} is checked out by {state.checked_out_other or 'someone else'}")
                    continue
                if getattr(state, "is_source_controlled", False) and not getattr(state, "is_current", True):
                    notes.append(f"{os.path.basename(path)} is not at the latest revision (sync it)")
                if getattr(state, "can_check_out", False) and not getattr(state, "is_checked_out", False):
                    if self.sc.check_out_file(path, True):
                        notes.append(f"checked out {os.path.basename(path)}")
                    else:
                        problems.append(f"could not check out {os.path.basename(path)}: {self.error()}")
                        continue
            if not os.access(path, os.W_OK):
                problems.append(f"{os.path.basename(path)} is read-only" + (
                    "" if self.checkouts else " (not checked out? enable revision control in the editor, or check "
                    "it out first)"))
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
        raise RuntimeError(f"no ground below {actor.get_actor_label()} (nothing with collision under it)")
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
    return old_z, loc.z, (landed.get_actor_label() if landed else "?")


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
                        owners.append(f"{obj.get_actor_label()}.{comp.get_name()}")
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
        game = _game_world()  # checked first: get_editor_world() logs an error during PIE
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
        schema = _reflect(unreal.ToolsetLibrary.list_struct_properties(cls), obj)
        names = list(schema)
        if match:
            rx = re.compile(match, re.I)
            names = [n for n in names if rx.search(n) or rx.search(_type_name(schema[n]))]
        values = _reflect(unreal.ToolsetLibrary.get_object_properties(obj, names), obj) if names else {}
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
        # Skip values that are already set: a write that changes nothing opens an empty transaction, which the
        # editor drops, and `ue undo` would then line up with the wrong entry.
        unchanged = [n for n in new if n in old and isinstance(new[n], (int, float, bool, dict)) and _took(new[n], old[n])
                     and (not isinstance(new[n], dict) or all(isinstance(v, (int, float, bool)) for v in new[n].values()))]
        for name in unchanged:
            del new[name]
        if not new:
            return _label(obj) + (" (PIE)" if pie else "") + "\n(no change: " + ", ".join(unchanged) + " already set)"

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
        # Simulate-In-Editor possesses a SpectatorPawn and shows the simulation in the level viewport
        simulate = pie and (cam is None or isinstance(unreal.GameplayStatics.get_player_pawn(game, 0), unreal.SpectatorPawn))
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
    def spawn(what: str, at: str = "0,0,0", rot: str = "", label: str = "", ground: bool = False, align: bool = False,
              token: str = "") -> str:
        """Place an actor in the edited level (undoable): from a class (PointLight, /Script/Engine.TargetPoint,
        a Blueprint's generated class) or an asset (a StaticMesh, a Blueprint, ...).

        Args:
            what: Class name, /Script/... class path, or /Game/... asset path.
            at: Location 'x,y,z'.
            rot: Rotation 'pitch,yaw,roll'.
            label: Actor label to give it.
            ground: Drop it onto the first surface below `at` (its bounds' bottom on the ground).
            align: With ground: tilt it to the surface normal.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
        if what.startswith(("/Game/", "/Engine/")):  # an asset: mesh, Blueprint, ...
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
        _record_undo(before, f"spawn {actor.get_actor_label()}")
        loc = actor.get_actor_location()
        return f"spawned {actor.get_actor_label()} ({actor.get_class().get_name()}) at ({loc.x:g}, {loc.y:g}, {loc.z:.6g})" + \
            (f" on {landed}" if landed and not landed.startswith("not grounded") else f" ({landed})" if landed else "")

    @_tool
    @staticmethod
    def drop(targets: str, align: bool = False, token: str = "") -> str:
        """Put actors on the ground: move each straight down (or up, if buried) so the bottom of its bounds rests
        on the first blocking surface below it. One undoable step.

        Args:
            targets: Comma-separated actor labels or names.
            align: Also tilt each to the surface normal.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

        Returns:
            One line per actor: old -> new height and what it landed on.
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
        with unreal.ScopedEditorTransaction("ue drop"):
            for actor in actors:
                actor.modify()
                if actor.root_component:
                    actor.root_component.modify()
                try:
                    old_z, new_z, landed = _ground_actor(actor, align)
                    rows.append(f"{actor.get_actor_label()}: z {old_z:.6g} -> {new_z:.6g} (on {landed})")
                except RuntimeError as exc:
                    rows.append(f"{actor.get_actor_label()}: {exc}")
        _record_undo(before, "drop " + ", ".join(a.get_actor_label() for a in actors))
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
    def delete(targets: str, token: str = "") -> str:
        """Delete actors from the edited level (undoable).

        Args:
            targets: Comma-separated actor labels or names.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
        labels = [a.get_actor_label() for a in actors]
        before = unreal.ToolsetLibrary.get_active_undo_count() if not _LEGACY else 0
        with unreal.ScopedEditorTransaction("ue delete"):
            ok = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(actors)
        if not ok:
            raise RuntimeError("the editor refused to delete them")
        _record_undo(before, "delete " + ", ".join(labels))
        return "deleted " + ", ".join(labels)

    @_tool
    @staticmethod
    def duplicate(target: str, offset: str = "100,0,0", count: int = 1, token: str = "") -> str:
        """Duplicate an actor (undoable), each copy shifted by `offset` from the previous one.

        Args:
            target: Actor label or name.
            offset: Shift per copy 'x,y,z'.
            count: Number of copies.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
        with unreal.ScopedEditorTransaction(f"ue duplicate {source.get_actor_label()}"):  # all copies, one undo step
            for _ in range(max(1, count)):
                copy = subsystem.duplicate_actor(previous, None, step)
                if copy is None:
                    break
                copies.append(copy)
                previous = copy
        if not copies:
            raise RuntimeError("the editor refused to duplicate it")
        _record_undo(before, f"duplicate {source.get_actor_label()} x{len(copies)}")
        return "\n".join(f"{c.get_actor_label()} at ({c.get_actor_location().x:g}, {c.get_actor_location().y:g}, "
                         f"{c.get_actor_location().z:g})" for c in copies)

    @_tool
    @staticmethod
    def mv(asset: str, dest: str, token: str = "") -> str:
        """Move or rename an asset and fix up what referenced it (not undoable; version control is the undo).

        Args:
            asset: /Game/... asset path.
            dest: New /Game/... path (folder/Name), or a folder ending in '/' to keep the name.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
        problems, _, notes = vcs.prepare([source_file])
        if problems:
            raise RuntimeError("; ".join(problems))
        dirty_before = set(sum(_dirty_packages(), []))
        if not library.rename_asset(source, target):
            raise RuntimeError("the rename failed (see ue log)")
        leftover = unreal.load_asset(source) if library.does_asset_exist(source) else None
        if leftover is not None and leftover.get_class().get_name() == "ObjectRedirector":
            unreal.AssetToolsHelpers.get_asset_tools().fixup_referencers([leftover])  # repoint, drop redirector
            notes.append("redirector fixed up")
        notes += vcs.added([_package_file(target, source_file.endswith(".umap"))]) + vcs.deleted([source_file])
        maps, content = _dirty_packages()
        touched = [p for p in maps + content if p != target and p not in dirty_before]
        if touched:
            notes.append(f"{len(touched)} referencing package(s) updated but unsaved: " + ", ".join(touched[:5])
                         + " (ue save)")
        return f"moved {source} -> {target}" + (" (" + "; ".join(notes) + ")" if notes else "")

    @_tool
    @staticmethod
    def rm(asset: str, force: bool = False, token: str = "") -> str:
        """Delete an asset, refusing while anything references it (not undoable; version control is the undo).

        Args:
            asset: /Game/... asset path.
            force: Delete even if referenced (the references break).
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
            added = [now[p].get_actor_label() for p in now if p not in base]
            removed = [base[p][0] for p in base if p not in now]
            moved = [now[p].get_actor_label() for p in now if p in base and _pose_key(now[p]) != base[p][2]]
            dirty_actor = {p.rsplit("/", 1)[-1] for p in actor_packages}
            changed = [now[p].get_actor_label() for p in now if p in base and now[p].get_actor_label() not in moved
                       and now[p].get_outermost().get_name().rsplit("/", 1)[-1] in dirty_actor]
            parts = [f"{n} {kind} ({cap(names)})" for kind, names in
                     (("added", added), ("removed", removed), ("moved", moved), ("changed", changed)) for n in [len(names)] if n]
            lines.append(f"  level {level} since load/save: " + ("; ".join(parts) if parts else
                         "no actors added, removed or moved (other edits: see the map package)"))
        elif level and base is None:
            lines.append(f"  level {level}: no baseline yet (it is taken when a map loads)")
        return "\n".join(lines)

    @_tool
    @staticmethod
    def save(paths: str = "", token: str = "") -> str:
        """Save unsaved packages: the given ones, or all of them. Untitled /Temp maps are skipped (saving them
        would open a Save As dialog).

        Args:
            paths: Comma-separated package paths ('' saves every unsaved package).
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
        problems, new_files, notes = vcs.prepare(list(files.values()))
        blocked = [p for p in todo if any(os.path.basename(files[p]) in problem for problem in problems)]
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
            out.append(f"  {asset.blueprint_get_size_x()} x {asset.blueprint_get_size_y()}, "
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
                out.append(f"+ {actor.get_actor_label()} ({actor.get_class().get_name()}){where(actor)}")
            for name in [n for n in old_actors if n not in new_actors]:
                actor = old_actors[name]
                out.append(f"- {actor.get_actor_label()} ({actor.get_class().get_name()})")
            for name in [n for n in new_actors if n in old_actors]:
                lines = []
                _diff_parts(_actor_parts(old_actors[name]), _actor_parts(new_actors[name]), roots, lines)
                if lines:
                    out.append(f"~ {new_actors[name].get_actor_label()} ({new_actors[name].get_class().get_name()})")
                    out.extend("  " + line for line in lines)
        else:
            _diff_props("properties", a, b, roots, out, limit)
        if not out:
            return ("no actors added, removed or changed in the map package itself" if isinstance(b, unreal.World)
                    else "no differences in reflected properties")
        return "\n".join(out[:limit]) + (f"\n... +{len(out) - limit} more lines" if len(out) > limit else "")

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
            errors = [str(e) for e in lib.recompile_material(base)] if isinstance(base, unreal.Material) else []
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
            point = _pie_point(where, pawn, controller)
            if offset:
                point = point + unreal.Vector(*(_numbers(offset) or [0, 0, 0]))
            pawn.set_actor_location(point, False, True)
            loc = pawn.get_actor_location()
            detail = f"player at ({loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f})"
        elif verb == "look":
            camera = unreal.GameplayStatics.get_player_camera_manager(game, 0)
            eye = camera.get_camera_location() if camera else pawn.get_actor_location()
            rotation = unreal.MathLibrary.find_look_at_rotation(eye, _pie_point(rest, pawn, controller))
            controller.set_control_rotation(rotation)
            detail = f"looking pitch {rotation.pitch:.0f} yaw {rotation.yaw:.0f}"
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
        elif verb == "call":
            parts = rest.split(None, 2)
            if len(parts) < 2:
                raise RuntimeError("call <target> <Function> [json args]")
            obj = _pie_object(parts[0], pawn, controller)
            args = json.loads(parts[2]) if len(parts) > 2 else []
            args = tuple(args) if isinstance(args, list) else (args,)
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
        elif verb == "exec":
            unreal.SystemLibrary.execute_console_command(game, rest)
            detail = "ran"
        else:
            raise RuntimeError(f"unknown step '{verb}' (at, look, move, near, tap, hold, release, call, expect, "
                               "exists, gone, exec; the CLI adds wait, until, press, log, shot)")
        return json.dumps({"ok": ok, "detail": detail})

    @_tool
    @staticmethod
    def nav(action: str = "status", start: str = "", end: str = "", match: str = "", pie: bool = False,
            limit: int = 40) -> str:
        """Navigation: whether the level has a usable navmesh (and why not), paths between points, and which
        actors stand off the navmesh.

        Args:
            action: status, path (start -> end) or check (actors matching `match`).
            start: Path start: 'x,y,z' or an actor.
            end: Path end: 'x,y,z' or an actor.
            match: With check: regex on actor label and class (default: player starts, spawns, patrol points).
            pie: Use the PIE world (runtime-generated navmesh) instead of the editor level.
            limit: Maximum number of actor lines for check.

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
                out.append(f"  nav data {nav_data.get_actor_label()} ({nav_data.get_class().get_name()}, "
                           f"runtime generation {generation}{agent})")
            if not data:
                out.append("  no navigation data actor (a NavMeshBoundsVolume creates RecastNavMesh-Default)")
            volumes = list(actors(world, unreal.NavMeshBoundsVolume))
            empty = 0
            for volume in volumes:
                centre, extent = volume.get_actor_bounds(False)
                found = navsys.get_random_point_in_navigable_radius(world, centre, max(extent.x, extent.y))
                empty += found is None
                out.append(f"  bounds {volume.get_actor_label()}: {extent.x * 2 / 100:.0f} x {extent.y * 2 / 100:.0f} x "
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
            path = navsys.find_path_to_location_synchronously(world, a, b)
            if path is None or not path.is_valid() or not path.path_points:
                notes = []
                for label, point in (("start", a), ("end", b)):
                    if navsys.project_point_to_navigation(world, point, None, None, unreal.Vector(50, 50, 250)) is None:
                        near = navsys.project_point_to_navigation(world, point, None, None, unreal.Vector(1000, 1000, 1000))
                        notes.append(f"{label} is off the navmesh" +
                                     (f" ({point.distance(near):.0f} cm from it)" if near is not None else " (none within 10 m)"))
                return f"no path ({straight:.0f} cm straight line)" + (": " + "; ".join(notes) if notes else "")
            length = path.get_path_length()
            return (f"{'partial path' if path.is_partial() else 'path'}: {length:.0f} cm over {len(path.path_points)} "
                    f"points (straight line {straight:.0f} cm)" +
                    (f"; it ends {path.path_points[-1].distance(b):.0f} cm short" if path.is_partial() else ""))
        if action == "check":
            rx = re.compile(match or r"PlayerStart|Spawn|Patrol|Waypoint|TargetPoint", re.I)
            rows, off = [], 0
            for actor in actors(world, unreal.Actor):
                label = actor.get_actor_label()
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
    def umg(click: str = "", token: str = "") -> str:
        """UMG widgets on the PIE viewport as a compact tree: texts, buttons (by their label), text boxes and
        check boxes. The fallback for `ue ui` when Epic's SlateInspectorToolset is not enabled: raw Slate
        widgets are invisible from Python. With click, fire the OnClicked of the button whose label or name
        matches (case-insensitive).

        Args:
            click: Button label or widget name to click ('' lists).
            token: Session token from Saved/Tether/session.json (needed only with click).

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
        for widget in unreal.ObjectIterator(unreal.Widget):
            owner = _try(lambda: lib.get_outer_user_widget(widget))
            if owner in tops:
                children.setdefault(widget.get_parent(), []).append(widget)
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

        def walk(widget, depth):
            if not widget.is_visible():
                return
            kind = None
            if isinstance(widget, unreal.Button):
                kind, text = "button", label(widget)
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
            match = [b for t, b in buttons if t.lower() == key or b.get_name().lower() == key] or \
                [b for t, b in buttons if key in t.lower()]
            if not match:
                raise RuntimeError(f"no visible button '{click}' (buttons: {', '.join(t or b.get_name() for t, b in buttons[:10])})")
            if not match[0].get_is_enabled():
                raise RuntimeError(f"'{click}' is disabled")
            match[0].on_clicked.broadcast()
            return f"clicked {match[0].get_name()}"
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
            controllers = [c for c in controllers if key in (c.get_actor_label().lower(), c.get_name().lower())
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
                rows.append(f"{_label(pawn) if pawn else controller.get_actor_label()} at {where}: {f['move']}"
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
            lines.append(f"  controller {controller.get_actor_label()} ({controller.get_class().get_name()})")
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
    def pie_net(action: str = "status", clients: int = 1, net: str = "listen", target: int = -1, token: str = "") -> str:
        """Multiplayer PIE: configure the next session's clients and net mode (restorable), pick which PIE world
        the other tools act on, or list the running PIE worlds with their net mode and players.

        Args:
            action: status, configure, restore or target.
            clients: With configure: number of clients.
            net: With configure: standalone, listen (listen server + clients) or client (dedicated server + clients).
            target: With target: the PIE instance index to act on (0 = the first world), or -1 for the default.
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
            if getattr(sys, "_tether_play_saved", None) is None:  # what to put back afterwards
                sys._tether_play_saved = unreal.ToolsetLibrary.get_object_properties(settings, keys)
            unreal.ToolsetLibrary.set_object_properties(settings, json.dumps(
                {"playNumberOfClients": max(1, clients), "playNetMode": mode, "runUnderOneProcess": True}))
            return f"next PIE: {clients} client(s), {mode}"
        if action == "restore":
            _authorize(token)
            saved = getattr(sys, "_tether_play_saved", None)
            sys._tether_pie_target = None
            if saved is None:
                return "nothing to restore"
            unreal.ToolsetLibrary.set_object_properties(settings, saved)
            sys._tether_play_saved = None
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

        Returns:
            JSON {"center": [x, y, z], "radius": r} after place.
        """
        _authorize(token)
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        staged = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
                  if a.get_actor_label().startswith("TetherLook_")]
        if staged:
            subsystem.destroy_actors(staged)
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
        light = subsystem.spawn_actor_from_class(unreal.PointLight, center + unreal.Vector(-radius * 2, -radius * 2,
                                                                                            radius * 2),
                                                 unreal.Rotator(), transient=True)
        light.set_actor_label("TetherLook_Light")
        comp = light.get_component_by_class(unreal.PointLightComponent)
        comp.set_editor_property("attenuation_radius", radius * 20)
        comp.set_editor_property("intensity", 150.0 * max(1.0, (radius / 100.0) ** 2))  # a fill, not a spotlight
        for sprite in light.get_components_by_class(unreal.BillboardComponent):  # the editor's light icon
            sprite.set_visibility(False)
        return json.dumps({"center": [center.x, center.y, center.z], "radius": radius,
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
            token: Session token from Saved/Tether/session.json (the ue CLI sends it).

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
