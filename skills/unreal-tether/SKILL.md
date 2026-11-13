---
name: unreal-tether
description: Drive a running Unreal Editor (UE 5.8+) through the `ue` CLI. Use it to query and set actor and asset properties, take viewport screenshots, run automation tests, editor Python and console commands, call Epic's ~830 editor tools, build C++/C# without restarts, and read logs. Use when working in an Unreal project whose editor runs with the Tether plugin and Epic's ModelContextProtocol server.
---

# Unreal via `ue`

Run `ue` from the project folder (or set `UE_PROJECT`). The Tether Claude Code plugin puts it on PATH;
if `ue` is not found, use `python "${CLAUDE_PLUGIN_ROOT}/cli/ue.py"` instead. If `ue status` exits 2,
start the editor with `ue open --headless` (`--render` if you need `ue shot`).

Once the editor is running, talk to it; do not boot it again. Each `ue` call takes tens of
milliseconds. A `-run=pythonscript` commandlet takes ~25 s and floods the log.

Several reads or edits in a row? Put them in one `ue batch` (one command per line, from stdin or a file):
one process and one editor session for all of them.

If something behaves unexpectedly, `ue doctor` checks the setup (engine, plugins, editor, Live Coding lock,
.NET, LFS) in a second, and `ue selftest` checks every capability against the editor and names what is broken.

## First moves
- New to the project? `ue overview` (key classes, maps, input, content) before exploring files by hand.
- `ue status`: is the editor up, which map, PIE on/off, heartbeat. Exit code 2 = not available.
- `ue call tether.level_summary`: what is in the level (counts by class).
- `ue actors <regex>`: label, class, location. `ue props <Actor|Actor.Component|/Game/Path> [regex]`:
  names, types and **values**. Use these instead of guessing property names. Add `--pie` to read the
  PIE world instead of the edited level.
- `ue shot [--actor <A>]`: PNG of the viewport; prints the path. Open it only when you need to see it. Needs a
  rendering editor: `ue open --headless --render` (plain `--headless` is `-nullrhi`).

## Everyday commands
- `ue set <target> name=value [name+=delta ...]`: one undoable transaction, prints `old -> new`. Tuples for
  structs (`location+=0,0,200`); actors also take `location`, `rotation` (pitch,yaw,roll) and `scale`. Prefer it
  over `ue py` for property edits. `ue undo` reverts your last `set`/`spawn`/`duplicate`/`delete`.
- `ue py "<code>"`, `ue py -f script.py [args]` or `ue py -`: unrestricted editor Python; `unreal` is pre-imported.
  `ue api <Class> [regex]` gives real signatures: cheaper than guessing. Pass `unreal.Rotator` by keyword.
