"""Play testing: input maps, recording and watching play, regression baselines, load hitches, budgets."""
import argparse
import contextlib
import csv
import datetime
import json
import math
import os
import re
import shutil
import sys
import time

from .core import CliError, editor_mode, log_end, log_path, read_json, state_dir, subprocess
from .calls import flush_editor_log, scoped_cvars
from .assets import cmd_bp, cmd_size, code_usages, folder_packages, package_bytes
from .content import cmd_tex
from .images import cmd_shot, shot_args
from .jobs import cmd_package, package_staleness, packaged_executable
from .play import frame_stats, read_profile_csv
from .play import (
    cmd_mem, cmd_perf, cmd_pie, insights_exe, insights_export, parse_res, pie_do, pie_viewport_size, res_cvars,
    res_percentage, unthrottled,
)


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


def input_live(ctx, args):
    """`ue input --live <Action>`: the action's value over time while PIE runs (the user playing, or a step file),
    worked out from the keys behind it through their mappings' modifiers."""
    action = args.live
    was_running = ctx.tether("pie", action="status") == "running"
    if not was_running:
        raise CliError("start PIE first (ue pie start), then play while this watches")
    seconds = args.seconds or 10
    with unthrottled(ctx):
        print(ctx.tether("pie_watch", action="start", targets=json.dumps([action]), every=0.05))
        print(f"watching {action} for {seconds:g}s: play now")
        sys.stdout.flush()
        time.sleep(seconds)
        result = ctx.tether("pie_watch", action="stop")
    result = json.loads(result) if isinstance(result, str) else result
    changes, last = [], object()
    for row in result["samples"]:
        if row[2] != last:
            changes.append((row[0], row[2]))
            last = row[2]
    for t, value in changes[: args.limit]:
        print(f"  {t:6.2f}s  {_fmt(value)}")
    if len(changes) > args.limit:
        print(f"  ... +{len(changes) - args.limit} more changes (--limit N)")
    print(f"{len(changes) - 1} change(s) in {len(result['samples'])} samples. The value is worked out from the mapped "
          "keys through negate, swizzle, scalar and dead zone modifiers (triggers and other modifiers not modelled; "
          "see ue input " + action + ")")
    ctx.data = {"changes": changes}
    return 0


def cmd_input(ctx, args):
    if getattr(args, "live", None):
        return input_live(ctx, args)
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


def is_position(spec):
    """A watched value that is a place: an actor alone (its location) or a property named like one."""
    prop = spec.rpartition(".")[2] if "." in spec else "location"
    return bool(re.search(r"location|position|(^|_)pos$|^loc$", prop, re.I))


def is_rotation(spec):
    return bool(re.search(r"rotation|^control$|rotator|orientation", spec.rpartition(".")[2], re.I))


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
        elif all(isinstance(v, list) and len(v) == 3 for v in values) and not is_position(spec):
            sizes = [sum(x * x for x in v) ** 0.5 for v in values]
            kind = "rotation" if is_rotation(spec) else "size"
            if kind == "rotation":  # pitch, yaw, roll: how far each turned
                spans = [max(v[k] for v in values) - min(v[k] for v in values) for k in range(3)]
                text = (f"{_fmt(first)} -> {_fmt(last)}" + (f"  (yaw range {spans[1]:.1f}, pitch range {spans[0]:.1f})"
                                                            if any(spans) else "  throughout"))
            else:  # a velocity, an acceleration, a direction: its length
                peak = max(range(len(sizes)), key=sizes.__getitem__)
                text = (f"{_fmt(first)} -> {_fmt(last)}  (size {sizes[0]:.0f} -> {sizes[-1]:.0f}, max {sizes[peak]:.0f} "
                        f"at {seen[peak][0]:.1f}s)" if len(set(map(tuple, values))) > 1 else f"{_fmt(first)} throughout")
            summary[spec] = {"first": first, "last": last, "max_size": round(max(sizes), 1), "last_size": round(sizes[-1], 1)}
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


