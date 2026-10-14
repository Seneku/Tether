"""Claude Code hooks: the session start banner, pre-edit and pre-MCP guards, post-edit checks."""
import json
import os
import re
import sys
import time

from .core import (
    CliError, chosen_editor, editor_label, editor_live, editor_process, live_editors, find_project, pid_alive, project_of, read_json, stall_report, state_dir,
    write_json,
)
from .transport import FAB_GUARD, SLATE_INPUT, Mcp, fab_tab_open
from .build import classify_changes, csharp_changes, csharp_layout
from .assets import project_overview


def hook_pre_edit(payload):
    """Deny edits to C# under an UnrealSharp project's Script/ while that project's editor is in PIE:
    UnrealSharp reprocesses saved C# at once, which can crash the running session."""
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.lower().endswith(".cs"):
        return None
    path = os.path.abspath(path)
    project = project_of(path)
    layout = csharp_layout(project) if project else None
    if not layout or not os.path.normcase(path).startswith(os.path.normcase(layout[0]) + os.sep):
        return None
    if not (editor_live(project) or {}).get("pie"):
        return None
    name = os.path.splitext(os.path.basename(project))[0]
    return (f"{name}'s editor is in Play-In-Editor. UnrealSharp reprocesses saved C# files immediately, "
            "which can crash the running PIE session. Run `ue pie stop` first, make the edit, then `ue build`.")


def hook_pre_mcp(payload):
    """Deny Slate input while a Fab tab is open, and Epic's StartPIE while saved C# is not hot-reloaded."""
    return hook_fab_input(payload) or hook_start_pie(payload)


def hook_fab_input(payload):
    """Deny Epic's Slate inspector input tools (click, type, keys, drag) while the editor shows a Fab tab: the
    store page is a web browser the inspector cannot see into, so input could land on Buy or Add to My Library."""
    args = payload.get("tool_input") or {}
    if args.get("tool_name") not in SLATE_INPUT or "SlateInspector" not in str(args.get("toolset_name", "")):
        return None
    os.chdir(payload.get("cwd") or os.getcwd())
    project = find_project(None)
    beacon = editor_live(project)
    if not beacon or not beacon.get("mcp_port"):
        return None
    try:
        mcp = Mcp(beacon["mcp_port"], 5).connect(None)
        text, failed = mcp.call(args["toolset_name"], "Snapshot", {"ref": "", "maxDepth": 120})
    except CliError:
        return None
    return FAB_GUARD if not failed and fab_tab_open(text) else None


def hook_start_pie(payload):
    """Deny Epic's StartPIE (called directly or from a batch script) while saved C# is not hot-reloaded:
    UnrealSharp would recompile the stale type on its first lookup and drop its subsystems from PIE."""
    if "StartPIE" not in (payload.get("tool_name") or "") + json.dumps(payload.get("tool_input") or {}):
        return None
    os.chdir(payload.get("cwd") or os.getcwd())
    project = find_project(None)
    if not editor_live(project):
        return None
    changed = csharp_changes(project)
    if not changed:
        return None
    names = ", ".join(os.path.basename(p) for p in changed[:3]) + (f" +{len(changed) - 3} more" if len(changed) > 3 else "")
    return (f"C# changed since the last reload ({names}). Starting PIE now breaks UnrealSharp subsystems for the "
            "whole session: UnrealSharp recompiles the stale type on its first lookup and drops its subsystems "
            "from the PIE world. Use `ue pie start` instead (it hot-reloads first), or run `ue build`, then retry.")


def project_in(cwd):
    """The project a session in `cwd` works on (the .uproject at or above it, or one level below), or None."""
    try:
        here = os.getcwd()
        os.chdir(cwd or here)
        try:
            return find_project(None)
        finally:
            os.chdir(here)
    except (CliError, OSError):
        return None


