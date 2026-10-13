"""Play testing: input maps, recording a play session, watching properties over time."""
import argparse
import csv
import json
import os
import re
import time

from .core import CliError, state_dir
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


def pie_command(ctx, args):
    """`ue pie`: watch and record live here; everything else is play.cmd_pie."""
    if args.action == "watch":
        return pie_watch(ctx, args)
    return cmd_pie(ctx, args)
