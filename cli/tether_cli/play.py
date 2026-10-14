"""Play-in-editor and runtime checks: pie, ui, perf, mem, world, audio, soak."""
import argparse
import contextlib
import json
import os
import re
import sys
import time

from .core import (
    CliError, compact_error, crash_summary, editor_executable, editor_live, engine_root, filter_log, host_platform,
    human_bytes, log_end, log_path, read_json, recent_crash, short, state_dir, subprocess, unwrap, write_json,
)
from .transport import catalog, py_json, resolve_toolset
from .build import csharp_changes, csharp_layout, csharp_reload_build
from .calls import flush_editor_log, scoped_cvars
from .images import (
    cmd_shot, contact_sheet, draw_text, crop_pixels, hud_screenshot, image_diff, png_read, png_write, shot_args, widget_capture,
)
from .assets import mcp_tool
from .jobs import cmd_job, jobs_dir


MEMREPORT_CLASS = re.compile(r"^\s*(\w+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s")
MEMREPORT_OBJECT = re.compile(r"^\s*(\w+) (/\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s")
MEMREPORT_TEXTURE = re.compile(r"^\d+x\d+ \(\d+ KB[^)]*\), (\d+)x(\d+) \((\d+) KB\), (\w+), (\w+), (/\S+),")


def memreport_summary(text, top=8):
    """The few numbers that matter from a memreport file."""
    sections, current = {}, None
    for line in text.splitlines():
        begin = re.match(r'MemReport: Begin command "(.*)"', line)
        if begin:
            current = begin.group(1)
            sections[current] = []
        elif line.startswith("MemReport: End command"):
            current = None
        elif current is not None:
            sections[current].append(line)
    out = []
    head = "\n".join(sections.get("Mem FromReport", []))
    process = re.search(r"Process Physical Memory: ([\d.]+) MB used, ([\d.]+) MB peak", head)
    if process:
        line = f"process: {float(process.group(1)) / 1024:.1f} GB used, {float(process.group(2)) / 1024:.1f} GB peak"
        extras = [f"{name} {float(m.group(1)):.0f} MB" for name, rx in
                  (("asset registry", r"AssetRegistry memory usage = ([\d.]+) MB"), ("names", r"Nametable memory usage = ([\d.]+) MB"))
                  for m in [re.search(rx, head)] if m]
        out.append(line + (f" (of which {', '.join(extras)})" if extras else ""))
    rows = []
    for line in sections.get("obj list -resourcesizesort", []):
        m = MEMREPORT_CLASS.match(line)
        if m and m.group(1) != "Class":
            rows.append((float(m.group(3)) + float(m.group(5)), m.group(1), int(m.group(2))))
    if rows:
        rows.sort(reverse=True)
        out.append("by class (object + resource memory): " + ", ".join(
            f"{name} x{count} {human_bytes(int(kb * 1024))}" for kb, name, count in rows[:top]))
    for cls in ("SkeletalMesh", "StaticMesh"):
        objects = []
        for line in sections.get(f"obj list class={cls} -resourcesizesort", []):
            m = MEMREPORT_OBJECT.match(line)
            if m:
                objects.append((float(m.group(3)) + float(m.group(5)), m.group(2).split(".")[0]))
        if objects:
            objects.sort(reverse=True)
            out.append(f"biggest {cls}es: " + ", ".join(f"{p.rsplit('/', 1)[-1]} {human_bytes(int(kb * 1024))}"
                                                      for kb, p in objects[:5]))
    textures = sections.get("ListTextures", [])
    total = next((re.search(r"InMem= ([\d.]+) MB", l) for l in textures if l.startswith("Total size:")), None)
    listed = []
    for line in textures:
        m = MEMREPORT_TEXTURE.match(line)
        if m:
            listed.append((int(m.group(3)), m.group(6).split(".")[0], m.group(5).replace("TEXTUREGROUP_", "")))
    if total:
        listed.sort(reverse=True)
        out.append(f"textures: {len(listed)} loaded, {float(total.group(1)):.0f} MB in memory" +
                   (("; biggest " + ", ".join(f"{p.rsplit('/', 1)[-1]} {kb / 1024:.1f} MB ({g})" for kb, p, g in listed[:5]))
                    if listed and listed[0][0] else ""))
    return out


def memreport_numbers(text):
    """{process_mb, peak_mb, textures_mb} from a memreport (textures only with -full)."""
    numbers = {}
    process = re.search(r"Process Physical Memory: ([\d.]+) MB used, ([\d.]+) MB peak", text)
    if process:
        numbers.update(process_mb=float(process.group(1)), peak_mb=float(process.group(2)))
    textures = re.search(r"^Total size: InMem= ([\d.]+) MB", text, re.M)
    if textures:
        numbers["textures_mb"] = float(textures.group(1))
    return numbers