def hook_session_start(payload):
    """Editor state plus a brief project overview at the start of a session, so the agent need not explore."""
    project = project_in(payload.get("cwd"))
    if not project:
        return None
    name = os.path.splitext(os.path.basename(project))[0]
    try:
        brief = "\n".join(project_overview(project, full=False)[1:])
    except Exception:  # a hook must never fail
        brief = ""
    beacon = chosen_editor(project) or {}
    if not editor_process(beacon.get("pid")):
        head = (f"Unreal project {name}: editor not running. Use the `ue` CLI (unreal-tether skill); "
                "start it with `ue open --headless` (`--render` if you need `ue shot`).")
    else:
        report = stall_report(project)
        state = report or ("PIE running" if beacon.get("pie") else "ready")
        head = (f"Unreal project {name}: editor {state} | map {beacon.get('map')} | UE "
                f"{beacon.get('engine', '?').split('+')[0]} | mcp :{beacon.get('mcp_port')} | {editor_label(beacon)}\n"
                "Use the `ue` CLI (unreal-tether skill); `ue status` refreshes this.")
        if beacon.get("launched_by_tether") is False:
            head += ("\nThis is the user's own editor: they may be working in it. Don't close or restart it "
                     "(ue build --restart included) without asking.")
        others = [b for b in live_editors(project) if b.get("pid") != beacon.get("pid")]
        if others:
            head += ("\nAlso open on this project: " + "; ".join(editor_label(b) for b in others)
                     + ". Saving from two editors loses work; see `ue status`.")
    return head + (f"\nProject (ue overview for more):\n{brief}" if brief else "")


RESTART_MACROS = re.compile(r"\b(UPROPERTY|UCLASS|USTRUCT|UENUM|UINTERFACE|GENERATED_BODY|GENERATED_UCLASS_BODY)\b")
FUNCTION_MACRO = re.compile(r"\bUFUNCTION\b")


def classify_edit(path, tool_input):
    """How an edit to project source reaches the running editor: (route, reason)."""
    lower = path.lower()
    if lower.endswith((".build.cs", ".target.cs", ".uproject", ".uplugin")):
        return "restart", "module or project files changed"
    if lower.endswith(".cs"):
        return "csharp", "C# hot reload"
    before = tool_input.get("old_string", "")
    after = tool_input.get("new_string", tool_input.get("content", ""))
    if "content" in tool_input and "new_string" not in tool_input:  # Write: no old text to compare
        if RESTART_MACROS.search(after) and lower.endswith((".h", ".hpp")):
            return "maybe-restart", "rewrote a reflected header"
        return "live", "function bodies"
    if sorted(RESTART_MACROS.findall(before)) != sorted(RESTART_MACROS.findall(after)) or (
            RESTART_MACROS.search(before + after) and lower.endswith((".h", ".hpp"))):
        return "restart", "reflected declarations changed"
    if len(FUNCTION_MACRO.findall(after)) > len(FUNCTION_MACRO.findall(before)):
        return "live-ufunction", "new UFUNCTION"
    if lower.endswith((".h", ".hpp")):  # inline bodies patch live; new members change the class layout
        return "maybe-restart", "header edit"
    return "live", "function bodies"


EDIT_ADVICE = {
    "restart": "needs `ue build --restart` (Live Coding cannot patch class layout or reflection); batch such changes, "
               "then `ue bp check` to find Blueprints that used what changed",
    "maybe-restart": "`ue build` (Live Coding) if only inline function bodies changed; `ue build --restart` if "
                     "members, UPROPERTYs, class layout or defaults changed",
    "live-ufunction": "`ue build` (Live Coding) makes it callable via reflection (cdo.call_method); Python and "
                      "Blueprint wrappers stay stale until `ue build --restart`",
    "live": "`ue build` (Live Coding, ~5 s) when you are done editing; no restart needed",
    "csharp": "`ue build` hot-reloads it in ~1 s when you are done editing",
}