def compare_watch(baseline, result, time_tolerance=1.5, distance_tolerance=150.0, value_tolerance=0.05):
    """How a watched run differs from its baseline: [(value, message)]. Numbers: final value and extremes beyond
    5% of the baseline's range (or of the value); positions: where it ended and how far it went; anything else:
    the sequence of values, and when each was first reached (beyond time_tolerance seconds)."""
    def first_times(points):
        seen = {}
        for t, v in points:
            key = json.dumps(v)
            seen.setdefault(key, t)
        return seen

    def series(res, i):
        return [(row[0], row[2 + i]) for row in res["samples"] if row[2 + i] is not None]
    found = []
    base_index = {spec: i for i, spec in enumerate(baseline["targets"])}
    for i, spec in enumerate(result["targets"]):
        if spec not in base_index:
            continue
        old, new = series(baseline, base_index[spec]), series(result, i)
        if bool(old) != bool(new):
            found.append((spec, "was found in the baseline but not now" if old else "now found, but not in the baseline"))
            continue
        if not old:
            continue
        old_values, new_values = [v for _, v in old], [v for _, v in new]
        if all(_num(v) for v in old_values + new_values):
            span = max(max(old_values) - min(old_values), abs(old_values[-1]), 1e-6)
            for label, a, b in (("ends at", old_values[-1], new_values[-1]), ("min", min(old_values), min(new_values)),
                                ("max", max(old_values), max(new_values))):
                if abs(a - b) > value_tolerance * span:
                    found.append((spec, f"{label} {_fmt(b)} (baseline {_fmt(a)})"))
        elif all(isinstance(v, list) and len(v) == 3 for v in old_values + new_values):
            end = sum((x - y) ** 2 for x, y in zip(old_values[-1], new_values[-1])) ** 0.5
            if end > distance_tolerance:
                found.append((spec, f"ends {end:.0f} cm from where it did: {_fmt(new_values[-1])} (baseline "
                                    f"{_fmt(old_values[-1])})"))
            travel = lambda vals: sum(sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 for a, b in zip(vals, vals[1:]))  # noqa: E731
            a, b = travel(old_values), travel(new_values)
            if abs(a - b) > max(distance_tolerance, 0.25 * a):
                found.append((spec, f"moved {b:.0f} cm (baseline {a:.0f} cm)"))
        else:
            runs = lambda vals: [v for k, v in enumerate(vals) if k == 0 or v != vals[k - 1]]  # noqa: E731
            if runs(old_values) != runs(new_values):
                found.append((spec, f"went {' -> '.join(_fmt(v) for v in runs(new_values)[:8])} (baseline "
                                    f"{' -> '.join(_fmt(v) for v in runs(old_values)[:8])})"))
            else:
                old_t, new_t = first_times(old), first_times(new)
                for key, t in new_t.items():
                    if key in old_t and abs(t - old_t[key]) > time_tolerance:
                        found.append((spec, f"reached {_fmt(json.loads(key))} at {t:.1f}s (baseline {old_t[key]:.1f}s)"))
    return found


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
    if getattr(args, "baseline", None):
        target = baseline_path(ctx.project, args.baseline)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"targets": result["targets"], "samples": result["samples"], "steps": args.file,
                       "recorded": time.strftime("%Y-%m-%d %H:%M")}, f)
        print(f"baseline recorded: {target} (commit it; --compare {args.baseline} checks later runs against it)")
    if getattr(args, "compare", None):
        target = baseline_path(ctx.project, args.compare)
        if not os.path.isfile(target):
            raise CliError(f"no baseline {target} (record one with --baseline {args.compare})")
        with open(target, encoding="utf-8") as f:
            base = json.load(f)
        diffs = compare_watch(base, result)
        for spec, message in diffs:
            print(f"DIFF {spec}: {message}")
        print(("matches" if not diffs else f"{len(diffs)} difference(s) from") + f" baseline {args.compare}"
              + f" (recorded {base.get('recorded', '?')})")
        ctx.data["differences"] = [{"value": s, "message": m} for s, m in diffs]
        code = code or (1 if diffs else 0)
    return code


