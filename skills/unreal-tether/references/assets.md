# Assets and content: new projects, import, lint, Fab, materials, data, diffs

## New projects and new assets

Starting from nothing: `ue new project Folder/Name --cpp [--template VehicleAdv]` makes a project from an engine
template (Build.cs ready for 5.8, a game mode, the redirects that keep the template's Blueprints on its renamed
C++ classes) and runs `ue init`; `ue template add Vehicles` copies template content (cars, LevelPrototyping,
mannequins) to the /Game path its packages expect. `ue init --claude-plugin` makes later sessions in that folder
load Tether (no plugin there: no `ue` on PATH, no edit hooks).

- New assets: `ue new bp /Game/X/BP_Door --parent Actor` (or a C++ class, or another Blueprint), `ue new mi
  /Game/X/MI_Rock --parent /Game/X/M_Rock`, `ue new datatable /Game/X/DT_Items --parent ItemRow`, `ue new level
  /Game/Maps/L_Test`, `ue new folder /Game/X`. Then `ue save` (or pass --save).

## Finding and sizing

- `ue asset <path>` summarises an asset in a few lines (mesh triangles/LODs/Nanite, texture size and
  compression, material parameters...); use it before `ue props`, which lists every property.
- `ue find <regex> [--class StaticMesh] [--under /Game/x]` (or a path: `ue find "/Game/X/Presets/*"`) to locate assets; `ue refs <asset>` before deleting
  or renaming one (what uses it), `--deps` for what it uses.
- Budgets: `ue size [folder]` (biggest assets on disk), `ue size /Game/Maps/L --deps` (what a level pulls in,
  by folder), `ue mem` (what the editor or PIE holds in memory, by class and mesh; ~20 s).

## Conventions and clean-up

- Content conventions: `ue lint [/Game/Folder]` checks naming prefixes (SM_, T_, BP_, ...), textures (power of
  two, size, normal maps compressed as such, masks without sRGB) and meshes (simple collision, LODs or Nanite)
  in under a second, from registry data. Project rules go in Config/Tether/lint.json (prefixes, ignore globs,
  disable, max_texture_size, lod_triangles). Run it on folders you added to; `ue lint <folder> --fix --dry-run`
  then `--fix --save` corrects texture settings and missing collision (renames: `--rule naming --fix`; heavy
  meshes without LODs: `--rule mesh-lods --fix`, Nanite on, or `--lod-fix lods`). A pack at the content root:
  `ue move-pack /Game/CityPack` moves it to /Game/ThirdParty/CityPack with references fixed.
- Cleaning up content: `ue unused [/Game/Folder]` lists assets nothing reaches from the maps, the config, the
  code and the cook settings, biggest first. Confirm with `ue usages <asset>` before `ue rm` (paths built at
  runtime are invisible to it). `ue unused --redirectors --fix` repoints references and deletes redirectors.
- One setting on many assets: `ue set --assets /Game/Env/Textures --class Texture2D LODGroup=TEXTUREGROUP_World
  --dry-run`, then without --dry-run (and --save); `--match "*_ORM"` narrows by name.

## Fab

- Needs an asset (a rock, a tree, a sky, a character)? Use what the user already has first: `ue fab library rock`
  lists downloaded Fab packs and Fab content in their other projects; `ue fab add "<name>" [--from <project>]
  --dry-run`, then without --dry-run, copies it in. Fab marks each listing "Allows usage with AI: Yes/No" and
  the vault does not record it: `fab add` prints the listing link; have the user check it before an AI-driven
  project uses the pack. `ue fab remove "<name>"` deletes one again (through the editor, which lets go of the
  files; refuses while other assets reference it). `ue fab` shows what this project already has from Fab. If
  nothing fits, suggest listings with `ue fab open "<search>"` (it prints a fab.com link) and let the user decide:
  claiming, buying and downloading are always theirs. Never click, type or press keys in the Fab tab or the store
  page, by any route (Tether refuses while a Fab tab is open; ask the user to close it if a step needs input).

## Importing and checking meshes, skeletons, sounds

