"""The command line: argument parsing, ue batch, and main()."""
import argparse
import json
import os
import re
import sys
import time

from .core import CliError, crash_summary, find_project, recent_crash, stall_report
from .transport import Ctx
from .editor import cmd_build, cmd_close, cmd_crash, cmd_open, cmd_status
from .calls import (
    cmd_actors, cmd_ai, cmd_anim, cmd_api, cmd_asset, cmd_call, cmd_changes, cmd_config, cmd_delete, cmd_drop,
    cmd_duplicate, cmd_exec, cmd_find, cmd_log, cmd_mat, cmd_mv, cmd_nav, cmd_new, cmd_props, cmd_py, cmd_refs,
    cmd_rm, cmd_save, cmd_savegame, cmd_scatter, cmd_set, cmd_spawn, cmd_tools, cmd_undo, cmd_watch,
)
from .images import cmd_look, cmd_shot
from .testing import cmd_test
from .assets import cmd_bp, cmd_diff, cmd_overview, cmd_size, cmd_table, cmd_usages, cmd_validate
from .jobs import cmd_job, cmd_package
from .play import cmd_audio, cmd_mem, cmd_perf, cmd_pie, cmd_soak, cmd_ui, cmd_world
from .hooks import cmd_hook
from .setup import cmd_doctor, cmd_init, cmd_reload
from .selftest import cmd_ci, cmd_selftest


UNREAL_ROOTS = ("Game", "Script", "Engine", "Temp", "Memory")


def undo_msys_paths(argv):
    """Git Bash (MSYS) rewrites arguments that look like POSIX paths when it starts a Windows program, so
    `/Game/Maps/L` arrives as `C:/Program Files/Git/Game/Maps/L` (also after `key=`). Undo that for
    Unreal package roots, bare (`/Game`) or with a path."""
    if os.name != "nt" or not os.environ.get("MSYSTEM"):
        return argv
    import shutil
    bash = shutil.which("bash")
    if not bash:
        return argv
    root = os.path.dirname(os.path.dirname(os.path.dirname(bash))).replace("\\", "/")  # ...\Git\usr\bin\bash
    rx = re.compile("(^|=)" + re.escape(root) + "(?=/(?:" + "|".join(UNREAL_ROOTS) + r")(?:[/,]|$))", re.I)
    return [rx.sub(r"\1", a) for a in argv]