- `ue exec "<console command>" [--pie]`; `ue call <toolset>.<tool> key=value ...` for Epic's tools (`ue tools
  [query]` finds them). **Never** dump a whole toolset schema: one is ~77k tokens.
- `ue log [--level Warning|Display|Log] [--cat X] [--grep re]`: only what is new since the last `ue log`. Run it
  after anything that might have warned or errored.
- `ue pie run check.py --expect '<log regex>' --reject 'Error|no subsystem'` (or `-c "<code>"`): starts PIE, runs
  the script in the PIE world, waits for the log lines, stops PIE, exits 0/1. Prefer it for verifying gameplay.

## Everything else: read the file for your area
Paths are relative to this file. Read one before using a command you have not used; `ue help <command>` gives one
command's options and `ue help` lists them all. Most tasks have a command: look before writing `ue py`.

| Working on | Commands | Read |
|---|---|---|
| Leases in depth, second editors, sub-agents | `lease who git`, `open --second`, `wait lease` | `references/shared-editors.md` |
| Editor Python, property edits, undo, tools, logs, gotchas | `py set undo redo rerun changes exec watch tools call log reload api` | `references/scripting.md` |
| New projects and assets, import, lint, clean-up, Fab, materials, tables, config, binary diffs | `new template init find refs asset size mem lint unused move-pack fab import mesh skel sound mat data table config diff locks` | `references/assets.md` |
| Levels, placing, terrain, foliage, water, lights, effects | `map collide level wp spawn drop scatter duplicate delete mv rm landscape foliage water lights fx pcg` | `references/level.md` |
| C++, C#, automated tests | `build test`, `lint --cpp` | `references/code.md` |
| Blueprints and classes | `bp class tree usages` | `references/blueprints.md` |
| Playing in PIE: steps, watches, input, game UI | `pie trace collision input ui soak`, `wait N game` | `references/play.md` |
| Screenshots and visual comparisons | `shot look post`, `wait compile` | `references/shots.md` |
| Performance, memory, loading | `perf gpu tex cvar budget stats` | `references/perf.md` |
| Multiplayer, AI, navigation, animation, audio, saves, packaging | `net game ai nav world statetree anim audio savegame package job ci` | `references/systems.md` |

## Editors you share
- If the user has the project open in their own editor, `ue` works with that one (`ue status` says "opened by
  hand"): do not start a second editor, and do not close or restart theirs (`ue build --restart` included) without
  asking; the user may be working in it, so say before you save. With two editors open, saving the same assets from
  both loses work.
- Other Claude sessions may share this editor (`ue who` lists who ran what in the last 15 min). Before anything
  that takes the editor away from them (a map rebuild, a long PIE run, `ue build --restart`, `ue close`), run
  `ue lease take --for "map rebuild + PIE"` and `ue lease release` when done.
- While another session holds the lease, `open`, `build --restart`, `close`, `pie start`, a UBT build with no
  editor open and anything that writes to disk (`save`, `import`, `mv`, `rm`, `--save`) refuse with exit 5 and say
  who holds it. So do `pie do`/`run`/`stop`/`watch`/`record`/`loop`, `exec` and `ui click|type|key` aimed at a PIE
  that is running, and at a PIE another active session started. Wait, or `ue lease request --for "why"`, rather
  than passing `--ignore-lease`; `ue wait lease` then returns once it is yours or free. `lease take` exits 5 too:
  in a script put `ue lease take --for "..." || exit` on its own line. Never pipe it (`| tail` hides the exit
  code, and an `&&` after the pipe still runs).
- Asked for yours? `ue lease grant` when you reach a stopping point. While someone waits, a lease lapses after
  3 minutes with no `ue` command of yours and no PIE session of yours running: do the thinking and editing before
  you take it, and run the editor work as one script. Test scripts that hide `ue` output never see requests: call
  `ue lease yield --back` between suites.
- The user watches and orders the queue in `ue dash` (a local page; `--once` prints it): it can move your request,
  hand the editor on or end your lease. Told yours was taken? Stop editor work and `ue lease request` again.
- Need another map or a throwaway import while others use the editor? `ue open --second` starts your own headless
  editor; `ue close` it as soon as you are done: while it runs, the shared editor cannot save the packages it loaded.
- A build error in a file you did not touch says who last edited it: leave it to that session. Committing from a
  shared checkout: `ue git stage --mine`.

## Before you call a task done
Your edits stay unsaved until saved. After changing content: `ue changes` to review, `ue save` (or tell the user
what is unsaved and why), then `ue validate`, and fix the errors it reports.

## Other people's files and money
- In a Perforce (or other check-out) workspace, teammates share binary files that can't be merged. Before a
  task, `ue locks /Game/Area` shows who holds what there and what is behind the depot. Every Tether edit checks
  out the files it modified right away; an edit to a file someone else holds, or one behind the depot, is undone
  and fails naming them ("is checked out by bob. Nothing was changed"): tell the user, don't work around it.
  One command may check out at most 20 files (Config/Tether/vcs.json): above that it is refused; ask the user
  before rerunning with `--bulk`. `ue save`/`mv`/`rm` check out and mark adds/deletes themselves; if a file is
  read-only without a connection, `ue save` exits 1: tell the user rather than making files writable yourself.
- Fab: use what the user already has first (`ue fab library <word>`). Suggest listings with `ue fab open
  "<search>"` and let the user decide: claiming, buying and downloading are always theirs. Never click, type or
  press keys in the Fab tab or the store page, by any route. Have the user check a pack's "Allows usage with AI"
  on its listing before an AI-driven project uses it.
- `ue savegame <slot> Name=value` edits a save: ask before editing a user's real save.

## Building
- `ue build`: if the editor is up it uses Live Coding (~5 s, no restart); if not, UBT. Never run Build.bat while an editor is open: one Live Coding session blocks UBT for every
  project on that engine. Do not compile the editor target with UBT `-Module=` while the editor runs: Live Coding
  breaks until a restart.
- Exit 3 means a restart is required (a new or changed UFUNCTION, UPROPERTY, UCLASS specifier): patched in, it can
  crash the next PIE. Class layout, constructor defaults and Build.cs/.uproject changes need a restart too.
  `ue build --restart` closes the editor, builds, reopens the same map and waits. It refuses unsaved work unless you
  pass `--save` or `--discard`; ask the user before discarding. Batch reflection changes so you restart once.
- Live Coding refuses (exit 5) when files another session edited in the last 15 minutes would be compiled, and
  names them; ask that session, or pass `--include-others`. It patches every editor open on the project at once.
  To check your own files while another session holds the editor: `ue build --check --files Source/X/A.cpp`.
- C# (UnrealSharp, a `Script/` folder): edit `.cs` files, then one `ue build` (hot-reloads in ~1 s, no restart).
  **Stop PIE before editing C#** (`ue pie stop`): running C# code can crash PIE. **Start PIE only with `ue pie
  start`, never Epic's `StartPIE`**: it hot-reloads pending C# first, and without that a type's subsystems vanish
  from the running world.

## Traps that do damage
- `StaticMesh.build_from_static_mesh_descriptions` on a mesh that already has render data crashes the editor;
  build into a new mesh instead.
- A `ue py` call that times out (default 120 s) leaves the script running in the editor: do not run it again;
  `ue wait idle` returns when it has finished (or pass `--timeout 600`).
- During PIE, editor-world code (EditorActorSubsystem, spawns, saves) acts on the edited level and spawns return
  None, so a generator stage can save a broken map. `ue py` refuses it: `ue pie stop` first, or `ue pie run -c`
  for the game world.
- Python setters on components (spline points, set_world_location, instances) change them in memory without
  Modify(): `ue changes` does not see it, yet anyone's next level save writes it. Call `comp.modify()` first.
- `ue map open` printing a WARNING (exit 1) means the level came back broken: don't save it from there (`ue save`
  refuses); `ue build --restart` and reopen. If `ue changes` warns that an actor belongs to a Tether diff copy, do
  not save that level; reopen it with `ue map open <map> --discard` (ask first: it drops unsaved edits).
- Material Parameter Collections: never rewrite `scalar_parameters`/`vector_parameters` with new structs; every
  material's node reads 0 with no warning. `ue mat` on a base material (M_, not MI_) changes the default every
  instance sees: run the undo command it prints before saving a temporary value.
- `ue call datatable.set_rows` upserts rows; never use `fill_data_table_from_json_string`, which wipes every row.
- `ue test <filter>`: never run without a filter (the engine has ~9k tests), and anchor project filters (`^MyGame.`):
  a bare substring also matches engine tests, and some of those change the open level.
- More gotchas (silent `None`s, struct copies): read `references/scripting.md` before writing more than a few
  lines of editor Python.

## Output and exit codes
- If the editor dies (`ue status` says "crashed", or a call reports "the editor crashed"), run `ue crash`
  before restarting: it shows the error, whether your modules were on the stack, and what ran last.
Output is compact text, capped at 30 rows (`--limit N`). Exit codes: 0 ok, 1 tool
error (one-line message), 2 editor unavailable, 3 restart required, 4 timeout, 5 another session holds the
editor lease (`ue lease`).
- `--json` on any command prints one JSON document.
- Exit 4 with `blocked by modal: "<title>"` means a dialog is open in a windowed editor and the editor
  cannot answer until someone closes it. Ask the user to deal with it; do not retry in a loop.