def baseline_path(project, name):
    """A baseline file: a path as given, or tests/play/baselines/<name>.json next to the play tests."""
    if name.endswith(".json") or os.sep in name or "/" in name:
        return os.path.abspath(name)
    return os.path.join(os.path.dirname(project), "tests", "play", "baselines", name + ".json")


def watch_header(path):
    """The values a step file asks to watch (`# watch: player.Health BP_Enemy.State`), or []."""
    specs = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            m = re.match(r"\s*#\s*watch:\s*(.+)$", line)
            if m:
                specs += m.group(1).split()
    return specs


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
    if editor_mode(ctx.project, ctx.beacon).get("headless"):
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
    """`ue perf`: load and --scalability live here; the frame-time capture is play.cmd_perf."""
    if getattr(args, "action", None) == "load":
        return perf_load(ctx, args)
    if getattr(args, "scalability", None):
        return perf_scalability(ctx, args)
    if getattr(args, "res", None):
        return perf_at_res(ctx, args)
    return cmd_perf(ctx, args)


def perf_at_res(ctx, args):
    """`ue perf --res WxH`: the capture with the 3D scene rendered at that resolution's pixel count (screen
    percentage on the PIE viewport, which offscreen is small and fixed)."""
    res = parse_res(args.res)
    started = str(ctx.tether("pie", action="status")).strip() != "running"
    if started and cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=1.0, limit=0, script=None)):
        raise CliError("could not start PIE to measure")
    try:
        size = pie_viewport_size(ctx)
        percentage, clamped = res_percentage(res, size)
        print(f"rendering the 3D scene at {percentage:.0f}%{clamped} of the {size[0]}x{size[1]} PIE viewport: the pixel "
              f"count of {res[0]}x{res[1]} (post after the upscaler and the UI stay at the viewport's size)")
        with scoped_cvars(ctx, res_cvars(percentage, True)):
            time.sleep(1.0)  # let the render targets resize
            args.no_record = True  # not the baseline for later captures at the viewport's own size
            return cmd_perf(ctx, args)
    finally:
        if started:
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))


SCALABILITY_LEVELS = {"low": 0, "medium": 1, "high": 2, "epic": 3, "cinematic": 4}
SCALABILITY_GROUPS = ("ViewDistance", "AntiAliasing", "Shadow", "GlobalIllumination", "Reflection", "PostProcess",
                      "Texture", "Effects", "Foliage", "Shading", "Landscape")


def scalability_cvars(level):
    """The sg.*Quality console variables for a level name or number (low=0 ... cinematic=4)."""
    name = str(level).strip().lower()
    value = SCALABILITY_LEVELS.get(name, int(name) if name.isdigit() and int(name) <= 4 else None)
    if value is None:
        raise CliError(f"unknown scalability level '{level}' (low, medium, high, epic, cinematic or 0-4)")
    return {f"sg.{group}Quality": str(value) for group in SCALABILITY_GROUPS}


def scalability_rows(results):
    """Table lines for `ue perf --scalability`: one per level, frame/game/render/GPU in ms."""
    lines = [f"  {'level':<10} {'frame avg':>9} {'p95':>6} {'game':>6} {'render':>6} {'GPU':>6} {'hitches':>7}  bound"]
    for level, data in results:
        get = lambda key, stat="avg": data.get(key, {}).get(stat) if isinstance(data.get(key), dict) else None  # noqa: E731
        cell = lambda v: f"{v:6.1f}" if v else "     -"  # noqa: E731
        frame = get("FrameTime")
        lines.append(f"  {level:<10} {cell(frame):>9} {cell(get('FrameTime', 'p95'))} {cell(get('GameThreadTime'))} "
                     f"{cell(get('RenderThreadTime'))} {cell(get('GPUTime'))} {data.get('hitches', '-'):>7}  "
                     + (data.get("bound") or "-") + (" (capped)" if data.get("capped") else "")
                     + (f"  {data['shot']}" if data.get("shot") else ""))
    return lines


