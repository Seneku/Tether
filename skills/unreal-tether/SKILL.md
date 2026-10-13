---
name: unreal-tether
description: Drive a running Unreal Editor (UE 5.8+) through the `ue` CLI. Use it to query and set actor and asset properties, take viewport screenshots, run automation tests, editor Python and console commands, call Epic's ~830 editor tools, build C++/C# without restarts, and read logs. Use when working in an Unreal project whose editor runs with the Tether plugin and Epic's ModelContextProtocol server.
---

# Unreal via `ue`

Run `ue` from the project folder (or set `UE_PROJECT`). The Tether Claude Code plugin puts it on PATH;
if `ue` is not found, use `python "${CLAUDE_PLUGIN_ROOT}/cli/ue.py"` instead. If `ue status` exits 2,
start the editor with `ue open --headless` (`--render` if you need `ue shot`).

Several reads or edits in a row? Put them in one `ue batch` (one command per line, from stdin or a file):
one process and one editor session for all of them.

Once the editor is running, talk to it; do not boot it again. Each `ue` call takes tens of
milliseconds. A `-run=pythonscript` commandlet takes ~25 s and floods the log.

If something behaves unexpectedly, `ue doctor` checks the setup (engine, plugins, editor, Live Coding lock,
.NET, LFS) in a second, and `ue selftest` checks every capability against the editor and names what is broken.

## First moves
- New to the project? `ue overview` (key classes, maps, input, content) before exploring files by hand.
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
- Your edits stay unsaved until saved. Before finishing a task that changed content, run `ue changes` to
  review what you changed, then `ue save` (or tell the user what is unsaved and why), then `ue validate`:
  data validation on the changed and unsaved assets plus Map Check of the open level, one line per
  problem (`ue validate /Game/Some/Folder` for a folder). Fix errors it reports before calling the task done.
- In a Perforce (or other check-out) workspace, `ue save`/`mv`/`rm` check files out and mark new ones for add
  themselves; if one is checked out by someone else, or read-only without a connection, it says so and
  `ue save` exits 1: tell the user rather than making files writable yourself.
- New assets: `ue new bp /Game/X/BP_Door --parent Actor` (or a C++ class, or another Blueprint), `ue new mi
  /Game/X/MI_Rock --parent /Game/X/M_Rock`, `ue new datatable /Game/X/DT_Items --parent ItemRow`, `ue new level
  /Game/Maps/L_Test`, `ue new folder /Game/X`. Then `ue save` (or pass --save).
- Content conventions: `ue lint [/Game/Folder]` checks naming prefixes (SM_, T_, BP_, ...), textures (power of
  two, size, normal maps compressed as such, masks without sRGB) and meshes (simple collision, LODs or Nanite)
  in under a second, from registry data. Project rules go in Config/Tether/lint.json (prefixes, ignore globs,
  disable, max_texture_size, lod_triangles). Run it on folders you added to.
- Cleaning up content: `ue unused [/Game/Folder]` lists assets nothing reaches from the maps, the config, the
  code and the cook settings, biggest first. Confirm with `ue usages <asset>` before `ue rm` (paths built at
  runtime are invisible to it). `ue unused --redirectors --fix` repoints references and deletes redirectors.
- Files into the project (from Blender, a generator, a download): `ue import Art/Crate.fbx Art/Textures /Game/Props
  --collision box --lods 3 --save` (meshes, textures, sounds; folders keep their subfolders; no dialogs; not
  while PIE runs). Names ending in _ORM/_Mask/_AO/... become linear mask textures. After the source files
  change: `ue import --reimport /Game/Props` reimports the changed ones and re-applies those options.
- On terrain, never guess heights: `ue spawn /Game/X/SM_Rock --at 1200,-300,0 --ground` (or `ue drop Rock1 Rock2`,
  `--align` to follow the slope) puts things on the surface below.
