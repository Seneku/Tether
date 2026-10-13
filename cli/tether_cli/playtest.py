"""Play testing: input maps, recording a play session, watching properties over time, load hitches."""
import argparse
import csv
import datetime
import json
import math
import os
import re
import shutil
import time

from .core import CliError, log_end, log_path, read_json, state_dir
from .calls import flush_editor_log
from .assets import cmd_bp, code_usages
from .play import cmd_perf, cmd_pie, insights_exe, insights_export, pie_do, unthrottled


def input_lines(data, match=None, code_refs=None):
    """Text for `ue input`: one line per action (its keys, by context), then the contexts and who adds them."""
    rx = re.compile(match, re.I) if match else None
    code_refs = code_refs or {}
    by_action = {}
    for context in data["contexts"]:
        for m in context["mappings"]:
            by_action.setdefault(m["action"], []).append((context["name"], m))
    several = len(data["contexts"]) > 1
    lines = []
    actions = [a for a in data["actions"] if not rx or rx.search(a["name"])]
    for action in actions:
        keys = []
        for context, m in by_action.get(action["name"], []):
            extras = m["modifiers"] + [f"on {t}" for t in m["triggers"]]
            keys.append(m["key"] + (f" ({', '.join(extras)})" if extras else "") + (f" [{context}]" if several else ""))
        head = f"{action['name']}  {action['type'] or '?'}" + (f", {'/'.join(action['triggers'])}" if action["triggers"] else "")
        line = f"{head}: {', '.join(keys)}" if keys else f"{head}: not in any mapping context"
        if data["pie"]:
            live = data["live"].get(action["path"], [])
            line += f"  | in PIE: {', '.join(live)}" if live else "  | in PIE: no active context maps it"
        lines.append(line)
    if rx:
        return lines
    lines.append(f"{len(data['contexts'])} mapping context(s):")
    for context in data["contexts"]:
        users = [_short(r) for r in context["referencers"]] + code_refs.get(context["name"], [])
        state = ""
        if data["pie"]:
            state = f"  active in PIE (priority {context['priority']})" if context["active"] else "  not active in PIE"
        lines.append(f"  {context['name']}  {len(context['mappings'])} mapping(s)"
                     + (f"  added by: {', '.join(users[:4])}" + (f" +{len(users) - 4}" if len(users) > 4 else "")
                        if users else "  referenced by nothing (never added?)") + state)
    return lines


def _short(package):
    return package.rsplit("/", 1)[-1]


def cmd_input(ctx, args):
    data = ctx.tether("input_map")
    if not data["actions"] and not data["contexts"]:
        print("no Enhanced Input actions or mapping contexts in this project")
        return 0
    if args.action and not any(re.search(args.action, a["name"], re.I) for a in data["actions"]):
        raise CliError(f"no input action matches /{args.action}/ ({', '.join(a['name'] for a in data['actions'][:12])})")
    code_refs = {}
    if not args.action:  # contexts are often added from C++: name the code that mentions each one
        for context in data["contexts"]:
            hits = code_usages(ctx.project, context["name"], limit=3)
            code_refs[context["name"]] = [f"{os.path.relpath(p, os.path.dirname(ctx.project))}:{n}" for p, n, _ in hits]
    ctx.data = data
    if not args.action:
        print(f"{len(data['actions'])} input action(s):")
    for line in input_lines(data, args.action, code_refs):
        print(line)
    return 0


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value):
    if _num(value):
        return f"{value:.6g}" if isinstance(value, float) else str(value)
    if isinstance(value, list):
        return "(" + ", ".join(f"{v:.0f}" for v in value) + ")"
    return str(value)