def perf_scalability(ctx, args):
    """The same PIE capture at each scalability level, then the original levels back."""
    levels = [l for l in args.scalability.split(",") if l.strip()]
    for level in levels:
        scalability_cvars(level)  # check them all before starting
    started = str(ctx.tether("pie", action="status")).strip() != "running"
    if started and cmd_pie(ctx, argparse.Namespace(action="start", simulate=False, warmup=1.0, limit=0, script=None)):
        raise CliError("could not start PIE to measure")
    results = []
    try:
        for level in levels:
            with scoped_cvars(ctx, scalability_cvars(level)):
                time.sleep(1.0)  # let the new settings settle (shader permutations, render targets, streaming)
                code, data = _quietly(cmd_perf, ctx, argparse.Namespace(
                    seconds=args.seconds or 5, editor=False, no_record=True, trace=False, top=6, limit=0, action=None))
                if code:
                    raise CliError(f"the capture at {level} failed")
                if args.shots:
                    shot = os.path.join(state_dir(ctx.project), "shots", f"scalability-{level.strip().lower()}.png")
                    _quietly(cmd_shot, ctx, shot_args(output=shot))
                    data["shot"] = shot
            results.append((level.strip().lower(), data))
            print(f"{level.strip().lower()}: frame {data['FrameTime']['avg']:.1f} ms", flush=True)
    finally:
        if started:
            cmd_pie(ctx, argparse.Namespace(action="stop", simulate=False, warmup=0, limit=0, script=None))
    print(f"scalability ({args.seconds or 5:g}s of PIE each; original levels restored):")
    for line in scalability_rows(results):
        print(line)
    ctx.data = [dict(data, level=level) for level, data in results]
    return 0


def pie_command(ctx, args):
    """`ue pie`: watch and record live here; everything else is play.cmd_pie."""
    if args.action == "watch":
        return pie_watch(ctx, args)
    if args.action == "do" and (args.watch or (args.file and watch_header(args.file))):
        watch_args = argparse.Namespace(**vars(args))
        watch_args.script = args.watch or watch_header(args.file)
        watch_args.seconds = None
        if not args.file:  # inline steps: play them from a file like any step file
            folder = os.path.join(state_dir(ctx.project), "watch")
            os.makedirs(folder, exist_ok=True)
            watch_args.file = os.path.join(folder, "inline-steps.txt")
            with open(watch_args.file, "w", encoding="utf-8") as f:
                f.write("\n".join(args.script) + "\n")
        # A step file under tests/play that watches values is a regression test: compare with its baseline, or
        # record one. Anywhere else, only --baseline records and --compare compares.
        plays = os.path.join(os.path.dirname(ctx.project), "tests", "play")
        in_tests = bool(args.file) and os.path.normcase(os.path.abspath(args.file)).startswith(os.path.normcase(plays))
        if args.baseline or args.compare:
            watch_args.baseline, watch_args.compare = args.baseline, args.compare
        elif in_tests:
            name = os.path.splitext(os.path.basename(args.file))[0]
            recorded = os.path.isfile(baseline_path(ctx.project, name))
            watch_args.baseline, watch_args.compare = (None, name) if recorded else (name, None)
        else:
            watch_args.baseline = watch_args.compare = None
        return pie_watch(ctx, watch_args)
    if args.action == "record":
        return pie_record(ctx, args)
    return cmd_pie(ctx, args)


# Config/Tether/budget.json: limits, and what to measure them on.
BUDGET_KEYS = {
    "frame_ms_p95": ("frame time p95", "ms"), "game_ms_p95": ("game thread p95", "ms"),
    "render_ms_p95": ("render thread p95", "ms"), "gpu_ms_p95": ("GPU p95", "ms"), "hitches": ("hitches", ""),
    "memory_mb": ("editor + PIE memory", "MB"), "textures_mb": ("texture memory", "MB"),
    "texture_assets_mb": ("/Game textures at full resolution", "MB"),
    "map_mb": ("map on disk", "MB"), "content_mb": ("content on disk", "MB"),
    "pie_start_s": ("PIE start", "s"), "streaming_s": ("streaming done", "s"), "load_hitches": ("load hitches", ""),
}
PERF_STATS = {"frame_ms_p95": "FrameTime", "game_ms_p95": "GameThreadTime", "render_ms_p95": "RenderThreadTime",
              "gpu_ms_p95": "GPUTime"}