- Many props at once: `ue scatter /Game/X/SM_Rock --count 30 --area 1200,-300,2000 --spacing 250 --scale 0.7,1.4
  --align` (one undo step; same `--seed`, same layout).
- Level edits: `ue spawn PointLight --at 0,0,300 --label Lamp` (or an asset path), `ue duplicate Lamp --count 3`,
  `ue delete Lamp`. Asset moves: `ue mv /Game/A/X /Game/B/` (then `ue save`), `ue rm /Game/A/X` (refuses while
  referenced; check `ue refs` first). Prefer these to raw `ue py`: they are undoable and checked.
- `ue undo` reverts your last `set`/`spawn`/`duplicate`/`delete` (it refuses if anything else changed since). `ue props X --export f.json`
  then `ue set X --from f.json` round-trips any object or asset, e.g. a DataAsset.
- `ue watch '<log regex>'` or `ue watch --prop <target> <name> --until <regex>` waits for something to happen.
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
- `ue asset <path>` summarises an asset in a few lines (mesh triangles/LODs/Nanite, texture size and
  compression, material parameters...); use it before `ue props`, which lists every property.
- Materials: `ue mat /Game/Path/MI_X` lists parameters with values (and which are overridden);
  `ue mat MI_X Roughness=0.3 Tint=1,0,0 UseDetail=true` sets them (undoable); `ue mat <actor>` shows which
  material is on each slot; `ue mat M_X --compile` lists shader compile errors. Prefer it over `ue props`
  on materials, which shows parameter arrays raw.
- Settings: `ue config` lists sections; `ue config maps` / `ue config rendering lumen` reads one;
  `ue config maps gameDefaultMap=/Game/Maps/L` sets and saves it (to Config/Default*.ini for project settings).
  Prefer it to editing .ini files by hand: names and values are checked, and the editor applies them live.
- Budgets: `ue size [folder]` (biggest assets on disk), `ue size /Game/Maps/L --deps` (what a level pulls in,
  by folder), `ue mem` (what the editor or PIE holds in memory, by class and mesh; ~20 s).