def cmd_mem(ctx, args):
    """Memory of the running editor (and PIE), from a memreport, in a few lines."""
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Profiling", "MemReports")

    def reports():
        found = set()
        for dirpath, _, files in os.walk(folder):
            found |= {os.path.join(dirpath, f) for f in files if f.endswith(".memreport")}
        return found

    before = reports()
    ctx.tether("console", command="memreport" + (" -full" if args.full else ""))
    # The report is written on a later frame; a call answers once the game thread is free again (~20 s).
    deadline, new = time.time() + max(args.timeout, 120), []
    while not new and time.time() < deadline:
        ctx.tether("py", code="1")
        new = sorted(reports() - before, key=os.path.getmtime)
        time.sleep(0.5 if not new else 0)
    if not new:
        raise CliError("memreport wrote no file (ue log --grep MemReport)")
    with open(new[-1], encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = memreport_summary(text, args.top)
    ctx.data = dict(memreport_numbers(text), report=new[-1])
    beacon = ctx.beacon or {}
    if beacon.get("pie"):
        lines.insert(0, "(PIE is running: the numbers include the play session)")
    for line in lines:
        print(line)
    print(f"full report: {os.path.relpath(new[-1], os.path.dirname(ctx.project))}")
    return 0


SNAPSHOT_LINE = re.compile(r'^(?P<indent> *)(?P<role>[\w-]+)(?: "(?P<label>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)\s*$')
# Source files of the editor's own viewport controls (toolbars, tabs, menus) in a Slate snapshot.
EDITOR_CHROME = ("SDock", "SToolBar", "SClippingHorizontalBox", "SComboButton", "SEditorViewport", "SLevelViewport",
                 "SViewportToolBar", "SViewportToolbar", "SCommonEditorViewport", "STransformViewport", "SActorPilot",
                 "SToolMenu", "SMenuEntryBlock", "SLevelEditor", "SPIEToolbar", "SGameLayerManager", "SNotification")
INTERACTIVE = {"button", "checkbox", "combobox", "textbox", "slider", "hyperlink", "menuitem", "radiobutton", "spinbox"}
UI_TOOLSET = "SlateInspectorToolset.SlateInspectorToolset"


def parse_snapshot(text):
    nodes = []
    for line in text.splitlines():
        m = SNAPSHOT_LINE.match(line)
        if not m:
            continue
        attrs = re.findall(r"\[([^\]]*)\]", m.group("attrs") or "")
        # One bracket can hold several keys: [pos=0,0 size=1280,720]
        fields = dict(token.split("=", 1) for a in attrs for token in a.split() if "=" in token)
        nodes.append({"indent": len(m.group("indent")), "role": m.group("role"),
                      "label": (m.group("label") or "").replace('\\"', '"'), "ref": fields.get("ref", ""),
                      "src": fields.get("src", ""), "size": fields.get("size", ""), "pos": fields.get("pos", ""),
                      "flags": [a for a in attrs if "=" not in a]})
    return nodes


def subtree(nodes, i):
    end = i + 1
    while end < len(nodes) and nodes[end]["indent"] > nodes[i]["indent"]:
        end += 1
    return nodes[i + 1:end]


def game_ui(nodes, client=0):
    """The game's widgets in a whole-editor Slate snapshot: the level viewport's panel minus the editor's own
    controls (told apart by the source file that created each widget), or for a multiplayer client (or PIE in
    a new window) that client's PIE window. Stale windows of earlier sessions are not taken for the game."""
    def pie_window(wanted):
        for i, node in enumerate(nodes):
            if node["role"] == "window" and re.search(wanted, node["label"]):
                return subtree(nodes, i)
        return None

    if client:
        return pie_window(rf"NetMode: Client {client}\]") or []
    for i, node in enumerate(nodes):
        if node["role"] != "tab" or not node["label"].startswith("Viewport") or not node["src"].startswith("SLevelEditor"):
            continue
        parent = next((j for j in range(i - 1, -1, -1) if nodes[j]["indent"] < node["indent"]), None)
        if parent is None:
            continue
        game, keep = [], False
        for n in subtree(nodes, parent):
            if n["indent"] == node["indent"]:
                keep = not n["src"].startswith(EDITOR_CHROME) and n is not node
            if keep:
                game.append(n)
        if game:
            return game
    return pie_window(r"Preview \[NetMode|\(PIE\)") or []


def ui_nodes(ctx):
    """(game widget nodes, True) via Epic's Slate inspector, or (None, False) when it is not enabled."""
    if ctx.transport == "remote" or UI_TOOLSET not in catalog(ctx):
        return None, False
    text = mcp_tool(ctx, "SlateInspectorToolset", "Snapshot", {"ref": "", "maxDepth": 120, "bIncludeSourceLocations": True})
    target = re.search(r"^\* (\d+):", pie_net(ctx, "status"), re.M)  # `ue pie target` picks a client's window
    nodes = parse_snapshot(text if isinstance(text, str) else json.dumps(text))
    ctx.ui_all = nodes  # every window, for hit tests
    return game_ui(nodes, int(target.group(1)) if target else 0), True


def box(node):
    """(x, y, w, h) of a snapshot node, or None."""
    try:
        x, y = (float(v) for v in node["pos"].split(","))
        w, h = (float(v) for v in node["size"].split(","))
        return x, y, w, h
    except (KeyError, ValueError):
        return None


def windows(nodes):
    """[(index, title, (x, y, w, h))] of the top-level windows, in absolute pixels. A window's own size is in
    DPI-independent units while everything inside it is in pixels, so it is scaled to fit its widgets."""
    out = []
    top = min((n["indent"] for n in nodes), default=0)
    for i, node in enumerate(nodes):
        if node["role"] != "window" or node["indent"] != top or not box(node):
            continue
        x, y, w, h = box(node)
        scale = 1.0
        for child in subtree(nodes, i):
            b = box(child)
            if b and b[2] and b[3] and w and h:
                scale = max(scale, (b[0] + b[2] - x) / w, (b[1] + b[3] - y) / h)
        scale = min(3.0, round(scale * 4) / 4)  # DPI scales come in quarters (125 %, 150 %, ...)
        out.append((i, node["label"], (x, y, w * scale, h * scale)))
    return out


def inside(point, rect):
    return rect[0] <= point[0] < rect[0] + rect[2] and rect[1] <= point[1] < rect[1] + rect[3]


def covering_window(nodes, node):
    """The title of another top-level window over the centre of `node`, where a synthesized click would land
    instead (a floating Message Log over the PIE viewport did exactly that), or None."""
    b = box(node)
    if not b:
        return None
    point = (b[0] + b[2] / 2, b[1] + b[3] / 2)
    index = next(i for i, n in enumerate(nodes) if n is node) if any(n is node for n in nodes) else None
    own = None
    for i, title, rect in windows(nodes):
        if index is not None and i < index <= i + len(subtree(nodes, i)):
            own = i
    for i, title, rect in windows(nodes):
        if i != own and not title.endswith("Unreal Editor") and inside(point, rect):
            return title or "an untitled window"
    return None


def ui_lines(nodes, show_all=False, match=None):
    base = min((n["indent"] for n in nodes), default=0)
    rx = re.compile(match, re.I) if match else None
    out = []
    for n in nodes:
        if not show_all and (n["size"].startswith("0,") or n["size"].endswith(",0")
                             or (n["role"] == "image" and not n["label"])):
            continue
        line = "  " * ((n["indent"] - base) // 2) + n["role"] + (f' "{n["label"]}"' if n["label"] else "") + \
            "".join(f" [{f}]" for f in n["flags"]) + (f"  ({n['ref']})" if n["role"] in INTERACTIVE else "")
        if not rx or rx.search(line):
            out.append(line)
    return out


def find_ui(nodes, target, roles=INTERACTIVE):
    """A game widget by ref or by label (exact, then contained; case-insensitive)."""
    if re.fullmatch(r"[a-z]+\d+", target):
        hit = [n for n in nodes if n["ref"] == target]
    else:
        key = target.lower()
        pool = [n for n in nodes if n["role"] in roles]
        hit = [n for n in pool if n["label"].lower() == key] or [n for n in pool if key in n["label"].lower()]
    if not hit:
        names = ", ".join(f'{n["role"]} "{n["label"]}"' for n in nodes if n["role"] in roles and n["label"])[:300]
        raise CliError(f"no {'/'.join(sorted(roles)) if len(roles) < 4 else 'interactive'} widget '{target}' on screen"
                       + (f" (have: {names})" if names else ""))
    if "disabled" in hit[0]["flags"]:
        raise CliError(f'{hit[0]["role"]} "{hit[0]["label"]}" is disabled')
    return hit[0]


def ui_click(ctx, target, event=False):
    nodes, slate = ui_nodes(ctx)
    if not slate or event:  # a UMG button's OnClicked, fired directly
        return ctx.tether("umg", click=target)
    node = find_ui(nodes, target)
    # Epic's Click synthesizes a mouse press at the widget's centre on screen: whatever window is on top there gets
    # it. Check first, and for a UMG button fire its OnClicked instead of clicking another window.
    cover = covering_window(getattr(ctx, "ui_all", nodes), node)
    if cover:
        try:
            fired = ctx.tether("umg", click=node["label"] or target)
            return fired + f' (window "{cover}" covers it on screen, so its OnClicked was fired instead of a click)'
        except CliError:
            raise CliError(f'{node["role"]} "{node["label"]}" is covered by window "{cover}": a click would land '
                           f"there. Close it (ue call SlateInspectorToolset.Windows action=list, then action=close "
                           f"index=N) and retry") from None
    mcp_tool(ctx, "SlateInspectorToolset", "Click", {"ref": node["ref"]})
    return f'clicked {node["role"]} "{node["label"]}"'


def ui_has_text(ctx, text):
    nodes, slate = ui_nodes(ctx)
    lines = ui_lines(nodes, show_all=True) if slate else ctx.tether("umg").splitlines()
    return any(text.lower() in line.lower() for line in lines)


def cmd_ui(ctx, args):
    """The game's on-screen UI in PIE (Slate and UMG), and clicking, typing and keys."""
    if ctx.transport != "remote" and not (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is not running (ue pie start)")
    action = args.action or "show"
    if action == "click":
        print(ui_click(ctx, " ".join(args.rest), getattr(args, "event", False)))
        return 0
    if action == "at":  # what is on screen at a point, every window included
        try:
            point = tuple(float(v) for v in " ".join(args.rest).replace(" ", "").split(","))
            assert len(point) == 2
        except (ValueError, AssertionError):
            raise CliError("ue ui at <x,y> (absolute screen pixels, as ue ui shows positions)") from None
        _, slate = ui_nodes(ctx)
        if not slate:
            raise CliError("ue ui at needs Epic's SlateInspectorToolset (ue init enables it)")
        nodes = ctx.ui_all
        hits = []
        for i, title, rect in windows(nodes):
            if inside(point, rect):
                hits.append(f'window "{title}" at {rect[0]:.0f},{rect[1]:.0f} size {rect[2]:.0f}x{rect[3]:.0f}')
                for n in subtree(nodes, i):
                    b = box(n)
                    if b and b[2] and b[3] and inside(point, b) and (n["label"] or n["role"] in INTERACTIVE):
                        hits.append(f'  {n["role"]}' + (f' "{n["label"]}"' if n["label"] else "") + f"  ({n['ref']})")
        print("\n".join(hits) if hits else f"nothing at {point[0]:.0f},{point[1]:.0f}")
        if sum(1 for h in hits if h.startswith("window")) > 1:
            print("note: several windows overlap here; a click goes to the one on top")
        return 0
    if action in ("type", "key"):
        nodes, slate = ui_nodes(ctx)
        if not slate:
            raise CliError(f"ue ui {action} needs Epic's SlateInspectorToolset (ue init enables it)")
        if action == "key":
            mcp_tool(ctx, "SlateInspectorToolset", "PressKey", {"key": " ".join(args.rest)})
            print(f"pressed {' '.join(args.rest)}")
            return 0
        if len(args.rest) < 2:
            raise CliError("ue ui type <textbox label|ref> <text> [--submit]")
        node = find_ui(nodes, args.rest[0], {"textbox", "spinbox"})
        mcp_tool(ctx, "SlateInspectorToolset", "Type", {"ref": node["ref"], "text": " ".join(args.rest[1:]),
                                                        "submit": args.submit})
        print(f'typed into textbox "{node["label"]}"')
        return 0
    match = " ".join(([action] if action != "show" else []) + args.rest)
    nodes, slate = ui_nodes(ctx)
    if not slate:
        print("(UMG only: enable Epic's SlateInspectorToolset, e.g. with ue init, to also see Slate widgets)")
        print(ctx.tether("umg"))
        return 0
    lines = ui_lines(nodes, args.all, match or None)
    ctx.data = [{k: n[k] for k in ("role", "label", "ref", "flags")} for n in nodes]
    print(f"{len(lines)} widget(s) on screen" + (f" matching /{match}/" if match else "") + (":" if lines else ""))
    for line in lines[: args.limit if args.limit != 30 else 200]:
        print(line)
    return 0


WP_BUILDERS = {"hlod": "WorldPartitionHLODsBuilder", "minimap": "WorldPartitionMiniMapBuilder",
               "nav-wp": "WorldPartitionNavigationDataBuilder", "resave": "WorldPartitionResaveActorsBuilder"}
LIGHTING_DONE = re.compile(r"LogStaticLightingSystem: (?:Warning: )?(Illumination: .*|Failed to build lighting.*|"
                           r".*Skipping Lighting Build.*|Failed to (?:initialize|connect to) Swarm.*)")


DIRTY_PACKAGES = ("import json\nU = unreal.EditorLoadingAndSavingUtils\n"
                  "print(json.dumps([p.get_name() for p in U.get_dirty_map_packages() + U.get_dirty_content_packages()]))")
NAV_DATA_PACKAGES = ("import json\nw = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
                     "print(json.dumps(sorted({a.get_package().get_name() for a in "
                     "unreal.GameplayStatics.get_all_actors_of_class(w, unreal.NavigationData)})))")


def new_log_lines(ctx, start):
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        return f.read().decode("utf-8", "replace").splitlines()


def cmd_world(ctx, args):
    """World builds: navmesh and lighting in the live editor; HLODs and other World Partition builders as jobs."""
    what = args.what.lower()
    if what == "nav":
        dirty_before = set(py_json(ctx, DIRTY_PACKAGES))
        started = time.time()
        ctx.tether("console", command="RebuildNavigation")
        building = "unreal.NavigationSystemV1.is_navigation_being_built(unreal.get_editor_subsystem(" \
                   "unreal.UnrealEditorSubsystem).get_editor_world())"
        time.sleep(0.5)
        while str(ctx.tether("py", code=building)).strip().endswith("True"):
            if time.time() - started > args.wait:
                raise CliError(f"navigation still building after {args.wait:g}s", 4)
            time.sleep(0.5)
        print(f"navigation rebuilt in {time.time() - started:.1f}s")
        print(ctx.tether("nav", action="status", start="", end="", match="", pie=False, limit=args.limit))
        dirty_after = py_json(ctx, DIRTY_PACKAGES)
        changed = [p for p in dirty_after if p not in dirty_before and not p.startswith("/Temp/")]
        # The navmesh lives in the map (or its nav-data actor's package). A map that was already unsaved before the
        # build, e.g. because a nav agent change replaced RecastNavMesh-Default, holds it too: save it as well.
        nav_maps = py_json(ctx, NAV_DATA_PACKAGES) if args.save else []
        held = [p for p in nav_maps if p in dirty_after and p not in changed]
        if held and args.save:
            print(f"also saving {', '.join(held)}: it holds the navmesh and had unsaved changes from before the build")
            changed += held
        if changed and args.save:
            print(ctx.tether("save", paths=",".join(changed)))
        elif changed:
            print(f"{len(changed)} package(s) changed, unsaved (--save, or ue save): " + ", ".join(changed[:5]))
        return 0
    if what == "lighting":
        start, started = log_end(ctx.project), time.time()
        quality = {"preview": "QUALITY_PREVIEW", "medium": "QUALITY_MEDIUM", "high": "QUALITY_HIGH",
                   "production": "QUALITY_PRODUCTION"}[args.quality.lower()]
        ctx.tether("py", code=f"unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).build_light_maps("
                              f"unreal.LightingBuildQuality.{quality}, True)")
        result = None
        while result is None:
            for line in new_log_lines(ctx, start):
                m = LIGHTING_DONE.search(line)
                if m:
                    result = m.group(1)
            if result is None:
                if time.time() - started > args.wait:
                    raise CliError(f"no lighting result after {args.wait:g}s (Lightmass may still run; ue log)", 4)
                time.sleep(1)
        problems = [l for l in filter_log(new_log_lines(ctx, start), level="Warning")
                    if "Lighting" in l or "Lightmass" in l or "MapCheck" in l]
        print(f"lighting ({args.quality}) in {time.time() - started:.0f}s: {result}")
        for line in problems[: args.limit]:
            print("  " + line)
        return 0 if result.startswith("Illumination") else 1
    builder = WP_BUILDERS.get(what, args.what)
    target_map = args.map or (ctx.beacon or {}).get("map")
    if not target_map:
        raise CliError("give the map: ue world build hlod --map /Game/Maps/L")
    live = editor_live(ctx.project)
    if live and live.get("map") == target_map and not args.force:
        raise CliError(f"{target_map} is open in the editor; a builder commandlet would write the same packages. "
                       "Close the editor (ue close) or pass --force")
    engine = engine_root(ctx.project)
    exe = editor_executable(engine)
    cmd_exe = exe.replace("UnrealEditor.exe", "UnrealEditor-Cmd.exe") if os.path.isfile(
        exe.replace("UnrealEditor.exe", "UnrealEditor-Cmd.exe")) else exe
    command = [cmd_exe, ctx.project, target_map, "-run=WorldPartitionBuilderCommandlet", f"-Builder={builder}",
               "-AllowCommandletRendering", "-unattended", "-nosplash", "-SCCProvider=None"]
    cmd_job(ctx, argparse.Namespace(action="start", platform=None, id=None, name=f"{builder} {target_map}",
                                    limit=args.limit, project=args.project, job_command=command))
    if args.no_wait:
        return 0
    jobs = sorted(f[:-5] for f in os.listdir(jobs_dir(ctx.project)) if f.endswith(".json"))
    return cmd_job(ctx, argparse.Namespace(action="wait", id=jobs[-1], wait=args.wait, verbose=False,
                                           limit=args.limit, project=args.project))


ACTIVE_SOUND = re.compile(r"LogAudio: Display: (\S.*?) \(([\d.e+-]+)\) - (.*)$")
WAVE_INSTANCE = re.compile(r"LogAudio: Display: {3,}(\S.*?) \(([\d.e+-]+)\) \((\d+)\) - ([\d.e+-]+)")


def parse_active_sounds(lines):
    """The sounds listed by au.DumpActiveSounds (the last dump in lines), each with its wave instances."""
    start = max((i for i, l in enumerate(lines) if "Active Sound Count:" in l), default=None)
    if start is None:
        return None
    sounds = []
    for line in lines[start + 1:]:
        wave = WAVE_INSTANCE.search(line)
        if wave and sounds:
            sounds[-1]["waves"].append({"wave": wave.group(1), "volume": float(wave.group(4))})
            continue
        sound = ACTIVE_SOUND.search(line)
        if sound and not line.split("Display: ", 1)[-1].startswith(" "):
            sounds.append({"sound": sound.group(1), "duration": float(sound.group(2)),
                           "component": sound.group(3).strip(), "waves": []})
    return sounds


def active_sounds(ctx):
    """Every sound the audio device is playing for PIE, fire-and-forget ones included (au.DumpActiveSounds)."""
    start = log_end(ctx.project)
    ctx.tether("console", command="au.DumpActiveSounds", pie=True)
    deadline = time.time() + 5
    while time.time() < deadline:  # the dump is written from the audio thread
        sounds = parse_active_sounds(new_log_lines(ctx, start))
        if sounds is not None:
            time.sleep(0.3)
            return parse_active_sounds(new_log_lines(ctx, start)) or []
        time.sleep(0.3)
    raise CliError("au.DumpActiveSounds wrote nothing: this editor has no audio device "
                   "(reopen with `ue open --headless --sound`, which keeps it silent)")


def cmd_audio(ctx, args):
    if getattr(args, "match", None) == "mix":
        return audio_mix(ctx, args)
    """Sounds playing in PIE: what, from where, how loud."""
    if not (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is not running (ue pie start)")
    sounds = active_sounds(ctx)
    comps = ctx.tether("audio_components", limit=200)
    comps = json.loads(comps) if isinstance(comps, str) else comps
    rx = re.compile(args.match, re.I) if args.match else None
    rows = []
    # The dump names a component by its short name only ("AudioComponent" on every actor that has one), so pair
    # on the sound too, and use each component once; components the dump missed are listed after.
    unused = list(comps)
    for s in sounds:
        comp = next((c for c in unused if c["component"] == s["component"] and c.get("sound") == s["sound"]), None)
        if comp:
            unused.remove(comp)
        length = "loop" if s["duration"] >= 10000 else f"{s['duration']:.1f} s"
        volume = max((w["volume"] for w in s["waves"]), default=None)
        row = (f"{s['sound']} ({length})" + (f" on {comp['owner']}" if comp and comp.get("owner") else
                                              f" via {s['component']}" if s["component"] not in ("", "None") else "")
               + (f", {comp['distance']} cm away" if comp and comp.get("distance") is not None else "")
               + (f", volume {volume:.2f}" if volume is not None else "")
               + (f", attenuation {comp['attenuation']}" if comp and comp.get("attenuation") else ""))
        if not rx or rx.search(row):
            rows.append(row)
    for comp in unused:  # playing components the device dump did not list (virtualized, or starting)
        row = (f"{comp.get('sound')} on {comp.get('owner') or comp['component']}"
               + (f", {comp['distance']} cm away" if comp.get("distance") is not None else "")
               + (f", attenuation {comp['attenuation']}" if comp.get("attenuation") else "") + " (component only)")
        if not rx or rx.search(row):
            rows.append(row)
    ctx.data = {"sounds": sounds, "components": comps}
    print(f"{len(rows)} sound(s) playing" + (f" matching /{args.match}/" if args.match else "") + (":" if rows else ""))
    for row in rows[: args.limit]:
        print("  " + row)
    return 0


def process_memory_mb(pid):
    """A process's private memory in MB (Windows: PrivateUsage; elsewhere: VmRSS), or None."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                                "PrivateUsage")]
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))  # query limited information | vm read
        if not handle:
            return None
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ok = psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return counters.PrivateUsage / 1024 ** 2 if ok else None
    try:
        with open(f"/proc/{int(pid)}/status") as f:
            match = re.search(r"VmRSS:\s+(\d+) kB", f.read())
        return int(match.group(1)) / 1024 if match else None
    except OSError:
        return None


def slope_per_minute(samples):
    """Least-squares slope of (seconds, value) samples, per minute."""
    if len(samples) < 3:
        return 0.0
    n = len(samples)
    mx, my = sum(t for t, _ in samples) / n, sum(v for _, v in samples) / n
    var = sum((t - mx) ** 2 for t, _ in samples)
    return 0.0 if not var else 60 * sum((t - mx) * (v - my) for t, v in samples) / var


def cmd_soak(ctx, args):
    """A long PIE run (a step file in a loop, or wandering) watching memory, frame rate and errors."""
    beacon = ctx.beacon or {}
    pid = beacon.get("pid")
    if not editor_live(ctx.project):
        raise CliError("the editor is not running (ue open --headless)")
    log_start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running"
    if not was_running and cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=2.0, limit=0, script=None)):
        raise CliError("could not start PIE")
    frames = lambda: int(str(ctx.tether("py", code="unreal.SystemLibrary.get_frame_count()")).strip().splitlines()[-1])
    wander = ("import random\nw = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
              "p = unreal.GameplayStatics.get_player_pawn(w, 0)\n"
              # with navigation off there is no navigation system, and querying one trips an engine ensure
              "nav = unreal.NavigationSystemV1.get_navigation_system(w)\n"
              "goal = unreal.NavigationSystemV1.get_random_reachable_point_in_radius(w, p.get_actor_location(), 3000) "
              "if nav else None\n"
              "if goal is None:\n    loc = p.get_actor_location()\n"
              "    goal = unreal.Vector(loc.x + random.uniform(-800, 800), loc.y + random.uniform(-800, 800), loc.z)\n"
              "    p.set_actor_location(goal, False, True)\n"
              "else:\n    unreal.AIHelperLibrary.simple_move_to_location(unreal.GameplayStatics.get_player_controller(w, 0), goal)\n"
              "print('walk' if goal else 'hop')")
    samples, fps, failures, crashed, iterations = [], [], [], None, 0
    deadline = started + args.minutes * 60
    next_sample, last_frames, last_time = time.time(), frames(), time.time()
    try:
        while time.time() < deadline:
            if args.file:
                code = pie_do(ctx, argparse.Namespace(script=[], file=args.file, reject=None, warmup=0, keep=True,
                                                      keep_going=False, quiet=True))
                if code:
                    failures.append(f"iteration {iterations + 1}: the step file failed")
                    if not args.keep_going:
                        break
            else:
                ctx.tether("py", code=wander)
                time.sleep(min(args.sample, 5))
            iterations += 1
            if time.time() >= next_sample:
                memory = process_memory_mb(pid)
                now, count = time.time(), frames()
                if memory is not None:
                    samples.append((now - started, memory))
                fps.append((count - last_frames) / max(now - last_time, 1e-3))
                last_frames, last_time, next_sample = count, now, now + args.sample
    except CliError as exc:
        crashed = recent_crash(ctx.project, started - 5)
        failures.append(f"stopped: {exc}")
    finally:
        if not crashed and not was_running and editor_live(ctx.project):
            with __import__("contextlib").suppress(CliError):
                cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    elapsed = (time.time() - started) / 60
    errors = []
    if os.path.isfile(log_path(ctx.project)):
        with open(log_path(ctx.project), "rb") as f:
            f.seek(log_start)
            errors = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Error")
    growth = slope_per_minute(samples[1:])  # the first sample includes PIE start-up
    print(f"soak {elapsed:.1f} min, {iterations} iteration(s)" + (f" of {os.path.basename(args.file)}" if args.file else " of wandering"))
    if samples:
        print(f"  memory {samples[0][1]:.0f} -> {samples[-1][1]:.0f} MB (peak {max(v for _, v in samples):.0f}), "
              f"trend {growth:+.1f} MB/min over {len(samples)} samples")
    if fps:
        print(f"  frame rate avg {sum(fps) / len(fps):.0f} fps, lowest window {min(fps):.0f} fps")
    print(f"  {len(errors)} error line(s) logged" + (":" if errors else ""))
    for line in errors[: args.limit]:
        print("    " + line)
    if crashed:
        print("  CRASHED: " + crash_summary(crashed) + " (ue crash)")
    for failure in failures[:5]:
        print("  " + failure)
    leaking = len(samples) >= 4 and growth > args.max_growth
    if leaking:
        print(f"  memory grows {growth:.1f} MB/min, over --max-growth {args.max_growth:g}")
    failed = bool(crashed or failures or leaking)
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


@contextlib.contextmanager
def unthrottled(ctx):
    """Lift "Use Less CPU when in Background" for a while. A headless or background editor otherwise runs PIE at a
    few frames per second, game time included. Set directly, not with set_props: an editor setting is not level
    work, and a transaction would put it in the undo history (`ue undo` would then "undo" the restore)."""
    throttle = ("s = unreal.find_object(None, '/Script/UnrealEd.Default__EditorPerformanceSettings')\n"
                "s.set_editor_property('bThrottleCPUWhenNotForeground', {})\n"
                "s.get_editor_property('bThrottleCPUWhenNotForeground')")
    throttled = str(ctx.tether("py", code=throttle.format("s.get_editor_property('bThrottleCPUWhenNotForeground')"))
                    ).strip().endswith("True")
    if throttled:
        ctx.tether("py", code=throttle.format(False))
    try:
        yield
    finally:
        if throttled:
            ctx.tether("py", code=throttle.format(True))


GPU_LINE = re.compile(r"^( *)(\d+(?:\.\d+)?)%\s+([\d.]+)ms(?: \+ ([\d.]+) Wait)?\s+(.*?)\s*$")
GPU_HEAD = re.compile(r"GPU Profile for Frame (\d+) - (.+?) - GPU (\d+)")
GPU_KINDS = [  # (kind, name regex), first match from a pass up through its parents
    ("UI", r"Slate"), ("Lumen", r"Lumen"), ("shadows", r"Shadow"), ("Nanite", r"Nanite"),
    ("ray tracing", r"RayTracing|PathTracing"), ("ambient occlusion", r"AmbientOcclusion|ScreenSpace AO|^AO\b"),
    ("reflections", r"Reflection|SSR\b"), ("translucency", r"Translucen"), ("particles", r"Niagara|GPUParticles|FXSystem"),
    ("fog, sky and clouds", r"Volumetric|Fog|Cloud|SkyAtmosphere|SkyPass|^Sky"),
    ("post processing", r"PostProcess|TemporalSuperResolution|^TSR|Bloom|Tonemap|MotionBlur|DOF|DepthOfField|Upscale"),
    ("lighting", r"Light|Deferred"), ("base pass", r"^BasePass"), ("depth pre-pass", r"^PrePass|DepthPass"),
    ("visibility and culling", r"HZB|Occlusion|Cull|GPUScene|InstanceCulling|Visibility|BuildRenderingCommands"),
]


def gpu_profile_blocks(lines):
    """Every ProfileGPU block in log lines (plain-text format): [{frame, pipeline, frame_ms, nodes}], each node
    {name, depth, ms, wait, children: [indexes]} in tree order; node 0 is <root>."""
    blocks, block, base = [], None, None
    for raw in lines:
        text = re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", raw)
        text = re.sub(r"^LogRHI: (?:Display: )?", "", text)
        head = GPU_HEAD.search(text)
        if head:
            block = {"frame": int(head.group(1)), "pipeline": head.group(2), "frame_ms": None, "nodes": []}
            blocks.append(block)
            base = None
            continue
        if block is None:
            continue
        m = re.search(r"- Frame Time\s*:\s*([\d.]+)ms", text)
        if m:
            block["frame_ms"] = float(m.group(1))
            continue
        m = GPU_LINE.match(text)
        if not m:
            if block["nodes"] and not text.strip():
                block = None  # a blank line ends the tree
            continue
        indent = len(m.group(1))
        base = indent if base is None else base
        block["nodes"].append({"name": m.group(5), "depth": (indent - base) // 3, "ms": float(m.group(3)),
                               "wait": float(m.group(4) or 0), "children": []})
    for block in blocks:
        stack = []
        for index, node in enumerate(block["nodes"]):
            while stack and block["nodes"][stack[-1]]["depth"] >= node["depth"]:
                stack.pop()
            node["parent"] = stack[-1] if stack else None
            if stack:
                block["nodes"][stack[-1]]["children"].append(index)
            stack.append(index)
    return blocks


def gpu_kind(block, index):
    node = index
    while node is not None:
        name = block["nodes"][node]["name"]
        for kind, rx in GPU_KINDS:
            if re.search(rx, name):
                return kind
        node = block["nodes"][node]["parent"]
    return "other"


def gpu_summary(blocks):
    """Average the graphics-pipeline blocks: {frames, gpu_ms, kinds: {kind: ms}, passes: [(path, ms)], scene}."""
    graphics = [b for b in blocks if b["nodes"] and "Graphics" in b["pipeline"]]
    if not graphics:
        return None
    kinds, passes, total = {}, {}, 0.0
    scene_seen = False
    for block in graphics:
        nodes = block["nodes"]
        total += nodes[0]["ms"]
        for index, node in enumerate(nodes):
            exclusive = max(0.0, node["ms"] - sum(nodes[c]["ms"] for c in node["children"]))
            if exclusive and index:
                kind = gpu_kind(block, index)
                kinds[kind] = kinds.get(kind, 0.0) + exclusive
            if node["name"] == "Scene" or node["name"].startswith("SceneRender"):
                scene_seen = True
        scene = next((i for i, n in enumerate(nodes) if n["name"] == "Scene"), None)
        # The passes worth naming: the Scene's children, and whatever else the frame did outside the scene.
        frame = next((i for i in nodes[0]["children"] if nodes[i]["name"].startswith("Frame")), 0)
        roots = list(nodes[scene]["children"]) if scene is not None else []
        roots += [c for c in nodes[frame]["children"] if c != scene and not _contains(nodes, c, scene)]
        roots += [c for c in nodes[0]["children"] if c != frame]
        for c in roots:
            passes[nodes[c]["name"]] = passes.get(nodes[c]["name"], 0.0) + nodes[c]["ms"]
    n = len(graphics)
    return {"frames": n, "gpu_ms": round(total / n, 3), "scene": scene_seen,
            "frame_numbers": [b["frame"] for b in graphics],
            "kinds": {k: round(v / n, 3) for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])},
            "passes": sorted(((name, round(ms / n, 3)) for name, ms in passes.items()), key=lambda p: -p[1])}


def _contains(nodes, ancestor, index):
    while index is not None:
        if index == ancestor:
            return True
        index = nodes[index]["parent"]
    return False


GPU_CVARS = {"r.ProfileGPU.ShowUI": "0", "r.ProfileGPU.TableFormatting": "0", "r.ProfileGPU.UnicodeOutput": "0",
             "r.ProfileGPU.ShowEmptyQueues": "0"}


def cmd_gpu(ctx, args):
    """GPU time per pass from ProfileGPU (PIE if it runs, else the editor's viewports), averaged over --frames."""
    pie = str(ctx.tether("pie", action="status")).strip() == "running"
    if args.pie and not pie:
        raise CliError("PIE is not running (ue pie start)")
    blocks, timeout = [], time.time() + 20 + 3 * args.frames
    with unthrottled(ctx), scoped_cvars(ctx, GPU_CVARS):  # ShowUI=0: no profiler window on the user's screen
        for _ in range(args.frames):
            start = log_end(ctx.project)
            ctx.tether("console", command="ProfileGPU", pie=pie)
            got = []
            while time.time() < timeout:
                flush_editor_log(ctx)
                with open(log_path(ctx.project), "rb") as f:
                    f.seek(start)
                    got = gpu_profile_blocks(f.read().decode("utf-8", "replace").splitlines())
                if any("Graphics" in b["pipeline"] and b["nodes"] for b in got):
                    time.sleep(0.2)  # let the other pipelines' blocks land too
                    break
                time.sleep(0.3)
            else:
                raise CliError("no GPU profile in the log (does this editor render? ue open --headless needs --render)", 4)
            blocks += got
    summary = gpu_summary(blocks)
    if summary is None:
        raise CliError("ProfileGPU logged no graphics pipeline")
    where = "PIE" if pie else "editor"
    print(f"GPU {summary['gpu_ms']:.2f} ms per frame ({where}, " + (f"average of {summary['frames']} frames"
          if summary["frames"] > 1 else f"frame {summary['frame_numbers'][0]}") + ")")
    if not summary["scene"]:
        print("note: no 3D scene was rendered in " + ("that frame" if summary["frames"] == 1 else "those frames")
              + (": the PIE viewport is not drawing" if pie else ": start PIE (ue pie start) or make a level viewport "
                 "realtime") + "; what is left is editor UI")
    total = summary["gpu_ms"] or 1.0
    kinds = [(k, ms) for k, ms in summary["kinds"].items() if ms >= args.min]
    print("by kind: " + ", ".join(f"{k} {ms:.2f} ms ({100 * ms / total:.0f}%)" for k, ms in kinds[: args.top]))
    print("passes:")
    for name, ms in [p for p in summary["passes"] if p[1] >= args.min][: args.top]:
        print(f"  {ms:7.2f} ms {100 * ms / total:4.0f}%  {short(name, 100)}")
    other = [b for b in blocks if b["nodes"] and "Graphics" not in b["pipeline"] and b["nodes"][0]["ms"] >= args.min]
    for b in other[:2]:
        print(f"{b['pipeline']}: {b['nodes'][0]['ms']:.2f} ms (overlaps the graphics pipeline)")
    if args.tree:
        block = next(b for b in reversed(blocks) if "Graphics" in b["pipeline"] and b["nodes"])
        print(f"tree (frame {block['frame']}, >= {args.min} ms):")
        for node in block["nodes"]:
            if node["ms"] >= args.min and node["depth"] <= args.tree:
                print(f"  {'  ' * node['depth']}{node['ms']:.2f} ms  {short(node['name'], 100)}")
    ctx.data = summary
    return 0


def read_profile_csv(path):
    """{column: [floats per frame]} from a CsvProfiler capture (the header repeats at the end)."""
    import csv
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))  # the EVENTS column can hold more than the 128 KB default
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    header, columns = rows[0], {}
    for row in rows[1:]:
        if row and row[0] == header[0] and row[1:3] == header[1:3]:
            break  # repeated header: metadata follows
        for name, cell in zip(header, row):
            try:
                columns.setdefault(name, []).append(float(cell))
            except ValueError:
                pass
    return columns


def frame_stats(values):
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return {"avg": sum(values) / len(values), "p50": pick(0.5), "p95": pick(0.95), "max": ordered[-1]}


def cmd_perf(ctx, args):
    """Frame-time capture through the CSV profiler: frame/game/render/GPU, hitches, change vs last capture."""
    args.seconds = args.seconds or 10
    if ctx.transport == "remote":
        raise CliError("ue perf needs UE 5.8+ in this version")
    toolset = resolve_toolset(ctx, "editorapp")
    started_pie = False
    if unwrap(ctx.mcp.call(toolset, "IsPIERunning", {})[0]) is not True and not args.editor:
        if cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=1.0, limit=0, script=None)):
            raise CliError("could not start PIE to measure (or pass --editor to measure the editor itself)")
        started_pie = True
    folder = os.path.join(os.path.dirname(ctx.project), "Saved", "Profiling", "CSV")
    before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    # A world context routes the command to the PIE world, where CsvProfile does nothing; None reaches it.
    path, complete = None, False
    unthrottle = unthrottled(ctx)
    unthrottle.__enter__()  # frame times mean nothing at a background editor's few fps
    try:
        if args.trace:  # an Unreal Insights trace over the same window, for per-scope timings
            trace_dir = os.path.join(state_dir(ctx.project), "traces")
            os.makedirs(trace_dir, exist_ok=True)
            trace_path = os.path.join(trace_dir, time.strftime("perf-%Y%m%d-%H%M%S.utrace")).replace("\\", "/")
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'trace.file \"{trace_path}\" cpu,frame')")
        ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'CsvProfile Start')")
        # The editor keeps recent trace data in a ring buffer and trace.file writes that history too, so the
        # capture window is marked as a region and the export is limited to it.
        region = time.strftime("TetherPerf%H%M%S")
        if args.trace:
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'Trace.RegionBegin {region}')")
        time.sleep(args.seconds)
        if args.trace:
            ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, 'Trace.RegionEnd {region}')")
        ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'CsvProfile Stop')")
        if args.trace:
            ctx.tether("py", code="unreal.SystemLibrary.execute_console_command(None, 'trace.stop')")
        deadline = time.time() + 30
        while time.time() < deadline and not complete:  # the writer finishes the file with a metadata footer
            new = [f for f in (os.listdir(folder) if os.path.isdir(folder) else [])
                   if f.endswith(".csv") and f not in before]
            if new:
                path = os.path.join(folder, max(new, key=lambda f: os.path.getmtime(os.path.join(folder, f))))
                try:
                    with open(path, "rb") as f:
                        f.seek(max(0, os.path.getsize(path) - 4096))
                        complete = b"[HasHeaderRowAtEnd]" in f.read()
                except PermissionError:  # still open for writing (Windows locks it)
                    pass
            time.sleep(0.5)
    finally:
        unthrottle.__exit__(None, None, None)
        if started_pie:
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    if not path or not complete:
        raise CliError("the CSV profiler wrote no capture (ue log --grep Csv)", 4)
    columns = read_profile_csv(path)
    frames = columns.get("FrameTime", [])
    if not frames:
        raise CliError(f"no FrameTime column in {path}")
    stats = {name: frame_stats(columns[name]) for name in ("FrameTime", "GameThreadTime", "RenderThreadTime", "GPUTime")
             if columns.get(name)}
    ft = stats["FrameTime"]
    threads = {n.replace("ThreadTime", "").replace("Time", ""): s["avg"] for n, s in stats.items() if n != "FrameTime"}
    timed = {k: v for k, v in threads.items() if v > 0}  # -nullrhi records no thread or GPU timings
    bound = max(timed, key=timed.get) if timed else None
    capped = bool(timed) and max(timed.values()) < 0.85 * ft["p50"]  # the frame waits on a cap, not on work
    hitch_limit = max(2 * ft["p50"], 33.3)
    hitches = [(i, t) for i, t in enumerate(frames) if t > hitch_limit]
    last_file = os.path.join(state_dir(ctx.project), "perf", "last.json")
    last = read_json(last_file)
    delta = lambda key: (f" ({ft[key] - last['FrameTime'][key]:+.1f})" if last and last.get("FrameTime") else "")
    print(f"{len(frames)} frames in {args.seconds:g}s{' of PIE' if not args.editor else ''}: frame avg "
          f"{ft['avg']:.1f} ms{delta('avg')} ({1000 / ft['avg']:.0f} fps), p95 {ft['p95']:.1f}{delta('p95')}, "
          f"max {ft['max']:.1f}")
    if timed:
        column = {"Game": "GameThreadTime", "Render": "RenderThreadTime", "GPU": "GPUTime"}
        moved = lambda k, v: (f" ({v - last[column[k]]['avg']:+.1f})" if last and last.get(column.get(k, ""), {}).get("avg")
                              else "")
        print("  per frame: " + ", ".join(f"{k} {v:.1f} ms{moved(k, v)}" for k, v in timed.items()) + f" | bound by {bound}"
              + (f" | frame rate capped at ~{1000 / ft['p50']:.0f} fps, so compare the per-frame work" if capped else ""))
    else:
        print("  no game/render/GPU timings: the editor runs with -nullrhi (`ue open --headless --render` for them)")
    if hitches:
        worst = sorted(hitches, key=lambda h: -h[1])[:3]
        print(f"  {len(hitches)} hitches over {hitch_limit:.0f} ms; worst: "
              + ", ".join(f"frame {i} {t:.0f} ms" for i, t in worst))
    else:
        print(f"  no hitches over {hitch_limit:.0f} ms")
    if columns.get("PhysicalUsedMB"):
        print(f"  memory {columns['PhysicalUsedMB'][-1]:.0f} MB used")
    if last:
        print(f"  (+/- vs last capture, {time.strftime('%H:%M', time.localtime(last.get('time', 0)))})")
    print(f"  csv: {path}")
    ctx.data = dict(stats, frames=len(frames), hitches=len(hitches), bound=bound, capped=capped, csv=path)
    if not args.no_record:  # the baseline the next capture compares with
        write_json(last_file, dict(stats, time=time.time(), csv=path))
    if args.trace:
        perf_trace_report(ctx, trace_path, region, len(frames), args)
    return 0