def watch_summary(result, max_changes=10):
    """One line per watched value: numbers' range, positions' travel, and every change of anything else."""
    lines, summary = [], {}
    for i, spec in enumerate(result["targets"]):
        points = [(row[0], row[2 + i]) for row in result["samples"]]
        seen = [(t, v) for t, v in points if v is not None]
        notes = []
        if not seen:
            lines.append(f"{spec}  never found")
            summary[spec] = {"found": False}
            continue
        if points[0][1] is None:
            notes.append(f"appeared at {seen[0][0]:.1f}s")
        if points[-1][1] is None:
            gone = next(t for t, v in reversed(points) if v is not None)
            notes.append(f"gone after {gone:.1f}s")
        values = [v for _, v in seen]
        first, last = values[0], values[-1]
        if all(_num(v) for v in values):
            low, high = min(values), max(values)
            changes = sum(1 for a, b in zip(values, values[1:]) if a != b)
            at = lambda target: next(t for t, v in seen if v == target)  # noqa: E731
            text = (f"{_fmt(first)} -> {_fmt(last)}  (min {_fmt(low)} at {at(low):.1f}s, max {_fmt(high)} at "
                    f"{at(high):.1f}s, {changes} change{'' if changes == 1 else 's'})" if low != high
                    else f"{_fmt(first)} throughout")
            summary[spec] = {"first": first, "last": last, "min": low, "max": high, "changes": changes}
        elif all(isinstance(v, list) and len(v) == 3 for v in values):
            travelled, top = 0.0, 0.0
            for (t0, a), (t1, b) in zip(seen, seen[1:]):
                step = sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
                travelled += step
                if t1 > t0:
                    top = max(top, step / (t1 - t0))
            text = (f"moved {travelled:.0f} cm: {_fmt(first)} -> {_fmt(last)}, top speed {top:.0f} cm/s"
                    if travelled >= 1 else f"stayed at {_fmt(first)}")
            summary[spec] = {"first": first, "last": last, "travelled": round(travelled), "top_speed": round(top)}
        else:
            runs = [(seen[0][0], first)]
            for t, v in seen[1:]:
                if v != runs[-1][1]:
                    runs.append((t, v))
            shown = " -> ".join(f"{_fmt(v)} ({t:.1f}s)" for t, v in runs[:max_changes])
            text = shown + (f" -> ... +{len(runs) - max_changes} more" if len(runs) > max_changes else "")
            if len(runs) == 1:
                text = f"{_fmt(first)} throughout"
            summary[spec] = {"first": first, "last": last, "changes": [[t, v] for t, v in runs]}
        lines.append(f"{spec}  {text}" + (f"  [{'; '.join(notes)}]" if notes else ""))
    return lines, summary


def write_watch_csv(path, result):
    header = ["real_s", "game_s"]
    vectors = set()
    for i, spec in enumerate(result["targets"]):
        if any(isinstance(row[2 + i], list) for row in result["samples"]):
            vectors.add(i)
            header += [f"{spec}.x", f"{spec}.y", f"{spec}.z"]
        else:
            header.append(spec)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        out.writerow(header)
        for row in result["samples"]:
            cells = row[:2]
            for i, value in enumerate(row[2:]):
                if i in vectors:
                    cells += value if isinstance(value, list) else ["", "", ""]
                else:
                    cells.append("" if value is None else value)
            out.writerow(cells)


def pie_watch(ctx, args):
    """Sample properties while PIE runs (for --seconds, or while a step file plays), then summarise them."""
    specs = list(args.script or [])
    if not specs:
        raise CliError("give values to watch: ue pie watch player.Health BP_Enemy_C_1.State BP_Enemy_C_1 "
                       "(an actor alone: its location)")
    was_running = ctx.tether("pie", action="status") == "running"
    if not was_running:
        code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup, limit=0, script=None))
        if code:
            return code
    code = 0
    try:
        with unthrottled(ctx):  # samples at the rate asked for, and the game at its real speed
            print(ctx.tether("pie_watch", action="start", targets=json.dumps(specs), every=args.every))
            if args.file:
                code = pie_do(ctx, argparse.Namespace(script=[], file=args.file, reject=args.reject, keep=True,
                                                      keep_going=args.keep_going, warmup=args.warmup, quiet=True))
                if args.seconds:
                    time.sleep(args.seconds)
            else:
                time.sleep(args.seconds or 10)
            result = ctx.tether("pie_watch", action="stop")
        result = json.loads(result) if isinstance(result, str) else result
    finally:
        if not (args.keep or was_running):
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    samples = result["samples"]
    span = samples[-1][0] if samples else 0
    path = args.output or os.path.join(state_dir(ctx.project), "watch", time.strftime("watch-%Y%m%d-%H%M%S.csv"))
    write_watch_csv(path, result)
    print(f"{len(samples)} sample(s) over {span:.1f}s" + (f" ({result['ended']})" if result.get("ended") else "")
          + (" (capped)" if result.get("capped") else "") + f" -> {path}")
    lines, summary = watch_summary(result)
    for line in lines:
        print("  " + line)
    ctx.data = {"csv": path, "samples": len(samples), "seconds": span, "values": summary}
    return code