def build_parser():
    # Global options are accepted before or after the subcommand (agents write both).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help=".uproject path (default: search upward from cwd, or UE_PROJECT)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="MCP port (default: beacon, UE_MCP_PORT, then 8000)")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds to wait (120)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="one JSON document: {command, ok, exit, error, lines, data}")
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="max rows to print (30)")
    common.add_argument("--time", action="store_true", default=argparse.SUPPRESS, help="elapsed time to stderr")

    p = argparse.ArgumentParser(prog="ue", description=__doc__.split("\n\n")[0], parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text, parents=[common])
        s.set_defaults(fn=fn)
        return s

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
    s.add_argument("--wait", type=float, default=600, help="seconds to wait for ready (600)")
    s.add_argument("--no-wait", action="store_true")
    s = add("build", cmd_build, "compile: Live Coding if the editor is up, UBT if not; --restart for reflection changes")
    s.add_argument("--restart", action="store_true", help="save/close editor, UBT build, reopen same map")
    s.add_argument("--save", action="store_true", help="with --restart: save dirty packages first")
    s.add_argument("--discard", action="store_true", help="with --restart: drop unsaved changes")
    s.add_argument("--target", help="UBT target (default <Project>Editor)")
    s.add_argument("--config", default="Development")
    s.add_argument("--wait", type=float, default=300, help="seconds to wait for Live Coding (300)")
    s = add("close", cmd_close, "quit the editor; refuses to drop unsaved work unless --save/--discard")
    s.add_argument("--save", action="store_true", help="save dirty packages first")
    s.add_argument("--discard", action="store_true", help="drop unsaved changes (terminates if a dialog blocks)")
    s.add_argument("--force", action="store_true", help="terminate immediately")
    s = add("tools", cmd_tools, "list toolsets, a toolset's tools, or one tool's schema (toolset:tool)")
    s.add_argument("query", nargs="?")
    s.add_argument("-v", "--verbose", action="store_true")
    s = add("call", cmd_call, "invoke <toolset>.<tool> k=v ... (required blanks auto-filled)")
    s.add_argument("target")
    s.add_argument("kv", nargs="*")
    s.add_argument("--args", help="raw JSON arguments instead of k=v")
    s = add("py", cmd_py, "run Python in the live editor (code, -f file, or - for stdin)")
    s.add_argument("code", nargs="*")
    s.add_argument("-f", "--file")
    s = add("props", cmd_props, "reflected properties of an object/actor/component")
    s.add_argument("target")
    s.add_argument("match", nargs="?")
    s.add_argument("--pie", action="store_true", help="read the PIE world's actor instead")
    s.add_argument("--export", metavar="FILE", help="write every property value to FILE as JSON (e.g. a DataAsset)")
    s = add("set", cmd_set, "set properties (name=value, name+=delta) as one undoable transaction")
    s.add_argument("target")
    s.add_argument("assignments", nargs="*", metavar="name=value")
    s.add_argument("--pie", action="store_true", help="change the PIE world (not undoable, not saved)")
    s.add_argument("--from", dest="from_file", metavar="FILE", help="apply the values in FILE (JSON) that differ")
    s.add_argument("--dry-run", action="store_true", help="with --from: show what would change")
    add("undo", cmd_undo, "undo Tether's last `ue set` (only if nothing else changed since)")
    s = add("spawn", cmd_spawn, "place an actor from a class or asset (undoable)")
    s.add_argument("what", help="class (PointLight, /Script/Module.Class) or asset (/Game/Meshes/SM_Rock)")
    s.add_argument("--at", default="0,0,0", help="location x,y,z")
    s.add_argument("--rot", help="rotation pitch,yaw,roll")
    s.add_argument("--label", help="actor label")
    s.add_argument("--ground", action="store_true", help="drop it onto the surface below --at")
    s.add_argument("--align", action="store_true", help="with --ground: tilt it to the surface")
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
    s = add("drop", cmd_drop, "put actors on the ground below them (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
    s.add_argument("--align", action="store_true", help="also tilt each to the surface normal")
    s = add("delete", cmd_delete, "delete actors from the level (undoable)")
    s.add_argument("actors", nargs="+", help="actor labels or names")
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
    s.add_argument("--actor", help="frame this actor, keeping the current view direction")
    s.add_argument("--at", help="camera location x,y,z")
    s.add_argument("--look-at", help="aim at x,y,z or an actor")
    s.add_argument("--rot", help="camera rotation pitch,yaw,roll")
    s.add_argument("--annotate", action="store_true", help="overlay Epic's world grid and actor labels")
    s.add_argument("--grid-z", type=float, default=0, help="with --annotate: grid height (0)")
    s.add_argument("--ui", action="store_true", help="include gizmos and selection outlines")
    s.add_argument("-o", "--output", help="PNG path (default Saved/Tether/shots/shot-<time>.png)")
    s.add_argument("--baseline", metavar="NAME", help="also save this shot and its camera as a named baseline")
    s.add_argument("--compare", metavar="NAME", help="re-shoot a baseline's camera and report what changed (exit 1)")
    s.add_argument("--tolerance", type=float, default=0.5, help="with --compare: changed pixels allowed, %% (0.5)")
    s.add_argument("--threshold", type=int, default=32, help="with --compare: per-channel difference that counts (32)")
    s = add("look", cmd_look, "render an asset (mesh, Blueprint, material) from six angles into one contact sheet")
    s.add_argument("asset", help="/Game/... asset path")
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
    s = add("exec", cmd_exec, "console command; prints the log lines it produced")
    s.add_argument("command", nargs="+")
    s.add_argument("--pie", action="store_true", help="run in the PIE world")
    s = add("log", cmd_log, "new log entries since the last call (cursor kept on disk)")
    s.add_argument("--since", choices=["last", "start"], default="last")
    s.add_argument("--level", default="Warning", help="Error, Warning, Display, Log, Verbose")
    s.add_argument("--cat", help="only this log category")
    s.add_argument("--grep", help="regex filter")
    s = add("pie", cmd_pie, "Play-In-Editor: start | stop | status | run <script> | do <steps...>")
    s.add_argument("action", choices=["start", "stop", "status", "run", "do", "target"])
    s.add_argument("script", nargs="*", help="with run: Python file to execute in the PIE world; with do: steps")
    s.add_argument("-f", "--file", help="with do: a file of steps, one per line (# comments)")
    s.add_argument("--keep-going", action="store_true", help="with do: run every step even after a failure")
    s.add_argument("--clients", type=int, help="with start: multiplayer PIE with this many clients")
    s.add_argument("--net", choices=["listen", "client", "standalone"],
                   help="with --clients: listen server (default), client (dedicated server), or standalone")
    s.add_argument("--expect", action="append", help="with run: log regex that must appear (repeatable)")
    s.add_argument("--reject", action="append", help="with run/do: log regex that must not appear (repeatable)")
    s.add_argument("--seconds", type=float, default=5, help="with run: how long to wait for expectations (5)")
    s.add_argument("--keep", action="store_true", help="with run/do: leave PIE running afterwards")
    s.add_argument("--simulate", action="store_true", help="Simulate-In-Editor (no player pawn)")
    s.add_argument("--warmup", type=float, default=0.5, help="seconds to let BeginPlay settle (0.5)")
    s = add("bp", cmd_bp, "Blueprint graphs as text: read, --diff against the last read, --apply edited text")
    s.add_argument("blueprint", nargs="?", help="/Game/Path/BP_Name, or `check [/Game/path]` to compile all and list the broken")
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
    s = add("table", cmd_table, "DataTable round-trips: show, --export JSON/CSV, --import with a row diff")
    s.add_argument("table", help="/Game/Path/DT_Name")
    s.add_argument("--export", metavar="FILE", help="write all rows to FILE (.json or .csv)")
    s.add_argument("--import", dest="import_file", metavar="FILE", help="upsert rows from FILE (.json or .csv)")
    s.add_argument("--prune", action="store_true", help="with --import: remove rows missing from FILE")
    s.add_argument("--dry-run", action="store_true", help="with --import: show the diff only")
    s.add_argument("--save", action="store_true", help="save the DataTable afterwards")
    s = add("perf", cmd_perf, "frame-time capture in PIE: frame/game/render/GPU ms, hitches, change vs last capture")
    s.add_argument("--seconds", type=float, default=10, help="capture length (10)")
    s.add_argument("--editor", action="store_true", help="measure the editor as is, without starting PIE")
    s.add_argument("--no-record", action="store_true", help="do not make this capture the comparison baseline")
    s.add_argument("--trace", action="store_true", help="also an Unreal Insights trace: top CPU scopes per thread")
    s.add_argument("--top", type=int, default=6, help="with --trace: scopes per thread (6)")
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
    s = add("mat", cmd_mat, "material parameters with values and sources; Name=value sets them (undoable)")
    s.add_argument("target", help="material or instance path, or an actor (lists its materials)")
    s.add_argument("assignments", nargs="*", metavar="Name=value", help="scalar, r,g,b[,a], true/false, texture path")
    s.add_argument("--reset", action="append", metavar="NAME", help="stop overriding NAME (instances; repeatable)")
    s.add_argument("--compile", action="store_true", help="recompile the base material and list compile errors")
    s = add("nav", cmd_nav, "navmesh: status (and why it is missing), path <a> <b>, check [actor regex]")
    s.add_argument("action", nargs="?", choices=["status", "path", "check"])
    s.add_argument("points", nargs="*", help="path: start end (x,y,z or actor); check: actor regex")
    s.add_argument("--pie", action="store_true", help="the PIE world's (runtime-generated) navmesh")
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
    s.add_argument("action", nargs="?", help="click <label|ref>, type <textbox> <text>, key <Key>, or a regex filter")
    s.add_argument("rest", nargs="*")
    s.add_argument("--all", action="store_true", help="also images and zero-size widgets")
    s.add_argument("--submit", action="store_true", help="with type: press Enter afterwards")
    s = add("anim", cmd_anim, "animation state in PIE: anim BP, montage, curves, its variables, movement")
    s.add_argument("target", help="actor, 'player', or Actor.Component")
    s.add_argument("--editor", action="store_true", help="read the editor level instead of PIE")
    s = add("ai", cmd_ai, "AI in PIE: movement/path, focus, perception, brain, blackboard, project getters")
    s.add_argument("target", nargs="?", help="a pawn or controller (default: one line per AI)")
    s = add("new", cmd_new, "create an asset: blueprint, material, mi, datatable, dataasset, level, folder")
    s.add_argument("kind", choices=["blueprint", "bp", "material", "mi", "datatable", "dataasset", "level", "folder"])
    s.add_argument("path", help="/Game/Folder/Name")
    s.add_argument("--parent", help="blueprint: parent class; mi: parent material; datatable: row struct; dataasset: class")
    s.add_argument("--save", action="store_true", help="save it right away")
    s = add("usages", cmd_usages, "where a C++ function/property/class or an asset is used: code, Blueprints, assets")
    s.add_argument("symbol", help="e.g. TryAttack, AEnemyCharacter, Health, or /Game/Path/Asset")
    s.add_argument("--max-mb", type=float, default=32, help="skip asset files bigger than this (32)")
    s.add_argument("--budget", type=float, default=20, help="seconds to spend reading Blueprint graphs (20)")
    s = add("overview", cmd_overview, "one-screen project brief from its files: modules, key classes, maps, input, content")
    s.add_argument("--brief", action="store_true", help="the short version the session-start hook uses")
    s = add("world", cmd_world, "world builds: build nav|lighting (live editor), hlod|minimap|nav-wp|<Builder> (job)")
    s.add_argument("action", choices=["build"])
    s.add_argument("what", help="nav, lighting, hlod, minimap, nav-wp, resave, or a WorldPartition builder class")
    s.add_argument("--map", help="with builders: the map (default: the editor's)")
    s.add_argument("--quality", default="Preview", help="with lighting: Preview, Medium, High, Production")
    s.add_argument("--save", action="store_true", help="with nav: save the packages the build changed")
    s.add_argument("--force", action="store_true", help="run a builder even though the map is open in the editor")
    s.add_argument("--no-wait", action="store_true", help="with builders: start the job and return")
    s.add_argument("--wait", type=float, default=1800, help="seconds to wait for nav or lighting (1800)")
    s = add("audio", cmd_audio, "sounds playing in PIE: sound, owner, distance, volume, attenuation")
    s.add_argument("match", nargs="?", help="regex filter")
    s = add("soak", cmd_soak, "long PIE run (a step file in a loop, or wandering): memory trend, fps, errors, crashes")
    s.add_argument("--minutes", type=float, default=5, help="how long (5)")
    s.add_argument("-f", "--file", help="a `ue pie do` step file to loop (default: wander the player)")
    s.add_argument("--sample", type=float, default=10, help="seconds between memory/fps samples (10)")
    s.add_argument("--max-growth", type=float, default=20, help="MB/min of memory growth that fails the run (20)")
    s.add_argument("--keep-going", action="store_true", help="keep looping after a failing iteration")
    s = add("savegame", cmd_savegame, "save slots: list, show one (regex filter), or Name=value to edit and save (.bak kept)")
    s.add_argument("slot", nargs="?", help="slot name (default: list the slots)")
    s.add_argument("rest", nargs="*", metavar="regex|Name=value")
    s.add_argument("--user", type=int, default=0, help="user index (0)")
    s = add("find", cmd_find, "search assets by name and/or class (asset registry)")
    s.add_argument("query", nargs="?", help="regex on the asset name")
    s.add_argument("--class", dest="cls", help="regex on the class name, e.g. StaticMesh or 'Material$'")
    s.add_argument("--under", default="/Game", help="content folder to search (default /Game)")
    s = add("refs", cmd_refs, "what references an asset (--deps: what it depends on)")
    s.add_argument("asset", help="/Game/Path/Asset")
    s.add_argument("--deps", action="store_true", help="list dependencies instead of referencers")
    s = add("api", cmd_api, "the unreal Python API: a class's methods and properties, or one member's doc")
    s.add_argument("name", help="class or class.member, e.g. EditorActorSubsystem or Actor.set_actor_location")
    s.add_argument("match", nargs="?", help="regex filter on member names")
    s = add("job", cmd_job, "background jobs for long work: start -- <cmd>, cook, status, wait, log")
    s.add_argument("action", choices=["start", "cook", "package", "status", "wait", "log", "stop", "_run"])
    s.add_argument("id", nargs="?", help="job id (default: the latest)")
    s.add_argument("--name", help="label for a started job")
    s.add_argument("--platform", help="with cook: target platform (default: this host)")
    s.add_argument("--wait", type=float, default=7200, help="with wait: seconds before giving up (7200)")
    s.add_argument("-v", "--verbose", action="store_true", help="with wait: also print warnings")
    s = add("package", cmd_package, "package the game (background job), then boot it headless and report crashes")
    s.add_argument("--platform", help="target platform (default: this host)")
    s.add_argument("--config", default="Development", help="Development (default), Shipping, DebugGame")
    s.add_argument("--seconds", type=float, default=20, help="how long the boot test runs (20)")
    s.add_argument("--wait", type=float, default=7200, help="seconds to allow the packaging job (7200)")
    s.add_argument("--no-wait", action="store_true", help="start the packaging job and return")
    s.add_argument("--no-boot", action="store_true", help="package only")
    s.add_argument("--boot-only", action="store_true", help="boot-test the last package without rebuilding")
    s.add_argument("--nullrhi", action="store_true", help="boot without rendering (-nullrhi)")
    s.add_argument("--strict", action="store_true", help="fail on logged errors too")
    s = add("ci", cmd_ci, "project checks in one pass/fail: open if needed, tests, Blueprint compile, validation, cook, close")
    s.add_argument("--tests", help="test filter (default ^<Project>.)")
    s.add_argument("--bp", help="content path for the Blueprint check (default /Game)")
    s.add_argument("--play", nargs="?", const="", metavar="GLOB",
                   help="also run `ue pie do` step files (default tests/play/*.txt or Tests/Play/*.txt)")
    s.add_argument("--validate", nargs="?", const="/Game", metavar="PATH",
                   help="also run data validation on PATH (default /Game; ~0.1 s per asset) and Map Check")
    s.add_argument("--cook", action="store_true", help="also cook for this platform")
    s.add_argument("--cook-timeout", type=float, default=3600, help="seconds to allow the cook (3600)")
    s.add_argument("--report", metavar="FILE", help="write a JSON report (for CI artifacts)")
    s.add_argument("-v", "--verbose", action="store_true", help="print the tail of failing steps")
    s = add("selftest", cmd_selftest, "check every capability against the running editor (pass/fail/skip)")
    s.add_argument("--reload", action="store_true", help="also hot-reload the Tether package and re-check")
    s = add("init", cmd_init, "set up this project: enable the needed plugins, copy the Tether editor plugin in")
    s.add_argument("--unrealsharp", action="store_true", help="also install TetherUnrealSharp (auto if Script/ exists)")
    s.add_argument("--dry-run", action="store_true", help="show what would change")
    s = add("batch", cmd_batch, "run many ue commands in one process and editor session (one per line)")
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
    for key, default in (("project", None), ("port", None), ("timeout", 120.0), ("json", False),
                         ("limit", 30), ("time", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
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
        worst = max(worst, code)
        if code and args.stop_on_error:
            print(f"(stopped at the first failure; {len(lines) - lines.index(line) - 1} line(s) not run)")
            break
    sys.stdout.flush()
    print(f"({len(lines)} command(s) in {time.perf_counter() - started:.1f}s, exit {worst})", file=sys.stderr)
    return worst


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252; editor text is often not
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_command(build_parser(), undo_msys_paths(sys.argv[1:] if argv is None else argv))
    started, started_wall = time.perf_counter(), time.time()
    # --json: one JSON document for any command (its own JSON for those that have one, below).
    envelope = args.json and args.fn not in (cmd_call, cmd_test, cmd_selftest)
    captured, real_stdout, ctx, error = None, sys.stdout, None, None
    if envelope:
        import io
        captured = sys.stdout = io.StringIO()
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
    except BaseException:
        sys.stdout = real_stdout  # a crash or Ctrl+C prints its traceback, not a half-written envelope
        raise
    if envelope:
        sys.stdout = real_stdout
        lines = captured.getvalue().splitlines()
        print(json.dumps({"command": args.cmd, "ok": not code, "exit": code or 0, "error": error, "lines": lines,
                          "data": getattr(ctx, "data", None)}, default=str))
    if args.time:
        sys.stdout.flush()
        print(f"({(time.perf_counter() - started) * 1000:.0f} ms)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