def budget_rows(limits, measured):
    """(label, value text, limit, verdict) per limit, in BUDGET_KEYS order; verdict OK, OVER or n/a."""
    rows = []
    for key, (label, unit) in BUDGET_KEYS.items():
        if key not in limits:
            continue
        value = measured.get(key)
        if value is None:
            rows.append((label, "not measured", limits[key], "n/a"))
            continue
        text = f"{value:.1f} {unit}".strip() if isinstance(value, float) else f"{value} {unit}".strip()
        rows.append((label, text, limits[key], "OVER" if value > limits[key] else "OK"))
    return rows


def _quietly(fn, ctx, args):
    """Run a command for its data, keeping its own output out of the budget table."""
    import contextlib
    import io
    saved, ctx.data = ctx.data, None
    with contextlib.redirect_stdout(io.StringIO()):
        code = fn(ctx, args)
    data, ctx.data = ctx.data, saved
    return code, data or {}


def cmd_budget(ctx, args):
    """Measure what Config/Tether/budget.json limits and compare. Exit 1 when anything is over."""
    path = os.path.join(os.path.dirname(ctx.project), "Config", "Tether", "budget.json")
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except OSError:
        raise CliError(f"no {os.path.relpath(path, os.path.dirname(ctx.project))}: write one, e.g. "
                       '{"map": "/Game/Maps/Main", "frame_ms_p95": 16.7, "memory_mb": 8000, "pie_start_s": 5}')
    except ValueError as exc:
        raise CliError(f"{path}: {exc}")
    limits = {k: v for k, v in config.items() if k in BUDGET_KEYS}
    unknown = [k for k in config if k not in BUDGET_KEYS and k not in ("map", "route", "seconds", "scalability")]
    if unknown:
        print(f"note: ignoring unknown keys {', '.join(unknown)} (known: {', '.join(BUDGET_KEYS)})")
    measured, seconds = {}, float(config.get("seconds", 10))
    wanted = set(limits)
    if config.get("map") or wanted & {"pie_start_s", "streaming_s", "load_hitches"}:
        load_args = argparse.Namespace(action="load", map=config.get("map"), file=config.get("route"), hitch=50.0, top=8,
                                       seconds=None if config.get("route") else 3)
        _, data = _quietly(perf_load, ctx, load_args)
        measured.update(pie_start_s=data.get("pie_start_s"), streaming_s=data.get("streaming_done_s"),
                        load_hitches=len(data.get("hitches", [])))
    if wanted & (set(PERF_STATS) | {"hitches"}):
        level = config.get("scalability")
        with scoped_cvars(ctx, scalability_cvars(level)) if level is not None else contextlib.nullcontext():
            _, data = _quietly(cmd_perf, ctx, argparse.Namespace(seconds=seconds, editor=False, no_record=True,
                                                                 trace=False, top=6, limit=0, action=None))
        for key, stat in PERF_STATS.items():
            if isinstance(data.get(stat), dict):
                measured[key] = round(data[stat].get("p95", 0.0), 1)
        measured["hitches"] = data.get("hitches")
    if wanted & {"memory_mb", "textures_mb"}:
        _, data = _quietly(cmd_mem, ctx, argparse.Namespace(full="textures_mb" in wanted, top=3, timeout=args.timeout))
        measured.update(memory_mb=data.get("process_mb"), textures_mb=data.get("textures_mb"))
    if "texture_assets_mb" in wanted:
        _, data = _quietly(cmd_tex, ctx, argparse.Namespace(path="/Game", streaming=False, top=1, max=None, strict=False,
                                                            limit=1))
        measured["texture_assets_mb"] = round(data.get("total_bytes", 0) / 1048576, 1)
    if "content_mb" in wanted:
        _, data = _quietly(cmd_size, ctx, argparse.Namespace(path="/Game", deps=False, top=1, limit=1))
        measured["content_mb"] = round(data.get("total_bytes", 0) / 1048576, 1)
    if "map_mb" in wanted:
        level = config.get("map") or (ctx.beacon or {}).get("map") or ""
        if level.startswith("/Game/"):  # the map package and, with World Partition, its actors' packages
            try:
                actors = folder_packages(ctx.project, "/Game/__ExternalActors__/" + level[len("/Game/"):])
            except CliError:  # not a World Partition map
                actors = {}
            measured["map_mb"] = round(((package_bytes(ctx.project, level) or 0) + sum(actors.values())) / 1048576, 1)
    rows = budget_rows(limits, measured)
    width = max(len(r[0]) for r in rows) if rows else 0
    print(f"budget ({os.path.relpath(path, os.path.dirname(ctx.project))}" + (f", map {config['map']}" if config.get("map") else "")
          + (f", scalability {config['scalability']}" if config.get("scalability") is not None else "") + "):")
    for label, text, limit, verdict in rows:
        print(f"  {verdict:<4} {label:<{width}}  {text} (limit {limit:g})")
    over = [r for r in rows if r[3] == "OVER"]
    print(f"{len(over)} over budget" if over else "within budget")
    ctx.data = [{"limit": lim, "label": lab, "value": text, "verdict": v} for lab, text, lim, v in rows]
    return 1 if over else 0