def _apply_modifiers(value, modifiers):
    """A key's raw (x, y, z) through its mapping's modifiers, as Enhanced Input applies them."""
    x = list(value)
    for mod in modifiers:
        kind, _, arg = mod.partition(":")
        if kind == "negate":
            x = [-v if axis in (arg or "xyz") else v for v, axis in zip(x, "xyz")]
        elif kind == "swizzle":
            order = arg or "YXZ"
            x = [x["XYZ".index(c)] for c in order]
        elif kind == "scalar" and arg:
            x = [v * float(s) for v, s in zip(x, arg.split(","))]
    return x


def _fmt_num(v):
    return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def record_steps(result, wanted_map=None, tap_seconds=0.25, look_degrees=10.0):
    """Turn a recording (key events, poses, the mapping contexts' key mappings) into `pie do` steps."""
    mappings, active = result["mappings"], set(result.get("active") or [])
    by_key = {}
    for m in mappings:
        by_key.setdefault(m["key"], []).append(m)
    for key, rows in by_key.items():  # a key in several contexts: the ones active when recording began win
        if any(r["context"] in active for r in rows):
            by_key[key] = [r for r in rows if r["context"] in active]
    kinds = {m["action"]: m["type"] for m in mappings}
    raw, values, timed = {}, {}, []  # key -> (x, y, z); action -> rounded value; [(t, order, step)]
    pressed_at = {}
    first_pose = last_pose = last_look = None

    def action_value(action):
        total = [0.0, 0.0, 0.0]
        for key, value in raw.items():
            for m in by_key.get(key, []):
                if m["action"] == action:
                    total = [a + b for a, b in zip(total, _apply_modifiers(value, m["modifiers"]))]
        return tuple(round(max(-1.0, min(1.0, v)), 1) for v in total)

    def look_step(pose):
        (pitch, yaw), eye = pose[3], pose[4]
        p, y = math.radians(pitch), math.radians(yaw)
        point = [eye[0] + 1000 * math.cos(p) * math.cos(y), eye[1] + 1000 * math.cos(p) * math.sin(y),
                 eye[2] + 1000 * math.sin(p)]
        return "look " + ",".join(f"{v:.0f}" for v in point)

    def turned(a, b):
        return max(abs(a[3][0] - b[3][0]), abs((a[3][1] - b[3][1] + 180) % 360 - 180))

    for event in result["events"]:
        t, kind = event[0], event[1]
        if kind == "pose":
            if first_pose is None:
                first_pose = last_look = event
            elif turned(event, last_look) >= look_degrees:
                timed.append((t, 0, look_step(event)))
                last_look = event
            last_pose = event
            continue
        key = event[2]
        if kind == "down":
            raw[key] = (1.0, 0.0, 0.0)
        elif kind == "up":
            raw.pop(key, None)
        elif kind == "axis":
            raw[key] = (event[3][0], event[3][1], 0.0)
            if not any(event[3]):
                raw.pop(key)
        for action in {m["action"] for m in by_key.get(key, [])}:
            value = action_value(action)
            if value == values.get(action, (0.0, 0.0, 0.0)):
                continue
            values[action] = value
            if kinds.get(action) == "boolean":
                if any(value) and action not in pressed_at:
                    if last_pose and last_look and turned(last_pose, last_look) >= 3:  # aim before using
                        timed.append((t, 0, look_step(last_pose)))
                        last_look = last_pose
                    pressed_at[action] = (t, len(timed))
                    timed.append((t, 1, f"hold {action}"))
                elif not any(value) and action in pressed_at:
                    start, index = pressed_at.pop(action)
                    if t - start < tap_seconds:
                        timed[index] = (start, 1, f"tap {action}")
                    else:
                        timed.append((t, 1, f"release {action}"))
            elif any(value):
                dims = 1 if kinds.get(action) == "axis1d" else 3 if kinds.get(action) == "axis3d" else 2
                timed.append((t, 1, f"hold {action} " + ",".join(_fmt_num(v) for v in value[:dims])))
            else:
                timed.append((t, 1, f"release {action}"))
    end = result["events"][-1][0] if result["events"] else 0.0
    for action, (start, index) in pressed_at.items():  # still held when the recording stopped
        timed.append((end, 1, f"release {action}"))
    for action, value in values.items():
        if kinds.get(action) != "boolean" and any(value):
            timed.append((end, 1, f"release {action}"))
    timed.sort(key=lambda row: (row[0], row[1]))
    steps = []
    if first_pose:
        steps += [f"at {','.join(str(v) for v in first_pose[2])}", look_step(first_pose)]
    clock = first_pose[0] if first_pose else 0.0
    for t, _, step in timed:
        if t - clock >= 0.05:
            steps.append(f"wait {_fmt_num(round((t - clock) * 20) / 20)}")
            clock = t
        steps.append(step)
    if last_pose and end - clock >= 0.05:
        steps.append(f"wait {_fmt_num(round((end - clock) * 20) / 20)}")
    header = [f"# map: {wanted_map}"] if wanted_map else []
    header.append(f"# recorded with ue pie record: {result.get('seconds', end):.0f}s, {len(timed)} input/view change(s)")
    if last_pose:
        header.append("# the last step checks the player ends up near where the recording did; replay drifts a "
                      "little (mouse look is replayed as `look` steps), so widen or drop it if needed")
        steps.append(f"near {','.join(str(v) for v in last_pose[2])} 500")
    return header + steps


