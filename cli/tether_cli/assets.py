"""Assets and code: Blueprints, data tables, diff, validate, size, usages, overview."""
import json
import os
import re
import sys
import time

from .core import (
    CliError, LEVELS, LOG_LINE, NOISE, NOISE_MESSAGE, compact_error, editor_live, human_bytes, log_end, log_path,
    read_json, run_git, short, state_dir, unwrap, write_json,
)
from .transport import py_json, resolve_toolset
from .calls import flush_editor_log


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
    ctx.data = dict(report, map_check=[{"level": l, "message": m} for l, m in map_lines if l in ("Error", "Warning")],
                    logged=other, errors=errors)
    write_json(os.path.join(state_dir(ctx.project), "validated.json"), {"time": time.time(), "errors": errors})
    return 1 if errors or (args.strict and (warnings or other)) else 0


PACKAGE_EXTS = (".uasset", ".umap", ".uexp", ".ubulk", ".uptnl")


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
    ctx.data = {"total_bytes": sum(sizes.values()), "packages": len(sizes),
                "folders": {k: {"bytes": b, "count": n} for k, (b, n) in groups},
                "biggest": [{"package": p, "bytes": b} for p, b in biggest]}
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
    ctx.data = {"code": [{"file": p, "line": n, "text": t} for p, n, t in hits], "assets": sorted(assets)}
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
    lines = project_overview(ctx.project, full=not args.brief)
    ctx.data = {line.split(": ", 1)[0]: line.split(": ", 1)[-1] for line in lines[1:] if ": " in line}
    for line in lines:
        print(line)
    return 0


NIAGARA_STAGES = (("systemSpawnScript", "system spawn"), ("systemUpdateScript", "system update"),
                  ("emitterSpawnScript", "emitter spawn"), ("emitterUpdateScript", "emitter update"),
                  ("particleSpawnScript", "particle spawn"), ("particleUpdateScript", "particle update"))


def niagara_value(value):
    """A user variable's value from Epic's JSON ({"struct": ..., "value": {...}}) as short text."""
    if isinstance(value, dict):
        inner = value.get("value", value)
        if isinstance(inner, dict):
            nums = [inner[k] for k in ("x", "y", "z", "w", "r", "g", "b", "a", "value") if k in inner]
            if nums:
                return ", ".join(f"{n:g}" if isinstance(n, (int, float)) else str(n) for n in nums)
        return json.dumps(inner, separators=(",", ":"))[:60]
    return str(value)


def niagara_type(var):
    """'LinearColor' from a user variable's type reference."""
    kind = var.get("type") or var.get("typeDef") or {}
    ref = (kind.get("classStructOrEnum") or {}).get("refPath") if isinstance(kind, dict) else None
    return (ref or str(kind) or "?").rsplit(".", 1)[-1].replace("Niagara", "") or "?"


def niagara_lines(summary, topologies, user):
    """`ue fx <system>`: emitters with sim target, renderers and modules per stage; user parameters."""
    emitters = summary.get("emitters") or []
    lines = [f"{summary.get('systemName', '?')}: {len(emitters)} emitter(s), {len(user)} user parameter(s)"]
    for var in user:
        name = var.get("name") or (var.get("variable") or {}).get("name", "?")
        value = var.get("value") or var.get("defaultValue")
        lines.append(f"  user {name}: {niagara_type(var)}" + (f" = {niagara_value(value)}" if value is not None else ""))
    for emitter in emitters:
        name = emitter.get("emitterName", "?")
        renderers = [(r.get("refPath") or "?").rsplit(".", 1)[-1].replace("Niagara", "").replace("RendererProperties", "")
                     for r in emitter.get("rendererClasses") or []]
        sim = (emitter.get("simTarget") or "?").replace("Sim", "").replace("ComputeShader", "GPU")
        lines.append(f"  {name}" + ("" if emitter.get("bEnabled", True) else " (disabled)")
                     + f": {sim}, " + (", ".join(r.lower() for r in renderers) or "no") + " renderer(s)")
        topology = topologies.get(name) or {}
        for key, label in NIAGARA_STAGES:
            modules = (topology.get(key) or {}).get("modules") or []
            if modules:
                lines.append(f"    {label}: " + ", ".join(m.get("moduleName", "?") + ("" if m.get("enabled", True) else " (off)")
                                                         for m in modules))
        extra = [k for k, v in topology.items() if k.endswith("Script") and k not in dict(NIAGARA_STAGES)
                 and isinstance(v, dict) and v.get("modules")]
        for key in extra:
            lines.append(f"    {key}: " + ", ".join(m.get("moduleName", "?") for m in topology[key]["modules"]))
    return lines


