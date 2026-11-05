# Scripting the editor: ue py, ue set, ue call, logs, Python gotchas

## ue py

- `ue py "<code>"`, `ue py -f script.py [args]` or `ue py -` (stdin): unrestricted editor Python. `unreal` is
  pre-imported, the namespace persists between calls, and a trailing expression is printed. Words after the
  script file arrive as `sys.argv[1:]`. A call that times out (default 120 s) leaves the script running in the
  editor: do not run it again; `ue wait idle` returns when it has finished (or pass `--timeout 600`).
  A save that failed during the call (a read-only file: git-lfs leaves lockable `.uasset`/`.umap` files read-only
  after a commit) prints `save failed:` lines and exits 1; the work is still in the editor: `ue save --writable`.
  While PIE runs, `-f` scripts and editor-world code (EditorActorSubsystem, spawns, saves) are refused: they act on
  the edited level and spawns return None, so a generator stage can save a broken map (`ue pie stop`, or
  `--allow-pie`). `-f` also refuses when another session edited the script or a module it imports in the last
  15 min (exit 5; `--include-others`). To test your own edits to shared generator scripts in the shared editor,
  stage copies laid out like the project in Saved/Tether/staging/<session>/ (e.g. Scripts/plan.py) and run
  `ue py -f Scripts/gen.py --overlay -- args`: your copies win for that run only, `importlib.reload` included.
- When `ue py` prints `engine: ...` lines, that is why a call returned None (e.g. Editor Scripting refusing in PIE).

## ue set, ue changes and undo