def pie_record(ctx, args):
    """Record a human playing in PIE until PIE stops (or --seconds), then write it as a `pie do` step file."""
    launch = read_json(os.path.join(state_dir(ctx.project), "launch.json")) or {}
    if launch.get("headless") and launch.get("port") == (ctx.beacon or {}).get("port"):
        print("note: this editor is headless, so nobody can play in it; open one with `ue open` (no --headless)")
    was_running = ctx.tether("pie", action="status") == "running"
    if not was_running:
        code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=args.warmup, limit=0, script=None))
        if code:
            return code
    print(ctx.tether("pie_record", action="start", every=0.25))
    seconds = args.seconds or 0
    print("play now; recording until PIE stops (Esc or the Stop button)" + (f" or {seconds:g}s pass" if seconds else ""),
          flush=True)
    started = time.time()
    try:
        while ctx.tether("pie", action="status") == "running" and not (seconds and time.time() - started > seconds):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("stopped (Ctrl+C)")
    result = ctx.tether("pie_record", action="stop")
    result = json.loads(result) if isinstance(result, str) else result
    if not (args.keep or was_running) and ctx.tether("pie", action="status") == "running":
        cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    lines = record_steps(result, (ctx.beacon or {}).get("map"))
    path = args.output or os.path.join(state_dir(ctx.project), "recordings", time.strftime("play-%Y%m%d-%H%M%S.txt"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    presses = sum(1 for e in result["events"] if e[1] == "down")
    print(f"{result.get('seconds', 0):.0f}s recorded, {presses} key press(es) -> {path}")
    print(f"replay: ue pie do -f {path}" + ("" if "tests" in path.replace("\\", "/").split("/") else
                                             "  (move it to tests/play/ to make it part of ue ci --play)"))
    ctx.data = {"file": path, "steps": [l for l in lines if not l.startswith("#")]}
    return 0


LOG_STAMP = re.compile(r"^\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d:\d{3})\]\[\s*\d+\]")
LOAD_START = re.compile(r"LogStreaming: Verbose: CreateAsyncPackages: AddPackage: (\S+)")
LOAD_DONE = re.compile(r"LogStreaming: Verbose: GameThread: LoadCompleted: (\S+)")
LOAD_FLUSH = re.compile(r"LogStreaming: \w+: FlushAsyncLoading\(\d+\): (\d+) QueuedPackages, (\d+) AsyncPackages")
FRAME_MARK = re.compile(r"TetherFrameLog mark ([\d.]+)")


def _log_seconds(stamp):
    when = datetime.datetime.strptime(stamp, "%Y.%m.%d-%H.%M.%S:%f")
    return (when - datetime.datetime(1970, 1, 1)).total_seconds()


def parse_load_log(lines):
    """Package loads from a verbose LogStreaming log, on the wall clock of the TetherFrameLog marker:
    ([(wall time, 'start'|'done'|'flush', package or queued count)], marker found)."""
    offset, events = None, []
    for line in lines:
        stamp = LOG_STAMP.match(line)
        if not stamp:
            continue
        mark = FRAME_MARK.search(line)
        if mark:
            offset = float(mark.group(1)) - _log_seconds(stamp.group(1))
            continue
        for kind, rx in (("start", LOAD_START), ("done", LOAD_DONE), ("flush", LOAD_FLUSH)):
            m = rx.search(line)
            if m:
                events.append((_log_seconds(stamp.group(1)), kind, m.group(1)))
                break
    if offset is None:
        return [], False
    return [(t + offset, kind, what) for t, kind, what in events], True


def load_report(frames, events, started, hitch_ms=50.0, top=8):
    """Lines and data for `ue perf load`: hitches with the loads that finished in them, the slowest loads."""
    frames = [f for f in frames if f[0] >= started and (len(f) < 3 or f[2])]  # PIE frames only
    lines, data = [], {"frames": len(frames)}
    if frames:
        deltas = sorted(f[1] for f in frames)
        median = deltas[len(deltas) // 2]
        limit = max(hitch_ms / 1000.0, median * 3)
        hitches = sorted((f for f in frames if f[1] > limit), key=lambda f: -f[1])
        data.update(median_ms=round(median * 1000, 1), hitches=[])
        lines.append(f"{len(frames)} frames, median {median * 1000:.1f} ms; {len(hitches)} hitch(es) over "
                     f"{limit * 1000:.0f} ms" + (":" if hitches else ""))
        for end, length, *_ in hitches[:top]:
            begin = end - length - 0.01
            loaded = [what for t, kind, what in events if kind == "done" and begin <= t <= end + 0.01]
            flushes = [what for t, kind, what in events if kind == "flush" and begin <= t <= end + 0.01]
            note = []
            if flushes:
                note.append(f"game thread waited for loading ({len(flushes)} flush)")
            if loaded:
                shown = ", ".join(p.rsplit("/", 1)[-1] for p in loaded[:4])
                note.append(f"{len(loaded)} package(s) finished: {shown}" + (" ..." if len(loaded) > 4 else ""))
            lines.append(f"  +{end - started:6.2f}s  {length * 1000:6.0f} ms  " + ("; ".join(note) or "no loading in it"))
            data["hitches"].append({"at": round(end - started, 3), "ms": round(length * 1000, 1), "loaded": loaded,
                                    "flushes": len(flushes)})
    begun, durations = {}, []
    for t, kind, what in events:
        if t < started:
            continue
        if kind == "start":
            begun.setdefault(what, t)
        elif kind == "done" and what in begun:
            durations.append((t - begun.pop(what), what))
    durations.sort(reverse=True)
    data["loads"] = [{"package": p, "ms": round(d * 1000, 1)} for d, p in durations]
    if durations:
        lines.append(f"{len(durations)} package(s) loaded; slowest (request to done):")
        for seconds, package in durations[:top]:
            lines.append(f"  {seconds * 1000:7.0f} ms  {package}")
    else:
        lines.append("no packages loaded during the run (all already in memory: the editor keeps what it loaded)")
    return lines, data


def perf_load(ctx, args):
    """Map load and streaming: PIE start time, World Partition streaming completion, hitches and the loads in them."""
    if ctx.tether("pie", action="status") == "running":
        raise CliError("PIE is running; perf load starts it itself to time the start (ue pie stop)")
    if args.map and (ctx.beacon or {}).get("map") != args.map.split(".")[0]:
        dirty = ctx.tether("py", code="len([p for p in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages() "
                                      "if not p.get_name().startswith('/Temp/')])")
        if str(dirty).strip() not in ("0", ""):
            raise CliError(f"the open map has unsaved changes; save or discard them before loading {args.map}")
        began = time.time()
        ctx.tether("py", code=f"unreal.EditorLoadingAndSavingUtils.load_map({args.map.split('.')[0]!r})")
        print(f"editor: opened {args.map} in {time.time() - began:.1f}s")
    start = log_end(ctx.project)
    with unthrottled(ctx):
        print(ctx.tether("frame_log", action="start"))
        began = time.time()
        code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=0, limit=0, script=None))
        if code:
            ctx.tether("frame_log", action="stop")
            return code
        try:
            if args.file:
                code = pie_do(ctx, argparse.Namespace(script=[], file=args.file, reject=None, keep=True,
                                                      keep_going=False, warmup=0, quiet=True))
            time.sleep(args.seconds if args.seconds is not None else (0 if args.file else 10))
        finally:
            result = ctx.tether("frame_log", action="stop")
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    result = json.loads(result) if isinstance(result, str) else result
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        events, marked = parse_load_log(f.read().decode("utf-8", "replace").splitlines())
    first = next((f[0] for f in result["frames"] if f[0] >= began and f[2]), None)
    streaming = [(t, done) for t, done in result["streaming"] if done is not None]
    done_at = next((t for t, done in streaming if done), None)
    print(f"PIE start: {(first or began) - began:.2f}s to the first frame" +
          (f"; World Partition streaming complete at +{done_at - began:.2f}s" if done_at else
           "; World Partition streaming never completed" if streaming else "; no World Partition"))
    if not marked:
        print("note: the log marker was not found, so loads are not matched to frames")
    lines, data = load_report(result["frames"], events, began, args.hitch, args.top)
    for line in lines:
        print(line)
    data.update(pie_start_s=round((first or began) - began, 3),
                streaming_done_s=round(done_at - began, 3) if done_at else None)
    ctx.data = data
    return code


# With named events on, every UFunction call through ProcessEvent is a CPU scope named after the function object:
# 'Function/Game/Doors/BP_Door.BP_Door_C.OnInteract'. A class ending in _C is a Blueprint's.
BP_FUNCTION = re.compile(r"^Function(/[^.]+)\.(\w+)_C\.(\w+)$")


def bp_calls(timers, events):
    """Blueprint functions and events from an Insights timing export taken with named events on:
    [(first start relative to the export's first event, Blueprint, function, count)], in order."""
    functions = {}
    for timer in timers:
        m = BP_FUNCTION.match(timer["Name"])
        if m and not m.group(1).startswith("/Script/") and not m.group(3).startswith("ExecuteUbergraph_"):
            functions[timer["Id"]] = (m.group(2), m.group(3))
    calls, origin = {}, None
    for e in events:
        start = float(e["StartTime"])
        origin = start if origin is None else min(origin, start)
        key = functions.get(e["TimerId"])
        if key:
            first, count = calls.get(key, (start, 0))
            calls[key] = (min(first, start), count + 1)
    return sorted(((first - origin, bp, fn, count) for (bp, fn), (first, count) in calls.items()), key=lambda c: c[0])


def bp_trace(ctx, args):
    """Which Blueprint events and functions ran during a PIE session, in order with counts."""
    exe = insights_exe(ctx.project)
    if not os.path.isfile(exe):
        raise CliError(f"needs Unreal Insights ({exe})")
    was_running = ctx.tether("pie", action="status") == "running"
    region = time.strftime("TetherBp%H%M%S")
    trace_dir = os.path.join(state_dir(ctx.project), "traces")
    os.makedirs(trace_dir, exist_ok=True)
    trace_path = os.path.join(trace_dir, f"bp-{region[8:]}.utrace").replace("\\", "/")

    def console(command):
        ctx.tether("py", code=f"unreal.SystemLibrary.execute_console_command(None, {command!r})")
    code = 0
    with unthrottled(ctx):
        console("stat namedevents")  # object and function names on CPU scopes; a toggle, off by default
        try:
            console(f'trace.file "{trace_path}" cpu,frame')
            console(f"Trace.RegionBegin {region}")
            if not was_running:  # start inside the region, so BeginPlay is in it
                code = cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=0, limit=0, script=None))
            if not code and args.do:
                code = pie_do(ctx, argparse.Namespace(script=[], file=args.do, reject=None, keep=True,
                                                      keep_going=False, warmup=0, quiet=True))
            if not code:
                time.sleep(args.seconds if args.seconds is not None else (0 if args.do else 5))
        finally:
            console(f"Trace.RegionEnd {region}")
            console("trace.stop")
            console("stat namedevents")
            if not (args.keep or was_running):
                cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.isfile(trace_path):
        time.sleep(0.5)
    out_dir = os.path.splitext(trace_path)[0]
    problem = insights_export(exe, trace_path, out_dir, [
        f'TimingInsights.ExportTimers "{out_dir}/timers.csv"',
        f'TimingInsights.ExportTimingEvents "{out_dir}/events.csv" -threads=GameThread -region={region}'])
    if problem:
        raise CliError(problem)
    try:
        with open(os.path.join(out_dir, "timers.csv"), encoding="utf-8") as f:
            timers = list(csv.DictReader(f))
        with open(os.path.join(out_dir, "events.csv"), encoding="utf-8") as f:
            events = list(csv.DictReader(f))
    except OSError:
        raise CliError(f"the trace export failed (see {os.path.join(out_dir, 'insights.log')})")
    calls = bp_calls(timers, events)
    if args.match:
        calls = [c for c in calls if re.search(args.match, f"{c[1]}.{c[2]}", re.I)]
    named = any(t["Name"].startswith("Function/") for t in timers)
    print(f"{len(calls)} Blueprint function(s)/event(s) ran" + (f" matching /{args.match}/" if args.match else "")
          + " (first call, count):")
    for first, bp, fn, count in calls[: args.limit]:
        print(f"  +{first:6.2f}s  {bp}.{fn}" + (f"  x{count}" if count > 1 else ""))
    if len(calls) > args.limit:
        print(f"  ... +{len(calls) - args.limit} more (--limit N, --match REGEX)")
    if not named:
        print("note: the trace has no object names; `stat namedevents` is a toggle and may have been on already. "
              "Run again.")
    print(f"trace: {trace_path}")
    ctx.data = [{"at": round(t, 3), "blueprint": bp, "function": fn, "count": n} for t, bp, fn, n in calls]
    old = sorted(f for f in os.listdir(trace_dir) if f.startswith("bp-") and f.endswith(".utrace"))
    for name in old[:-3]:  # tens of MB each: keep the last three
        try:
            os.remove(os.path.join(trace_dir, name))
        except OSError:
            pass
        shutil.rmtree(os.path.join(trace_dir, name[:-7]), ignore_errors=True)
    return code


def bp_command(ctx, args):
    """`ue bp`: trace lives here; reading, editing and checking graphs is assets.cmd_bp."""
    if args.blueprint == "trace":
        return bp_trace(ctx, args)
    return cmd_bp(ctx, args)


def perf_command(ctx, args):
    """`ue perf`: load lives here; the frame-time capture is play.cmd_perf."""
    if getattr(args, "action", None) == "load":
        return perf_load(ctx, args)
    return cmd_perf(ctx, args)


def pie_command(ctx, args):
    """`ue pie`: watch and record live here; everything else is play.cmd_pie."""
    if args.action == "watch":
        return pie_watch(ctx, args)
    if args.action == "record":
        return pie_record(ctx, args)
    return cmd_pie(ctx, args)
