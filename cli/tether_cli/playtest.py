"""Play testing: input maps, recording a play session, watching properties over time."""
import os
import re

from .core import CliError
from .assets import code_usages
from .play import cmd_pie


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


def pie_command(ctx, args):
    """`ue pie`: record and watch live here; everything else is play.cmd_pie."""
    return cmd_pie(ctx, args)
