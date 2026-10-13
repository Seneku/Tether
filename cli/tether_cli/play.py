"""Play-in-editor and runtime checks: pie, ui, perf, mem, world, audio, soak."""
import argparse
import contextlib
import json
import os
import re
import time

from .core import (
    CliError, compact_error, crash_summary, editor_executable, editor_live, engine_root, filter_log, host_platform,
    human_bytes, log_end, log_path, read_json, recent_crash, state_dir, subprocess, unwrap, write_json,
)
from .transport import catalog, py_json, resolve_toolset
from .build import csharp_changes, csharp_layout, csharp_reload_build
from .calls import flush_editor_log
from .images import cmd_shot, image_diff, png_read, png_write
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
        fields = dict(a.split("=", 1) for a in attrs if "=" in a)
        nodes.append({"indent": len(m.group("indent")), "role": m.group("role"),
                      "label": (m.group("label") or "").replace('\\"', '"'), "ref": fields.get("ref", ""),
                      "src": fields.get("src", ""), "size": fields.get("size", ""),
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
    return game_ui(parse_snapshot(text if isinstance(text, str) else json.dumps(text)),
                   int(target.group(1)) if target else 0), True


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


def ui_click(ctx, target):
    nodes, slate = ui_nodes(ctx)
    if not slate:
        return ctx.tether("umg", click=target)
    node = find_ui(nodes, target)
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
        print(ui_click(ctx, " ".join(args.rest)))
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
    """Sounds playing in PIE: what, from where, how loud."""
    if not (ctx.beacon or {}).get("pie"):
        raise CliError("PIE is not running (ue pie start)")
    sounds = active_sounds(ctx)
    comps = ctx.tether("audio_components", limit=200)
    comps = json.loads(comps) if isinstance(comps, str) else comps
    by_name = {c["component"]: c for c in comps}
    rx = re.compile(args.match, re.I) if args.match else None
    rows = []
    for s in sounds:
        comp = next((c for n, c in by_name.items() if n and n in s["component"]), None)
        length = "loop" if s["duration"] >= 10000 else f"{s['duration']:.1f} s"
        volume = max((w["volume"] for w in s["waves"]), default=None)
        row = (f"{s['sound']} ({length})" + (f" on {comp['owner']}" if comp and comp.get("owner") else
                                              f" via {s['component']}" if s["component"] not in ("", "None") else "")
               + (f", {comp['distance']} cm away" if comp and comp.get("distance") is not None else "")
               + (f", volume {volume:.2f}" if volume is not None else "")
               + (f", attenuation {comp['attenuation']}" if comp and comp.get("attenuation") else ""))
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


def read_profile_csv(path):
    """{column: [floats per frame]} from a CsvProfiler capture (the header repeats at the end)."""
    import csv
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
            time.sleep(float(rest or 1))
            return True, ""
        if verb == "until":
            return poll(lambda: tool("expect " + rest), seconds or 5)
        if verb == "log":
            rx = re.compile(rest, re.I)
            return poll(lambda: (any(rx.search(l) for l in log_lines()), "no matching log line"), seconds or 5)
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
            shot_args = argparse.Namespace(actor=None, at=None, look_at=None, rot=None, annotate=False, grid_z=0,
                                           ui=False, output=rest or None)
            buf = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(buf):
                cmd_shot(ctx, shot_args)
            return True, buf.getvalue().strip()
        return tool(step)

    failures, held = [], set()
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
        cmd_shot(ctx, argparse.Namespace(actor=None, at=None, look_at=None, rot=None, annotate=False, grid_z=0, ui=False,
                                         output=path, baseline=None, compare=None, tolerance=tolerance, threshold=32))
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
    else:
        text, is_error = ctx.mcp.call(toolset, "IsPIERunning", {})
        if is_error:
            raise CliError(compact_error(text))
        if unwrap(text) is True:
            print("pie: running")
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


def pie_net(ctx, action, clients=1, net="", target=-1):
    # Epic's MCP layer treats a string parameter with an empty default as required, so pass them all.
    return ctx.tether("pie_net", action=action, clients=clients, net=net or "-", target=target)
