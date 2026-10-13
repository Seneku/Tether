"""ue test: Automation tests through Epic's test toolset."""
import json
import os
import re
import time

from .core import CliError, compact_error, log_end, log_path, state_dir, strip_common, unwrap, write_json
from .transport import catalog
from .calls import flush_editor_log


TEST_LOCATION = re.compile(r"LogAutomationController: (?:Error|Warning): (.*) \[(.+)\((\d+)\)\]\s*$")


def test_locations(ctx, start):
    """Map each automation error/warning message to 'file:line', from the log lines since `start`."""
    flush_editor_log(ctx)
    with open(log_path(ctx.project), "rb") as f:
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    root = os.path.normcase(os.path.dirname(ctx.project)) + os.sep
    found = {}
    for line in lines:
        match = TEST_LOCATION.search(line)
        if match:
            path = match.group(2)
            if os.path.normcase(path).startswith(root):
                path = path[len(root):]
            found.setdefault(match.group(1).strip(), f"{path}:{match.group(3)}")
    return found


def cmd_test(ctx, args):
    """Run automation tests in the live editor through Epic's AutomationTestToolset; print only failures."""
    if not args.filter and not args.list:
        raise CliError("give a test filter (e.g. `ue test MyGame.`), or `ue test --list [filter]`")
    if not args.timeout_given:
        ctx.args.timeout = 3600.0  # a run blocks until every selected test has finished
    toolset = "AutomationTestToolset.AutomationTestToolset"
    if toolset not in catalog(ctx):
        raise CliError("Epic's AutomationTestToolset plugin is not enabled; add {\"Name\": \"AutomationTestToolset\", "
                       "\"Enabled\": true} to the .uproject's Plugins (or use AllToolsets), then `ue build --restart`")

    def call(tool, payload):
        text, is_error = ctx.mcp.call(toolset, tool, payload)
        if is_error:
            raise CliError(compact_error(text))
        return unwrap(text)

    call("DiscoverTests", {"bForceRediscover": True})  # picks up tests added by Live Coding
    if args.list:
        # ListTests takes a plain substring, so drop the run filter's anchors and prefixes.
        name = re.sub(r"^(StartsWith:|\^)|\$$", "", args.filter or "")
        found = call("ListTests", {"nameFilter": name, "tagFilter": "", "limit": 0})
        names = found.get("tests", [])
        prefix, short_names = strip_common(names)
        print(f"{len(names)} tests" + (f" under {prefix}" if prefix else "") + (f" matching '{args.filter}'" if args.filter else ""))
        for name in short_names[: args.limit]:
            print("  " + name)
        if len(names) > args.limit:
            print(f"... +{len(names) - args.limit} more (--limit)")
        return 0
    start, started = log_end(ctx.project), time.time()
    text, is_error = ctx.mcp.call(toolset, "RunTestsByFilter", {"filterExpression": args.filter})
    elapsed = time.time() - started
    result = unwrap(text) if not is_error else None
    if is_error:
        # Epic's MCP server fails the whole call when any test logs a script warning (some of Epic's own
        # tests do so on purpose), although the run completed. The results are still there.
        if "matched filter" in text:
            raise CliError(compact_error(text) + f" (ue test --list {args.filter})")
        result = call("GetTestResults", {})
        if not (isinstance(result, dict) and result.get("tests")):
            raise CliError(compact_error(text))
        print(f"note: the run call reported '{compact_error(text)[:120]}'; results read back from the controller")
    tests = result.get("tests", []) if isinstance(result, dict) else []
    if not tests:
        raise CliError(f"no tests match '{args.filter}' (ue test --list {args.filter})")
    os.makedirs(os.path.join(state_dir(ctx.project), "tests"), exist_ok=True)
    write_json(os.path.join(state_dir(ctx.project), "tests", "last.json"), dict(result, filter=args.filter))
    if args.json:
        print(json.dumps(result))
        return 1 if result.get("failed") else 0
    failed = [t for t in tests if t.get("state") not in ("Success", "Skipped", "NotRun")]
    skipped = sum(1 for t in tests if t.get("state") in ("Skipped", "NotRun"))
    warned = [t for t in tests if t.get("warnings")]
    parts = [f"{len(tests) - len(failed) - skipped} passed"] + ([f"{len(failed)} failed"] if failed else []) \
        + ([f"{skipped} skipped"] if skipped else []) + ([f"{len(warned)} with warnings"] if warned else [])
    print(f"{len(tests)} tests: {', '.join(parts)} in {elapsed:.1f}s")
    where = test_locations(ctx, start) if failed or (warned and args.verbose) else {}
    for test in failed:
        print(f"FAIL {test['name']} ({test.get('duration', 0):.2f}s)" + (f" [{test.get('state')}]" if test.get("state") != "Fail" else ""))
        for message in test.get("errors", [])[:5] or ["(no error message; ue log --level Warning)"]:
            loc = where.get(message.strip())
            print(f"  {message}" + (f"  [{loc}]" if loc else ""))
    if args.verbose:
        for test in warned:
            for message in test["warnings"][:5]:
                loc = where.get(message.strip())
                print(f"WARN {test['name']}: {message}" + (f"  [{loc}]" if loc else ""))
    return 1 if failed else 0
