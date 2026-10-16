"""Thin commands over one editor tool each: tools, call, py, props, set, spawn and friends."""
import contextlib
import difflib
import fnmatch
import json
import os
import re
import sys
import time

from .core import (
    CliError, GATED, LEVELS, TETHER, UE_SCRIPT, compact_error, editor_process, editor_token, emit, filter_log, human_bytes, log_end, log_ignore_entries,
    log_ignore_path, log_path, pid_alive, PY_OUTPUT, summarize_log, write_json,
    port_open,
    read_json, read_new_log, short, state_dir, unwrap,
)
from .fabfiles import fab_origin, listing_url, project_fab
from .share import lease_note
from .transport import catalog, fill_args, py_json, resolve_toolset, schema, signature


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
        call_args["token"] = editor_token(ctx.project, ctx.beacon)
    text, is_error = ctx.mcp.call(toolset, name, call_args)
    if is_error:
        raise CliError(compact_error(text))
    emit(args, unwrap(text))
    return 0


PY_PITFALLS = [  # (pattern in `ue py` code, what goes wrong): engine behaviour that cost real sessions time
    (re.compile(r"build_from_static_mesh_descriptions\s*\("),
     "StaticMesh.build_from_static_mesh_descriptions on a mesh that already has render data crashes the editor "
     "(\"FRenderResource was deleted without being released first\"); building into a new mesh is fine"),
    (re.compile(r"(?s)(AnimSequence|additive_anim_type|ref_pose_type).*set_editor_properties\s*\(|"
                r"set_editor_properties\s*\((?:(?!\)\s*$).)*(additive_anim_type|ref_pose_type|ref_frame_index)"),
     "set_editor_properties (plural) on an AnimSequence posts a change event without the property, so the "
     "sequence is not recompressed: marking it additive keeps the non-additive compressed data until a restart. "
     "Use set_editor_property (singular) for each additive setting"),
    (re.compile(r"MP_CUSTOM_DATA[01]"),
     "unreal.MaterialProperty has no MP_CUSTOM_DATA0/1 (clear coat) in Python: build clear coat through a Make "
     "Material Attributes node and set the material's use_material_attributes"),
    (re.compile(r"(?s)Collection(?:Scalar|Vector)Parameter.*\.parameter_name\b(?!\s*\()"),
     "CollectionScalarParameter/CollectionVectorParameter.parameter_name raises; read and write it with "
     "get_editor_property('parameter_name') / set_editor_property(...)"),
    (re.compile(r"(?s)MaterialParameterCollection.*['\"](?:scalar|vector)_parameters['\"]"),
     "rewriting a Material Parameter Collection's parameter array gives the parameters new IDs: every material "
     "compiled against the old ones reads 0 for them, silently. Edit the existing entries' values in place, or "
     "recompile and check the materials that use the collection (ue lint --mpc)"),
    (re.compile(r"MaterialExpressionFontSampleParameter"),
     "a FontSampleParameter compiles only with a default font set (set_editor_property('font', ...))"),
]
EDITOR_WORLD_API = re.compile(r"EditorActorSubsystem|EditorLevelLibrary|LevelEditorSubsystem|get_editor_world|"
                              r"get_all_level_actors|spawn_actor_from_(?:class|object)|ScopedEditorTransaction")
PIE_PY_NOTE = ("PIE is running: Editor Scripting calls act on the edited level, not the game (and some refuse during "
               "PIE), so a script can take the wrong branch. For the game world: `ue pie run -c \"...\"` "
               "(tether.world(), tether.player()); or `ue pie stop` first")
STRUCT_LOOP = re.compile(r"for\s+(\w+)\s+in\s+[^\n]*get_editor_property\([^\n]*:\s*\n((?:[ \t]+[^\n]*\n?)+)")


def py_pitfalls(code):
    """Warnings for known engine traps in code about to run in the editor."""
    notes = [message for rx, message in PY_PITFALLS if rx.search(code)]
    for m in STRUCT_LOOP.finditer(code):
        name, body = re.escape(m.group(1)), m.group(2)
        # the edited copy is lost only when nothing uses it afterwards (appended, assigned back, passed on)
        used = re.search(r"\b" + name + r"\b(?!\s*\.)", body)
        if re.search(r"\b" + name + r"\.set_editor_property\(", body) and not used:
            notes.append(f"'{m.group(1)}' comes from iterating an unreal.Array: elements of struct arrays are copies, "
                         "so set_editor_property on them changes nothing; set the edited element back by index "
                         "(arr[i] = item) and write the array back")
            break
    return notes


