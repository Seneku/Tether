"""The command line: argument parsing, ue batch, and main()."""
import argparse
import json
import os
import re
import sys
import time

from . import core
from .core import UE_SCRIPT, CliError, crash_summary, find_project, live_editors, recent_crash, stall_report
from .transport import Ctx
from .share import LEASE_MINUTES, cmd_stats, lease_messages, record_activity, record_stat
from .editor import cmd_build, cmd_close, cmd_crash, cmd_git, cmd_lease, cmd_open, cmd_status, cmd_who
from .calls import (
    cmd_actors, cmd_ai, cmd_anim, cmd_api, cmd_asset, cmd_call, cmd_changes, cmd_config, cmd_delete, cmd_drop,
    cmd_duplicate, cmd_exec, cmd_find, cmd_log, cmd_mat, cmd_mv, cmd_nav, cmd_new, cmd_props, cmd_py, cmd_refs,
    cmd_class, cmd_collision, cmd_cvar, cmd_tree, cmd_lights, cmd_post, cmd_wp, cmd_level, cmd_rm, cmd_statetree, cmd_trace, cmd_save, cmd_savegame, cmd_scatter, cmd_set, cmd_spawn, cmd_tools, cmd_undo, cmd_watch,
)
from .images import VIEW_MODES, camera_at, cmd_look, cmd_shot, shot_args
from .testing import cmd_test
from .assets import (
    cmd_data, cmd_diff, cmd_fx, cmd_move_pack, cmd_net, cmd_overview, cmd_pcg, cmd_skel, cmd_size, cmd_table, cmd_usages,
    cmd_validate,
)
from .content import (
    cmd_collide, cmd_foliage, cmd_import, cmd_landscape, cmd_lint, cmd_map, cmd_mesh, cmd_tex, cmd_unused, cmd_water,
)
from .fab import cmd_fab
from .jobs import cmd_game, cmd_job, cmd_package
from .play import DIRTY_PACKAGES, cmd_audio, cmd_gpu, cmd_mem, cmd_sound, cmd_soak, cmd_ui, cmd_wait, cmd_world, wait_compile
from .transport import py_json
from .playtest import bp_command, cmd_budget, cmd_input, package_command, perf_command, pie_command
from .hooks import cmd_hook
from .setup import cmd_doctor, cmd_init, cmd_reload, cmd_template, new_project, tether_version
from .selftest import cmd_ci, cmd_selftest


def build_command(ctx, args):
    """`ue build` (editor.cmd_build); --when-closed hands it to a background job that waits for the editors."""
    if args.when_closed and live_editors(ctx.project):
        pids = ", ".join(str(b.get("pid")) for b in live_editors(ctx.project))
        # not the --pid editor: it is one of those that must close first, and the job's build only runs UBT
        chosen = os.environ.pop("UE_EDITOR_PID", None)
        try:
            code = cmd_job(ctx, argparse.Namespace(
                action="start", name="build when the editors close", platform=None,
                job_command=[sys.executable, UE_SCRIPT, "--project", ctx.project, "build", "--after-close"]))
        finally:
            if chosen is not None:
                os.environ["UE_EDITOR_PID"] = chosen
        print(f"it waits for editor(s) {pids} to close, then runs `ue build` (UBT)")
        return code
    return cmd_build(ctx, args)


def exec_command(ctx, args):
    """`ue exec` (calls.cmd_exec); with --shot, `ue shot` runs right after the command, before its log is read."""
    if args.shot is None:
        return cmd_exec(ctx, args)
    shoot = lambda: cmd_shot(ctx, shot_args(output=args.shot or None, hud=args.hud))  # noqa: E731
    return cmd_exec(ctx, args, after=shoot)


def run_line(ctx, line):
    """Run one `ue` command line in this process, with its output captured: (exit code, output)."""
    import contextlib
    import io
    import shlex
    argv = shlex.split(line, posix=True)
    argv = argv[1:] if argv and argv[0] == "ue" else argv
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            sub = parse_command(build_parser(), undo_msys_paths(argv))
            if sub.fn in (cmd_batch, cmd_hook, gpu_command):
                raise CliError(f"`ue {sub.cmd}` cannot run inside another command")
            code = sub.fn(ctx, sub) or 0
        except CliError as exc:
            print(f"error: {exc}")
            code = exc.code
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, buf.getvalue()


def gpu_ab(ctx, args):
    """`ue gpu --ab "<ue command A>" "<ue command B>"`: the GPU frame at the same camera with each of two states,
    alternating A, B, A, B so drift averages out; waits for shader compiles after each switch. --restore runs a
    last command (the state to leave behind)."""
    import contextlib
    import io
    dirty_before = set(py_json(ctx, DIRTY_PACKAGES) or [])
    runs = {"A": [], "B": []}
    profile = argparse.Namespace(**{k: v for k, v in vars(args).items() if k not in ("ab", "restore", "rounds")})
    for _ in range(max(1, args.rounds)):
        for label, line in (("A", args.ab[0]), ("B", args.ab[1])):
            code, out = run_line(ctx, line)
            if code:
                raise CliError(f"{label} (`ue {line}`) failed: {out.strip().splitlines()[-1] if out.strip() else code}")
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.suppress(CliError):  # not on Windows: no compile wait, just a pause
                    wait_compile(ctx, argparse.Namespace(max=args.compile_max, workers_only=True))
                time.sleep(1.0)  # the first frames after a switch reallocate and recompile
                cmd_gpu(ctx, profile)
            runs[label].append(ctx.data)
    restored = ""
    if args.restore:
        code, out = run_line(ctx, args.restore)
        restored = f"; restored with `ue {args.restore}`" if not code else f"; --restore failed: {out.strip()[-200:]}"
    mean = lambda rows, f: sum(f(r) for r in rows) / len(rows)  # noqa: E731
    a_ms, b_ms = mean(runs["A"], lambda r: r["gpu_ms"]), mean(runs["B"], lambda r: r["gpu_ms"])
    spread = max(abs(runs[k][i]["gpu_ms"] - runs[k][0]["gpu_ms"]) for k in runs for i in range(len(runs[k])))
    print(f"A {a_ms:.2f} ms, B {b_ms:.2f} ms per frame: B is {b_ms - a_ms:+.2f} ms ({100 * (b_ms - a_ms) / (a_ms or 1):+.1f}%)"
          f"; {args.rounds} round(s) of {args.frames} frame(s) each, runs of one state within {spread:.2f} ms" + restored)
    print(f"  A: ue {args.ab[0]}\n  B: ue {args.ab[1]}")
    if not all(r.get("scene", True) for rows in runs.values() for r in rows):
        print("note: no 3D scene was rendered in some of the frames, so they measured editor UI: start PIE (ue pie "
              "start) or make a level viewport realtime, and put the camera where the difference shows")
    kinds = sorted({k for r in runs["A"] + runs["B"] for k in r["kinds"]})
    deltas = [(k, mean(runs["B"], lambda r: r["kinds"].get(k, 0.0)) - mean(runs["A"], lambda r: r["kinds"].get(k, 0.0)))
              for k in kinds]
    shown = [(k, d) for k, d in sorted(deltas, key=lambda kd: -abs(kd[1])) if abs(d) >= args.min][: args.top]
    print("by kind (B - A): " + (", ".join(f"{k} {d:+.2f} ms" for k, d in shown) or f"nothing moved by {args.min} ms or more"))
    passes = {}
    for label in runs:
        for r in runs[label]:
            for name, ms in r["passes"]:
                passes.setdefault(name, {"A": 0.0, "B": 0.0})[label] += ms / len(runs[label])
    moved = sorted(((n, v["B"] - v["A"]) for n, v in passes.items()), key=lambda nd: -abs(nd[1]))
    moved = [(n, d) for n, d in moved if abs(d) >= args.min][: args.top]
    if moved:
        print("passes (B - A):")
        for name, d in moved:
            print(f"  {d:+7.2f} ms  {name[:100]}")
    left = sorted(set(py_json(ctx, DIRTY_PACKAGES) or []) - dirty_before)
    if left:
        print(f"note: {len(left)} package(s) left unsaved by the switch: {', '.join(left[:6])}"
              + ("" if args.restore else " (--restore \"<command>\" puts a state back; ue changes)"))
    ctx.data = {"A": a_ms, "B": b_ms, "delta": b_ms - a_ms, "kinds": dict(deltas), "runs": runs}
    return 0


def gpu_command(ctx, args):
    """`ue gpu` (play.cmd_gpu), or with --ab an A/B comparison of two states; --at/--camera measure from a
    camera of your choosing."""
    if args.restore and not args.ab:
        raise CliError("--restore goes with --ab")
    with camera_at(ctx, args) as pose:
        if pose:
            loc = pose["location"]
            print(f"camera at ({loc['x']:.0f}, {loc['y']:.0f}, {loc['z']:.0f}) for the measurement")
        return gpu_ab(ctx, args) if args.ab else cmd_gpu(ctx, args)


def new_command(ctx, args):
    """`ue new project` makes a project (no editor needed); every other kind is an asset in the running editor."""
    if args.kind == "project":
        return new_project(ctx, args)
    if args.cpp or args.template:
        raise CliError("--cpp and --template are for `ue new project`")
    return cmd_new(ctx, args)


UNREAL_ROOTS = ("Game", "Script", "Engine", "Temp", "Memory")


def undo_msys_paths(argv):
    """Git Bash (MSYS) rewrites arguments that look like POSIX paths when it starts a Windows program, so
    `/Game/Maps/L` arrives as `C:/Program Files/Git/Game/Maps/L` (also after `key=`, and inside Python code
    passed to `ue py`). Undo that for Unreal package roots, bare (`/Game`) or with a path."""
    if os.name != "nt" or not os.environ.get("MSYSTEM"):
        return argv
    import shutil
    bash = shutil.which("bash")
    if not bash:
        return argv
    root = os.path.dirname(os.path.dirname(os.path.dirname(bash))).replace("\\", "/")  # ...\Git\usr\bin\bash
    # Unreal mount points are capitalised (/Game, /Engine, plugin ones like /Niagara); Git's own folders are not.
    rx = re.compile(re.escape(root) + r"(?=/(?:(?i:" + "|".join(UNREAL_ROOTS) + r")(?!\w)|[A-Z]\w*(?:[/.\s'\"]|$)))")
    return [rx.sub("", a) for a in argv]