# Insights adds the GPU queues to every timer-statistics export whatever the thread filter, so "gpu" (a filter
# matching no CPU thread) yields the GPU passes alone, and they are subtracted from the CPU threads.
TRACE_THREADS = (("game", "GameThread"), ("render", "RenderThread*"), ("rhi", "RHIThread"), ("gpu", "NoCpuThread"))
TIMER_FORMAT = re.compile(r"\(|\s=\s|\s%")  # 'TSR(sg.AntiAliasingQuality=%d) %dx%d' -> 'TSR'
WAIT_SCOPE = re.compile(r"Wait|Sleep|Idle|UpdateTimeAndHandleMaxTickRate|Stall", re.I)  # time spent not working


def read_timer_stats(path):
    """{timer: (count, inclusive s, exclusive s)} from an Insights ExportTimerStatistics CSV."""
    import csv
    stats = {}
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    name = TIMER_FORMAT.split(row["Name"], 1)[0].strip() or row["Name"]
                    count, incl, excl = stats.get(name, (0, 0.0, 0.0))  # same-named timers add up
                    stats[name] = (count + int(float(row["Count"])), incl + float(row["Incl"]), excl + float(row["Excl"]))
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return stats


def cpu_only(by_thread):
    """Each CPU thread's timers minus the GPU passes that Insights mixes into every export."""
    gpu = by_thread.get("gpu") or {}
    out = {"gpu": gpu}
    for key, stats in by_thread.items():
        if key == "gpu":
            continue
        out[key] = {}
        for name, (count, incl, excl) in stats.items():
            g = gpu.get(name, (0, 0.0, 0.0))
            left = (count - g[0], incl - g[1], excl - g[2])
            if left[0] > 0 and left[2] > 1e-9:
                out[key][name] = left
    return out