def cmd_fx(ctx, args):
    """Niagara: a system as text, user parameters on a placed component, or the components in the level/PIE."""
    target = args.target or ""
    sets = [a for a in args.assignments if "=" in a]
    if target.startswith("/") and not args.pie:
        system = target if "." in target.rsplit("/", 1)[-1] else f"{target}.{target.rsplit('/', 1)[-1]}"
        summary = mcp_tool(ctx, "NiagaraToolset_System", "GetSystemSummary", {"system": system})
        user = mcp_tool(ctx, "NiagaraToolset_System", "GetUserVariables", {"system": system})
        user = (user or {}).get("userVariables", []) if isinstance(user, dict) else []
        topologies = {}
        for emitter in summary.get("emitters") or []:
            ref = {"system": system, "emitterName": emitter.get("emitterName"), "scriptName": "", "moduleName": "",
                   "rendererIndex": -1, "inputNameStack": []}
            try:
                topologies[emitter.get("emitterName")] = mcp_tool(ctx, "NiagaraToolset_System", "GetEmitterTopology",
                                                                  {"emitterRef": ref})
            except CliError as exc:
                topologies[emitter.get("emitterName")] = {}
                print(f"note: {emitter.get('emitterName')}: {exc}")
        for line in niagara_lines(summary, topologies, user)[: args.limit + 1]:
            print(line)
        ctx.data = {"summary": summary, "user": user, "topology": topologies}
        return 0
    if target and sets:
        print(ctx.tether("set_niagara", target=target, values=json.dumps(dict(a.split("=", 1) for a in sets)),
                         pie=args.pie))
        return 0
    rows = ctx.tether("niagara_components", match="" if target.startswith("/") else target, pie=args.pie,
                      limit=args.limit)
    rows = json.loads(rows) if isinstance(rows, str) else rows
    print(f"{len(rows)} Niagara component(s) in {'PIE' if args.pie else 'the level'}"
          + (f" matching '{target}'" if target else ""))
    for row in rows:
        state = "active" if row["active"] else "inactive" + (" (auto-activates)" if row["auto_activate"] else "")
        print(f"  {row['actor']}.{row['component']}: {row['system'] or 'no system'}, {state}"
              + ("" if row["visible"] else ", hidden"))
        if target and len(rows) == 1 and row["system"]:  # one component: its user parameter values too
            try:
                values = mcp_tool(ctx, "NiagaraToolset_Component", "GetUserVariables", {"component": row["path"]})
            except CliError:
                values = None
            values = (values or {}).get("userVariables", []) if isinstance(values, dict) else values or []
            for var in values:
                name = var.get("name") or (var.get("variable") or {}).get("name", "?")
                print(f"    {name}: {niagara_type(var)} = {niagara_value(var.get('value'))}")
    ctx.data = rows
    return 0


def pcg_value(value):
    """A PCG node parameter as short text: refPaths to their last part, floats rounded, structures cut short."""
    if isinstance(value, dict) and set(value) == {"refPath"}:
        return (value["refPath"] or "None").rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"))
        return text if len(text) <= 60 else text[:57] + "..."
    return str(value)


def pcg_graph_lines(structure, params=None, meshes=None):
    """`ue pcg <graph>`: nodes in dataflow order, each with what feeds it and its non-default settings."""
    nodes = {n["name"]: n for n in structure.get("nodes") or []}
    edges = structure.get("edges") or []
    inputs = {}
    for e in edges:
        inputs.setdefault(e["destNode"], []).append(e)
    order, seen = [], set()

    def visit(name):  # sources first, so the text reads top to bottom like the data flows
        if name in seen or name not in nodes:
            return
        seen.add(name)
        for e in inputs.get(name, []):
            visit(e["srcNode"])
        order.append(name)
    for name in sorted(nodes, key=lambda n: (nodes[n].get("nodeType") != "Input Node", n == "DefaultOutputNode")):
        visit(name)
    lines = [f"{structure.get('name', '?')}: {len(nodes)} node(s), {len(edges)} edge(s)"
             + (f" - {structure['description']}" if structure.get("description") else "")]
    for name in order:
        node = nodes[name]
        kind = node.get("nodeType", "?")
        title = node.get("title")
        head = name + (f' "{title}"' if title and title not in (kind, name) else "") + f" ({kind})"
        if not node.get("bEnabled", True):
            head += " (disabled)"
        feeds = [f"{e['srcNode']}.{e['srcPin']}" + (f"->{e['destPin']}" if e["destPin"] not in ("In", "Out") else "")
                 for e in inputs.get(name, [])]
        settings = {k: v for k, v in (node.get("paramOverrides") or {}).items()
                    if not (kind in ("Input Node", "Output Node") and k == "pins")}
        if (meshes or {}).get(name):
            settings.pop("meshSelectorParameters", None)
            settings["meshes"] = ", ".join(meshes[name])
        line = "  " + head + (f" <- {', '.join(feeds)}" if feeds else "")
        if settings:
            line += "; " + ", ".join(f"{k} {pcg_value(v)}" for k, v in settings.items())
        lines.append(line)
        if node.get("comment"):
            lines.append(f"    # {node['comment']}")
    unused = [n for n in nodes if not any(e["srcNode"] == n for e in edges) and nodes[n].get("nodeType") != "Output Node"]
    if unused:
        lines.append("feeding nothing: " + ", ".join(unused))
    for name, value in (params or {}).items():
        lines.append(f"  param {name} = {pcg_value(value)}")
    return lines


