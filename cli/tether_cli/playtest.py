"""Play testing: input maps, recording a play session, watching properties over time."""
import argparse
import csv
import json
import math
import os
import re
import time

from .core import CliError, read_json, state_dir
from .assets import code_usages
from .play import cmd_pie, pie_do, unthrottled


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


def pie_command(ctx, args):
    """`ue pie`: watch and record live here; everything else is play.cmd_pie."""
    if args.action == "watch":
        return pie_watch(ctx, args)
    if args.action == "record":
        return pie_record(ctx, args)
    return cmd_pie(ctx, args)