def cmd_py(ctx, args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            code = f.read()
        # Words after the file are the script's arguments, as with `python script.py clean`.
        path = os.path.abspath(args.file)
        argv = [os.path.basename(args.file)] + list(args.code)
        code = (f"import sys as _tether_sys\n_tether_sys.argv = {argv!r}\n__file__ = {path!r}\ndel _tether_sys\n"
                + code.lstrip("﻿"))
    elif args.code == ["-"]:
        code = sys.stdin.read()
    else:
        code = " ".join(args.code)
    in_pie = (ctx.beacon or {}).get("pie") and EDITOR_WORLD_API.search(code)
    for note in py_pitfalls(code) + [lease_note(ctx.project)] + ([PIE_PY_NOTE] if in_pie else []):
        if note:
            print(note if note.startswith("note: ") else f"note: {note}", file=sys.stderr)
    start = log_end(ctx.project)
    try:
        out = ctx.tether("py", code=code.lstrip("﻿"))  # PowerShell pipes a BOM
    except CliError as exc:
        if exc.code == 4 and str(exc).startswith("timed out"):
            raise CliError(f"`ue py` stopped waiting after {ctx.args.timeout:g}s, but the script is still running in "
                           "the editor (Python holds its game thread until it ends): do not run it again. `ue wait "
                           "idle` returns when it has finished; `ue py --timeout 600 ...` waits longer", 4) from None
        error, marker, printed = str(exc).partition(f"\n{PY_OUTPUT}\n")
        if marker:  # what the script printed before it raised, with its newlines, then the error
            print(printed.rstrip("\n"))
        for line in engine_complaints(ctx, start):
            print(f"engine: {line}", file=sys.stderr)
        hint = property_name_hint(ctx, error)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        if marker:
            raise CliError(error, exc.code) from None
        raise
    if out not in ("", None):  # output that is itself JSON comes back decoded
        print(out.rstrip("\n") if isinstance(out, str) else json.dumps(out))
    for line in engine_complaints(ctx, start):  # why a call returned None, said only in the log
        print(f"engine: {line}", file=sys.stderr)
    return 0


MISSING_PROPERTY = re.compile(r"Failed to find property '[^']*' for attribute '(\w+)' on '(\w+)'")
# The class's docstring lists its editor properties by their Python names: "- ``order`` (bool): ..."
PYTHON_PROPERTY_NAMES = ("import re as _r, json as _j\n"
                         "_t = getattr(unreal, {cls!r}, None)\n"
                         "_n = _r.findall(r'^- ``(\\w+)`` \\(', (_t.__doc__ or '') if _t else '', _r.M)\n"
                         "_b = []\n"
                         "if {cls!r}.endswith('_C'):  # a Blueprint class: its variables by their own names\n"
                         "    _reg = unreal.AssetRegistryHelpers.get_asset_registry()\n"
                         "    for _a in _reg.get_assets_by_class(unreal.TopLevelAssetPath('/Script/Engine', 'Blueprint'), True):\n"
                         "        if str(_a.asset_name) == {cls!r}[:-2]:\n"
                         "            _c = unreal.load_asset(str(_a.package_name)).generated_class()\n"
                         "            _b = list(_j.loads(unreal.ToolsetLibrary.list_struct_properties(_c)))\n"
                         "            break\n"
                         "_j.dumps({{'python': _n, 'blueprint': _b}})")


PROTECTED_PROPERTY = re.compile(r"Property '(\w+)' for attribute '\w+' on '(\w+)' is protected and cannot be read")


WORLD_SUBSYSTEM = re.compile(r"has no attribute '(?:SubsystemBlueprintLibrary|get_world_subsystem|"
                             r"get_world_subsystem_by_class)'")
MISSING_ATTRIBUTE = re.compile(r"AttributeError: '(\w+)' object has no attribute '(\w+)'")
UFUNCTION_EXISTS = ("_c = getattr(unreal, {cls!r}, None)\n"
                    "_c = _c.static_class() if _c is not None and hasattr(_c, 'static_class') else None\n"
                    "print(bool(_c and unreal.find_object(_c, {fn!r})))")


def property_name_hint(ctx, error):
    """For "Failed to find property": the class's Python property names closest to the one asked for. The
    pythonizer drops a bool's b prefix, then an In prefix (bInOrder is `order`), so the C++ name is no guide.
    For a protected one: `ue props` reads it (through the engine's own export)."""
    if WORLD_SUBSYSTEM.search(error):
        return ("5.8's Python has no SubsystemBlueprintLibrary or get_world_subsystem: "
                "tether.world_subsystem(unreal.MyWorldSubsystem) returns the editor world's (or PIE's, while it runs; "
                "pie=False for the editor's), then call its UFUNCTIONs on it")
    p = PROTECTED_PROPERTY.search(error)
    if p:
        return (f"{p.group(2)}.{p.group(1)} is protected from Python, but `ue props <object> --match {p.group(1)}` "
                "(--pie in PIE) reads it, and `ue pie watch Actor.{0}` samples it".format(p.group(1)))
    a = MISSING_ATTRIBUTE.search(error)
    if a:  # a UFUNCTION Python has no name for (CallInEditor without BlueprintCallable)
        cls, attr = a.groups()
        camel = "".join(part[:1].upper() + part[1:] for part in attr.split("_"))
        try:
            found = str(ctx.tether("py", code=UFUNCTION_EXISTS.format(cls=cls, fn=camel))).strip().endswith("True")
        except CliError:
            found = False
        if found:
            return (f"{cls}.{camel} is a UFUNCTION Python has no name for (CallInEditor or not BlueprintCallable): "
                    f"call it with obj.call_method('{camel}') (arguments as a tuple: call_method('{camel}', (a, b)))")
    m = MISSING_PROPERTY.search(error)
    if not m:
        return None
    attr, cls = m.groups()
    try:
        names = py_json(ctx, PYTHON_PROPERTY_NAMES.format(cls=cls))
    except (CliError, ValueError, IndexError):
        return None
    names = names if isinstance(names, dict) else {"python": names or [], "blueprint": []}
    close = difflib.get_close_matches(attr, names.get("python") or [], n=3, cutoff=0.6)
    # A Blueprint variable resolves by its own name, spaces and all ('Time of Day'), not as time_of_day
    squash = {re.sub(r"[\s_]", "", n).lower(): n for n in names.get("blueprint") or [] if n not in close}
    spaced = [squash[k] for k in difflib.get_close_matches(re.sub(r"[\s_]", "", attr).lower(), list(squash), n=3,
                                                           cutoff=0.75)]
    if spaced:
        return (f"{cls} is a Blueprint class: its variables resolve by their own names, e.g. "
                + ", ".join(f"get_editor_property({n!r})" for n in spaced))
    if not close:
        return None
    return (f"{cls}'s Python property names closest to '{attr}': {', '.join(close)} (Python names drop a bool's "
            "b prefix and then an In prefix: bInOrder is `order`)")


SCRIPTING_LOGS = {"LogEditorScripting", "LogUtils", "LogPython", "LogBlueprintEditorLib", "LogEditorAssetSubsystem",
                  "LogAssetTools", "LogUObjectGlobals"}


def engine_complaints(ctx, start):
    """Warnings and errors the editor's scripting libraries logged while a `ue py` call ran: Editor Scripting
    Utilities and friends often return None and say why only in the log (e.g. refusing to run during PIE)."""
    try:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, CliError):
        return []
    return [line for line in filter_log(lines, level="Warning", ignore=False)
            if line.split(" ", 2)[1].rstrip(":") in SCRIPTING_LOGS][:10]


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
    if getattr(args, "assets", None):
        return set_assets(ctx, args)
    if not args.target:
        raise CliError("ue set <target> Name=value ... (or --assets <folder> for many assets)")
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