def cmd_help(ctx, args):
    """`ue help [command]`: argparse's own help for a command, or the list of commands."""
    try:
        build_parser().parse_args([args.topic, "-h"] if args.topic else ["-h"])
    except SystemExit as exc:
        return 2 if exc.code else 0
    return 0


def build_parser():
    # Global options are accepted before or after the subcommand (agents write both).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help=".uproject path (default: search upward from cwd, or UE_PROJECT)")
    common.add_argument("--pid", type=int, default=argparse.SUPPRESS,
                        help="with several editors open on the project: the one to use (default: the one you opened)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="MCP port (default: beacon, UE_MCP_PORT, then 8000)")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds to wait (120)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="one JSON document: {command, ok, exit, error, lines, data}")
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="max rows to print (30)")
    common.add_argument("--time", action="store_true", default=argparse.SUPPRESS, help="elapsed time to stderr")

    p = argparse.ArgumentParser(prog="ue", description=sys.modules[__package__].__doc__.split("\n\n")[0], parents=[common])
    p.add_argument("--version", action="version", version=f"ue (Tether) {tether_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text, parents=[common])
        s.set_defaults(fn=fn)
        return s

    s = add("help", cmd_help, "help on a command: ue help pie (same as ue pie -h)")
    s.add_argument("topic", nargs="?", help="a command name")
    add("status", cmd_status, "editor state from the beacon (no HTTP needed)")
    add("doctor", cmd_doctor, "check the setup (engine, plugins, editor, ports, Live Coding lock, .NET, git/LFS, disk)")
    s = add("open", cmd_open, "launch the editor with the MCP server on a per-project port and wait until ready")
    s.add_argument("--map", help="map to open, e.g. /Game/Maps/Lvl_Test")
    s.add_argument("--headless", action="store_true", help="no window: -nullrhi -nosound (+ -unattended, see below)")
    s.add_argument("--render", action="store_true", help="with --headless: render offscreen instead of -nullrhi (for ue shot)")
    s.add_argument("--sound", action="store_true", help="with --headless: keep a (silent) audio device, for ue audio")
    s.add_argument("--unattended", dest="unattended", action="store_true", default=None,
                   help="force -unattended (default for headless, except UnrealSharp projects)")
    s.add_argument("--attended", dest="unattended", action="store_false", help="never pass -unattended")
    s.add_argument("--no-build", action="store_true", help="skip the C++/C# pre-build")
    s.add_argument("--force", action="store_true", help="with --second: start it even while the shared editor has "
                                                        "unsaved packages or another session's lease")
    s.add_argument("--second", action="store_true", help="a private headless editor next to the open one (own MCP "
                                                         "port; this session's from now on, no lease needed; other "
                                                         "sessions keep the shared one). No pre-build")
    s.add_argument("--wait", type=float, default=600, help="seconds to wait for ready (600)")
    s.add_argument("--no-wait", action="store_true")
    s = add("build", build_command, "compile: Live Coding if the editor is up, UBT if not; --restart for reflection changes")
    s.add_argument("--restart", action="store_true", help="save/close editor, UBT build, reopen same map")
    s.add_argument("--save", action="store_true", help="with --restart: save dirty packages first")
    s.add_argument("--discard", action="store_true", help="with --restart: drop unsaved changes")
    s.add_argument("--check", action="store_true", help="compile the game target only (UBT): the running editor and its "
                                                         "Live Coding are untouched; misses editor-only code")
    s.add_argument("--no-check", action="store_true", help="with --restart: skip the compile check made before closing")
    s.add_argument("--files", nargs="+", metavar="PATH", help="with --check: compile only these files (a header: the "
                                                              ".cpp files including it), no link; needs no lease")
    s.add_argument("--when-closed", action="store_true", help="build with UBT once every editor on the project has "
                                                             "closed (a background job: ue job status), so the code a "
                                                             "launcher start loads has the Live Coding patches")
    s.add_argument("--after-close", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--overlay", metavar="DIR", nargs="?", const="-",
                   help="with --check: compile a copy of the project's code with DIR's files on top "
                        "(DIR/Source/<Module>/...), for work kept out of Source/ while another session's Live Coding "
                        "builds. No DIR: this session's own, Saved/Tether/staging/<session>/")
    s.add_argument("--include-others", action="store_true", help="Live Coding: compile even files another session edited "
                                                                 "in the last 15 min (it refuses otherwise)")
    s.add_argument("--ignore-lease", action="store_true", help="with --restart: go ahead although another session holds "
                                                               "the lease or used the editor in the last few minutes")
    s.add_argument("--timing", action="store_true", help="compile the game target with UBT -Timing and list the "
                                                         "slowest files, modules and headers (the editor is untouched)")
    s.add_argument("--target", help="UBT target (default <Project>Editor)")
    s.add_argument("--config", default="Development")
    s.add_argument("--wait", type=float, default=300, help="seconds to wait for Live Coding (300)")
    s = add("close", cmd_close, "quit the editor; refuses to drop unsaved work unless --save/--discard")
    s.add_argument("--save", action="store_true", help="save dirty packages first")
    s.add_argument("--discard", action="store_true", help="drop unsaved changes (terminates if a dialog blocks)")
    s.add_argument("--force", action="store_true", help="terminate immediately")
    s.add_argument("--ignore-lease", action="store_true", help="close although another session holds the lease or "
                                                               "used the editor in the last few minutes")
    s = add("lease", cmd_lease, "share the editor between sessions: take --for WHAT [--minutes N] | release | request "
                                    "--for WHAT | grant [SESSION] | (show)")
    s.add_argument("action", nargs="?", choices=["take", "release", "request", "grant"])
    s.add_argument("to", nargs="?", help="with grant: the session to hand the lease to (default: the oldest request)")
    s.add_argument("--for", "--reason", dest="what", help="with take/request: what the editor is for, as the other sessions will see it")
    s.add_argument("--minutes", type=float, default=LEASE_MINUTES, help=f"with take: how long ({LEASE_MINUTES}); "
                                                                         "take again to renew")
    s.add_argument("--as", dest="name", help="a name for this session that the others see (kept)")
    s.add_argument("--force", action="store_true", help="with release: release another session's lease")
    s = add("wait", cmd_wait, "wait N seconds, N seconds of game time in PIE (ue wait 4 game), or until shaders and "
                              "assets have compiled (ue wait compile)")
    s.add_argument("what", help="seconds, compile, grass (until landscape grass for the view has been built), or idle "
                                "(until the editor ticks again: a timed-out `ue py` script finished), or lease (until the lease is free or "
                                "granted to this session and PIE has stopped)")
    s.add_argument("clock", nargs="?", choices=["game", "real"], default="real", help="game: PIE game time")
    s.add_argument("--max", type=float, default=900, help="with compile, grass, idle or lease: give up "
                                                                         "after this many seconds (900)")
    s = add("who", cmd_who, "sessions that used this editor recently, their last command, the lease, who started PIE")
    s.add_argument("--minutes", type=float, default=15, help="how far back (15)")
    s.add_argument("--as", dest="name", help="a name for this session that the others see (kept)")
    s = add("git", cmd_git, "stage --mine: stage the changed files this session wrote last (the edit hook's record), "
                            "naming those another session also wrote")
    s.add_argument("action", choices=["stage"])
    s.add_argument("--mine", action="store_true", help="the files this session wrote (Edit/Write) since nobody else did")
    s.add_argument("--dry-run", action="store_true", help="list, stage nothing")
    s = add("tools",cmd_tools, "list toolsets, a toolset's tools, or one tool's schema (toolset:tool)")
    s.add_argument("query", nargs="?")
    s.add_argument("-v", "--verbose", action="store_true")
    s = add("call", cmd_call, "invoke <toolset>.<tool> k=v ... (required blanks auto-filled)")
    s.add_argument("target")
    s.add_argument("kv", nargs="*")
    s.add_argument("--args", help="raw JSON arguments instead of k=v")
    s = add("py", cmd_py, "run Python in the live editor (code, -f file, or - for stdin)")
    s.add_argument("code", nargs="*", help="Python code; with -f, the script's arguments (its sys.argv[1:])")
    s.add_argument("-f", "--file")
    s = add("props", cmd_props, "reflected properties of an object/actor/component")
    s.add_argument("target")
    s.add_argument("match", nargs="?")
    s.add_argument("--pie", action="store_true", help="read the PIE world's actor instead")
    s.add_argument("--export", metavar="FILE", help="write every property value to FILE as JSON (e.g. a DataAsset)")
    s = add("set", cmd_set, "set properties (name=value, name+=delta) as one undoable transaction; --assets: many assets")
    s.add_argument("target", nargs="?")
    s.add_argument("assignments", nargs="*", metavar="name=value")
    s.add_argument("--pie", action="store_true", help="change the PIE world (not undoable, not saved)")
    s.add_argument("--from", dest="from_file", metavar="FILE", help="apply the values in FILE (JSON) that differ")
    s.add_argument("--dry-run", action="store_true", help="with --from or --assets: show what would change")
    s.add_argument("--assets", metavar="FOLDER", help="set on every asset under a folder ('/Game/Props', '/Game/T/*_N')")
    s.add_argument("--class", dest="cls", help="with --assets: regex on the asset class, e.g. 'Texture2D$'")
    s.add_argument("--match", help="with --assets: name pattern, e.g. '*_ORM'")
    s.add_argument("--save", action="store_true", help="with --assets: save each changed asset")
    s = add("undo", cmd_undo, "undo Tether's last `ue set` (only if nothing else changed since); --editor: any edit")
    s.add_argument("count", nargs="?", type=int, default=1, help="with --editor: how many steps (1)")
    s.add_argument("--editor", action="store_true", help="undo the editor's own history: any transacted edit")
    s = add("redo", cmd_undo, "redo steps undone with `ue undo --editor`")
    s.add_argument("count", nargs="?", type=int, default=1, help="how many steps (1)")
    s = add("spawn", cmd_spawn, "place an actor from a class or asset (undoable)")
    s.add_argument("what", help="class (PointLight, /Script/Module.Class) or asset (/Game/Meshes/SM_Rock)")
    s.add_argument("--at", default="0,0,0", help="location x,y,z")
    s.add_argument("--rot", help="rotation pitch,yaw,roll")
    s.add_argument("--label", help="actor label")
    s.add_argument("--ground", action="store_true", help="drop it onto the surface below --at")
    s.add_argument("--align", action="store_true", help="with --ground: tilt it to the surface")
    s.add_argument("--pie", action="store_true", help="spawn into the running PIE world instead (gone when PIE stops)")
    s.add_argument("--replace", action="store_true", help="with --pie --label: remove the PIE actors with that label "
                                                          "first, so repeated test runs do not stack copies")
    s = add("scatter", cmd_scatter, "place many copies on the ground in an area or along a line (one undo step)")
    s.add_argument("what", help="asset (/Game/...) or class")
    s.add_argument("--count", type=int, default=10)
    s.add_argument("--area", help="x,y,radius")
    s.add_argument("--line", nargs=2, metavar=("X,Y,Z", "X,Y,Z"), help="place evenly along this segment")
    s.add_argument("--yaw", default="0,360", help="random yaw range min,max (0,360)")
    s.add_argument("--scale", default="1,1", help="random uniform scale range min,max (1,1)")
    s.add_argument("--spacing", type=float, default=0, help="minimum distance between copies, cm")
    s.add_argument("--seed", type=int, default=0, help="same seed, same layout")
    s.add_argument("--align", action="store_true", help="tilt each to the surface")
    s.add_argument("--label", help="label prefix (default: the asset name)")
    s = add("landscape", cmd_landscape, "landscape: info, at x,y, map [x,y,size], flatten|raise|smooth x,y,r, restore, "
                                        "export|import <heightmap>, paint <layer> --mask <picture>")
    s.add_argument("action", nargs="?", default="info", choices=["info", "at", "map", "flatten", "raise", "smooth", "restore",
                                                                 "export", "import", "paint"])
    s.add_argument("where", nargs="?", help="at: x,y; map: x,y,size (default: all); flatten/raise/smooth: x,y,radius; "
                                            "export/import: a 16-bit PNG or .r16 file; paint: the layer name")
    s.add_argument("--mask", help="paint: a greyscale picture (PNG, .r16, .u8), white = full weight, stretched over "
                                  "the landscape")
    s.add_argument("--flip-y", action="store_true", help="export/import/paint: the file's first row is +Y (north-up)")
    s.add_argument("--height", type=float, help="flatten: target z (default: the height at the centre)")
    s.add_argument("--by", type=float, help="raise: cm (negative lowers)")
    s.add_argument("--strength", type=float, help="0..1 toward the result (smooth: number of passes, 2)")
    s.add_argument("--px", type=int, default=128, help="map: pixels per side (128, max 256)")
    s.add_argument("-o", "--output", help="map: PNG path (default Saved/Tether/shots/)")
    s.add_argument("--backup", help="restore: the backup file an edit printed")
    add("water", cmd_water, "water zones and bodies, with checks: a body editing the landscape, a coarse shoreline, "
                            "a stock material left on the distant water mesh")
    s = add("wp", cmd_wp, "World Partition: info, actors [regex|x,y,r] (unloaded too), load|unload <x,y,r|regex>")
    s.add_argument("action", nargs="?", default="info", choices=["info", "actors", "load", "unload"])
    s.add_argument("where", nargs="?", help="a name/class regex, or an area x,y,radius")
    s = gpu_parser = add("gpu", gpu_command, "GPU time per pass (ProfileGPU): by kind (Lumen, shadows, base pass, post...) and the top passes")
    s.add_argument("--res", metavar="WxH", help="render the 3D scene with this resolution's pixel count (screen percentage "
                                               "on the small offscreen viewport), e.g. 2560x1440")
    s.add_argument("--frames", type=int, default=3, help="frames to profile and average (3)")
    s.add_argument("--pie", action="store_true", help="require PIE (default: PIE when it runs, else the editor)")
    s.add_argument("--top", type=int, default=12, help="how many passes to list (12)")
    s.add_argument("--min", type=float, default=0.05, help="leave out passes under this many ms (0.05)")
    s.add_argument("--tree", type=int, nargs="?", const=6, metavar="DEPTH", help="also print the last frame's tree")
    s.add_argument("--ab", nargs=2, metavar=("A", "B"), help="compare two states: two ue command lines run in turn "
                   "(A, B, A, B), the GPU frame measured after each at the same camera, e.g. --ab "
                   "\"mat MI_X UseWindows=false\" \"mat MI_X UseWindows=true\"")
    s.add_argument("--restore", metavar="CMD", help="with --ab: a ue command line run at the end (the state to leave)")
    s.add_argument("--rounds", type=int, default=2, help="with --ab: A/B rounds (2)")
    s.add_argument("--compile-max", type=float, default=300, help="with --ab: longest wait for shader compiles per "
                                                                 "switch, in seconds (300)")
    s = add("tex", cmd_tex, "textures: size, format, memory and issues under a folder (or one), or --streaming: the live pool")
    s.add_argument("path", nargs="?", help="a folder (default /Game) or one texture")
    s.add_argument("--streaming", action="store_true", help="what is in memory now: pool use, largest, ones wanting more mips")
    s.add_argument("--top", type=int, default=10, help="how many of the largest to list (10)")
    s.add_argument("--max", type=int, help="flag textures over this many pixels a side (lint.json max_texture_size, 4096)")
    s.add_argument("--strict", action="store_true", help="exit 1 when any issue is found")
    s = add("collide", cmd_collide, "what blocks at one height around a point, as a picture coloured by class (overlap "
                                    "boxes on a grid: procedural roads and instanced buildings included)")
    s.add_argument("where", metavar="x,y,radius", help="centre and radius, cm (--m: metres)")
    s.add_argument("--z", type=float, help="height of the boxes' centres (100)")
    s.add_argument("--step", type=float, help="cell size, cm (default: 160 cells across, at least 25 cm)")
    s.add_argument("--half", type=float, default=40, help="box half-height, cm (40)")
    s.add_argument("--m", action="store_true", help="x,y,radius in metres")
    s.add_argument("--pie", action="store_true", help="the PIE world")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/collide-<time>.png)")
    s = add("map", cmd_map, "the level from above as a picture: mesh footprints, landscape height, gameplay actors by "
                            "class; `map open <path>` loads another map into the editor")
    s.add_argument("area", nargs="?", help="x,y,radius (default: the whole level), or open")
    s.add_argument("path", nargs="?", help="with open: the map to load")
    s.add_argument("--save", action="store_true", help="with open: save the current map's unsaved edits first")
    s.add_argument("--discard", action="store_true", help="with open: drop the current map's unsaved edits")
    s.add_argument("--footprints", action="store_true", help="the collidable geometry as data (outline, top, bottom per "
                                                             "mesh) in a JSON file, instead of a picture")
    s.add_argument("--match", help="only markers whose label or class matches this regex")
    s.add_argument("--pie", action="store_true", help="the PIE world (spawned actors where they are now)")
    s.add_argument("--px", type=int, default=800, help="picture size (800)")
    s.add_argument("--numbers", type=int, default=60, help="number the markers when there are at most this many (60)")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/map-<time>.png)")
    add("lights", cmd_lights, "lighting overview: lights, sky, fog, exposure, and what commonly goes wrong")
    s = add("stats", cmd_stats, "which ue commands this project ran, failed and took long (kept on this machine only)")
    s.add_argument("--days", type=float, help="only the last N days")
    s = add("move-pack", cmd_move_pack, "move a content folder (a Fab or marketplace pack) with its references fixed "
                                        "up, e.g. into /Game/ThirdParty/<name>")
    s.add_argument("folder", help="/Game/... folder to move")
    s.add_argument("dest", nargs="?", help="/Game/... folder it becomes (default /Game/ThirdParty/<its name>)")
    s.add_argument("--dry-run", action="store_true", help="show what would move and what references it")
    s = add("post", cmd_post, "post-process volumes in effect at a point (default: the camera) and their merged settings")
    s.add_argument("at", nargs="?", help="x,y,z or an actor; default the player's camera in PIE, else the viewport's")
    s = add("cvar", cmd_cvar, "console variables: value, who set it, default; set, --reset, or set only --for a command")
    s.add_argument("items", nargs="*", metavar="regex | Name=value", help="a name regex/prefix to list, or Name=value to set")
    s.add_argument("--changed", action="store_true", help="only variables that differ from their built-in default")
    s.add_argument("--reset", nargs="+", metavar="NAME", help="undo console-set values (back to ini/scalability/default)")
    s.add_argument("--for", dest="run", metavar="'UE COMMAND'", help="with Name=value: set only while this ue command runs")
    s = add("trace", cmd_trace, "what a trace hits (PIE by default): from the player's view, or between points/actors")
    s.add_argument("--from", dest="start", default="player", help="player (PIE camera), camera (editor view), an actor, or x,y,z")
    s.add_argument("--to", default="look", help="look (along the view), an actor, or x,y,z")
    s.add_argument("--distance", type=float, default=5000, help="with --to look: how far, cm (5000)")
    s.add_argument("--channel", default="Visibility", help="trace channel: Visibility, Camera, or the project's (e.g. Interact)")
    s.add_argument("--sphere", type=float, default=0, help="sweep a sphere of this radius")
    s.add_argument("--simple", action="store_true", help="trace simple collision only (default: complex)")
    s.add_argument("--editor", action="store_true", help="trace the edited level instead of PIE")
    s = add("fx", cmd_fx, "Niagara: a system as text (emitters, modules, user parameters), set user parameters on a "
                          "placed component, or list the level's/PIE's Niagara components")
    s.add_argument("target", nargs="?", help="a system /Game/... path, an actor (or regex to list), or nothing for all")
    s.add_argument("assignments", nargs="*", metavar="Name=value", help="with an actor: user parameters to set")
    s.add_argument("--pie", action="store_true", help="the PIE world's components")
    s = add("pcg", cmd_pcg, "PCG: a graph as text (ue pcg /Game/PCG_X), the level's PCG components, generate|cleanup <actor>")
    s.add_argument("action", nargs="?", help="a graph path, list (default), status, generate or cleanup")
    s.add_argument("target", nargs="?", help="with generate/cleanup/status: the actor; with list: a name regex")
    s.add_argument("--seed", type=int, help="with generate: use this seed (undoable)")
    s.add_argument("--wait", type=float, default=60, help="with generate: seconds to wait for it to finish (60)")
    s = add("net", cmd_net, "replication: what a class replicates (properties, RPCs), an actor's role/owner/relevancy per "
                            "PIE world; `net compare Actor.Prop`: server vs clients")
    s.add_argument("target", help="an actor (or player), a class, a Blueprint path, or 'compare'")
    s.add_argument("prop", nargs="?", help="with compare: Actor.Property (or player.Health, Actor.location)")
    s.add_argument("--pie", action="store_true", help="look the actor up in PIE (the world ue pie target picked)")
    s.add_argument("--all", action="store_true", help="every parent class in full, not just counts")
    s = add("skel", cmd_skel, "a skeleton or skeletal mesh: bone tree with sockets, virtual bones, LODs, materials, physics, rigs")
    s.add_argument("target", help="a SkeletalMesh or Skeleton /Game/... path")
    s = add("tree", cmd_tree, "an actor's or Blueprint's components as a tree: sockets, transforms, mobility, meshes, flags")
    s.add_argument("target", help="actor label, 'player' (PIE pawn), or a Blueprint /Game/... path")
    s.add_argument("--pie", action="store_true", help="the PIE world's actor")
    s = add("collision", cmd_collision, "an actor's collision: profiles, object types, responses per channel")
    s.add_argument("actor")
    s.add_argument("--pie", action="store_true", help="the PIE world's actor")
    s = add("level", cmd_level, "level check: floating/buried props, stacked duplicates, invisible blockers, lights")
    s.add_argument("action", choices=["check"])
    s.add_argument("--gap", type=float, default=5, help="cm above the surface that counts as floating (5)")
    s.add_argument("--strict", action="store_true", help="exit 1 when anything is reported")
    s = add("foliage", cmd_foliage, "foliage instances: list, add over an area or a mask, remove; check (culling, LODs, "
                                    "billboards, wind, shadows), convert actors to instances, fix billboards|cull|wpo")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "check", "convert", "fix"])
    s.add_argument("what", nargs="?", help="add: a FoliageType asset or a static mesh; remove: one (default all); "
                                           "check: a mesh-name regex; convert: a regex over actor labels, tags or "
                                           "mesh names; fix: billboards, cull or wpo")
    s.add_argument("--area", help="x,y,radius (required for add, unless --mask, and for remove)")
    s.add_argument("--mask", help="add: a greyscale PNG/.r16/.u8 density mask (white = full density) over the "
                                  "landscape (or --mask-area); rows run along +Y, columns along +X, as a heightmap")
    s.add_argument("--mask-area", help="add: x0,y0,x1,y1 the mask covers (default: the landscape's bounds)")
    s.add_argument("--flip-y", action="store_true", help="add: the mask's first row is +Y (north-up pictures)")
    s.add_argument("--avoid", action="append", metavar="REGEX:RADIUS",
                   help="add: no instances within RADIUS cm of actors whose label, class or tag matches (repeatable)")
    s.add_argument("--distance", type=float, help="fix cull|wpo: the distance in cm")
    s.add_argument("--scope", help="fix: a mesh-name regex, or a content folder (billboards) to fix every mesh in")
    s.add_argument("--density", type=float, help="add: instances per 10 x 10 m (default: the foliage type's)")
    s.add_argument("--count", type=int, help="add: exactly this many instead")
    s.add_argument("--spacing", type=float, help="add: minimum distance, cm (default: the type's radius)")
    s.add_argument("--slope", help="add: allowed ground slope min,max degrees (default: the type's)")
    s.add_argument("--height", help="add: allowed world z min,max (default: the type's)")
    s.add_argument("--scale", help="add: uniform scale min,max (default: the type's)")
    s.add_argument("--align", action="store_true", help="add: tilt to the ground")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--folder", default="/Game/Foliage", help="add with a mesh: where its new foliage type goes")
    s = add("drop", cmd_drop, "put actors on the ground below them (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
    s.add_argument("--align", action="store_true", help="also tilt each to the surface normal")
    s.add_argument("--group", action="store_true", help="move them, and what is attached to them, as one unit "
                                                        "(a structure of separate pieces keeps its shape on a slope)")
    s.add_argument("--rest", choices=["lowest", "highest", "mean"], default="lowest",
                   help="with --group: rest on the lowest (default: nothing floats), highest or mean ground under it")
    s = add("delete", cmd_delete, "delete actors from the level (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
    s.add_argument("--pie", action="store_true", help="from the running PIE world instead (they come back with PIE)")
    s = add("duplicate", cmd_duplicate, "duplicate an actor (undoable)")
    s.add_argument("actor")
    s.add_argument("--offset", default="100,0,0", help="shift per copy x,y,z (100,0,0)")
    s.add_argument("--count", type=int, default=1)
    s = add("mv", cmd_mv, "move/rename an asset and fix up references (not undoable)")
    s.add_argument("asset")
    s.add_argument("dest", help="new path, or a folder ending in /")
    s = add("rm", cmd_rm, "delete an asset; refuses while referenced (not undoable)")
    s.add_argument("asset")
    s.add_argument("--force", action="store_true", help="delete even if referenced")
    s = add("crash", cmd_crash, "why the editor died: the last crash's error, module chain and last log lines")
    s.add_argument("index", nargs="?", type=int, default=0, help="0 = newest (default), 1 = the one before, ...")
    s.add_argument("--list", action="store_true", help="list recent crashes instead")
    add("changes", cmd_changes, "unsaved work: dirty packages and the level's actors added/removed/moved since load")
    s = add("save", cmd_save, "save unsaved packages (all, or the given ones); untitled /Temp maps are skipped")
    s.add_argument("paths", nargs="*", help="package paths (default: everything unsaved)")
    s = add("watch", cmd_watch, "wait for a log line, or for a property value (exit 4 on timeout)")
    s.add_argument("pattern", nargs="?", help="log regex to wait for")
    s.add_argument("--prop", nargs=2, metavar=("TARGET", "NAME"), help="watch this property instead")
    s.add_argument("--until", help="with --prop: regex the value must match")
    s.add_argument("--seconds", type=float, default=60, help="give up after this long (60)")
    s.add_argument("--pie", action="store_true", help="with --prop: read the PIE world")
    s = add("shot", cmd_shot, "capture the viewport (or the PIE view) to a PNG; prints the path")
    s.add_argument("--res", metavar="WxH", help="render at a real resolution (HighResShot), e.g. 2560x1440; not with "
                                               "--hud/--annotate/--ui")
    s.add_argument("--actor", help="frame this actor, keeping the current view direction")
    s.add_argument("--at", help="camera location x,y,z")
    s.add_argument("--look-at", help="aim at x,y,z or an actor")
    s.add_argument("--rot", help="camera rotation pitch,yaw,roll")
    s.add_argument("--annotate", action="store_true", help="overlay Epic's world grid and actor labels")
    s.add_argument("--grid-z", type=float, default=0, help="with --annotate: grid height (0)")
    s.add_argument("--ui", action="store_true", help="include gizmos and selection outlines")
    s.add_argument("--hud", action="store_true", help="in PIE: the player's view with the game's UMG HUD on it")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/shot-<time>.png)")
    s.add_argument("--baseline", metavar="NAME", help="also save this shot and its camera as a named baseline")
    s.add_argument("--compare", metavar="NAME", help="re-shoot a baseline's camera and report what changed (exit 1)")
    s.add_argument("--tolerance", type=float, default=0.5, help="with --compare: changed pixels allowed, %% (0.5)")
    s.add_argument("--threshold", type=int, default=32, help="with --compare: per-channel difference that counts (32)")
    s.add_argument("--frames", type=int, metavar="N", help="N captures --interval apart in one contact sheet; in PIE the "
                                                           "game slows while capturing, so short intervals land")
    s.add_argument("--interval", type=float, default=0.25, help="with --frames: seconds between captures, game time in "
                                                                "PIE (0.25)")
    s.add_argument("--after", metavar="CMD", help="with --frames: a console command run just before the first capture")
    s.add_argument("--clear", action="store_true", help="with --look-at or --actor: if something blocks the view (the "
                                                       "camera is inside a building), move it in front of that")
    s.add_argument("--view", choices=sorted(VIEW_MODES), metavar="MODE", help="a debug view for the shot, put back "
                   "afterwards: " + ", ".join(sorted(VIEW_MODES)) + " (waits for the view's shaders)")
    s.add_argument("--burst", type=int, metavar="N", help="N full-size PNGs --every S apart into one folder (a fixed "
                                                         "spectator camera in PIE is placed once for all of them)")
    s.add_argument("--every", type=float, default=0.5, help="with --burst: seconds between shots, game time in PIE (0.5)")
    s.add_argument("--window", metavar="TITLE", help="an editor window or dock tab (Outliner, Details, a tool tab) "
                                                    "instead of a viewport; a tab is brought to the front first")
    s = add("look", cmd_look, "render an asset (mesh, Blueprint, material) from six angles into one contact sheet")
    s.add_argument("asset", help="/Game/... asset path")
    s.add_argument("--studio", action="store_true", help="neutral studio light (HDRI sky light, key light, auto "
                                                        "exposure) instead of the level's, e.g. in a night level")
    s.add_argument("--tile", type=int, default=400, help="width of each view in pixels (400)")
    s.add_argument("--distance", type=float, default=2.6, help="camera distance in bounds radii (2.6)")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/look-<asset>-<time>.png)")
    s = add("test", cmd_test, "run automation tests in the live editor; prints failures only (exit 1 on failure)")
    s.add_argument("filter", nargs="?", help="Epic filter: substring, ^Prefix, Suffix$, StartsWith:X, Group:Name; '+' joins")
    s.add_argument("--list", action="store_true", help="list matching tests instead of running them")
    s.add_argument("-v", "--verbose", action="store_true", help="also print warnings")
    s = add("actors", cmd_actors, "actors in the editor level")
    s.add_argument("match", nargs="?")
    s.add_argument("--pie", action="store_true", help="list the PIE world's actors instead")
    s = add("exec", exec_command, "console command; prints the log lines it produced")
    s.add_argument("command", nargs="+")
    s.add_argument("--pie", action="store_true", help="run in the PIE world")
    s.add_argument("--shot", nargs="?", const="", metavar="PNG", help="take a shot right after the command (a short "
                                                                     "effect is caught before it fades)")
    s.add_argument("--hud", action="store_true", help="with --shot in PIE: with the game's UMG HUD")
    s = add("log", cmd_log, "new log entries since the last call; --summary: the session grouped; --ignore: known noise")
    s.add_argument("--since", default="last",
                   help="last: new since the previous ue log (default); start: this editor session; pie: the last PIE "
                        "session; or a duration: 90s, 2m, 1h")
    s.add_argument("--summary", action="store_true", help="the whole session's warnings/errors grouped by message shape")
    s.add_argument("--tail", type=int, metavar="N", help="the session's last N entries (--since start --limit N)")
    s.add_argument("--ignore", metavar="REGEX", help="add known noise to Config/Tether/log-ignore.json (hidden from log, pie, ci)")
    s.add_argument("--why", help="with --ignore: a note kept next to the pattern")
    s.add_argument("--all", action="store_true", help="show known noise too")
    s.add_argument("--level", help="Error, Warning (default), Display, Log, Verbose, VeryVerbose; with --grep: every level")
    s.add_argument("--cat", help="only this log category")
    s.add_argument("--grep", help="regex filter")
    s = add("pie", pie_command, "Play-In-Editor: start | stop | status | run <script> | do <steps...> | watch <props> | record "
                                "| loop <commands>")
    s.add_argument("action", choices=["start", "stop", "status", "run", "do", "target", "watch", "record", "loop"])
    s.add_argument("-c", "--code", help="with run: inline Python instead of a script file")
    s.add_argument("script", nargs="*", help="with run: Python file to execute in the PIE world, in a fresh namespace "
                                             "(tether.world() / tether.player(): the targeted PIE world and its pawn); "
                                             "with do: steps; "
                                             "with loop: \"cmd; wait N; cmd\", or status/stop; "
                                             "with watch: Actor.Property ... (an actor alone: its location)")
    s.add_argument("-f", "--file", help="with do: a file of steps, one per line (# comments; - reads them from stdin); "
                                        "with watch: run it while sampling")
    s.add_argument("--keep-going", action="store_true", help="with do: run every step even after a failure")
    s.add_argument("--film", type=float, metavar="SECONDS", help="with do: capture the view every N seconds into a "
                                                                  "timestamped contact sheet")
    s.add_argument("--hud", action="store_true", help="with do: shot steps include the game's UMG HUD (film frames always do)")
    s.add_argument("--film-output", metavar="PNG", help="with --film: where the contact sheet goes")
    s.add_argument("--clients", type=int, help="with start: multiplayer PIE with this many clients")
    s.add_argument("--game", metavar="CLASS", help="with start: play with this game mode (/Script/Module.Class or "
                                                  "/Game/BP_X.BP_X_C) instead of the project default; put back on stop")
    s.add_argument("--net", choices=["listen", "client", "standalone"],
                   help="with --clients: listen server (default), client (dedicated server), or standalone")
    s.add_argument("--expect", action="append", help="with run: log regex that must appear (repeatable)")
    s.add_argument("--reject", action="append", help="with run/do: log regex that must not appear (repeatable)")
    s.add_argument("--seconds", type=float, help="with run: how long to wait for expectations (5); with watch: how long "
                                                 "to sample (10, or until the -f steps finish); with record: stop "
                                                 "after this long (default: when PIE stops)")
    s.add_argument("--every", nargs="+", metavar="N", help="with watch: seconds between samples (0.1); with loop: "
                                                          "--every N \"cmd; cmd\" runs those every N seconds")
    s.add_argument("--watch", nargs="+", metavar="ACTOR.PROP", help="with do: watch these while the steps run "
                                                                   "(or a `# watch:` line in the step file)")
    s.add_argument("--baseline", metavar="NAME", help="with watch/do: record the watched values as a baseline "
                                                      "(tests/play/baselines/NAME.json)")
    s.add_argument("--compare", metavar="NAME", help="with watch/do: compare with that baseline; exit 1 on differences")
    s.add_argument("-o", "--output", help="with watch: CSV path (default Saved/Tether/watch/); with record: step file "
                                               "(default Saved/Tether/recordings/)")
    s.add_argument("--keep", action="store_true", help="with run/do: leave PIE running afterwards")
    s.add_argument("--wait", type=float, nargs="?", const=120, metavar="SECONDS", help="with run: the script goes on "
                   "over ticks (a tick callback); wait for it to call tether.finish(value) and print that (120)")
    s.add_argument("--wait-file", metavar="PATH", help="with run: wait until the script has written this file (within "
                                                       "--seconds, default 60) and print it")
    s.add_argument("--ignore-lease", action="store_true", help="with start/run/do: start PIE although another session "
                                                               "holds the editor lease")
    s.add_argument("--simulate", action="store_true", help="Simulate-In-Editor (no player pawn)")
    s.add_argument("--warmup", type=float, default=0.5, help="seconds to let BeginPlay settle (0.5)")
    s = add("bp", bp_command, "Blueprint graphs as text: read, --diff, --apply; check; edit; trace (what ran in PIE)")
    s.add_argument("blueprint", nargs="?", help="/Game/Path/BP_Name, `check [/Game/path]` to compile all and list the broken, "
                                                "`edit <BP> ...`, or `trace` (Blueprint calls during PIE)")
    s.add_argument("graph", nargs="?", help="graph name (default: all); with edit: the Blueprint")
    s.add_argument("rest", nargs="*", help="with edit: add-var Name Type [default] | set-default Name=v ... | "
                                           "add-component Name Class [prop=v ...] | add-function Name | add-dispatcher Name | remove-var Name")
    s.add_argument("--category", help="with edit add-var: the variable's category")
    s.add_argument("--instance-editable", action="store_true", help="with edit add-var: editable on placed instances")
    s.add_argument("--array", action="store_true", help="with edit add-var: an array of the type")
    s.add_argument("--parent", help="with edit add-component: attach under this component (default: the root)")
    s.add_argument("--diff", action="store_true", help="show only what changed since the last ue bp read")
    s.add_argument("--apply", metavar="FILE", help="write this DSL into the graph (compiles), then show the diff")
    s.add_argument("--save", action="store_true", help="save the Blueprint afterwards")
    s.add_argument("--docs", action="store_true", help="print Epic's graph DSL syntax reference")
    s.add_argument("--warnings", action="store_true", help="with check: also list Blueprints with warnings")
    s.add_argument("--seconds", type=float, help="with trace: how long to record (5, or 0 after --do)")
    s.add_argument("--do", metavar="FILE", help="with trace: play this `ue pie do` step file while recording")
    s.add_argument("--match", help="with trace: regex on Blueprint.Function")
    s.add_argument("--keep", action="store_true", help="with trace: leave PIE running afterwards")
    s = add("table", cmd_table, "DataTable round-trips: show, --export JSON/CSV, --import with a row diff")
    s.add_argument("table", help="/Game/Path/DT_Name")
    s.add_argument("--export", metavar="FILE", help="write all rows to FILE (.json or .csv)")
    s.add_argument("--import", dest="import_file", metavar="FILE", help="upsert rows from FILE (.json or .csv)")
    s.add_argument("--prune", action="store_true", help="with --import: remove rows missing from FILE")
    s.add_argument("--dry-run", action="store_true", help="with --import: show the diff only")
    s.add_argument("--save", action="store_true", help="save the DataTable afterwards")
    s = perf_parser = add("perf", perf_command, "frame-time capture in PIE: frame/game/render/GPU ms, hitches, change vs last "
                                   "capture; `perf load [map]`: PIE start, streaming, hitches and the loads in them")
    s.add_argument("action", nargs="?", choices=["load", "startup"], help="load: map load and streaming hitches; "
                   "startup: where the editor's startup went (or a packaged game's, --log), from its log")
    s.add_argument("--log", metavar="PATH", help="with startup: the log to read (default: the editor's)")
    s.add_argument("map", nargs="?", help="with load: the map to open first (default: the open one)")
    s.add_argument("--seconds", type=float, help="capture length (10); with load: how long to play (10, or 0 after -f)")
    s.add_argument("-f", "--file", help="with load: a `ue pie do` route to play while measuring")
    s.add_argument("--hitch", type=float, default=50, help="with load: frames longer than this many ms are hitches (50)")
    s.add_argument("--res", metavar="WxH", help="render the 3D scene with this resolution's pixel count (screen percentage "
                                               "on the PIE viewport), e.g. 2560x1440")
    s.add_argument("--editor", action="store_true", help="measure the editor as is, without starting PIE")
    s.add_argument("--no-record", action="store_true", help="do not make this capture the comparison baseline")
    s.add_argument("--trace", action="store_true", help="also an Unreal Insights trace: top CPU scopes per thread")
    s.add_argument("--top", type=int, default=6, help="with --trace: scopes per thread (6)")
    s.add_argument("--scalability", nargs="?", const="low,medium,high,epic", metavar="LEVELS",
                   help="one capture per scalability level (low,medium,high,epic[,cinematic]; 5 s each), then restore")
    s.add_argument("--shots", action="store_true", help="with --scalability: a screenshot at each level")
    add("budget", cmd_budget, "measure what Config/Tether/budget.json limits (frame time, memory, sizes, load) and compare")
    for s in (gpu_parser, perf_parser):
        s.add_argument("--at", metavar="X,Y,Z", help="measure from a camera here (the player's view moves there in PIE, "
                                                     "the level viewport's camera otherwise; put back afterwards)")
        s.add_argument("--look-at", metavar="X,Y,Z|ACTOR", help="with --at: aim the camera at this")
        s.add_argument("--rot", metavar="P,Y,R", help="with --at: the camera's rotation")
        s.add_argument("--actor", help="measure from a camera framing this actor")
        s.add_argument("--camera", metavar="NAME", help="measure from a `ue shot --baseline NAME` camera")
    s = add("mesh", cmd_mesh, "mesh check <mesh|folder>...: size and units, pivot, UVs/lightmap UV, inward faces, "
                              "collision, sockets, empty material slots")
    s.add_argument("action", choices=["check"])
    s.add_argument("paths", nargs="+", help="StaticMesh/SkeletalMesh paths, or folders")
    s.add_argument("--height", type=float, help="expected height in cm (e.g. 200): catches unit and up-axis mistakes")
    s.add_argument("--strict", action="store_true", help="exit 1 when any mesh has issues")
    s = add("data", cmd_data, "balancing tables: one property per line across data assets or Blueprint class defaults")
    s.add_argument("query", help="a class (its data assets, or the Blueprints deriving from it), a folder, or a /Game/...glob")
    s.add_argument("--columns", metavar="REGEX", help="only these properties")
    s.add_argument("--all", action="store_true", help="also properties that are the same everywhere")
    s.add_argument("--csv", metavar="FILE", help="write the table to a CSV (differing columns first)")
    s.add_argument("--import", dest="import_csv", metavar="FILE", help="apply a CSV's changed cells")
    s.add_argument("--dry-run", action="store_true", help="with --import: show what would change")
    s.add_argument("--save", action="store_true", help="with --import: save the changed assets")
    s = add("sound", cmd_sound, "a sound asset (duration, channels, attenuation, class, ...), or every wave a data asset, "
                                "Blueprint or folder uses; --stats: loudness")
    s.add_argument("assets", nargs="+", help="a sound, or data assets / Blueprints / folders whose waves to list")
    s.add_argument("--stats", action="store_true", help="loudness per wave: peak, RMS, loudest 50 ms, attack, brightness")
    s.add_argument("--sort", choices=["loud", "quiet", "length", "name"], default="loud", help="with --stats (loud)")
    s.add_argument("--all", action="store_true", help="every wave (default: the first --top)")
    s.add_argument("--top", type=int, default=40, help="rows (40)")
    s = add("asset", cmd_asset, "a few lines about assets, by type (mesh, texture, material, Blueprint, ...)")
    s.add_argument("paths", nargs="+", help="/Game/... asset paths")
    s = add("diff", cmd_diff, "what changed in an asset between git revisions, or a revision and the editor now")
    s.add_argument("asset", nargs="?", help="/Game/... asset, or its file under Content/")
    s.add_argument("rev", nargs="?", help="git revision to compare with (default HEAD)")
    s.add_argument("--to", metavar="REV", help="compare with this revision instead of the editor's current state")
    s.add_argument("--files", nargs=2, metavar=("OLD", "NEW"), help="compare two files (for git difftool)")
    s.add_argument("--as", dest="as_path", metavar="PATH", help="with --files: the asset's path in the repo ($MERGED)")
    s = add("validate", cmd_validate, "data validation + Map Check; default: assets changed in git or unsaved")
    s.add_argument("paths", nargs="*", help="assets or content folders (default: changed and unsaved assets)")
    s.add_argument("--changed", nargs="?", const="", metavar="REV",
                   help="also the assets changed in git (uncommitted, plus committed since REV if given)")
    s.add_argument("--map-check", action="store_true", help="also Map Check the open level (default without paths)")
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s = add("mat", cmd_mat, "material parameters with values and sources; Name=value sets them (undoable); "
                            "`mat graph <material> [op ...]` reads or edits the node graph")
    s.add_argument("target", help="material or instance path, or an actor (lists its materials)")
    s.add_argument("assignments", nargs="*", metavar="Name=value", help="scalar, r,g,b[,a], true/false, texture path")
    s.add_argument("--reset", action="append", metavar="NAME", help="stop overriding NAME (instances; repeatable)")
    s.add_argument("--compile", action="store_true", help="recompile the base material and list compile errors")
    s = add("nav", cmd_nav, "navmesh: status (and why it is missing), path <a> <b> [<a> <b> ...], check [actor regex]")
    s.add_argument("action", nargs="?", choices=["status", "path", "check"])
    s.add_argument("points", nargs="*", help="path: start end [start end ...] (x,y,z or actor); check: actor regex")
    s.add_argument("--pie", action="store_true", help="the PIE world's (runtime-generated) navmesh")
    s.add_argument("--agent", help="with path: an actor (pawn) whose navigation agent to path for")
    s.add_argument("--m", action="store_true", help="with path: x,y,z points are in metres")
    s = add("size", cmd_size, "biggest assets on disk under a folder, or what an asset loads with it (--deps)")
    s.add_argument("path", nargs="?", help="content folder (default /Game) or an asset")
    s.add_argument("--deps", action="store_true", help="with an asset: total it with everything it loads")
    s.add_argument("--top", type=int, default=10, help="rows per list (10)")
    s = add("mem", cmd_mem, "memory of the running editor/PIE from a memreport: process, classes, meshes, textures")
    s.add_argument("--full", action="store_true", help="memreport -full: adds the texture list (slower)")
    s.add_argument("--top", type=int, default=8, help="rows per list (8)")
    s = add("config", cmd_config, "project/editor settings: list sections, read one, or Name=value to set (saved)")
    s.add_argument("section", nargs="?", help="Maps, Rendering, Engine/General, or Container/Category/Section")
    s.add_argument("rest", nargs="*", metavar="regex|Name=value", help="a regex to filter settings, or values to set")
    s = add("ui", cmd_ui, "the game's on-screen UI in PIE (Slate + UMG); click | type | key to interact")
    s.add_argument("action", nargs="?", help="click <label|LABEL#N|ref>, type <textbox> <text>, key <Key>, or a regex filter")
    s.add_argument("rest", nargs="*")
    s.add_argument("--all", action="store_true", help="also images and zero-size widgets")
    s.add_argument("--submit", action="store_true", help="with type: press Enter afterwards")
    s.add_argument("--event", action="store_true", help="with click: fire a UMG button's OnClicked instead of clicking")
    s.add_argument("--near", metavar="TEXT", help="with click/type: of several same-label widgets, the one closest to "
                                                  "a widget showing TEXT")
    s.add_argument("--slate", action="store_true", help="with key: send it to the focused Slate widget instead of the game's "
                                                        "input (the editor's shortcuts then apply: Escape stops PIE). "
                                                        "Without --slate or --game, a key goes through Slate when a "
                                                        "game widget has keyboard focus (not Escape or chords)")
    s.add_argument("--game", action="store_true", help="with key: always through the game's input (player controller)")
    s.add_argument("--rects", action="store_true", help="each widget's screen rectangle (@x,y wxh, as ue ui at takes)")
    s = add("anim", cmd_anim, "animation state in PIE: anim BP, montage, curves, its variables, movement; "
                              "`anim graph <ABP>`: its state machines as text")
    s.add_argument("target", help="actor, 'player', or Actor.Component; or `graph`")
    s.add_argument("asset", nargs="?", help="with graph: the Animation Blueprint")
    s.add_argument("--editor", action="store_true", help="read the editor level instead of PIE")
    s = add("statetree", cmd_statetree, "a StateTree as text (states, conditions, tasks, transitions); no asset: list them")
    s.add_argument("asset", nargs="?", help="/Game/.../ST_Name (default: list the project's StateTrees)")
    s.add_argument("--pie", action="store_true", help="the PIE world's StateTree components, with tree and run status")
    s = add("ai", cmd_ai, "AI in PIE: movement/path, focus, perception, brain, blackboard, project getters")
    s.add_argument("target", nargs="?", help="a pawn or controller (default: one line per AI)")
    s = add("new", new_command, "create an asset: blueprint, material, mi, datatable, dataasset, level, folder; or a "
                                "project from an engine template")
    s.add_argument("kind", choices=["blueprint", "bp", "material", "mi", "datatable", "dataasset", "level", "folder",
                                    "project"])
    s.add_argument("path", help="/Game/Folder/Name; with project: the new project's folder (its name is the last part)")
    s.add_argument("--parent", help="blueprint: parent class; mi: parent material; datatable: row struct; dataasset: class")
    s.add_argument("--save", action="store_true", help="save it right away")
    s.add_argument("--cpp", action="store_true", help="with project: a C++ module (Build.cs ready for 5.8, a game mode)")
    s.add_argument("--template", help="with project: an engine template, e.g. ThirdPerson, VehicleAdv (default Blank)")
    s.add_argument("--engine", default="5.8", help="with project: engine version to associate (5.8)")
    s.add_argument("--claude-plugin", action="store_true", help="with project: enable Tether's Claude Code plugin there")
    s = add("template", cmd_template, "the engine's template content: list, or add <Pack> into /Game at its mount path")
    s.add_argument("action", choices=["list", "add"])
    s.add_argument("pack", nargs="?", help="with add: Vehicles, LevelPrototyping, Characters, ...")
    s.add_argument("--level", choices=["High", "Standard"], help="with add: detail level when a pack has both")
    s.add_argument("--dry-run", action="store_true", help="with add: say what would be copied")
    s = add("usages", cmd_usages, "where a C++ function/property/class or an asset is used: code, Blueprints, assets")
    s.add_argument("symbol", help="e.g. TryAttack, AEnemyCharacter, Health, or /Game/Path/Asset")
    s.add_argument("--max-mb", type=float, default=32, help="skip asset files bigger than this (32)")
    s.add_argument("--budget", type=float, default=20, help="seconds to spend reading Blueprint graphs (20)")
    s = add("overview", cmd_overview, "one-screen project brief from its files: modules, key classes, maps, input, content")
    s.add_argument("--brief", action="store_true", help="the short version the session-start hook uses")
    s = add("world", cmd_world, "world builds: build nav|lighting (live editor), hlod|minimap|nav-wp|<Builder> (job); "
                                "probe x,y,z: what is at a point")
    s.add_argument("action", choices=["build", "probe"])
    s.add_argument("what", nargs="+", help="build: nav, lighting, hlod, minimap, nav-wp, resave, or a WorldPartition "
                                          "builder class; probe: x,y,z points or actors")
    s.add_argument("--radius", type=float, default=50, help="with probe: overlap radius in cm (50)")
    s.add_argument("--m", action="store_true", help="with probe: x,y,z points are in metres")
    s.add_argument("--pie", action="store_true", help="with probe: the PIE world")
    s.add_argument("--map", help="with builders: the map (default: the editor's)")
    s.add_argument("--quality", default="Preview", help="with lighting: Preview, Medium, High, Production")
    s.add_argument("--save", action="store_true", help="with nav: save the packages the build changed")
    s.add_argument("--force", action="store_true", help="run a builder even though the map is open in the editor")
    s.add_argument("--no-wait", action="store_true", help="with builders: start the job and return")
    s.add_argument("--wait", type=float, default=1800, help="seconds to wait for nav or lighting (1800)")
    s = add("audio", cmd_audio, "sounds playing in PIE: sound, owner, distance, volume, attenuation; `audio mix`: classes and "
                                "submixes; --log N: what started over N seconds, as the mixer resolved it")
    s.add_argument("match", nargs="?", help="regex filter")
    s.add_argument("--log", type=float, metavar="SECONDS", help="record the sounds that start over N seconds of PIE")
    s.add_argument("--do", metavar="STEPS", help="with --log: play a ue pie do step file while recording")
    s = add("soak", cmd_soak, "long PIE run (a step file in a loop, or wandering): memory trend, fps, errors, crashes")
    s.add_argument("--minutes", type=float, default=5, help="how long (5)")
    s.add_argument("-f", "--file", help="a `ue pie do` step file to loop (default: wander the player)")
    s.add_argument("--sample", type=float, default=10, help="seconds between memory/fps samples (10)")
    s.add_argument("--max-growth", type=float, default=20, help="MB/min of memory growth that fails the run (20)")
    s.add_argument("--keep-going", action="store_true", help="keep looping after a failing iteration")
    s = add("input", cmd_input, "Enhanced Input: actions and their keys by mapping context; in PIE, what is active")
    s.add_argument("action", nargs="?", help="regex: only these actions")
    s.add_argument("--live", metavar="ACTION", help="while PIE runs: the action's value over time, from the keys behind it")
    s.add_argument("--seconds", type=float, help="with --live: how long to watch (10)")
    s = add("savegame", cmd_savegame, "save slots: list, show one (regex filter), or Name=value to edit and save (.bak kept)")
    s.add_argument("slot", nargs="?", help="slot name (default: list the slots)")
    s.add_argument("rest", nargs="*", metavar="regex|Name=value")
    s.add_argument("--user", type=int, default=0, help="user index (0)")
    s = add("import", cmd_import, "import files (mesh, texture, sound) into /Game without dialogs; --reimport changed ones")
    s.add_argument("paths", nargs="+", metavar="PATH", help="files or folders, then the content folder (/Game/...); "
                                                            "with --reimport: assets or content folders")
    s.add_argument("--reimport", action="store_true", help="reimport these assets from their source files if changed")
    s.add_argument("--force", action="store_true", help="with --reimport: also when the source is unchanged")
    s.add_argument("--name", help="asset name (one file only)")
    s.add_argument("--collision", choices=["none", "box", "sphere", "capsule", "convex", "complex"],
                   help="static meshes: replace the collision")
    s.add_argument("--lods", type=int, help="static meshes: generate this many LODs (halving triangles each)")
    s.add_argument("--nanite", dest="nanite", action="store_true", default=None, help="static meshes: Nanite on")
    s.add_argument("--no-nanite", dest="nanite", action="store_false", help="static meshes: Nanite off")
    s.add_argument("--texture-group", help="textures: LOD group, e.g. World, Character, UI, Effects")
    s.add_argument("--no-replace", action="store_true", help="skip files whose asset already exists")
    s.add_argument("--save", action="store_true", help="save the imported assets")
    s = add("fab", cmd_fab, "Fab content you have: in the project, downloaded packs, other projects; add it in "
                            "(never buys or claims anything)")
    s.add_argument("action", nargs="?", choices=["list", "library", "add", "remove", "open", "settings", "clean"],
                   help="list (default): Fab content in this project; library: packs you have downloaded and Fab "
                        "content in nearby projects; add: copy one in; remove: delete one (through the editor; refuses "
                        "while the rest of the project references it); open: a listing's URL for the user; settings: "
                        "download quality/format/cache; clean: unused Fab content and cache sizes")
    s.add_argument("rest", nargs="*", help="a name or regex (list, library, add, open), or key=value (settings)")
    s.add_argument("--from", dest="from_", metavar="PROJECT", help="library/add: another local project's folder")
    s.add_argument("--dry-run", action="store_true", help="add/remove: show what would be copied or deleted")
    s.add_argument("--force", action="store_true", help="remove: delete even when other assets reference it, or "
                                                        "without an editor to check")
    s.add_argument("--skip-existing", action="store_true", help="add: copy only new files when some differ")
    s.add_argument("--top", type=int, default=20, help="rows to show (20)")
    s.add_argument("--all", action="store_true", help="show every row")
    s.add_argument("--no-use", action="store_true", help="list: skip what-the-game-uses (faster)")
    s.add_argument("--library", action="store_true", help="open: the user's Fab library page")
    s.add_argument("--show", action="store_true", help="open: also open it in the default browser")
    s = add("unused", cmd_unused, "assets nothing uses (from maps, config, code, cook settings), biggest first; redirectors")
    s.add_argument("path", nargs="?", default="/Game", help="content folder to report on (default /Game)")
    s.add_argument("--top", type=int, default=20, help="rows in the biggest list (20)")
    s.add_argument("--redirectors", action="store_true", help="list redirectors instead")
    s.add_argument("--fix", action="store_true", help="with --redirectors: resave their referencers and delete them")
    s = add("lint", cmd_lint, "content rules beyond Epic's validators: naming, texture size/format, mesh collision/LODs")
    s.add_argument("path", nargs="?", default="/Game", help="content folder (default /Game)")
    s.add_argument("--rule", help="regex: only these rules (naming, characters, texture-*, mesh-*)")
    s.add_argument("--ignore", action="append", metavar="GLOB", help="skip packages matching, e.g. '/Game/Fab/*' (repeatable)")
    s.add_argument("--strict", action="store_true", help="exit 1 when anything is reported")
    s.add_argument("--cpp", action="store_true", help="C++: file-private names in several .cpp files of a module "
                                                     "(unity-build collisions)")
    s.add_argument("--mpc", action="store_true", help="material Collection Parameter nodes whose collection parameter "
                                                     "is gone or has a new ID (they read 0); --fix re-links by name")
    s.add_argument("--materials", action="store_true", help="materials with many nodes that reach no output (loads "
                                                           "them; `ue mat graph M prune` deletes those nodes)")
    s.add_argument("--min-unused", type=int, default=5, help="with --materials: report from this many unused nodes (5)")
    s.add_argument("--fix", action="store_true", help="fix what has a safe fix (renames only with --rule naming|characters, "
                                                     "meshes without LODs only with --rule mesh-lods)")
    s.add_argument("--lod-fix", choices=["nanite", "lods"], default="nanite", help="with --fix --rule mesh-lods: turn "
                   "Nanite on (default), or generate 4 LODs (for meshes Nanite does not suit: translucent, skinned "
                   "or deforming with WPO)")
    s.add_argument("--dry-run", action="store_true", help="with --fix: show the fixes only")
    s.add_argument("--save", action="store_true", help="with --fix: save the changed assets")
    s = add("find", cmd_find, "search assets by name and/or class (asset registry)")
    s.add_argument("query", nargs="?", help="regex on the asset name, or a package path: a folder or a glob "
                                           "(/Game/X/Presets/*)")
    s.add_argument("--class", dest="cls", help="regex on the class name, e.g. StaticMesh or 'Material$'")
    s.add_argument("--under", default="/Game", help="content folder to search (default /Game)")
    s = add("refs", cmd_refs, "what references an asset (--deps: what it depends on)")
    s.add_argument("asset", help="/Game/Path/Asset")
    s.add_argument("--deps", action="store_true", help="list dependencies instead of referencers")
    s = add("class", cmd_class, "a class at a glance: parents, own properties/functions, Blueprint parts, subclasses, references")
    s.add_argument("name", help="Character, AMyEnemy, /Script/Module.Class, BP_Door or /Game/.../BP_Door")
    for flag, key, text in (("--functions", "functions", "functions alone, one per line with parameters and types"),
                            ("--vars", "vars", "variables alone, one per line"),
                            ("--components", "components", "components alone"), ("--events", "events", "events alone"),
                            ("--props", "props", "C++ properties alone"), ("--derived", "derived", "subclasses alone")):
        s.add_argument(flag, dest="section", action="store_const", const=key, help=text)
    s.add_argument("--grep", metavar="REGEX", help="only the items matching (names and types)")
    s = add("api", cmd_api, "the unreal Python API: a class's methods and properties, or one member's doc")
    s.add_argument("name", help="class or class.member, e.g. EditorActorSubsystem or Actor.set_actor_location")
    s.add_argument("match", nargs="?", help="regex filter on member names")
    s = add("game", cmd_game, "outside PIE: server + clients as game processes (start|status|log|shot|stop), or smoke: "
                              "each map in -game for a few seconds")
    s.add_argument("action", nargs="?", choices=["start", "status", "log", "shot", "stop", "smoke"])
    s.add_argument("--maps", help="with smoke: comma-separated maps (default: the game map and every map in Content/, "
                                  "up to 12)")
    s.add_argument("--seconds", type=float, help="with smoke: seconds to play each map after it loads (15)")
    s.add_argument("index", nargs="?", type=int, help="with log/shot: which process (0 = the server)")
    s.add_argument("-o", "--output", help="with shot: PNG path (default Saved/Tether/shots/)")
    s.add_argument("--clients", type=int, default=1, help="with start: how many clients (1)")
    s.add_argument("--server", choices=["listen", "dedicated", "none"], default="listen",
                   help="with start: listen server (a player hosts), dedicated (-server), or none (each client plays on "
                        "its own, on --map; --connect sends them to a server you run)")
    s.add_argument("--connect", metavar="HOST:PORT", help="with start: clients join this server instead")
    s.add_argument("--python", action="store_true", help="with start (editor binary): keep editor Python, as Play > "
                                                         "Standalone does, so the project's start-up scripts run")
    s.add_argument("--map", help="with start: the server's map (default: the project's GameDefaultMap)")
    s.add_argument("--game", metavar="CLASS", help="with start: ?game= game mode for the server's map")
    s.add_argument("--packaged", action="store_true", help="with start: run the packaged build (ue package first)")
    s.add_argument("--windowed", action="store_true", help="with start: windows on screen (default: headless, -nullrhi)")
    s.add_argument("--game-port", type=int, help="with start: the server's port (17777)")
    s.add_argument("--server-args", default="", help="with start: extra command-line arguments for the server")
    s.add_argument("--client-args", default="", help="with start: extra command-line arguments for each client")
    s.add_argument("--wait", type=float, default=120, help="with start: seconds to wait for the clients to join; with "
                                                          "smoke: for each map to load (120)")
    s.add_argument("--server-delay", type=float, default=3, help="with start: seconds between server and clients (3)")
    s.add_argument("--level", help="with log: Error, Warning (default), Display, Log")
    s.add_argument("--grep", help="with log: regex filter")
    s.add_argument("--platform", help=argparse.SUPPRESS)
    s = add("job", cmd_job, "background jobs for long work: start -- <cmd>, cook, status, wait, log")
    s.add_argument("action", choices=["start", "cook", "package", "status", "wait", "log", "stop", "_run"])
    s.add_argument("id", nargs="?", help="job id (default: the latest)")
    s.add_argument("--name", help="label for a started job")
    s.add_argument("--platform", help="with cook: target platform (default: this host)")
    s.add_argument("--wait", type=float, default=7200, help="with wait: seconds before giving up (7200)")
    s.add_argument("-v", "--verbose", action="store_true", help="with wait: also print warnings")
    s = add("package", package_command, "package the game (background job), then boot it headless and report crashes")
    s.add_argument("--platform", help="target platform (default: this host)")
    s.add_argument("--config", default="Development", help="Development (default), Shipping, DebugGame")
    s.add_argument("--seconds", type=float, default=20, help="how long the boot test runs (20)")
    s.add_argument("--wait", type=float, default=7200, help="seconds to allow the packaging job (7200)")
    s.add_argument("--no-wait", action="store_true", help="start the packaging job and return")
    s.add_argument("--no-boot", action="store_true", help="package only")
    s.add_argument("--boot-only", action="store_true", help="use the last package without rebuilding (warns when it is older than your changes)")
    s.add_argument("--nullrhi", action="store_true", help="boot without rendering (-nullrhi)")
    s.add_argument("--strict", action="store_true", help="fail on logged errors too")
    s.add_argument("--play", type=int, metavar="CLIENTS", help="instead of the boot test: a listen server and N clients "
                                                               "from the new package (--boot-only: the last one)")
    s.add_argument("--perf", type=float, metavar="SECONDS", help="instead of the boot test: frame times in the packaged "
                                                                 "game for about N seconds (--boot-only: the last package)")
    s.add_argument("--map", help="with --perf: the map to measure (default: the game's start map)")
    s = add("ci", cmd_ci, "project checks in one pass/fail: open if needed, tests, Blueprint compile, validation, cook, close")
    s.add_argument("--tests", help="test filter (default ^<Project>.)")
    s.add_argument("--bp", help="content path for the Blueprint check (default /Game)")
    s.add_argument("--play", nargs="?", const="", metavar="GLOB",
                   help="also run `ue pie do` step files (default tests/play/*.txt or Tests/Play/*.txt)")
    s.add_argument("--validate", nargs="?", const="/Game", metavar="PATH",
                   help="also run data validation on PATH (default /Game; ~0.1 s per asset) and Map Check")
    s.add_argument("--budget", action="store_true", help="also check Config/Tether/budget.json (ue budget)")
    s.add_argument("--cook", action="store_true", help="also cook for this platform")
    s.add_argument("--cook-timeout", type=float, default=3600, help="seconds to allow the cook (3600)")
    s.add_argument("--report", metavar="FILE", help="write a JSON report (for CI artifacts)")
    s.add_argument("-v", "--verbose", action="store_true", help="print the tail of failing steps")
    s = add("selftest", cmd_selftest, "check every capability against the running editor (pass/fail/skip)")
    s.add_argument("--reload", action="store_true", help="also hot-reload the Tether package and re-check")
    s = add("init", cmd_init, "set up this project: enable the needed plugins, copy the Tether editor plugin in")
    s.add_argument("--unrealsharp", action="store_true", help="also install TetherUnrealSharp (auto if Script/ exists)")
    s.add_argument("--dry-run", action="store_true", help="show what would change")
    s.add_argument("--claude-plugin", action="store_true", help="also enable Tether's Claude Code plugin for this "
                                                                "folder (.claude/settings.local.json)")
    s = add("batch",cmd_batch, "run many ue commands in one process and editor session (one per line)")
    s.add_argument("file", nargs="?", help="file of ue command lines (default: stdin); # comments")
    s.add_argument("--stop-on-error", action="store_true", help="stop at the first failing command")
    add("reload", cmd_reload, "hot-reload the Tether Python package in the editor")
    s = add("hook", cmd_hook, "Claude Code hook entry points (hook JSON on stdin)")
    s.add_argument("event", choices=["pre-edit", "pre-mcp", "session-start", "post-edit", "stop"])

    return p