- Files into the project (from Blender, a generator, a download): `ue import Art/Crate.fbx Art/Textures /Game/Props
  --collision box --lods 3 --save` (meshes, textures, sounds; folders keep their subfolders; no dialogs; not
  while PIE runs). Names ending in _ORM/_Mask/_AO/... become linear mask textures. After the source files
  change: `ue import --reimport /Game/Props` reimports the changed ones and re-applies those options.
- After importing a mesh: `ue mesh check /Game/Props/SM_Crate --height 80` (the size you expect, in cm) catches
  the wrong unit (x100), a mesh lying on its side, an off pivot, missing lightmap UVs or collision, and faces
  pointing inward. `ue skel <SK_ or skeleton>` shows a character's bone tree, sockets, physics bodies and rigs:
  read it before naming a socket or bone in code. `ue sound <S_asset>` says whether a sound is stereo (no 3D
  position), attenuated, in a sound class; `ue audio mix` shows the class tree and mixes. When sounds are driven
  from data or code, `ue sound <DA_Sounds|/Game/Audio>` lists every wave they use (stereo ones first) and
  `--stats` adds loudness (peak, RMS, loudest 50 ms, per-folder medians) for levelling and picking sounds.

## Materials

- Materials: `ue mat /Game/Path/MI_X` lists parameters with values (and which are overridden);
  `ue mat MI_X Roughness=0.3 Tint=1,0,0 UseDetail=true` sets them (undoable); `ue mat <actor>` shows which
  material is on each slot; `ue mat M_X --compile` lists shader compile errors. Prefer it over `ue props`
  on materials, which shows parameter arrays raw. On a base material (M_, not MI_) it changes the parameter's
  saved default, which every instance without an override sees and a generator rebuilding the material can
  keep: it prints the command that puts the old value back; run it before saving a temporary debug value.
- Materials as text: `ue mat graph /Game/X/M_Rock` (outputs, nodes, wiring). Edit in small steps:
  `ue mat graph M add ScalarParameter Wetness 0.2`, `... connect Wetness Lerp.Alpha`, `... connect Lerp BaseColor`,
  `... set #3 value=0.5`, `... delete #7`; each recompiles. Instances: `ue mat MI Name=value`. Nodes that reach no
  output: `ue mat graph M prune` (one undo step); `ue lint --materials` finds materials full of them.

## Data and settings

- Data-driven content (enemy stats, weapon tuning): `ue data <ClassName|/Game/Folder>` compares every DataAsset
  or Blueprint subclass of a class side by side, only the properties that differ. To retune many at once:
  `--csv tuning.csv`, edit, `--import tuning.csv --dry-run`, then `--import tuning.csv --save`.
- `ue table /Game/Path/DT_X` shows rows; `--export rows.json|.csv`, edit, then `--import rows.json
  --dry-run` to see the diff and `--import rows.json --save` to apply (upsert; `--prune` deletes missing
  rows).
- Settings: `ue config` lists sections; `ue config maps` / `ue config rendering lumen` reads one;
  `ue config maps gameDefaultMap=/Game/Maps/L` sets and saves it (to Config/Default*.ini for project settings).
  Prefer it to editing .ini files by hand: names and values are checked, and the editor applies them live.

## Reviewing binary changes

- `ue diff /Game/Path/Asset [rev]` shows what changed in a binary asset since a git revision (default HEAD,
  compared with the editor's current version) or between two (`--to rev2`): Blueprint variables, components,
  defaults and graph text, DataTable rows, map actors. Use it to review your own asset edits before
  committing, and to understand someone else's commit (`ue diff X <sha>~1 --to <sha>`). Perforce revisions work
  too: `#have` (the default in a Perforce workspace), `#head`, `#3`, `@1234` (`ue diff X @1233 --to @1234`).
  World Partition actor files (`__ExternalActors__/...`) are refused: diff their map, which lists the changed
  actors. If `ue changes` warns that an actor belongs to a Tether diff copy, do not save that level; reopen it
  with `ue map open <map> --discard` (ask first: it drops the level's unsaved edits).