def trace_scopes(by_thread, frames, top=6, last=None):
    """Lines naming each thread's most expensive scopes by exclusive ms per frame, and the per-frame data."""
    frames = max(frames, 1)
    lines, data = [], {}
    for key, _ in TRACE_THREADS:
        stats = by_thread.get(key) or {}
        ranked = sorted(((excl * 1000 / frames, name) for name, (count, incl, excl) in stats.items()
                         if name != "Frame" and excl > 0 and not WAIT_SCOPE.search(name)), reverse=True)
        waiting = sum(excl for name, (_, _, excl) in stats.items() if WAIT_SCOPE.search(name)) * 1000 / frames
        data[key] = {name: round(ms, 3) for ms, name in ranked[:40]}
        if not ranked:
            continue
        before = (last or {}).get(key, {})
        parts = [f"{name} {ms:.2f}" + (f" ({ms - before[name]:+.2f})" if name in before and abs(ms - before[name]) >= 0.05
                                       else "") for ms, name in ranked[:top]]
        label = "gpu passes (timer totals, queues may overlap)" if key == "gpu" else key
        lines.append(f"  {label}: " + ", ".join(parts) + (f" | waiting {waiting:.1f}" if waiting >= 0.05 else ""))
    return lines, data


def insights_exe(project):
    return os.path.join(engine_root(project), "Engine", "Binaries", host_platform(),
                        "UnrealInsights" + (".exe" if os.name == "nt" else ""))