def cmd_spawn(ctx, args):
    if args.pie:
        return spawn_in_pie(ctx, args)
    print(ctx.tether("spawn", what=args.what, at=args.at, rot=args.rot or "", label=args.label or "",
                     ground=args.ground, align=args.align))
    return 0


# Python cannot spawn into the game world (GameplayStatics' deferred spawn is not exposed), but the cheat manager's
# `summon <class>` can, in front of the player: summon it, find it, then move it into place.
PIE_SPAWN_CLASS = """
import json
what = {what!r}
mesh = None
if what.startswith("/") and not what.startswith("/Script/"):
    asset = unreal.load_asset(what)
    if asset is None:
        raise RuntimeError(f"no asset at {{what}} (ue find)")
    if isinstance(asset, unreal.StaticMesh):
        cls, mesh = unreal.StaticMeshActor, asset.get_path_name()
    elif isinstance(asset, unreal.Blueprint):
        cls = asset.generated_class()
    else:
        raise RuntimeError(f"{{what}} is a {{asset.get_class().get_name()}}: give a StaticMesh, a Blueprint or a class")
else:
    cls = unreal.load_class(None, what) if what.startswith("/Script/") else getattr(unreal, what, None)
    if cls is None:
        raise RuntimeError(f"no class '{{what}}' (try a /Script/Module.Class path)")
if isinstance(cls, type):  # a Python wrapper type (unreal.StaticMeshActor), not the UClass
    cls = cls.static_class()
w = tether.world()
before = [a.get_name() for a in unreal.GameplayStatics.get_all_actors_of_class(w, cls)]
print(json.dumps({{"path": cls.get_path_name(), "mesh": mesh, "before": before}}))
"""
PIE_SPAWN_PLACE = """
import json
from tether import toolsets
w = tether.world()
cls = unreal.load_class(None, {path!r})
new = [a for a in unreal.GameplayStatics.get_all_actors_of_class(w, cls) if a.get_name() not in {before!r}]
if not new:
    print(json.dumps(None))
else:
    a = new[0]
    if {mesh!r}:
        c = a.static_mesh_component
        c.set_mobility(unreal.ComponentMobility.MOVABLE)
        c.set_static_mesh(unreal.load_asset({mesh!r}))
    if {at!r} is not None:
        a.set_actor_location(unreal.Vector(*{at!r}), False, True)
    a.set_actor_rotation(unreal.Rotator(roll={rot!r}[2], pitch={rot!r}[0], yaw={rot!r}[1]), True)
    if {label!r}:
        try:
            a.set_actor_label({label!r})
        except Exception:
            pass
    landed = None
    if {ground!r}:
        try:
            landed = toolsets._ground_actor(a, {align!r})[2]
        except RuntimeError as exc:
            landed = "not grounded: " + str(exc)
    loc = a.get_actor_location()
    print(json.dumps({{"name": a.get_name(), "label": a.get_actor_label(), "class": a.get_class().get_name(),
                      "at": [loc.x, loc.y, loc.z], "landed": landed}}))
"""


