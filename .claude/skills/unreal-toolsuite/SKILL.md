---
name: unreal-toolsuite
description: Drive a running Unreal Editor (UE 5.8+) through the `ue` CLI. Use it to query actors and properties, run editor Python and console commands, call Epic's ~870 editor tools, and read logs, all without headless commandlets or editor restarts. Use when working in an Unreal project whose editor runs with the ToolSuite plugin and Epic's ModelContextProtocol server.
---

# Unreal via `ue`

The editor is already running. Talk to it; do not boot it again. Each `ue` call takes tens of
milliseconds. A `-run=pythonscript` commandlet takes ~25 s and floods the log.

## First moves
- `ue status`: is the editor up, which map, PIE on/off, heartbeat. Exit code 2 = not available.
- `ue call toolsuite.level_summary`: what is in the level (counts by class).
- `ue actors <regex>`: label, class, location. `ue props <Actor|Actor.Component|/Game/Path> [regex]`:
  names, types and **values**. Use these instead of guessing property names.

## Doing things
- `ue py "<code>"`, `ue py -f script.py` or `ue py -` (stdin): unrestricted editor Python. `unreal` is
  pre-imported, the namespace persists between calls, and a trailing expression is printed.
- `ue exec "<console command>" [--pie]`: runs the command and prints the log lines it produced.
- `ue tools [query]` lists Epic toolsets; `ue tools <toolset>` gives one-line signatures;
  `ue tools <toolset>:<tool>` gives one schema. **Never** dump a whole toolset schema: one Niagara toolset is ~77k tokens.
- `ue call <toolset>.<tool> key=value ...`: required blank strings/arrays are auto-filled, and object
  params take a plain path. Examples: `ue call scene.find_actors tag=Enemy`,
  `ue call blueprint.read_graph_dsl graph=...`, `ue call datatable.set_rows ...` (upserts rows; never use
  `fill_data_table_from_json_string`, which wipes every row).
- `ue log [--level Warning|Display|Log] [--cat X] [--grep re]`: only entries that are new since the last
  `ue log`, de-duplicated. Run it after anything that might have warned or errored.
- `ue reload`: hot-reloads `Plugins/ToolSuite/Content/Python/toolsuite` after you edit or add a tool.
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
  If the reload fails, PIE does not start.
- A syntax error is rejected at save time. `ue build` and `ue pie start` report it as "UnrealSharp
  rejected the saved files", with the line. Fix the line, then rerun the command.
- After structural changes to a C# **subsystem** (new members or signatures), test PIE straight away.
  If `ue pie start` reports `InvalidCastException`, run `ue build --restart` (about 20 s).
- `ue call toolsuite.cs_state` reports idle, pending or reloading.

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

## Python gotchas (UE 5.8)
- `unreal.Rotator(roll, pitch, yaw)` positional order: always pass keywords.
- `set_editor_property` names drop the bool `b` prefix (`bLocked` -> `locked`); JSON property names
  from Epic tools keep it, in camelCase.
- Wrap edits in `with unreal.ScopedEditorTransaction("Agent: ...")` so the user can undo them.
- Tool calls run on the game thread. Keep each call short and split long jobs into steps.