def insights_export(exe, trace_path, out_dir, commands):
    """Run Unreal Insights headless on a trace with these export commands; None, or what went wrong."""
    os.makedirs(out_dir, exist_ok=True)
    script = os.path.join(out_dir, "export.txt")
    with open(script, "w", encoding="utf-8") as f:
        f.write("\n".join(commands) + "\n")
    try:
        subprocess.run([exe, f"-OpenTraceFile={trace_path}", "-AutoQuit", "-NoUI", f"-ExecOnAnalysisCompleteCmd=@={script}",
                        f"-ABSLOG={os.path.join(out_dir, 'insights.log')}", "-unattended"],
                       capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "Unreal Insights did not finish the export within 10 minutes"
    return None


def perf_trace_report(ctx, trace_path, region, frames, args):
    """Export the trace's timer statistics with Unreal Insights (headless) and print the top scopes."""
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.isfile(trace_path):
        time.sleep(0.5)
    exe = insights_exe(ctx.project)
    if not os.path.isfile(trace_path) or not os.path.isfile(exe):
        print("  trace: " + ("no trace was written (ue log --grep Trace)" if not os.path.isfile(trace_path)
                             else f"UnrealInsights not found at {exe}"))
        return
    out_dir = os.path.splitext(trace_path)[0]
    problem = insights_export(exe, trace_path, out_dir, [
        f'TimingInsights.ExportTimerStatistics "{out_dir}/{key}.csv" -threads={threads} -region={region}'
        for key, threads in TRACE_THREADS])
    if problem:
        print("  trace: " + problem)
        return
    by_thread = {key: read_timer_stats(os.path.join(out_dir, f"{key}.csv")) for key, _ in TRACE_THREADS}
    by_thread = cpu_only(by_thread)
    last_file = os.path.join(state_dir(ctx.project), "perf", "last-trace.json")
    last = read_json(last_file)
    lines, data = trace_scopes(by_thread, frames, args.top, last)
    if not lines:
        print(f"  trace: no CPU scopes in the export (see {os.path.join(out_dir, 'insights.log')})")
        return
    print(f"  top scopes, exclusive ms per frame over {frames} frames" + (" (+/- vs last trace)" if last else "") + ":")
    for line in lines:
        print(line)
    print(f"  trace: {trace_path} (open it in Unreal Insights for the full timeline)")
    if not args.no_record:
        write_json(last_file, data)
    old_traces = sorted(f for f in os.listdir(os.path.dirname(trace_path)) if f.startswith("perf-") and f.endswith(".utrace"))
    for name in old_traces[:-3]:  # ~10 MB a second: keep the last three
        with __import__("contextlib").suppress(OSError):
            os.remove(os.path.join(os.path.dirname(trace_path), name))
            __import__("shutil").rmtree(os.path.join(os.path.dirname(trace_path), name[:-7]), ignore_errors=True)


def pie_run(ctx, args):
    """A scripted gameplay check: PIE, a script in the game world, log expectations, then stop. Exit 0/1."""
    if not args.script:
        raise CliError("give a script: ue pie run checks.py --expect '<log regex>'")
    with open(args.script[0], encoding="utf-8-sig") as f:
        code = f.read()
    expects = [re.compile(e, re.I) for e in args.expect or []]
    rejects = [re.compile(r, re.I) for r in args.reject or []]
    start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running" if ctx.transport == "remote" else \
        unwrap(ctx.mcp.call(resolve_toolset(ctx, "editorapp"), "IsPIERunning", {})[0]) is True
    if not was_running:
        code_start = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup,
                                                     limit=0, script=None))
        if code_start:
            return code_start
    failures = []
    try:
        out = ctx.tether("py", code=code)
        if out:
            print("script: " + out.rstrip("\n").replace("\n", "\nscript: "))
    except CliError as exc:
        failures.append(f"script raised: {exc}")
    found, rejected = {}, []
    seconds = args.seconds or 5
    deadline = time.time() + seconds
    while True:
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()
        for rx in expects:
            if rx.pattern not in found:
                hit = next((l for l in lines if rx.search(l)), None)
                if hit:
                    found[rx.pattern] = hit
        rejected = [(rx.pattern, l) for rx in rejects for l in lines if rx.search(l)]
        if rejected or len(found) == len(expects) or time.time() > deadline:
            break
        time.sleep(0.25)
    if not (args.keep or was_running):
        cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    for rx in expects:
        hit = found.get(rx.pattern)
        print(f"{'ok  ' if hit else 'MISS'} /{rx.pattern}/" + (f"  {hit.split(']', 2)[-1].strip()[:140]}" if hit else ""))
        if not hit:
            failures.append(f"no log line matched /{rx.pattern}/ within {seconds:g}s")
    for pattern, line in rejected[:5]:
        print(f"BAD  /{pattern}/  {line.split(']', 2)[-1].strip()[:140]}")
        failures.append(f"rejected pattern /{pattern}/ appeared")
    print(("PASS" if not failures else "FAIL") + f" in {time.time() - started:.1f}s"
          + ("" if not failures else ": " + "; ".join(dict.fromkeys(failures))))
    return 1 if failures else 0