def spawn_in_pie(ctx, args):
    """`ue spawn ... --pie`: an actor in the running PIE world (the target `ue pie target` picked), gone when PIE
    stops. Through the cheat manager's `summon`, then moved to --at."""
    if not (ctx.beacon or {}).get("pie"):
        raise CliError("--pie needs PIE running (ue pie start)")
    at = [float(v) for v in args.at.split(",")] if args.at else None
    if at is not None and len(at) != 3:
        raise CliError("--at x,y,z")
    rot = [float(v) for v in args.rot.split(",")] if args.rot else [0.0, 0.0, 0.0]
    found = py_json(ctx, PIE_SPAWN_CLASS.format(what=args.what))
    ctx.tether("console", command=f"summon {found['path']}", pie=True)
    placed, deadline = None, time.time() + 8  # in multiplayer PIE the command runs on the world's next tick
    while placed is None and time.time() < deadline:
        time.sleep(0.25)
        placed = py_json(ctx, PIE_SPAWN_PLACE.format(path=found["path"], before=found["before"], mesh=found["mesh"],
                                                     at=at, rot=rot, label=args.label or "", ground=args.ground,
                                                     align=args.align))
    if placed is None:
        raise CliError(f"summon {found['path']} spawned nothing within 8 s (the PIE player needs a cheat manager; "
                       "ue log --grep Fabricate)", 4)
    ctx.data = placed
    x, y, z = placed["at"]
    landed = placed["landed"]
    print(f"spawned {placed['label'] or placed['name']} ({placed['class']}) in the PIE world at ({x:g}, {y:g}, {z:.6g})"
          + (f" on {landed}" if landed and not landed.startswith("not grounded") else f" ({landed})" if landed else "")
          + "; it goes when PIE stops")
    return 0


def cmd_scatter(ctx, args):
    if not args.area and not args.line:
        raise CliError("give --area x,y,radius or --line x,y,z x,y,z")
    print(ctx.tether("scatter", what=args.what, count=args.count, area=args.area or "",
                     line=" ".join(args.line) if args.line else "", yaw=args.yaw, scale=args.scale,
                     spacing=args.spacing, seed=args.seed, align=args.align, label=args.label or ""))
    return 0


def cmd_drop(ctx, args):
    print(ctx.tether("drop", targets=",".join(args.actors), align=args.align, group=args.group, rest=args.rest))
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
    if getattr(args, "editor", False) or args.cmd == "redo":
        return editor_undo(ctx, "redo" if args.cmd == "redo" else "undo", args.count)
    print(ctx.tether("undo"))
    return 0


def editor_undo(ctx, action, count):
    """Undo or redo entries of the editor's own history, naming each from the log."""
    start = log_end(ctx.project)
    result = ctx.tether("editor_undo", action=action, count=count)
    result = json.loads(result) if isinstance(result, str) else result
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        titles = [m.group(1) for m in re.finditer(rf"LogEditorTransaction: {action.title()} (.*)",
                                                  f.read().decode("utf-8", "replace"))]
    for title in titles:
        print(f"{ {'undo': 'undone', 'redo': 'redone'}[action]}: {title.strip()}")
    if not result["done"]:
        print(f"nothing to {action}")
    print(f"{result['undoable']} more undoable step(s) in the editor's history")
    ctx.data = dict(result, titles=titles)
    return 0 if result["done"] else 1


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
    items = None
    for path in args.paths:
        print(ctx.tether("asset_info", path=path))
        if path.startswith("/Game/"):
            items = project_fab(ctx.project) if items is None else items
            item = fab_origin(ctx.project, path.split(".")[0], items)
            if item:
                url = listing_url(item.get("listing"))
                print(f"  from Fab: {item['title']} ({item['kind']})" + (f" {url}" if url else ""))
    return 0


def cmd_mat(ctx, args):
    """Material parameters: list them (with where each value comes from), or set them (undoable). `ue mat graph`
    reads or edits the node graph."""
    if args.target == "graph":
        if not args.assignments:
            raise CliError("ue mat graph <material> [add|connect|set|delete|prune ...]")
        material, *rest = args.assignments
        out = ctx.tether("mat_graph", target=material, op=rest[0] if rest else "-", args=json.dumps(rest[1:]))
        print(out)
        if rest:
            text = ctx.tether("material", target=material, compile=True, limit=0)
            print(text[text.find("compiled "):] if "compiled " in text else text.splitlines()[-1])
        return 0
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
    if action == "path":
        if not args.points or len(args.points) % 2:
            raise CliError("ue nav path <start> <end> [<start> <end> ...]   (x,y,z or actor; --m for metres)")
        points = [metres_to_cm(p) if args.m else p for p in args.points]
        extra = {"agent": args.agent or ""}
        failed = 0
        for start, end in zip(points[::2], points[1::2]):
            try:
                result = ctx.tether("nav", action="path", start=start, end=end, match="", pie=args.pie,
                                    limit=args.limit, **extra)
            except CliError as exc:
                if '"agent"' not in str(exc) or args.agent:
                    raise
                extra = {}  # a project's plugin from before 0.20 has no agent parameter
                result = ctx.tether("nav", action="path", start=start, end=end, match="", pie=args.pie,
                                    limit=args.limit)
            failed += not str(result).startswith("path:")
            print(f"{start} -> {end}: {result}" if len(points) > 2 else result)
        return 1 if failed else 0
    match = " ".join(args.points) if action == "check" else ""
    print(ctx.tether("nav", action=action, start="", end="", match=match, pie=args.pie, limit=args.limit, agent=""))
    return 0