- `ue set <target> name=value [name+=delta ...]`: write properties as one undoable transaction, and
  print `old -> new` for each. Values: numbers, true/false, enum names, object paths, JSON, or tuples
  for structs (`location+=0,0,200`, `lightColor=1,0.5,0.2`; missing trailing fields keep their values).
  Actors also take `location`, `rotation` (pitch,yaw,roll) and `scale`. If one value fails, nothing
  changes. `--pie` edits the running game instead (not undoable, not saved). Prefer this over `ue py`
  for property edits. A value already set is skipped; `--force` sets it anyway, so the change notification
  fires (a Blueprint's construction script reruns). `ue rerun <actor>` (or `tether.rerun(actor)` in a script)
  reruns construction scripts: 5.8's Python has no rerun_construction_scripts, and set_editor_property from
  Python (or a value set without a notification) leaves a Blueprint like Ultra Dynamic Sky unapplied.
- Python setters on components (spline points, set_world_location, instances) change them in memory without
  Modify(): `ue changes` does not see it, yet anyone's next level save writes it. Call `comp.modify()` first
  (`ue py` notes it). `ue changes` reports actors deleted and spawned again with the same label and class (a
  generator's rebuild) as "rebuilt", and takes a new baseline when a map is opened again (`--discard`).
- Your edits stay unsaved until saved. Before finishing a task that changed content, run `ue changes` to
  review what you changed, then `ue save` (or tell the user what is unsaved and why), then `ue validate`:
  data validation on the changed and unsaved assets plus Map Check of the open level, one line per
  problem (`ue validate /Game/Some/Folder` for a folder). Fix errors it reports before calling the task done.
  Without git, it takes the assets saved since the editor started instead of the changed ones.
- Took a wrong step? `ue undo --editor` (or `ue undo --editor 3`) reverts the editor's last edits of any kind and
  names each; `ue redo` puts them back. Plain `ue undo` only covers `ue set`.
- `ue undo` reverts your last `set`/`spawn`/`duplicate`/`delete` (it refuses if anything else changed since). `ue props X --export f.json`
  then `ue set X --from f.json` round-trips any object or asset, e.g. a DataAsset.

## Console, Epic tools, API lookups, logs

- `ue watch '<log regex>'` or `ue watch --prop <target> <name> --until <regex>` waits for something to happen.
- `ue exec "<console command>" [--pie]`: runs the command and prints the log lines it produced.
- `ue tools [query]` lists Epic toolsets; `ue tools <toolset>` gives one-line signatures;
  `ue tools <toolset>:<tool>` gives one schema. **Never** dump a whole toolset schema: one Niagara toolset is ~77k tokens.
- `ue call <toolset>.<tool> key=value ...`: required blank strings/arrays are auto-filled, and object
  params take a plain path. Examples: `ue call scene.find_actors tag=Enemy`,
  `ue call blueprint.read_graph_dsl graph=...`, `ue call datatable.set_rows ...` (upserts rows; never use
  `fill_data_table_from_json_string`, which wipes every row).
- `ue api <Class> [regex]` before writing editor Python: real method signatures and property names in a
  few lines (`ue api Actor.set_actor_location` for one member). Cheaper than guessing.
- `ue log [--level Warning|Display|Log] [--cat X] [--grep re]`: only entries that are new since the last
  `ue log`, de-duplicated. Run it after anything that might have warned or errored.
- `ue log --since pie` reads from the last PIE start (`--since 2m` for the last two minutes); `--grep` searches
  every line (Verbose included) unless `--level` is given, and keeps the lines in order.
- `ue log --summary`: the whole session's warnings and errors grouped by message shape with counts. Noise the
  project knows about goes in Config/Tether/log-ignore.json (`ue log --ignore '^LogX: known thing' --why ...`);
  ue log, pie and ci then hide it and say how many lines they hid (`--all` shows them). Add an entry only for
  noise the user agrees is noise, never to make a real error go away.
- `ue reload`: hot-reloads `Plugins/Tether/Content/Python/tether` after you edit or add a tool.
  To make a throwaway inspection script reusable, add it there as a `@toolset_registry.tool_call`.

## Engine traps

- Engine traps worth knowing (`ue py` warns about the first two):
  - `StaticMesh.build_from_static_mesh_descriptions` on a mesh that already has render data crashes the editor;
    build into a new mesh instead.
  - Iterating an `unreal.Array` of structs gives copies: edit `arr[i]`, assign it back, then write the array back
    (a skeletal mesh's material slots silently stay None otherwise).
  - Interchange stacks Unity-style `Name_LOD0..2` meshes into one LOD (`ue import` warns); it wants `LODxx_Name`
    or an FBX LOD group.
  - `UMovementComponent::bTickBeforeOwner` (on by default) plus `AddTickPrerequisiteActor(owner)` makes a tick
    cycle warning.
  - A `UDeveloperSettings` subclass without `BlueprintType` has no Python type, but
    `unreal.find_object(None, '/Script/Module.Default__X')` still reaches its defaults.

## Python gotchas (UE 5.8)

- A UFUNCTION that is CallInEditor but not BlueprintCallable has no Python name (`obj.rebuild_junction()` raises
  AttributeError): `obj.call_method('RebuildJunction')`; `ue py` says so when it sees that error. The same for
  Actor.AddComponentByClass: `actor.call_method('AddComponentByClass', (cls, False, unreal.Transform(), False))`.
- During PIE, Editor Scripting (EditorActorSubsystem, EditorLevelLibrary) acts on the edited level, not the
  game: `ue pie run -c` for the game world (`ue py` refuses such code during PIE; `ue who` and `ue pie status`
  name the command that started PIE and any shell still running above it, such as a stray test loop).
- Material Parameter Collections: never rewrite `scalar_parameters`/`vector_parameters` with new structs. Each
  parameter gets a new ID and every material's Collection Parameter node keeps the old one, reading 0 with no
  warning. Edit values in place; `ue lint --mpc` finds stale nodes and `--fix` re-links them by name.
  `CollectionScalarParameter.parameter_name` raises: use `get_editor_property('parameter_name')`.
- `unreal.MaterialProperty` has no `MP_CUSTOM_DATA0/1`: clear coat needs a Make Material Attributes node and
  `use_material_attributes`. A FontSampleParameter compiles only with a default `font` set.
- GeometryScript `create_new_static_mesh_asset_from_mesh` puts every triangle in one section whatever the
  material IDs; `copy_mesh_to_static_mesh` with `replace_materials` splits them (`ue mesh check` flags it).
- `player`, `pawn` and `controller` in `ue props`, `pie watch` and `pie do` mean the PIE player's (the pawn
  possessed now; a watch follows it when it changes), never an editor actor named "Player...".
- `unreal.Rotator(roll, pitch, yaw)` positional order: always pass keywords.
- `set_editor_property` names drop the bool `b` prefix (`bLocked` -> `locked`); JSON property names
  from Epic tools keep it, in camelCase. Blueprint variables with spaces resolve only by their own name:
  `get_editor_property('Time of Day')`. Fields Python cannot set at all (uint32, e.g. a grass variety's
  InstanceWorldPositionOffsetDisableDistance) go through `ue set Asset "GrassVarieties[0].Field=4000"`.
- Ultra Dynamic Sky copies "Exposure Brightness Range" into its PostProcessComponent only at startup: write the
  component, not the variable.
- `set_collision_enabled(NO_COLLISION)` from editor Python is undone by the next `set_editor_property` on the same
  component (PostEditChange re-applies its collision profile): set the profile (`set_collision_profile_name(
  'NoCollision')`) instead, then check with `ue world probe x,y,z`.
- Components marked editor-only (`bIsEditorOnly`) exist in PIE but are null in `-game` and packaged runs: null-check
  them outside `WITH_EDITOR` code, and try a `ue game start --server none` run before a package.
- Wrap edits in `with unreal.ScopedEditorTransaction("Agent: ...")` so the user can undo them.
- Tool calls run on the game thread. Keep each call short and split long jobs into steps.

## Output, older engines and hooks

- `--json` on any command prints one JSON document: `{command, ok, exit, error, lines, data}`; `data` is
  structured where it helps (status, doctor, validate, audio, usages, size, ui, perf, overview), and
  `ue call`/`test`/`selftest` print their own JSON. Use it when a script reads the result.
- On UE 5.4–5.7 (`ue status` says "via python remote execution"), Epic's toolsets are missing: no `shot`,
  `test`, `bp`, `table`, `diff`, `config`, `perf` or `ue call` of Epic tools. `ue set` works (undo there
  covers `ue set` only); `ue selftest` lists what is available.
- `unauthorized` from `py`, `exec` or `set` means the call bypassed `ue` (the session token in
  `Saved/Tether/session.json` is missing). Use the `ue` commands, or `ue reload` after updating Tether.
- With the plugin's hooks, editor status arrives at session start, and each C++/C# edit gets a one-line
  note saying whether `ue build` (Live Coding) or `ue build --restart` will be needed.