WITHIN = re.compile(r"\s+within\s+([\d.]+)\s*s?\s*$", re.I)


def start_film(ctx, args):
    """Capture the PIE view every `args.film` seconds on a second connection while the steps run."""
    import threading
    from .transport import Ctx
    state = {"frames": [], "stop": threading.Event(), "t0": time.time(), "errors": 0, "hud": {}, "widget": {},
             "how": "viewport widget"}

    def loop():
        own = Ctx(ctx.args)  # its own MCP connection: the steps keep theirs
        while not state["stop"].is_set():
            began = time.time()
            try:
                # The viewport widget through Epic's Slate inspector (UMG HUD included, ~0.05 s), else the window
                # capture (~1.4 s). HighResShot at this cadence starved game time almost to a stop.
                # Kept as captured (PNG + crop rectangle): decoding is slow in Python, so it waits for the sheet.
                captured = widget_capture(own, state["widget"]) if state["how"] == "viewport widget" else None
                if captured is None:
                    state["how"] = "window"
                    captured = (hud_screenshot(own, state["hud"]), None)
                state["frames"].append((time.time() - state["t0"],) + tuple(captured))
            except Exception:  # noqa: BLE001 - PIE ending under it, a slow frame: keep the rest of the film
                state["errors"] += 1
            state["stop"].wait(max(0.0, args.film - (time.time() - began)))
    state["thread"] = threading.Thread(target=loop, daemon=True)
    state["thread"].start()
    return state


