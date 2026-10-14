"""Thin commands over one editor tool each: tools, call, py, props, set, spawn and friends."""
import contextlib
import json
import os
import re
import sys
import time

from .core import (
    CliError, GATED, TETHER, UE_SCRIPT, compact_error, editor_token, emit, filter_log, human_bytes, log_end, log_ignore_entries,
    log_ignore_path, log_path, pid_alive, summarize_log, write_json,
    port_open,
    read_json, read_new_log, short, state_dir, unwrap,
)
from .transport import catalog, fill_args, resolve_toolset, schema, signature


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


def cmd_py(ctx, args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            code = f.read()
    elif args.code == ["-"]:
        code = sys.stdin.read()
    else:
        code = " ".join(args.code)
    out = ctx.tether("py", code=code.lstrip("﻿"))  # PowerShell pipes a BOM
    if out not in ("", None):  # output that is itself JSON comes back decoded
        print(out.rstrip("\n") if isinstance(out, str) else json.dumps(out))
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
    for path in args.paths:
        print(ctx.tether("asset_info", path=path))
    return 0


def cmd_mat(ctx, args):
    """Material parameters: list them (with where each value comes from), or set them (undoable). `ue mat graph`
    reads or edits the node graph."""
    if args.target == "graph":
        if not args.assignments:
            raise CliError("ue mat graph <material> [add|connect|set|delete ...]")
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
    if action == "path" and len(args.points) != 2:
        raise CliError("ue nav path <start> <end>   (x,y,z or actor)")
    start, end = (args.points + ["", ""])[:2] if action == "path" else ("", "")
    match = " ".join(args.points) if action == "check" else ""
    print(ctx.tether("nav", action=action, start=start, end=end, match=match, pie=args.pie, limit=args.limit))
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
    print(ctx.tether("find", query=args.query or "", cls=args.cls or "", under=args.under, limit=args.limit))
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
    if args.ignore:
        return add_log_ignore(ctx, args.ignore, args.why)
    flush_editor_log(ctx)
    if args.summary:
        lines = read_new_log(ctx.project, from_start=True, advance=False)
        rows, hidden = summarize_log(lines, args.level, ignore=not args.all)
        total = sum(r["count"] for r in rows)
        print(f"{total} entries (>= {args.level}) in {len(rows)} group(s) from {len(lines)} lines this session"
              + (f"; {hidden} known noise hidden (--all)" if hidden else ""))
        for row in rows[: args.limit]:
            print(f"{row['count']:>5}x {row['level'][0]} {row['category']}: {short(row['first'], 160)}")
        if len(rows) > args.limit:
            print(f"... +{len(rows) - args.limit} more group(s) (--limit)")
        ctx.data = {"groups": rows, "hidden": hidden}
        return 0
    lines = read_new_log(ctx.project, from_start=args.since == "start")
    shown = filter_log(lines, args.level, args.cat, args.grep, ignore=not args.all)
    print(f"{len(shown)} entries (>= {args.level}) from {len(lines)} new lines"
          + (f"; {shown.hidden} known noise hidden (--all)" if shown.hidden else ""))
    for line in shown[-args.limit:] if args.since == "start" else shown[: args.limit]:
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


def cmd_foliage(ctx, args):
    """Foliage instances: list by type, add over an area, remove from an area."""
    def opt(value):
        return "-" if value is None else value
    print(ctx.tether("foliage", action=args.action, what=opt(args.what), area=opt(args.area),
                     density=-1.0 if args.density is None else args.density, count=-1 if args.count is None else args.count,
                     spacing=-1.0 if args.spacing is None else args.spacing, slope=opt(args.slope),
                     height=opt(args.height), scale=opt(args.scale), align=args.align, seed=args.seed,
                     folder=args.folder))
    return 0


LEVEL_KINDS = ("floating", "buried", "stacked", "invisible blocker", "below kill z", "outside", "stationary lights")


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
    print(ctx.tether("class_info", name=args.name, limit=args.limit))
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