def metres_to_cm(point):
    """'12,-4,18' in metres as Unreal centimetres; an actor name stays as it is."""
    try:
        return ",".join(f"{float(v) * 100:g}" for v in point.split(","))
    except ValueError:
        return point


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


def cmd_anim(ctx, args):
    if args.target == "graph":
        if not args.asset:
            raise CliError("ue anim graph /Game/.../ABP_Name")
        print(ctx.tether("anim_graph", target=args.asset))
        return 0
    print(ctx.tether("anim", target=args.target, pie=not args.editor, limit=args.limit))
    return 0


def cmd_ai(ctx, args):
    print(ctx.tether("ai", target=args.target or "", limit=args.limit))
    return 0


def cmd_new(ctx, args):
    print(ctx.tether("new_asset", kind=args.kind, path=args.path, parent=args.parent or "", save=args.save))
    return 0


def cmd_savegame(ctx, args):
    """Save slots: list them (from disk), show one (loaded in the editor), or edit and save one."""
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "SaveGames")
    if not args.slot:
        slots = sorted((f for f in os.listdir(folder) if f.endswith(".sav")), key=lambda f: -os.path.getmtime(
            os.path.join(folder, f))) if os.path.isdir(folder) else []
        print(f"{len(slots)} save slot(s) in Saved/SaveGames" + (":" if slots else ""))
        for name in slots:
            path = os.path.join(folder, name)
            print(f"  {name[:-4]}  {human_bytes(os.path.getsize(path))}  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(path)))}")
        return 0
    assignments = [a for a in args.rest if "=" in a]
    if assignments:
        values = dict(a.partition("=")[::2] for a in assignments)
        print(ctx.tether("set_savegame", slot=args.slot, values=json.dumps(values), user=args.user))
        return 0
    match = " ".join(args.rest)
    print(ctx.tether("savegame", slot=args.slot, user=args.user, match=match, limit=args.limit if args.limit != 30 else 120))
    return 0


def cmd_find(ctx, args):
    """`ue find <regex on names>`, or a package path: a folder (/Game/X/Presets: what is in it) or a glob
    (/Game/X/Presets/*, /Game/*/M_Neon*), matched on the whole package path."""
    query = args.query or ""
    if not query.startswith("/"):
        print(ctx.tether("find", query=query, cls=args.cls or "", under=args.under, limit=args.limit))
        return 0
    pattern = query.rstrip("/")
    if not any(c in pattern for c in "*?["):  # a folder: everything under it
        print(ctx.tether("find", query="", cls=args.cls or "", under=pattern, limit=args.limit))
        return 0
    parts = pattern.split("/")
    fixed = parts[: next(i for i, p in enumerate(parts) if any(c in p for c in "*?["))]
    under = "/".join(fixed) or "/Game"
    found = ctx.tether("find", query="", cls=args.cls or "", under=under, limit=1000000)
    rows = [line for line in str(found).splitlines()[1:] if line.startswith("/")]
    keep = [r for r in rows if fnmatch.fnmatch(r.split("  ")[0].lower(), pattern.lower())]
    print(f"{len(keep)} assets matching {pattern}" + (f" (class /{args.cls}/)" if args.cls else "")
          + ("" if keep else f"; {len(rows)} under {under}"))
    for row in keep[: args.limit]:
        print(row)
    if len(keep) > args.limit:
        print(f"... +{len(keep) - args.limit} more (--limit)")
    ctx.data = [r.split("  ")[0] for r in keep]
    return 0


def cmd_refs(ctx, args):
    print(ctx.tether("refs", asset=args.asset, deps=args.deps, limit=args.limit))
    return 0


def cmd_api(ctx, args):
    print(ctx.tether("api", name=args.name, match=args.match or "", limit=args.limit))
    return 0


def cmd_actors(ctx, args):
    print(ctx.tether("actors", match=args.match or "", limit=args.limit, pie=args.pie))
    return 0


def cmd_exec(ctx, args, after=None):
    """A console command and the log lines it produced. `after` runs right after the command (before waiting for
    its output): `ue exec ... --shot` captures a short-lived effect on the next frame."""
    note = lease_note(ctx.project)
    if note:
        print(note, file=sys.stderr)
    start = log_end(ctx.project)
    command = " ".join(args.command)
    world = ctx.tether("console", command=command, pie=args.pie)
    if after:
        after()
    # In multiplayer PIE the command runs on the world's next tick (so RPCs replicate), and what it causes on the
    # other side lands a frame or two later: wait for it to run, then a little longer.
    deadline = time.time() + (8 if "queued" in str(world) else 0)
    ran_at = None
    while True:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()
        if ran_at is None and any(l.rstrip().endswith(f"Cmd: {command}") for l in lines):
            ran_at = time.time()  # then a second more for the other side's reply (PIE ticks slowly in the background)
        if time.time() >= deadline or ran_at and time.time() - ran_at >= 1.0:
            break
        time.sleep(0.25)
    world = str(world).replace(" (queued for its next tick)", ", from its next tick")
    # A command's output in its own order: only repeats in a row are folded, and the output says so.
    shown = filter_log(lines, level="Log", consecutive=True)
    print(f"ran in {world}" + ("" if shown else " (no log output)"))
    for line in shown[: args.limit]:
        print(line)
    if shown.folded:
        print(f"({shown.folded} repeated line(s) in a row folded into \"xN\")")
    for path in written_screenshots(ctx, lines):
        print(f"screenshot: {path}")
    return 0