def finish_film(ctx, args, state):
    """Stop the film and lay its frames out as one timestamped contact sheet."""
    state["stop"].set()
    state["thread"].join(timeout=30)
    frames = state["frames"]
    if not frames:
        print("film: no frames captured" + (f" ({state['errors']} failed)" if state["errors"] else ""))
        return
    if len(frames) > 48:  # a long film: evenly spaced frames, first and last included
        step = (len(frames) - 1) / 47
        frames = [frames[round(i * step)] for i in range(48)]
    tiles = []
    for t, raw, rect in frames:
        try:
            image = png_read(raw)
            tiles.append((t, crop_pixels(image, rect) if rect else image))
        except CliError:
            pass
    columns = min(4, len(tiles))
    width, height, rgb = contact_sheet([tile for _, tile in tiles], columns=columns, tile_width=320)
    sheet = bytearray(rgb)
    tile_h = max(round(tile[1] * 320 / tile[0]) for _, tile in tiles)
    for i, (t, _) in enumerate(tiles):  # each tile's time, top left
        x0, y0 = 4 + (i % columns) * 324 + 4, 4 + (i // columns) * (tile_h + 4) + tile_h - 14  # bottom left
        draw_text(sheet, width, x0, y0, f"{t:.1f}")
    path = getattr(args, "film_output", None) or os.path.join(state_dir(ctx.project), "shots",
                                                             time.strftime("film-%Y%m%d-%H%M%S.png"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    png_write(path, width, height, bytes(sheet))
    taken = state["frames"]
    gaps = [b[0] - a[0] for a, b in zip(taken, taken[1:])]
    actual = sum(gaps) / len(gaps) if gaps else args.film
    slow = f" (asked {args.film:g}s; a {state['how']} capture takes about that long)" if actual > args.film * 1.5 else ""
    note = "" if state["how"] == "window" else "; canvas-drawn text (AHUD, debug messages) is not in widget captures"
    print(f"film: {len(taken)} frame(s), one every {actual:.2f}s{slow}" + (f", {len(tiles)} on the sheet" if len(taken) > len(tiles) else "")
          + f" -> {path}" + note)


def pie_do(ctx, args):
    """Drive the player through a list of steps in PIE and check the results. Exit 0/1."""
    steps, wanted_map = list(args.script or []), None
    if args.file:
        with open(args.file, encoding="utf-8-sig") as f:
            for line in f:
                header = re.match(r"\s*#\s*map:\s*(\S+)", line)
                if header:
                    wanted_map = header.group(1).split(".")[0]
                elif line.strip() and not line.strip().startswith("#"):
                    steps.append(line.strip())
    if not steps:
        raise CliError('give steps: ue pie do "at PlayerStart" "tap IA_Jump" "until player.location ~ ..." '
                       "(or -f steps.txt, one per line)")
    rejects = [re.compile(r, re.I) for r in args.reject or []]
    log_start, started = log_end(ctx.project), time.time()
    was_running = ctx.tether("pie", action="status") == "running"
    if wanted_map and not was_running and (ctx.beacon or {}).get("map") != wanted_map:  # the file's '# map:' header
        dirty = ctx.tether("py", code="len([p for p in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages() "
                                      "if not p.get_name().startswith('/Temp/')])")  # an untitled map is always dirty
        if str(dirty).strip() not in ("0", ""):
            raise CliError(f"{args.file} plays on {wanted_map}, but the open map has unsaved changes (ue changes)")
        ctx.tether("py", code=f"unreal.EditorLoadingAndSavingUtils.load_map({wanted_map!r})")
        print(f"loaded {wanted_map}")
    if not was_running:
        code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup, limit=0, script=None))
        if code:
            return code

    def tool(step):
        result = ctx.tether("pie_step", step=step)
        result = json.loads(result) if isinstance(result, str) else result
        return bool(result["ok"]), result.get("detail", "")

    def log_lines():
        flush_editor_log(ctx)
        with open(log_path(ctx.project), "rb") as f:
            f.seek(log_start)
            return f.read().decode("utf-8", "replace").splitlines()

    def poll(check, seconds):
        deadline = time.time() + seconds
        while True:
            ok, detail = check()
            if ok or time.time() > deadline:
                return ok, detail + ("" if ok else f" after {seconds:g}s")
            time.sleep(0.25)

    def run(step):
        verb, _, rest = step.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        within = WITHIN.search(rest)
        seconds = float(within.group(1)) if within else None
        rest = rest[: within.start()].strip() if within else rest
        if verb == "wait":
            amount, _, clock = (rest or "1").partition(" ")
            if clock.strip().lower() == "game":  # game time, whatever the frame rate
                return wait_game_seconds(ctx, float(amount))
            time.sleep(float(amount))
            return True, ""
        if verb == "until":
            return poll(lambda: tool("expect " + rest), seconds or 5)
        if verb == "log":
            rx = re.compile(rest, re.I)

            def matched():
                hit = next((l for l in log_lines() if rx.search(l)), None)
                return bool(hit), short(re.sub(r"^\[[^\]]*\]\[[^\]]*\]", "", hit), 120) if hit else "no matching log line"
            return poll(matched, seconds or 5)
        if verb == "move":
            tool("move " + rest)
            return poll(lambda: tool(f"near {rest} 120"), seconds or 15)
        if verb == "press":
            name, _, hold_for = rest.partition(" ")
            if not hold_for:
                return tool(f"tap {name}")
            _, detail = tool(f"hold {name}")
            held.add(name)
            time.sleep(float(hold_for))
            held.discard(name)
            return tool(f"release {name}")[0], detail.replace("hold ", "held ", 1) + f" for {hold_for}s"
        if verb == "hold":
            held.add(rest.split()[0])
        elif verb == "release":
            held.discard(rest.split()[0])
        if verb == "expect-sound":
            rx = re.compile(rest, re.I)
            def heard():
                ok = any(rx.search(s["sound"]) for s in active_sounds(ctx))
                return ok, "" if ok else "no such sound playing"
            return poll(heard, seconds or 3)
        if verb == "click":
            return True, ui_click(ctx, rest)
        if verb in ("expect-ui", "ui-gone"):
            want = verb == "expect-ui"
            def check():
                ok = ui_has_text(ctx, rest) == want
                return ok, "" if ok else ("still on screen" if not want else "not on screen")
            return poll(check, seconds or 3)
        if verb == "key" and re.search(r"\s(down|up|tap)\b", rest, re.I):
            return tool(step)  # raw key input through the key mappings (the toolset's forced keys)
        if verb in ("type", "key"):
            import shlex
            parts = shlex.split(rest)
            if verb == "key":
                mcp_tool(ctx, "SlateInspectorToolset", "PressKey", {"key": rest})
                return True, f"pressed {rest}"
            nodes, slate = ui_nodes(ctx)
            if not slate or len(parts) < 2:
                raise CliError('type needs SlateInspectorToolset and: type "<textbox label>" <text>')
            node = find_ui(nodes, parts[0], {"textbox", "spinbox"})
            mcp_tool(ctx, "SlateInspectorToolset", "Type", {"ref": node["ref"], "text": " ".join(parts[1:]), "submit": False})
            return True, f'typed into "{node["label"]}"'
        if verb == "shot-check":
            return shot_check(ctx, rest)
        if verb == "shot":
            buf = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(buf):
                cmd_shot(ctx, shot_args(output=rest or None, hud=bool(getattr(args, "hud", False))))
            return True, buf.getvalue().strip()
        return tool(step)

    failures, held = [], set()
    film = start_film(ctx, args) if getattr(args, "film", None) else None
    try:
        for number, step in enumerate(steps, 1):
            try:
                ok, detail = run(step.strip())
            except CliError as exc:
                ok, detail = False, str(exc)
            if not ok or not getattr(args, "quiet", False):
                print(f"{number:>2} {'ok  ' if ok else 'FAIL'} {step}" + (f"  ({detail})" if detail else ""))
            bad = [(rx.pattern, l) for rx in rejects for l in log_lines() if rx.search(l)] if rejects else []
            if bad:
                print(f"   BAD  /{bad[0][0]}/  {bad[0][1].split(']', 2)[-1].strip()[:140]}")
                failures.append(f"rejected /{bad[0][0]}/ appeared")
                break
            if not ok:
                failures.append(f"step {number} failed")
                if not args.keep_going:
                    break
    finally:
        for name in held:
            try:
                ctx.tether("pie_step", step=f"release {name}")
            except CliError:
                pass
        if film:
            finish_film(ctx, args, film)
        if not (args.keep or was_running):
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    if failures or not getattr(args, "quiet", False):
        print(("PASS" if not failures else "FAIL") + f" in {time.time() - started:.1f}s"
              + ("" if not failures else ": " + "; ".join(failures)))
    return 1 if failures else 0


def shot_check(ctx, rest):
    """`shot-check NAME [tolerance%]` in a step file: compare the view with tests/play/shots/NAME.png, recording it
    the first time. (ok, detail)."""
    import io
    import shutil
    from contextlib import redirect_stdout
    name, _, tolerance = rest.partition(" ")
    if not name:
        raise CliError("shot-check NAME [tolerance %]")
    tolerance = float(tolerance or 2.0)
    baseline = os.path.join(os.path.dirname(ctx.project), "tests", "play", "shots", name + ".png")
    path = os.path.join(state_dir(ctx.project), "shots", time.strftime(f"check-{name}-%H%M%S.png"))
    with redirect_stdout(io.StringIO()):
        cmd_shot(ctx, shot_args(output=path, tolerance=tolerance))
    if not os.path.isfile(baseline):
        os.makedirs(os.path.dirname(baseline), exist_ok=True)
        shutil.copyfile(path, baseline)
        return True, f"recorded {os.path.relpath(baseline, os.path.dirname(ctx.project))} (commit it)"
    with open(baseline, "rb") as f:
        before = png_read(f.read())
    with open(path, "rb") as f:
        after = png_read(f.read())
    share, regions, diff = image_diff(before, after, 32)
    diff_path = os.path.splitext(path)[0] + "-diff.png"
    png_write(diff_path, before[0], before[1], diff)
    ok = share * 100 <= tolerance
    return ok, f"{share * 100:.2f}% changed (tolerance {tolerance:g}%)" + ("" if ok else f"; diff in red: {diff_path}")


def cmd_pie(ctx, args):
    """Play-In-Editor through Epic's EditorAppToolset, reporting only what went wrong."""
    if args.action == "run":
        return pie_run(ctx, args)
    if args.action == "do":
        return pie_do(ctx, args)
    if args.action == "target":
        index = int(args.script[0]) if args.script else -1
        print(pie_net(ctx, "target", target=index))
        return 0
    if args.action == "start" and getattr(args, "game", None):
        print(pie_net(ctx, "game", game=args.game))
    if args.action == "start" and getattr(args, "clients", None):
        print(pie_net(ctx, "configure", clients=args.clients, net=args.net or "listen"))
    elif args.action == "start" and os.path.exists(os.path.join(state_dir(ctx.project), "play-settings.json")):
        # a multiplayer start whose `pie stop` never came (editor killed or crashed) left its settings in config
        print(f"note: {pie_net(ctx, 'restore')} (left over from an earlier `pie start --clients`)")
    toolset = None if ctx.transport == "remote" else resolve_toolset(ctx, "editorapp")
    start = log_end(ctx.project)
    started = time.time()
    if args.action == "start" and csharp_layout(ctx.project):
        # UnrealSharp recompiles a saved but unreloaded C# type in place on its first lookup, from the old
        # reflection data. That drops the type's subsystems from every live world, PIE included, for the
        # rest of the session. So PIE only ever starts on reloaded code.
        if csharp_changes(ctx.project) or ctx.tether("cs_state") == "pending":
            if csharp_reload_build(ctx, argparse.Namespace(wait=120, limit=args.limit)):
                raise CliError("PIE not started: the C# reload failed (errors above)")
            start, started = log_end(ctx.project), time.time()
    if ctx.transport == "remote":  # no EditorAppToolset before 5.8: LevelEditorSubsystem requests instead
        if args.action == "status":
            print("pie: " + ctx.tether("pie", action="status"))
            return 0
        ctx.tether("pie", action=args.action, simulate=args.simulate)
        want, deadline = ("running" if args.action == "start" else "not running"), time.time() + 60
        while ctx.tether("pie", action="status") != want:  # the request takes effect on a later tick
            if time.time() > deadline:
                raise CliError(f"PIE still not {want} after 60 s (ue log --level Warning)", 4)
            time.sleep(0.25)
        time.sleep(args.warmup if args.action == "start" else 0)
        text, is_error = "", False
    elif args.action == "start":
        options = {"bSimulate": args.simulate, "playMode": "PlayMode_InViewPort", "warmupSeconds": args.warmup}
        text, is_error = ctx.mcp.call(toolset, "StartPIE", {"options": options})
        pie_throttle(ctx, "lift")  # a background editor runs PIE at ~3 fps, so game time crawls
        if is_error and unwrap(ctx.mcp.call(toolset, "IsPIERunning", {})[0]) is True:
            # Epic fails the call when anything logged a script error during startup (e.g. a Blueprint
            # drawing to a render target under -nullrhi), although PIE is running. Report, don't fail.
            print(f"note: PIE started, but startup logged: {compact_error(text)[:160]}")
            is_error = False
        if is_error:
            with open(log_path(ctx.project), "rb") as f:
                f.seek(start)
                fatal = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Error",
                                   pattern="Fatal error detected")
            hint = ""
            if any("InvalidCastException" in l or "NullReferenceException" in l and "@" not in l for l in fatal):
                hint = "\n  hint: stale hot-reloaded C# type (common after structural C# changes); run `ue build --restart`"
            raise CliError(compact_error(text) + "".join("\n  " + l for l in fatal[:3]) + hint)
    elif args.action == "stop":
        text, is_error = ctx.mcp.call(toolset, "StopPIE", {})
        pie_throttle(ctx, "restore")
    else:
        text, is_error = ctx.mcp.call(toolset, "IsPIERunning", {})
        if is_error:
            raise CliError(compact_error(text))
        if unwrap(text) is True:
            fps, throttled = pie_rate(ctx)
            print("pie: running" + (f" (~{fps:.0f} fps" + (": the editor is in the background and throttled, so game time "
                                                           "runs slow; ue pie start lifts that for its sessions"
                                                           if throttled and fps < 15 else "") + ")" if fps else ""))
            net = pie_net(ctx, "status")
            if net.count(": ") > 1:  # several PIE worlds: multiplayer
                print(net)
            return 0
        # Say why the last session ended if it died rather than being stopped.
        with open(log_path(ctx.project), "rb") as f:
            text = f.read().decode("utf-8", "replace")
        last = text.rfind("Creating play world package")
        fatal = filter_log(text[last:].splitlines(), level="Error", pattern="Fatal error detected") if last >= 0 else []
        print("pie: not running" + (f" (last session ended by: {fatal[0][2:]})" if fatal else ""))
        return 0
    if is_error:
        raise CliError(compact_error(text))
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        issues = filter_log(f.read().decode("utf-8", "replace").splitlines(), level="Warning")
    print(f"pie {args.action}: OK in {time.time() - started:.1f}s" + (f", {len(issues)} warnings/errors" if issues else ""))
    for line in issues[: args.limit]:
        print("  " + line)
    if args.action == "stop":  # put back play settings a `--clients` start changed, and the world target
        with __import__("contextlib").suppress(CliError):
            note = pie_net(ctx, "restore")
            if note != "nothing to restore":
                print(note)
    elif args.action == "start" and getattr(args, "clients", None):
        print(pie_net(ctx, "status"))
    return 0


