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

If something behaves unexpectedly, `ue selftest` checks every capability in ~35 s and names what is
broken or unsupported.

## First moves
- `ue status`: is the editor up, which map, PIE on/off, heartbeat. Exit code 2 = not available.
- `ue call tether.level_summary`: what is in the level (counts by class).
- `ue actors <regex>`: label, class, location. `ue props <Actor|Actor.Component|/Game/Path> [regex]`:
  names, types and **values**. Use these instead of guessing property names. Add `--pie` to read the
  PIE world instead of the edited level.
- `ue shot`: PNG of the viewport (the player's view during PIE) saved under `Saved/Tether/shots/`; prints
  the path and size. Open it only when you need to see it. Frame a view with `--actor <A>`,
  `--at x,y,z --look-at <A|x,y,z>` or `--rot p,y,r`; `--annotate` overlays a world grid and actor labels.
  Needs a rendering editor: `ue open --headless --render` (plain `--headless` is `-nullrhi`).

## Doing things
- `ue py "<code>"`, `ue py -f script.py` or `ue py -` (stdin): unrestricted editor Python. `unreal` is
  pre-imported, the namespace persists between calls, and a trailing expression is printed.
- `ue set <target> name=value [name+=delta ...]`: write properties as one undoable transaction, and
  print `old -> new` for each. Values: numbers, true/false, enum names, object paths, JSON, or tuples
  for structs (`location+=0,0,200`, `lightColor=1,0.5,0.2`; missing trailing fields keep their values).
  Actors also take `location`, `rotation` (pitch,yaw,roll) and `scale`. If one value fails, nothing
  changes. `--pie` edits the running game instead (not undoable, not saved). Prefer this over `ue py`
  for property edits.
- `ue exec "<console command>" [--pie]`: runs the command and prints the log lines it produced.
- `ue tools [query]` lists Epic toolsets; `ue tools <toolset>` gives one-line signatures;
  `ue tools <toolset>:<tool>` gives one schema. **Never** dump a whole toolset schema: one Niagara toolset is ~77k tokens.
- `ue call <toolset>.<tool> key=value ...`: required blank strings/arrays are auto-filled, and object
  params take a plain path. Examples: `ue call scene.find_actors tag=Enemy`,
  `ue call blueprint.read_graph_dsl graph=...`, `ue call datatable.set_rows ...` (upserts rows; never use
  `fill_data_table_from_json_string`, which wipes every row).
- `ue log [--level Warning|Display|Log] [--cat X] [--grep re]`: only entries that are new since the last
  `ue log`, de-duplicated. Run it after anything that might have warned or errored.
- `ue reload`: hot-reloads `Plugins/Tether/Content/Python/tether` after you edit or add a tool.
  To make a throwaway inspection script reusable, add it there as a `@toolset_registry.tool_call`.

## C++ changes
- `ue build`: if the editor is up it uses Live Coding (~5 s, no restart); if not, UBT. Output is only
  your own errors and warnings, one line each. Never run Build.bat while an editor is open: one Live
  Coding session blocks UBT for every project on that engine.
- Function bodies and new UFUNCTIONs go live. Call a new UFUNCTION through reflection:
  `unreal.get_default_object(unreal.MyLib).call_method('NewFunc')`.
- New UPROPERTYs, class layout or constructor defaults, Build.cs/.uproject changes, or Blueprint/Python
  wrappers that need the new API: run `ue build --restart`. It closes the editor, builds, reopens the
  same map and waits until ready. It refuses if there is unsaved work unless you pass `--save` or
  `--discard`; ask the user before discarding. Exit code 3 from `ue build` means a restart is required.
  Batch reflection changes so you restart once.

## C# changes (UnrealSharp projects: a `Script/` folder plus `Plugins/UnrealSharp`)
- Edit `.cs` files under `Script/`, then run `ue build`. It hot-reloads in ~1 s, covering function
  bodies, new UFunctions, new UPropertys (constructor defaults apply) and brand-new classes, with no
  restart. Compile errors come back one line each, and the last good code stays loaded. Prefer C# for
  gameplay iteration when the project has it.
- Saving files does not reload. Batch your edits, then run one `ue build`.
- C# classes live at `/Script/UnrealSharp.<NameWithoutPrefix>_C`. Example:
  `cdo = unreal.get_default_object(unreal.load_class(None, '/Script/UnrealSharp.MyActor_C'))`, then
  `cdo.call_method('MyFunc', (1, 2))` and `cdo.get_editor_property('MyProp')`.
- **Stop PIE before editing C#** (`ue pie stop`). UnrealSharp reprocesses saved files at once, and
  running C# code can then crash PIE.
- **Start PIE only with `ue pie start`, never Epic's `StartPIE`.** It hot-reloads pending C# first.
  UnrealSharp recompiles a saved but unreloaded type on its first lookup, and that removes the type's
  subsystems from the running world: `GetWorldSubsystem` returns null for the rest of the session.
  If the reload fails, PIE does not start. A project hook also denies Epic's `StartPIE` while C# is
  unreloaded.
- A syntax error is rejected at save time. `ue build` and `ue pie start` report it as "UnrealSharp
  rejected the saved files", with the line. Fix the line, then rerun the command.
- After structural changes to a C# **subsystem** (new members or signatures), test PIE straight away.
  If `ue pie start` reports `InvalidCastException`, run `ue build --restart` (about 20 s).
- `ue call tether.cs_state` reports idle, pending or reloading.

## Blueprints, DataTables, API lookups, long jobs
- `ue find <regex> [--class StaticMesh] [--under /Game/x]` to locate assets; `ue refs <asset>` before deleting
  or renaming one (what uses it), `--deps` for what it uses.
- `ue api <Class> [regex]` before writing editor Python: real method signatures and property names in a
  few lines (`ue api Actor.set_actor_location` for one member). Cheaper than guessing.
- `ue bp /Game/Path/BP_X [graph]` prints Blueprint graphs as text. To change one: write the edited text to
  a file, `ue bp /Game/Path/BP_X EventGraph --apply file.dsl --save` (compiles, prints the diff).
  `ue bp ... --diff` shows only what changed since the last read; `ue bp --docs` is the syntax reference.
- After renaming or removing C++ functions, properties or classes (and `ue build --restart`), run
  `ue bp check` (or `ue bp check /Game/Some/Folder`): it lists only Blueprints that no longer compile, with why.
- `ue table /Game/Path/DT_X` shows rows; `--export rows.json|.csv`, edit, then `--import rows.json
  --dry-run` to see the diff and `--import rows.json --save` to apply (upsert; `--prune` deletes missing
  rows).
- Cooks and other long commands: `ue job cook` or `ue job start -- <cmd>`, then `ue job wait` (errors only)
  or `ue job status`. Do not block a shell on them.

## Automated tests
- `ue test <filter>` runs automation tests in the live editor and prints one summary line plus each
  failure with its source line; exit code 1 if any failed. Filter syntax is Epic's: a substring,
  `^Prefix`, `Suffix$`, `StartsWith:X`, `Group:Name`, joined with `+`. Never run without a filter (the
  engine has ~9k tests). Anchor project filters (`^MyGame.`): a bare substring also matches engine
  tests, and some of those change the open level. `ue test --list <substring>` lists names; `-v` adds
  warnings.
- New tests compiled with `ue build` are picked up automatically.

## Play testing
- `ue pie start` / `ue pie stop` / `ue pie status`. Status explains a session that died (fatal error
  plus your source line).
- Drive the test from Python: in PIE, `w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()`,
  then `unreal.GameplayStatics.get_player_pawn(w, 0)`, then `set_actor_location(loc, sweep=False, teleport=True)`.
  Overlaps fire on teleport. Timers need real time, so wait between calls rather than inside one.
- Verify through a custom log category (`ue log --cat LogMyFeature --level Display`). Logs are flushed
  before each read, so lines from the last action are always there.

## Output and exit codes
Output is compact text, capped at 30 rows (`--limit N`; `--json` for raw). Exit codes: 0 ok, 1 tool
error (one-line message), 2 editor unavailable, 3 restart required, 4 timeout.
- On UE 5.4–5.7 (`ue status` says "via python remote execution"), only `py`, `exec`, `actors`, `props`,
  `pie`, `build`, `log` and `call tether.*` work; for property writes use `ue py` with
  `set_editor_property` inside `unreal.ScopedEditorTransaction`.
- Exit 4 with `blocked by modal: "<title>"` means a dialog is open in a windowed editor and the editor
  cannot answer until someone closes it. Ask the user to deal with it; do not retry in a loop.
- `unauthorized` from `py`, `exec` or `set` means the call bypassed `ue` (the session token in
  `Saved/Tether/session.json` is missing). Use the `ue` commands, or `ue reload` after updating Tether.
- With the plugin's hooks, editor status arrives at session start, and each C++/C# edit gets a one-line
  note saying whether `ue build` (Live Coding) or `ue build --restart` will be needed.

## Python gotchas (UE 5.8)
- `unreal.Rotator(roll, pitch, yaw)` positional order: always pass keywords.
- `set_editor_property` names drop the bool `b` prefix (`bLocked` -> `locked`); JSON property names
  from Epic tools keep it, in camelCase.
- Wrap edits in `with unreal.ScopedEditorTransaction("Agent: ...")` so the user can undo them.
- Tool calls run on the game thread. Keep each call short and split long jobs into steps.