- `ue diff /Game/Path/Asset [rev]` shows what changed in a binary asset since a git revision (default HEAD,
  compared with the editor's current version) or between two (`--to rev2`): Blueprint variables, components,
  defaults and graph text, DataTable rows, map actors. Use it to review your own asset edits before
  committing, and to understand someone else's commit (`ue diff X <sha>~1 --to <sha>`).
- `ue find <regex> [--class StaticMesh] [--under /Game/x]` to locate assets; `ue refs <asset>` before deleting
  or renaming one (what uses it), `--deps` for what it uses.
- `ue api <Class> [regex]` before writing editor Python: real method signatures and property names in a
  few lines (`ue api Actor.set_actor_location` for one member). Cheaper than guessing.
- `ue bp /Game/Path/BP_X [graph]` prints Blueprint graphs as text. To change one: write the edited text to
  a file, `ue bp /Game/Path/BP_X EventGraph --apply file.dsl --save` (compiles, prints the diff).
  `ue bp ... --diff` shows only what changed since the last read; `ue bp --docs` is the syntax reference.
- Before renaming or removing C++ functions, properties or classes, `ue usages <Name>` lists the C++ lines and
  the Blueprints (with their graph lines) that use them.
- Build a Blueprint's shape in one-liners: `ue bp edit /Game/X/BP_Door add-var IsOpen bool false
  --instance-editable`, `... add-component Mesh StaticMesh StaticMesh=/Game/X/SM_Door`, `... add-function Open`,
  `... set-default IsOpen=true`; then write graph bodies with `ue bp ... --apply`.
- After renaming or removing C++ functions, properties or classes (and `ue build --restart`), run
  `ue bp check` (or `ue bp check /Game/Some/Folder`): it lists only Blueprints that no longer compile, with why.
- `ue table /Game/Path/DT_X` shows rows; `--export rows.json|.csv`, edit, then `--import rows.json
  --dry-run` to see the diff and `--import rows.json --save` to apply (upsert; `--prune` deletes missing
  rows).
- Does the shipped game boot? `ue package` (minutes to an hour: it is a background job it waits on) packages,
  then runs the packaged game headless for 20 s and reports crashes and errors; `--boot-only` re-tests the last
  package.
- Cooks and other long commands: `ue job cook` or `ue job start -- <cmd>`, then `ue job wait` (errors only)
  or `ue job status`. Do not block a shell on them.

## Visual checks
- To see what an asset looks like before using or changing it: `ue look /Game/Path/SM_Rock` (mesh, Blueprint or
  material) writes one PNG with six views; open it only if you need to.
- Before a change that should not alter a view (refactors, optimisations), `ue shot --at ... --look-at ... --baseline
  NAME`; after it, `ue shot --compare NAME` reports what changed and where (diff image in red). Animated things
  (clouds, water, particles) show up as separate areas; judge by the largest.

## Automated tests
- `ue test <filter>` runs automation tests in the live editor and prints one summary line plus each
  failure with its source line; exit code 1 if any failed. Filter syntax is Epic's: a substring,
  `^Prefix`, `Suffix$`, `StartsWith:X`, `Group:Name`, joined with `+`. Never run without a filter (the
  engine has ~9k tests). Anchor project filters (`^MyGame.`): a bare substring also matches engine
  tests, and some of those change the open level. `ue test --list <substring>` lists names; `-v` adds
  warnings.
- New tests compiled with `ue build` are picked up automatically.

## Play testing
- Performance: `ue perf` before and after a change; each run prints the difference from the previous one.
  `ue perf --trace` adds which scopes (functions, Blueprint events, render passes) take the time per thread.
  Stutter while moving through a level: `ue perf load [/Game/Maps/L] -f tests/play/route.txt` lists hitches
  with the packages that finished loading in each (and sync-load flushes), plus World Partition streaming time.
  Run it on a freshly opened editor: packages already in memory do not load again.
  When it says the frame rate is capped, compare the per-frame game/render/GPU work, not the fps.
- To play the game like a player would: `ue pie do "at ItemPickup_Key -150,0,100" "look ItemPickup_Key"
  "tap InteractAction" "until player.Inventory.Items ~ Key within 2" "gone ItemPickup_Key"` (or `-f steps.txt`).
  Steps: at, look, move, press/tap/hold/release (Enhanced Input actions: asset names, or actions the
  character creates in C++), call, expect/until (`= != < > <= >= ~`), log, exists/gone, wait, exec, shot.
  Each step prints ok/FAIL; it stops at the first failure. Write the step file once and rerun it after changes.
  Keep step files in `tests/play/` with a `# map: /Game/...` line: `ue ci --play` runs them all as regression tests.
- To turn the user's own play-through into a test: ask them to run `ue pie record -o tests/play/<name>.txt` in a
  windowed editor and play (it stops when they stop PIE). The file replays with `ue pie do -f`: keys become
  `tap`/`hold`/`release` of their input actions, mouse look becomes `look` steps, ending with a `near` check.
  Add `expect` steps for what should have happened.
- To see how values change during play (instead of adding print nodes): `ue pie watch player.Health
  BP_Enemy_C_1.State BP_Enemy_C_1 --seconds 10` (an actor alone = its location; `-f steps.txt` plays a step file
  while watching). Prints each value's range, path or changes with times; the full samples go to a CSV.
- Which input actions exist and what keys reach them: `ue input` (by mapping context, and who adds each context);
  in PIE it also says which contexts are active. `ue input Move` shows matching actions only. A `pie do` step
  that presses an action no active context maps still works, but warns: a player could not do it.
- Game UI in PIE: `ue ui` lists what is on screen (Slate and UMG texts, buttons, text boxes); `ue ui click
  "Close"`, `ue ui type "Name" hello`, `ue ui key Escape`. In `ue pie do`: `click <label>`, `expect-ui <text>`,
  `ui-gone <text>`. Clicks are real mouse clicks at the widget, so whatever is on top receives them; labels are
  stable, refs (b12) change between reads. Canvas-drawn HUD text is not a widget: check it with `ue shot`.
- Prefer `ue pie run check.py --expect '<log regex>' --reject 'Error|no subsystem'` for verifying gameplay:
  it starts PIE, runs the script in the PIE world, waits for the log lines, stops PIE and exits 0/1.
  Write the script once and rerun it after every change.
- Animation in PIE: `ue anim <actor|player>` (anim BP and its variables, montage, curves, movement mode and
  speed). Python cannot read state machine states; the variables the transitions use usually tell you.
- Audio in PIE: `ue audio` lists what is playing (needs `ue open --headless --sound`, which stays silent);
  `expect-sound <regex>` as a `ue pie do` step checks a sound fires.
- AI in PIE: `ue ai` (one line per AI), `ue ai <pawn>` (movement, focus, perception, brain, blackboard, and the
  project's getters). State a C++ class keeps in plain members is invisible: suggest exposing it as a
  `UFUNCTION(BlueprintPure)` getter.
- No navmesh in the editor (headless editors do not build it by themselves): `ue world build nav` rebuilds it
  and says what it changed. `ue world build lighting|hlod` for lightmaps and World Partition HLODs.
- AI that does not move: `ue nav` (is there a navmesh, and if not why), `ue nav check` (spawn/patrol points
  off the navmesh), `ue nav path <a> <b>` (reachable? how long?); add `--pie` for runtime-generated navmeshes.
- Leaks and rare failures: `ue soak --minutes 10 -f tests/play/x.txt` loops a step file (or wanders) and
  reports the memory trend, frame rate and errors. Run it in the background (it blocks for the duration).
- Multiplayer: `ue pie start --clients 2` (listen server; `--net client` for a dedicated one), `ue pie status`
  for the worlds, `ue pie target 1` to make `--pie` reads, `pie do` and `ai` act on client 1 (0 = server).
  Compare a value on the server and a client to check replication.
- Save games: `ue savegame` lists slots, `ue savegame <slot> [regex]` shows what is saved; `ue savegame <slot>
  Name=value` edits it (a .bak is kept) to set up a play test. Ask before editing a user's real save.
- `ue pie start` / `ue pie stop` / `ue pie status`. Status explains a session that died (fatal error
  plus your source line).
- Drive the test from Python: in PIE, `w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()`,
  then `unreal.GameplayStatics.get_player_pawn(w, 0)`, then `set_actor_location(loc, sweep=False, teleport=True)`.
  Overlaps fire on teleport. Timers need real time, so wait between calls rather than inside one.
- Verify through a custom log category (`ue log --cat LogMyFeature --level Display`). Logs are flushed
  before each read, so lines from the last action are always there.

## Output and exit codes
- If the editor dies (`ue status` says "crashed", or a call reports "the editor crashed"), run `ue crash`
  before restarting: it shows the error, whether your modules were on the stack, and what ran last.
Output is compact text, capped at 30 rows (`--limit N`). Exit codes: 0 ok, 1 tool
error (one-line message), 2 editor unavailable, 3 restart required, 4 timeout.
- `--json` on any command prints one JSON document: `{command, ok, exit, error, lines, data}`; `data` is
  structured where it helps (status, doctor, validate, audio, usages, size, ui, perf, overview), and
  `ue call`/`test`/`selftest` print their own JSON. Use it when a script reads the result.
- On UE 5.4–5.7 (`ue status` says "via python remote execution"), Epic's toolsets are missing: no `shot`,
  `test`, `bp`, `table`, `diff`, `config`, `perf` or `ue call` of Epic tools. `ue set` works (undo there
  covers `ue set` only); `ue selftest` lists what is available.
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