PCG_TRIGGERS = {"generateonload": "generates on load", "generateondemand": "on demand",
                "generateatruntime": "at runtime"}


def pcg_row_line(row):
    spawned = ", ".join(f"{n} x{c}" for n, c in sorted(row["instances"].items(), key=lambda kv: -kv[1]))
    state = "generating" if row["generating"] else "generated" if row["generated"] else "not generated"
    return (f"{row['actor']}.{row['component']}: {row['graph'] or 'no graph'}, seed {row['seed']}, "
            f"{PCG_TRIGGERS.get(row['trigger'], row['trigger'])}, "
            f"{state}" + (", partitioned" if row["partitioned"] else "")
            + (f"; {sum(row['instances'].values())} instances ({spawned})" if row["instances"] else "")
            + (f"; {row['actors']} actor(s)" if row["actors"] else ""))


def cmd_pcg(ctx, args):
    """PCG: a graph as text, the level's PCG components, or generate/clean up one."""
    if args.action in ("generate", "cleanup"):
        if not args.target:
            raise CliError(f"ue pcg {args.action} <actor>")
        rows = ctx.tether("pcg", action=args.action, target=args.target, seed=-1 if args.seed is None else args.seed)
        rows = json.loads(rows) if isinstance(rows, str) else rows
        deadline = time.time() + args.wait
        while time.time() < deadline:  # both finish on later editor ticks
            time.sleep(0.5)
            rows = ctx.tether("pcg", action="status", target=args.target)
            rows = json.loads(rows) if isinstance(rows, str) else rows
            if args.action == "generate" and not rows[0]["generating"] or                     args.action == "cleanup" and not rows[0]["generated"] and not rows[0]["instances"]:
                break
        row = rows[0]
        if args.action == "generate" and row["generating"]:
            print(f"still generating after {args.wait:g}s (ue pcg status {args.target})")
        print(pcg_row_line(row))
        ctx.data = row
        return 0
    if args.action == "graph" or (args.action and args.action.startswith("/")):
        graph = args.target if args.action == "graph" else args.action
        if not graph:
            raise CliError("ue pcg <graph path>")
        graph = graph if "." in graph.rsplit("/", 1)[-1] else f"{graph}.{graph.rsplit('/', 1)[-1]}"
        structure = mcp_tool(ctx, "PCGToolset.PCGToolset", "GetGraphStructure", {"graph": graph})
        try:
            schema = mcp_tool(ctx, "PCGToolset.PCGToolset", "GetGraphSchema", {"graph": graph})
        except CliError:
            schema = None
        params = schema.get("properties") if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else {}
        params = {k: (v.get("default") if isinstance(v, dict) else v) for k, v in params.items()}
        meshes = ctx.tether("pcg", action="meshes", target=graph)
        meshes = json.loads(meshes) if isinstance(meshes, str) else meshes
        for line in pcg_graph_lines(structure, params, meshes)[: args.limit + 1]:
            print(line)
        ctx.data = structure
        return 0
    match = args.target if args.action in (None, "list", "status") else args.action
    rows = ctx.tether("pcg", action="list", target=match or "", limit=args.limit)
    rows = json.loads(rows) if isinstance(rows, str) else rows
    print(f"{len(rows)} PCG component(s) in the level" + (f" matching '{match}'" if match else ""))
    for row in rows:
        print("  " + pcg_row_line(row))
    ctx.data = rows
    return 0