SCREENSHOT_TAKEN = re.compile(r'Screenshot "([^"]+)" taken|High resolution screenshot saved as (.+\.png)')


def written_screenshots(ctx, lines):
    """The files a console screenshot command (shot, shot showui, HighResShot) wrote, from its log lines: `shot`
    logs only a name, so it is found under Saved/Screenshots (the newest of that name)."""
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Screenshots")
    found = []
    for line in lines:
        m = SCREENSHOT_TAKEN.search(line)
        if not m:
            continue
        if m.group(2):
            found.append(m.group(2).strip())
            continue
        names = [os.path.join(d, n) for d, _, files in os.walk(folder) for n in files if n == f"{m.group(1)}.png"]
        if names:
            found.append(max(names, key=os.path.getmtime))
    return list(dict.fromkeys(found))


def flush_editor_log(ctx):
    """The editor writes its log asynchronously; ask it to flush so just-logged lines are on disk."""
    beacon = ctx.beacon
    if getattr(ctx, "transport", "mcp") == "remote":
        reachable = ctx._rex is not None  # a fresh remote connection costs ~1 s of discovery; skip it
    else:
        reachable = beacon and editor_process(beacon.get("pid")) and beacon.get("mcp_port") and port_open(beacon["mcp_port"])
    if reachable:
        try:
            ctx.tether("console", command="FLUSHLOG")
        except CliError:
            pass  # hung or busy editor: read whatever is on disk


# A PIE session's start ("PlayLevel: ..." comes once per session; a map travel inside PIE also creates a
# UEDPIE_0_ world package, so that line only counts when the session start is missing).
PIE_START = re.compile(r"LogPlayLevel: PlayLevel: ")
PIE_WORLD = re.compile(r"LogPlayLevel: Creating play world package: \S*UEDPIE_0_")
LOG_STAMP = re.compile(r"^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d):(\d{3})\]")


def since_pie(lines):
    """The lines from the start of the last PIE session on (all of them when there was none)."""
    starts = [i for i, line in enumerate(lines) if PIE_START.search(line)] or \
        [i for i, line in enumerate(lines) if PIE_WORLD.search(line)]
    return lines[starts[-1]:] if starts else lines


def since_duration(lines, text, now=None):
    """The lines logged in the last `text` ('90s', '2m', '1h'); log stamps are UTC."""
    import calendar
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", text.strip())
    if not m:
        raise CliError(f"--since takes last, start, pie or a duration like 90s, 2m, 1h (not '{text}')")
    cutoff = (now if now is not None else time.time()) - float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    for i, line in enumerate(lines):
        stamp = LOG_STAMP.match(line)
        if stamp:
            y, mo, d, h, mi, sec, ms = (int(v) for v in stamp.groups())
            if calendar.timegm((y, mo, d, h, mi, sec)) + ms / 1000 >= cutoff:
                return lines[i:]
    return []


def cmd_log(ctx, args):
    if args.ignore:
        return add_log_ignore(ctx, args.ignore, args.why)
    flush_editor_log(ctx)
    level = args.level or ("VeryVerbose" if args.grep else "Warning")  # a search looks at every line unless told otherwise
    if args.summary:
        lines = read_new_log(ctx.project, from_start=True, advance=False)
        lines = since_pie(lines) if args.since == "pie" else lines
        rows, hidden = summarize_log(lines, level, ignore=not args.all)
        total = sum(r["count"] for r in rows)
        print(f"{total} entries (>= {level}) in {len(rows)} group(s) from {len(lines)} lines "
              + ("since the last PIE start" if args.since == "pie" else "this session")
              + (f"; {hidden} known noise hidden (--all)" if hidden else ""))
        for row in rows[: args.limit]:
            print(f"{row['count']:>5}x {row['level'][0]} {row['category']}: {short(row['first'], 160)}")
        if len(rows) > args.limit:
            print(f"... +{len(rows) - args.limit} more group(s) (--limit)")
        ctx.data = {"groups": rows, "hidden": hidden}
        return 0
    if args.tail:  # the last N entries of the session: what `tail` would show
        args.since, args.limit = "start", args.tail
    if args.since == "pie":
        lines = since_pie(read_new_log(ctx.project, from_start=True, advance=False))
        where = "since the last PIE start"
    elif args.since not in ("last", "start"):
        lines = since_duration(read_new_log(ctx.project, from_start=True, advance=False), args.since)
        where = f"in the last {args.since}"
    else:
        lines = read_new_log(ctx.project, from_start=args.since == "start")
        where = "this session" if args.since == "start" else "new since the last ue log"
    # Reading events (info lines, or a search): keep their order, folding only repeats in a row.
    ordered = bool(args.grep) or LEVELS.get(level.capitalize(), 2) >= LEVELS["Display"]
    shown = filter_log(lines, level, args.cat, args.grep, ignore=not args.all, consecutive=ordered)
    filters = [f">= {level}"] + ([f"category {args.cat}"] if args.cat else []) + ([f"matching /{args.grep}/"] if args.grep else [])
    print(f"{len(shown)} entries ({', '.join(filters)}) of {len(lines)} lines {where}"
          + (f"; {shown.hidden} known noise hidden (--all)" if shown.hidden else "")
          + ("" if shown or level in ("Log", "Verbose", "VeryVerbose") else f" (--level Log shows info lines too)"))
    for line in shown[-args.limit:] if args.since != "last" else shown[: args.limit]:
        print(line)
    if len(shown) > args.limit:
        print(f"... +{len(shown) - args.limit} more (--limit)")
    return 0