THROTTLE = """import sys
s = unreal.find_object(None, '/Script/UnrealEd.Default__EditorPerformanceSettings')
key = 'bThrottleCPUWhenNotForeground'
if '{action}' == 'lift' and s.get_editor_property(key):
    s.set_editor_property(key, False)
    sys._tether_throttle = True
elif '{action}' == 'restore' and getattr(sys, '_tether_throttle', False):
    s.set_editor_property(key, True)
    sys._tether_throttle = False
"""


GAME_TIME = "unreal.GameplayStatics.get_time_seconds(unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world())"


def wait_game_seconds(ctx, seconds):
    """Wait until N seconds of game time have passed in PIE (a throttled or slow editor runs game time slower)."""
    start_game, start_real = float(ctx.tether("py", code=GAME_TIME)), time.time()
    while True:
        now = float(ctx.tether("py", code=GAME_TIME))
        if now - start_game >= seconds:
            return True, f"{now - start_game:.2f} s of game time took {time.time() - start_real:.1f} s"
        if time.time() - start_real > seconds * 30 + 10:
            return False, f"only {now - start_game:.2f} s of game time passed in {time.time() - start_real:.0f} s"
        time.sleep(0.1)


def pie_throttle(ctx, action):
    """Lift "Use Less CPU when in Background" for a PIE session Tether starts (put back on stop): throttled, a
    background editor runs PIE at a few frames per second and game time with it, so timed steps fall short.
    Set directly, not as a transaction (an editor setting is not level work)."""
    try:
        ctx.tether("py", code=THROTTLE.format(action=action))
    except CliError:
        pass


def pie_rate(ctx):
    """(frames per second of the PIE world now, whether the editor's background throttle is on)."""
    try:
        out = str(ctx.tether("py", code=(
            "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
            "d = unreal.GameplayStatics.get_world_delta_seconds(w) if w else 0\n"
            "t = unreal.find_object(None, '/Script/UnrealEd.Default__EditorPerformanceSettings')"
            ".get_editor_property('bThrottleCPUWhenNotForeground')\n"
            "f'{(1 / d) if d else 0:.1f} {t}'"))).strip().strip("'")
        rate, throttled = out.split()
        return float(rate), throttled == "True"
    except (CliError, ValueError):
        return 0.0, False


def pie_net(ctx, action, clients=1, net="", target=-1, game="-"):
    # Epic's MCP layer treats a string parameter with an empty default as required, so pass them all.
    return ctx.tether("pie_net", action=action, clients=clients, net=net or "-", target=target, game=game)


def tree_lines(rows, describe):
    """[name, parent, ...] rows as an indented tree (names shortened to the asset name)."""
    children, names = {}, {r[0] for r in rows}
    for row in rows:
        children.setdefault(row[1] if row[1] in names else "", []).append(row)
    out = []

    def walk(parent, depth):
        for row in sorted(children.get(parent, []), key=lambda r: r[0]):
            out.append("  " * depth + row[0].rsplit("/", 1)[-1] + describe(row))
            walk(row[0], depth + 1)
    walk("", 1)
    return out


def cmd_sound(ctx, args):
    """`ue sound <asset>`: a sound in a few lines; `ue audio mix` is the class/submix tree (see cmd_audio)."""
    info = ctx.tether("sound_info", target=args.asset)
    info = json.loads(info) if isinstance(info, str) else info
    duration = info.get("duration")
    head = f"{info['sound']} ({info['class']})" + (f": {duration:.2f} s" if isinstance(duration, (int, float)) and 0 < duration < 1e5 else "")
    if info.get("channels"):
        head += f", {'mono' if info['channels'] == 1 else 'stereo' if info['channels'] == 2 else str(info['channels']) + ' channels'}"
        head += f", {info['sample_rate']} Hz" if info.get("sample_rate") else ""
    head += ", looping" if info.get("looping") else ""
    print(head)
    if info.get("compression"):
        print(f"  compression {info['compression']}" + (f" quality {info['quality']}" if info.get("quality") else "")
              + (f", loading {info['streaming']}" if info.get("streaming") else ""))
    att = info.get("attenuation")
    if att:
        print(f"  attenuation {att['asset']}" + (f": falls off over {att['falloff_m']} m" if att.get("falloff_m") else "")
              + (f" after {att['inner_m']} m" if att.get("inner_m") else "") + ("" if att.get("spatialized", True) else ", not spatialized"))
    else:
        print("  no attenuation on the asset (code may add one when it plays it; if not, it is as loud everywhere and "
              "not positioned: fine for UI and music)")
    if info.get("channels", 1) and info.get("channels", 1) > 1 and att:
        print("  ! stereo with attenuation: a 3D sound should be mono")
    print("  sound class " + (" < ".join(info["sound_class"]) if info["sound_class"] else
                              "none on the asset (unless code sets one when it plays it, class volumes won't reach it)")
          + (f", submix {info['submix']}" if info.get("submix") else ""))
    if info.get("concurrency"):
        print("  concurrency " + ", ".join(info["concurrency"]))
    for wave in info.get("waves", []):
        print(f"  plays {wave}")
    print(f"  used by {info['used_by']} asset(s)")
    ctx.data = info
    return 0


def audio_mix(ctx, args):
    data = ctx.tether("audio_mix")
    data = json.loads(data) if isinstance(data, str) else data
    print(f"{len(data['classes'])} sound class(es):" if data["classes"] else "no sound classes under /Game (everything plays at default volume)")
    for line in tree_lines(data["classes"], lambda r: f"  volume {r[2]:g}" + (f", pitch {r[3]:g}" if r[3] not in (None, 1) else "")):
        print(line)
    print(f"{len(data['submixes'])} submix(es):" if data["submixes"] else "no submixes under /Game (all audio goes to the master submix)")
    for line in tree_lines(data["submixes"], lambda r: (f"  volume {r[2]:g}" if isinstance(r[2], (int, float)) else "")
                           + (f", {r[3]} effect(s)" if r[3] else "")):
        print(line)
    ctx.data = data
    return 0