def package_command(ctx, args):
    """`ue package`: --perf lives here (it reads profiler captures like ue perf); the rest is jobs.cmd_package."""
    if getattr(args, "perf", None):
        if not args.boot_only:  # package first, like the boot test; --boot-only measures the last package
            code = cmd_package(ctx, argparse.Namespace(**dict(vars(args), no_boot=True, play=None)))
            if code:
                return code
        return package_perf(ctx, args)
    return cmd_package(ctx, args)


CAPTURE_WRITTEN = re.compile(r"Capture Ended\. Writing CSV to file\s*:\s*(.+\.csv)")


def wait_for_capture(proc, log, before, root, name, platform, timeout):
    """The CSV path once this run's log (its own -abslog file) says the capture ended and the file stops growing;
    None when the game exits first or the time runs out. The log names it relative to the game's
    Binaries/<Platform> folder; a file that was already there (an earlier run's) never counts."""
    deadline, path, size = time.time() + timeout, None, -1
    binaries = os.path.join(root, name, "Binaries", platform)
    while time.time() < deadline:
        if path is None and os.path.isfile(log):
            with open(log, encoding="utf-8", errors="replace") as f:
                m = CAPTURE_WRITTEN.search(f.read())
            if m:
                found = os.path.normpath(os.path.join(binaries, m.group(1).strip()))
                path = found if os.path.basename(found) not in before else None
        if path and os.path.isfile(path):
            now = os.path.getsize(path)
            if now and now == size:
                return path
            size = now
        if proc.poll() is not None:
            return path
        time.sleep(1)
    return None