def add_log_ignore(ctx, pattern, why=None):
    """`ue log --ignore REGEX`: add a known-noise pattern to Config/Tether/log-ignore.json."""
    try:
        re.compile(pattern)
    except re.error as exc:
        raise CliError(f"not a valid regex: {exc}")
    path = log_ignore_path(ctx.project)
    data = read_json(path) if os.path.exists(path) else {"ignore": []}
    if data is None:
        raise CliError(f"{path} is not valid JSON")
    items = data.setdefault("ignore", []) if isinstance(data, dict) else data
    if pattern in log_ignore_entries(ctx.project):
        print(f"already ignored: {pattern}")
        return 0
    items.append({"pattern": pattern, "why": why} if why else pattern)
    write_json(path, data)
    print(f"ignoring {pattern!r} in ue log, pie and ci ({os.path.relpath(path, os.path.dirname(ctx.project))}, "
          f"{len(items)} pattern(s)); ue log --all shows everything")
    return 0


LEVEL_KINDS = ("floating", "buried", "stacked", "invisible blocker", "below kill z", "outside", "stationary lights",
               "collision profile")


def level_lines(data, per_kind=10):
    """`ue level check` output: a count line, then the issues grouped by kind."""
    by_kind = {}
    for issue in data["issues"]:
        by_kind.setdefault(issue["kind"], []).append(issue)
    kinds = sorted(by_kind, key=lambda k: LEVEL_KINDS.index(k) if k in LEVEL_KINDS else 99)
    lines = [f"{data['actors']} actors checked: " + (", ".join(f"{len(by_kind[k])} {k}" for k in kinds) if kinds
                                                     else "nothing to report") + (" (capped)" if data.get("capped") else "")]
    for kind in kinds:
        lines.append(f"{kind}:")
        for issue in by_kind[kind][:per_kind]:
            lines.append(f"  {issue['actor']}  {issue['detail']}")
        if len(by_kind[kind]) > per_kind:
            lines.append(f"  ... +{len(by_kind[kind]) - per_kind} more (--limit N)")
    return lines


def cmd_level(ctx, args):
    data = ctx.tether("level_check", gap=args.gap, limit=400)
    data = json.loads(data) if isinstance(data, str) else data
    for line in level_lines(data, args.limit if args.limit != 30 else 10):
        print(line)
    ctx.data = data
    return 1 if data["issues"] and args.strict else 0


def cmd_statetree(ctx, args):
    print(ctx.tether("statetree", target=args.asset or "-", pie=args.pie))
    return 0


def trace_lines(data):
    """`ue trace` output: the first blocking hit, then what the trace passed through."""
    hits = data["hits"]
    head = (f"{'sphere ' + format(data['sphere'], 'g') + ' cm' if data['sphere'] else 'line'} trace on {data['channel']} "
            f"from ({', '.join(map(str, data['start']))}) to ({', '.join(map(str, data['end']))})")
    lines = [head + (": nothing hit" if not hits else ":")]
    for hit in hits:
        what = f"{hit['actor']} ({hit['component']}) at {hit['distance']:g} cm, profile {hit['profile'] or '?'}"
        what += f", {hit['response'] or '?'} on {data['channel']}" + (f", {hit['phys_mat']}" if hit["phys_mat"] else "")
        lines.append(("  BLOCKED by " if hit["blocking"] else "  passes ") + what)
    return lines


def cmd_trace(ctx, args):
    data = ctx.tether("trace", start=args.start, end=args.to, distance=args.distance, channel=args.channel,
                      sphere=args.sphere, pie=not args.editor, complex=not args.simple)
    data = json.loads(data) if isinstance(data, str) else data
    for line in trace_lines(data):
        print(line)
    ctx.data = data
    return 0


def cmd_collision(ctx, args):
    print(ctx.tether("collision", target=args.actor, pie=args.pie))
    return 0


def cmd_tree(ctx, args):
    print(ctx.tether("component_tree", target=args.target, pie=args.pie, limit=args.limit))
    return 0


def cmd_class(ctx, args):
    section, grep = getattr(args, "section", None), getattr(args, "grep", None)
    limit = args.limit if not section or args.limit != 30 else 500  # one list: show it whole unless told
    print(ctx.tether("class_info", name=args.name, limit=limit, section=section or "-", grep=grep or "-"))
    return 0


def asset_pattern(pattern):
    """(folder, name glob) from '/Game/Props', '/Game/Props/**' or '/Game/Props/*Rock*'."""
    parts = pattern.rstrip("/").split("/")
    glob = ""
    if parts and any(ch in parts[-1] for ch in "*?["):
        glob = "" if parts[-1] == "**" else parts[-1]
        parts = parts[:-1]
    while parts and parts[-1] == "**":
        parts = parts[:-1]
    return "/".join(parts) or "/Game", glob