def parse_command(parser, raw):
    """Parsed arguments for one ue command line, with the global options' defaults filled in."""
    command = []
    if "job" in raw and "--" in raw[raw.index("job"):]:  # `ue job start [opts] -- <program> <args...>`
        cut = raw.index("--", raw.index("job"))
        raw, command = raw[:cut], raw[cut + 1:]
    args = parser.parse_args(raw)
    args.job_command = command
    args.timeout_given = hasattr(args, "timeout")
    for key, default in (("project", None), ("pid", None), ("port", None), ("timeout", 120.0), ("json", False),
                         ("limit", 30), ("time", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    if args.pid:  # every lookup of "the editor" reads this, the beacon helpers in core included
        os.environ["UE_EDITOR_PID"] = str(args.pid)
    if getattr(args, "ignore_lease", False):  # reaches the PIE starts inside run/do/watch/record too
        os.environ["UE_IGNORE_LEASE"] = "1"
    return args


def cmd_batch(ctx, args):
    """Run many ue commands in one process and one editor session: one per line of a file or stdin."""
    import shlex
    if args.file and args.file != "-":
        with open(args.file, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    parser, worst, started = build_parser(), 0, time.perf_counter()
    for line in lines:
        argv = shlex.split(line, posix=True)
        if argv and argv[0] == "ue":
            argv = argv[1:]
        print(f"$ ue {line[3:] if line.startswith('ue ') else line}", flush=True)
        chosen = os.environ.get("UE_EDITOR_PID")  # a line's --pid applies to that line only
        ignoring = os.environ.get("UE_IGNORE_LEASE")  # and its --ignore-lease
        try:
            sub = parse_command(parser, undo_msys_paths(argv))
            if sub.fn in (cmd_batch, cmd_hook):
                raise CliError("not inside ue batch")
            code = sub.fn(ctx, sub) or 0
        except CliError as exc:
            print(f"error: {exc}", flush=True)
            code = exc.code
        except SystemExit as exc:  # argparse rejected the line (its message is already printed)
            code = exc.code if isinstance(exc.code, int) else 2
        finally:
            if chosen is None:
                os.environ.pop("UE_EDITOR_PID", None)
            else:
                os.environ["UE_EDITOR_PID"] = chosen
            if ignoring is None:
                os.environ.pop("UE_IGNORE_LEASE", None)
        worst = max(worst, code)
        if code and args.stop_on_error:
            print(f"(stopped at the first failure; {len(lines) - lines.index(line) - 1} line(s) not run)")
            break
    sys.stdout.flush()
    print(f"({len(lines)} command(s) in {time.perf_counter() - started:.1f}s, exit {worst})", file=sys.stderr)
    return worst


# Commands that only look at the sharing state: running them does not count as using the editor.
UNRECORDED = {"help", "hook", "status", "who", "lease", "doctor"}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252; editor text is often not
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_command(build_parser(), undo_msys_paths(sys.argv[1:] if argv is None else argv))
    started, started_wall = time.perf_counter(), time.time()
    # --json: one JSON document for any command (its own JSON for those that have one, below).
    envelope = args.json and args.fn not in (cmd_call, cmd_test, cmd_selftest)
    captured, real_stdout, ctx, error, failure = None, sys.stdout, None, None, None
    if envelope:
        import io
        captured = sys.stdout = io.StringIO()
    if args.cmd in UNRECORDED:  # looking does not pick this session's editor (an explicit --pid still does)
        core.KEEP_TARGET = False
    else:
        try:  # who ran what, for `ue who` and the shared-editor checks
            record_activity(find_project(args.project), ["ue"] + (sys.argv[1:] if argv is None else list(argv)))
            for line in lease_messages(find_project(args.project)):  # requests for this session's lease, a grant
                print(line, file=sys.stderr)
        except CliError:
            pass  # not in a project: the command itself will say so
    try:
        ctx = Ctx(args)
        try:
            code = args.fn(ctx, args)
        finally:
            ctx.close()
    except CliError as exc:
        sys.stdout.flush()  # keep the error after the output that explains it when both are piped
        message = str(exc)
        if exc.code in (2, 4) and ("not reachable" in message or "timed out" in message):
            try:  # the editor may have died during this very call
                crash = recent_crash(find_project(args.project), started_wall - 5)
                message += f"; the editor crashed: {crash_summary(crash)} (ue crash)" if crash else ""
            except CliError:
                pass
        if exc.code == 4 and "blocked by modal" not in message:
            try:  # say why the editor stopped answering, if it is a dialog or a stalled game thread
                report = stall_report(find_project(args.project))
                message += f"; editor {report}" if report else ""
            except CliError:
                pass
        if envelope:
            error = message
        else:
            print(f"error: {message}", file=sys.stderr)
        code = exc.code
        failure = message
    except BaseException:
        sys.stdout = real_stdout  # a crash or Ctrl+C prints its traceback, not a half-written envelope
        raise
    if envelope:
        sys.stdout = real_stdout
        lines = captured.getvalue().splitlines()
        print(json.dumps({"command": args.cmd, "ok": not code, "exit": code or 0, "error": error, "lines": lines,
                          "data": getattr(ctx, "data", None)}, default=str))
    if args.cmd not in ("hook", "stats", "help"):  # `ue stats`: which commands run, fail and take long, kept locally
        try:
            action = getattr(args, "action", None)
            name = args.cmd + (f" {action}" if isinstance(action, str) and action else "")
            record_stat(find_project(args.project), name, code or 0, time.perf_counter() - started,
                        error=failure)
        except CliError:
            pass
    if args.time:
        sys.stdout.flush()
        print(f"({(time.perf_counter() - started) * 1000:.0f} ms)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