def hook_post_edit(payload):
    """After an edit to project C++/C#/build files, one line on how to get it into the running editor."""
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if not path.lower().endswith((".h", ".hpp", ".cpp", ".inl", ".cs", ".uproject", ".uplugin")):
        return None
    project = project_of(path)
    if not project:
        return None
    root = os.path.normcase(os.path.dirname(project)) + os.sep
    full = os.path.normcase(os.path.abspath(path))
    rel = full[len(root):].lower() if full.startswith(root) else ""  # normcase lowercases only on Windows
    if not (rel.startswith(("source" + os.sep, "plugins" + os.sep, "script" + os.sep)) or rel.endswith(".uproject")):
        return None
    if rel.endswith(".cs") and not rel.endswith((".build.cs", ".target.cs")) and not csharp_layout(project):
        return None
    route, reason = classify_edit(path, tool_input)
    live = editor_live(project)
    advice = EDIT_ADVICE[route] if live else "editor not running: `ue build` compiles with UBT"
    return f"Tether: {os.path.basename(path)} ({reason}): {advice}."


def hook_stop(payload):
    """At the end of a turn, nudge once to build if project code changed since the last green build."""
    if payload.get("stop_hook_active"):
        return None  # already nudged this stop; never loop
    project = project_in(payload.get("cwd"))
    if not project or not editor_live(project):
        return None
    recent = time.time() - 3600  # ignore edits older than an hour: likely not this session's
    cpp = classify_changes(project)
    changed = [p for p in (cpp[0] if cpp else []) if p.endswith((".h", ".hpp", ".cpp", ".inl", ".cs"))
               and os.path.getmtime(p) > recent]
    changed += [p for p in (csharp_changes(project) or []) if os.path.getmtime(p) > recent]
    advice = []
    if changed:
        names = ", ".join(os.path.basename(p) for p in changed[:3]) + (f" +{len(changed) - 3} more" if len(changed) > 3 else "")
        restart = cpp and (cpp[1] or cpp[2])
        advice.append(f"Project code changed since the last green build ({names}) and the editor is running. Run "
                      f"`ue build{' --restart' if restart else ''}` to compile and check it")
    # Assets saved since the last `ue validate` (or the last nudge about them): suggest validating once.
    marker_path = os.path.join(state_dir(project), "validated.json")
    marker = read_json(marker_path) or {}
    saved = assets_saved_since(project, max(recent, marker.get("time", 0), marker.get("nudged", 0)))
    if saved:
        names = ", ".join(os.path.basename(p) for p in saved[:3]) + (f" +{len(saved) - 3} more" if len(saved) > 3 else "")
        advice.append(f"{len(saved)} asset(s) were saved since the last validation ({names}). Run `ue validate` "
                      "(data validation + Map Check on changed and unsaved assets)")
        marker["nudged"] = time.time()
        write_json(marker_path, marker)
    if not advice:
        return None
    return "; ".join(advice) + ", or tell the user why it should wait."


def assets_saved_since(project, since):
    """.uasset/.umap files under the project's Content modified after `since` (a scandir walk; ~0.2 s per 3k)."""
    found, stack = [], [os.path.join(os.path.dirname(project), "Content")]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif entry.name.endswith((".uasset", ".umap")) and entry.stat().st_mtime > since:
                found.append(entry.path)
    return sorted(found)


def cmd_hook(ctx, args):
    """Claude Code hook entry points (hook JSON on stdin). A failing hook stays silent: it must never block."""
    try:
        payload = json.load(sys.stdin)
        if args.event in ("pre-edit", "pre-mcp"):
            reason = {"pre-edit": hook_pre_edit, "pre-mcp": hook_pre_mcp}[args.event](payload)
            if reason:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
        elif args.event == "session-start":
            context = hook_session_start(payload)
            if context:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
        elif args.event == "post-edit":
            context = hook_post_edit(payload)
            if context:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}))
        elif args.event == "stop":
            reason = hook_stop(payload)
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        pass
    return 0