def package_perf(ctx, args):
    """Frame times in the packaged game: run it with the CSV profiler for a fixed number of frames on a map, then
    report like ue perf, next to the last editor capture (editor numbers flatter or punish a game unfairly)."""
    from .core import host_platform
    platform = args.platform or host_platform()
    exe = packaged_executable(ctx.project, platform)
    if not exe:
        raise CliError("no packaged game (ue package first)")
    stale = args.boot_only and package_staleness(ctx.project, exe)
    if stale:
        print(f"warning: {stale}")
    name = os.path.splitext(os.path.basename(ctx.project))[0]
    csv_dir = os.path.join(os.path.dirname(exe), name, "Saved", "Profiling", "CSV")
    # This run's own log: the game's default one still holds the previous run's "Capture Ended" line until the
    # new process rewrites it, and that line names the previous run's CSV.
    game_log = os.path.join(state_dir(ctx.project), "perf", "packaged.log")
    os.makedirs(os.path.dirname(game_log), exist_ok=True)
    if os.path.exists(game_log):
        os.remove(game_log)
    before = set(os.listdir(csv_dir)) if os.path.isdir(csv_dir) else set()
    warmup = 90  # loading and shader warm-up frames, left out of the numbers
    frames = int(args.perf * 60) + warmup
    command = [exe] + ([args.map] if getattr(args, "map", None) else []) + [
        "-RenderOffscreen", "-nosound", "-unattended", "-windowed", "-ResX=1280", "-ResY=720",
        f"-csvCaptureFrames={frames}", "-csvExitOnCompletion", f"-abslog={game_log}"]
    started = time.time()
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    # -csvExitOnCompletion does not always quit the game: wait for the capture to be written, then end it.
    written = wait_for_capture(proc, game_log, before, os.path.dirname(exe), name, platform, max(180.0, args.perf * 20))
    exited = proc.poll() is not None
    from .jobs import kill_tree
    kill_tree(proc.pid)
    if written is None and not exited:
        raise CliError(f"the packaged game did not finish {frames} frames in time (stuck at a menu? pass a map; "
                       f"its log: {game_log})", 4)
    new = [f for f in (os.listdir(csv_dir) if os.path.isdir(csv_dir) else []) if f.endswith(".csv") and f not in before]
    if written and os.path.isfile(written) and os.path.basename(written) not in new:
        csv_dir, new = os.path.dirname(written), [os.path.basename(written)]
    if not new:
        raise CliError(f"the packaged game wrote no profiler capture (exit code {proc.poll()}); is it a Shipping build? "
                       "CSV profiling needs Development")
    path = os.path.join(csv_dir, max(new, key=lambda f: os.path.getmtime(os.path.join(csv_dir, f))))
    try:
        columns = read_profile_csv(path)
    except (IndexError, OSError):
        columns = {}
    if not columns.get("FrameTime"):
        raise CliError(f"the capture is empty: the game exited (code {proc.poll()}) before profiling any frames; "
                       f"its log: {game_log} (ue package --boot-only shows why)")
    captured = sum(columns["FrameTime"]) / 1000
    if captured > time.time() - started + 2:  # frames that took longer than the run itself: not this run's file
        raise CliError(f"{path} holds {captured:.0f}s of frames but the game ran {time.time() - started:.0f}s: not this "
                       f"run's capture (its log: {game_log})")
    stats = {k: frame_stats(v[warmup:] or v) for k, v in columns.items()
             if k in ("FrameTime", "GameThreadTime", "RenderThreadTime", "GPUTime") and v}
    if "FrameTime" not in stats:
        raise CliError(f"no FrameTime column in {path}")
    ft = stats["FrameTime"]
    count = len(columns["FrameTime"][warmup:])
    hitches = sum(1 for t in columns["FrameTime"][warmup:] if t > max(2 * ft["p50"], 33.3))
    editor = read_json(os.path.join(state_dir(ctx.project), "perf", "last.json")) or {}
    print(f"packaged game: {count} frames in {time.time() - started:.0f}s: frame avg {ft['avg']:.1f} ms "
          f"({1000 / ft['avg']:.0f} fps), p95 {ft['p95']:.1f}, max {ft['max']:.1f}; {hitches} hitch(es)")
    for key, label in (("GameThreadTime", "game"), ("RenderThreadTime", "render"), ("GPUTime", "GPU")):
        if key in stats:
            ed = editor.get(key, {}).get("avg")
            print(f"  {label:<6} {stats[key]['avg']:6.1f} ms avg, p95 {stats[key]['p95']:.1f}"
                  + (f"   (editor PIE last capture: {ed:.1f})" if ed else ""))
    if editor.get("FrameTime"):
        print(f"  editor PIE frame avg was {editor['FrameTime']['avg']:.1f} ms (ue perf); the packaged game has no editor "
              "overhead, cooked assets and its own scalability settings")
    print(f"  csv: {path}")
    ctx.data = dict(stats, frames=count, hitches=hitches, csv=path)
    return 0