def set_assets(ctx, args):
    """`ue set --assets`: the same properties on every matching asset."""
    assignments = ([args.target] if args.target else []) + list(args.assignments)
    values = {}
    for item in assignments:
        name, eq, value = item.partition("=")
        if not eq or not name:
            raise CliError(f"expected Name=value, got '{item}'")
        values[name.strip()] = value
    if not values:
        raise CliError("ue set --assets <folder> [--class C] [--match GLOB] Name=value ...")
    folder, glob = asset_pattern(args.assets)
    data = ctx.tether("set_assets", folder=folder, values=json.dumps(values), cls=args.cls or "",
                      match=args.match or glob, dry_run=args.dry_run, save=args.save, limit=500)
    data = json.loads(data) if isinstance(data, str) else data
    rows, failed = data["rows"], [r for r in data["rows"] if not r["ok"]]
    for row in rows[: args.limit]:
        print(f"{'' if row['ok'] else 'SKIP '}{row['asset']}: {row['detail'] or 'already set'}")
    if len(rows) > args.limit:
        print(f"... +{len(rows) - args.limit} more (--limit N)")
    verb = "would change" if args.dry_run else "changed"
    print(f"{data['matched']} asset(s) matched; {verb} {len(rows) - len(failed)}, skipped {len(failed)}"
          + (" (500 at a time)" if data["matched"] > 500 else "")
          + ("" if args.dry_run or args.save else "; not saved (ue save, or --save)"))
    ctx.data = data
    return 1 if failed and len(failed) == len(rows) else 0


def cmd_lights(ctx, args):
    print(ctx.tether("lights", limit=args.limit))
    return 0


def cmd_wp(ctx, args):
    """World Partition: summary, all actors (loaded or not), load/unload by area or name."""
    where = args.where or ""
    area = where if re.fullmatch(r"\s*-?[\d.]+\s*,\s*-?[\d.]+\s*,\s*[\d.]+\s*", where) else "-"
    match = "" if area != "-" else where
    print(ctx.tether("wp", action=args.action, match=match, area=area, limit=args.limit))
    return 0



def as_json(result):
    """A tool's JSON result: the transport hands it back already decoded, unless it came as text."""
    return json.loads(result) if isinstance(result, str) else result


def cvar_line(row):
    """`name = value  (SetBy; default D)`; nothing in brackets when it is the built-in value."""
    if row["setby"] == "Constructor":
        return f"{row['name']} = {row['value']}"
    default = row.get("default")
    same = default is None or str(default) == str(row["value"]) or row.get("same_as_default")
    return f"{row['name']} = {row['value']}  ({row['setby']}" + ("" if same else f"; default {default}") + ")"


def restore_cvars(ctx, changed):
    """Undo a set_cvars result: unset the console layer, or put back the old value where the console had set it."""
    restore = {r["name"]: r["before"] for r in changed if r["setby_before"] == "Console"}
    unset = [r["name"] for r in changed if r["setby_before"] != "Console"]
    return as_json(ctx.tether("set_cvars", values=json.dumps(restore), reset=json.dumps(unset)))


@contextlib.contextmanager
def scoped_cvars(ctx, values):
    """Console variables set for the duration of a block, then restored exactly (see restore_cvars)."""
    changed = as_json(ctx.tether("set_cvars", values=json.dumps(values), reset="[]"))
    try:
        yield changed
    finally:
        restore_cvars(ctx, changed)


def cmd_cvar(ctx, args):
    """Console variables: list (value, who set it, default), set, reset, or set only while a command runs."""
    sets = [item for item in args.items if "=" in item]
    if args.reset or sets:
        values = {}
        for item in sets:
            name, _, value = item.partition("=")
            values[name.strip()] = value
        changed = as_json(ctx.tether("set_cvars", values=json.dumps(values), reset=json.dumps(args.reset or [])))
        for row in changed:
            print(f"{row['name']}: {row['before']} ({row['setby_before']}) -> {row['after']} ({row['setby']})")
        ctx.data = changed
        if not args.run:
            return 0
        import shlex
        import subprocess
        child = [sys.executable, UE_SCRIPT, "--project", ctx.project] + shlex.split(args.run)
        env = dict(os.environ, **({"UE_EDITOR_PID": str(ctx.beacon["pid"])} if ctx.beacon else {}))
        try:
            sys.stdout.flush()
            code = subprocess.call(child, env=env)
        finally:
            back = restore_cvars(ctx, changed)
            print("restored " + ", ".join(f"{r['name']} = {r['after']} ({r['setby']})" for r in back))
        return code
    if args.run:
        raise CliError("--for needs Name=value to set while it runs")
    match = " ".join(args.items)
    data = as_json(ctx.tether("cvars", match=match, changed=args.changed, limit=args.limit))
    what = "differ from their default" if args.changed else "match"
    print(f"{data['matched']} of {data['total']} console variables {what}" + (f" '{match}'" if match else ""))
    for row in data["rows"]:
        print("  " + cvar_line(row))
    if data["matched"] > len(data["rows"]):
        print(f"  ... +{data['matched'] - len(data['rows'])} more (--limit N, or a narrower regex)")
    ctx.data = data
    return 0
