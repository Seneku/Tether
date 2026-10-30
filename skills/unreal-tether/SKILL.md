---
name: unreal-tether
description: Drive a running Unreal Editor (UE 5.8+) through the `ue` CLI. Use it to query and set actor and asset properties, take viewport screenshots, run automation tests, editor Python and console commands, call Epic's ~830 editor tools, build C++/C# without restarts, and read logs. Use when working in an Unreal project whose editor runs with the Tether plugin and Epic's ModelContextProtocol server.
---

# Unreal via `ue`

Run `ue` from the project folder (or set `UE_PROJECT`). The Tether Claude Code plugin puts it on PATH;
if `ue` is not found, use `python "${CLAUDE_PLUGIN_ROOT}/cli/ue.py"` instead. If `ue status` exits 2,
start the editor with `ue open --headless` (`--render` if you need `ue shot`).

If the user has the project open in their own editor, `ue` works with that one (`ue status` says "opened by hand"):
do not start a second editor, and do not close or restart theirs (`ue build --restart` included) without asking;
the user may be working in it, so say before you save. With two editors open (`ue status` lists both), commands
go to the user's; `ue --pid <pid> ...` picks the other, and saving the same assets from both loses work.

Other Claude sessions may share this editor (`ue who` lists who ran what in the last 15 min, and who started PIE).
Before anything that takes the editor away from them (a map rebuild, a long PIE run, `ue build --restart`,
`ue close`), run `ue lease take --for "map rebuild + PIE"` and `ue lease release` when done; `ue status` shows the
lease to everyone. While another session holds it, `build --restart`, `close`, `pie start`, a UBT build with no
editor open, and anything that writes to disk (`save`, `import`, `mv`, `rm`, `--save`) refuse with exit 5 and say
who holds it and for what (edits in memory go ahead, with a note; `lease take` exits 5 too, so chain with `&&`, not
`;`): wait, or `ue lease request --for "why"` (the holder sees it on its next
command and runs `ue lease grant` when done), rather than passing `--ignore-lease`; `ue wait lease` then
returns once it is yours or free and PIE has stopped. Asked for yours? Grant it when you reach a stopping point.
A lease lasts at least 5 minutes past the end of its holder's last `ue` command, and is held while one of its
commands runs (a long `ue py` stage), so it does not run out mid-task; `status`, `who` and `lease` show "busy:
<command> for N min" for a command in flight (a busy editor, not a hung one). But while someone waits, a lease lapses
after 3 minutes with no `ue` command of yours and no PIE session of yours running: do the thinking and editing
before you take it, run the editor work as one script, and release it when done (`take --hold` only for work in the
editor that a hand-over would lose, e.g. unsaved edits you are still making). A test script that runs `ue` with its
output thrown away never sees requests: call `ue lease yield --back` between suites (hands the lease to a waiting
request, then waits to get it back; `ue lease pending` exits 0 when someone waits). A script you run inside your
own lease can take and release its own: its lease goes on top of yours, and its release gives yours back. Work that
needs another map or a throwaway import while others use the editor: `ue open --second` starts your own headless
editor next to the shared one (its own port; your commands go there, others' do not; no lease needed; no
pre-build, as the binaries are in use), and `ue close` closes it as soon as you are done: while it runs, the shared
editor cannot save the packages it loaded (Windows keeps their files open), so it refuses to start while the shared
one holds unsaved work. A restart or
close also asks for the lease when another session used the editor in the last 5 minutes. A build error in a file
you did not touch says who last edited it ("edited by spire ... (not you)"): leave it to that session.
`ue who --as NAME` gives your session a name the others see. Committing from a shared checkout: `ue git stage
--mine` stages only the files your session wrote last (Edit/Write, and files your Python scripts or `ue py`
builders wrote, read from the session transcripts), and names the ones another session also wrote since their
last commit (stage your hunks with `git add -p`), the ones it wrote, and the ones it cannot place.
Starting from nothing: `ue new project Folder/Name --cpp [--template VehicleAdv]` makes a project from an engine
template (Build.cs ready for 5.8, a game mode, the redirects that keep the template's Blueprints on its renamed
C++ classes) and runs `ue init`; `ue template add Vehicles` copies template content (cars, LevelPrototyping,
mannequins) to the /Game path its packages expect. `ue init --claude-plugin` makes later sessions in that folder
load Tether (no plugin there: no `ue` on PATH, no edit hooks).

With several editors open, each session keeps talking to the one it opened or used last (`--pid` changes
that), so an editor the user opens mid-session does not take your commands. A new session starts on the editor
the lease holder and the other sessions use; `ue status` alone does not pin one. Sub-agents (the Agent tool)
count apart here: each keeps its own editor, its own `ue who --as` name, and a `--second` editor one agent opens
is the default for that agent only (the lease still covers the whole session). Tether tells them apart by their
transcripts; for an `ue` call made from inside a script it cannot, so pass `--pid` there.

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
- In a Perforce (or other check-out) workspace, teammates share binary files that can't be merged. Before a
  task, `ue locks /Game/Area` shows who holds what there and what is behind the depot. Every Tether edit checks
  out the files it modified right away; an edit to a file someone else holds, or one behind the depot, is undone
  and fails naming them ("is checked out by bob. Nothing was changed"): tell the user, don't work around it.
  One command may check out at most 20 files (Config/Tether/vcs.json): above that it is refused; ask the user
  before rerunning with `--bulk`. `ue save`/`mv`/`rm` check out and mark adds/deletes themselves; if a file is
  read-only without a connection, `ue save` exits 1: tell the user rather than making files writable yourself.
- New assets: `ue new bp /Game/X/BP_Door --parent Actor` (or a C++ class, or another Blueprint), `ue new mi
  /Game/X/MI_Rock --parent /Game/X/M_Rock`, `ue new datatable /Game/X/DT_Items --parent ItemRow`, `ue new level
  /Game/Maps/L_Test`, `ue new folder /Game/X`. Then `ue save` (or pass --save).
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
- Needs an asset (a rock, a tree, a sky, a character)? Use what the user already has first: `ue fab library rock`
  lists downloaded Fab packs and Fab content in their other projects; `ue fab add "<name>" [--from <project>]
  --dry-run`, then without --dry-run, copies it in. Fab marks each listing "Allows usage with AI: Yes/No" and
  the vault does not record it: `fab add` prints the listing link; have the user check it before an AI-driven
  project uses the pack. `ue fab remove "<name>"` deletes one again (through the editor, which lets go of the
  files; refuses while other assets reference it). `ue fab` shows what this project already has from Fab. If
  nothing fits, suggest listings with `ue fab open "<search>"` (it prints a fab.com link) and let the user decide:
  claiming, buying and downloading are always theirs. Never click, type or press keys in the Fab tab or the store
  page, by any route (Tether refuses while a Fab tab is open; ask the user to close it if a step needs input).
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
- Data-driven content (enemy stats, weapon tuning): `ue data <ClassName|/Game/Folder>` compares every DataAsset
  or Blueprint subclass of a class side by side, only the properties that differ. To retune many at once:
  `--csv tuning.csv`, edit, `--import tuning.csv --dry-run`, then `--import tuning.csv --save`.
- Where things are in a level: `ue map` (or `ue map x,y,r`, `--pie` for the running game) draws it from above
  (works headless) with player starts, spawners, AI and triggers as numbered dots; read the PNG to plan placement.
  `ue map --footprints` writes the collidable geometry as data (each mesh's turned outline, top and bottom) for
  minimaps and layout checks. `ue map open /Game/Maps/X` switches the editor's map (`--save`/`--discard` when the
  current one has unsaved edits) and compares the level with its last good load: a WARNING (exit 1) means it came
  back broken (actors at the origin, meshes gone, empty splines), typically in an editor carrying many Live Coding
  patches. Don't save it from there (`ue save` refuses); `ue build --restart` and reopen.
- Why AI (or players) get stuck somewhere: `ue collide x,y,r --z 100` draws what blocks at that height around the
  point, coloured by class, from overlap tests: generated roads, junction walls and raised medians show up.
- On terrain, never guess heights: `ue spawn /Game/X/SM_Rock --at 1200,-300,0 --ground` (or `ue drop Rock1 Rock2`,
  `--align` to follow the slope) puts things on the surface below. A structure built of separate pieces (walls,
  roof) goes down as one: `ue drop Wall1 Wall2 Roof --group` (attached actors come along); it says how much the
  ground varies under it, so move it to flatter ground rather than letting a slope swallow one side.
- Materials as text: `ue mat graph /Game/X/M_Rock` (outputs, nodes, wiring). Edit in small steps:
  `ue mat graph M add ScalarParameter Wetness 0.2`, `... connect Wetness Lerp.Alpha`, `... connect Lerp BaseColor`,
  `... set #3 value=0.5`, `... delete #7`; each recompiles. Instances: `ue mat MI Name=value`. Nodes that reach no
  output: `ue mat graph M prune` (one undo step); `ue lint --materials` finds materials full of them.
- Lighting and mood: `ue lights` lists lights, sky, fog and exposure with notes (e.g. auto exposure undoing a dark
  scene); check it before and after lighting changes, together with `ue shot`. It counts lights inside
  Blueprints (Ultra Dynamic Sky's sun and moon) and lists every unbound post process with its priority: a sky
  Blueprint's own post process (priority 1) beats a level volume at 0, so an exposure change there does nothing.
  A mesh kilometres across (a 5 x 12 km water slab) gets no Lumen reflections: tile it (~1 km).
- World Partition levels: other commands only see loaded actors. `ue wp` says how many are loaded; `ue wp actors
  Door` finds unloaded ones; `ue wp load x,y,r` (or `ue wp load <regex>`) loads them before you work on them.
- After placing or scattering things: `ue level check` finds floating or buried props, stacked duplicates,
  invisible walls and overlapping stationary lights; `ue drop <actor>` fixes a floating one.
- Terrain: `ue landscape` (size, layers), `ue landscape at x,y` (height, slope, layer weights: use it before placing
  things), `ue landscape map` (a PNG of the shape). `ue landscape flatten x,y,r [--height z]` (a pad for a
  building), `raise x,y,r --by cm`, `smooth x,y,r`; each prints a backup for `ue landscape restore --backup`.
  Whole heightmaps: `ue landscape export h.png` / `import h.png` (16-bit, the landscape's exact vertex grid;
  `--flip-y` for north-up files) and paint layers: `ue landscape paint Rock --mask rock.png`. These need a
  rendering editor (`ue open --headless --render`); each prints the file that undoes it. Python cannot create a
  new landscape; if the project has its own C++ helper for that, use it.
- After opening a big level or editing materials, `ue wait compile` waits until shaders and assets have
  compiled, before screenshots, timings or a nav build. It says what it waits on every 30 s; in a rendering
  editor, whose frames keep its own CPU busy, it ends once the shader workers have been quiet for 60 s
  (`--workers-only` waits for the workers alone).
- `ue shot` sets its own view per capture: `ue exec "viewmode unlit"` does not carry over; use
  `ue shot --view unlit` (or wireframe, lightingonly, ...).
- Water: `ue water` lists zones and bodies and flags the usual mistakes (an ocean whose brush flattens the
  terrain, a coarse shoreline, a stock material on the distant water mesh).
- Multiplayer: `ue pie start --clients 2` (add `--game /Script/MyGame.MyGameMode` to try another game mode), then
  `ue pie target 1` to act on the first client. `ue exec --pie` and `pie do` exec steps run on that world's next
  tick, so RPCs replicate; a `call` step or your own Python calling a function directly runs any RPC locally
  (editor Python makes every RPC local). `ue pie run` scripts run in a fresh namespace each time: `tether.world()`, `player()`,
  `console("cmd")` follow the targeted world. A UWorldSubsystem (5.8's Python has no SubsystemBlueprintLibrary):
  `tether.world_subsystem(unreal.MySubsystem)` in `ue py` (PIE's while it runs; `pie=False` for the editor's). `ue net MyCharacter` lists replicated properties and RPCs;
  `ue net player --pie` shows role, owner and relevancy per world; `ue net compare player.Health` checks the
  server and clients agree.
- Component layout: `ue tree BP_Door` (or an actor label, or `player` in PIE) shows the component hierarchy
  with sockets, transforms, meshes and collision/visibility flags.
- Effects: `ue fx /Game/FX/NS_Fire` reads a Niagara system (emitters, modules per stage, user parameters);
  `ue fx Torch_3 Intensity=4` sets a user parameter on a placed one. Procedural content: `ue pcg /Game/PCG/PCG_Forest`
  reads a PCG graph; `ue pcg` lists the level's PCG components; `ue pcg generate ForestVolume --seed 3`.
- Before changing a class or Blueprint: `ue class AEnemyCharacter` (or `ue class BP_Door`) shows its parents, own
  properties and functions, a Blueprint's components/variables/events, what derives from it and what uses it.
  A big one (Ultra Dynamic Sky: 900 variables): `ue class X --functions --grep cloud` (parameters and types,
  what `call_method` needs), `--vars`, `--components`, `--events`.
- "Can't interact with / pick up / shoot X": `ue pie start`, aim with `ue pie do "look X"`, then `ue trace --channel
  <the channel the code traces on>` (what blocks first, and each component's response); `ue collision X` shows its
  profiles and per-channel responses.
- One setting on many assets: `ue set --assets /Game/Env/Textures --class Texture2D LODGroup=TEXTUREGROUP_World
  --dry-run`, then without --dry-run (and --save); `--match "*_ORM"` narrows by name.
- Took a wrong step? `ue undo --editor` (or `ue undo --editor 3`) reverts the editor's last edits of any kind and
  names each; `ue redo` puts them back. Plain `ue undo` only covers `ue set`.
- Grass, bushes, trees: `ue foliage add /Game/Foliage/FT_Fern --area 1200,-300,2000 --density 40` (real foliage
  instances on the ground; a mesh path works too and gets a foliage type). `ue foliage` counts by type
  (`--area` for one spot); `ue foliage remove FT_Fern --area x,y,r` clears an area. Over a whole landscape
  from a density picture: `ue foliage add FT_Fern --mask forest.png --density 40 --avoid PlayerStart:2000`.
- Foliage performance: `ue foliage check` flags, per mesh, missing cull distances, wind (WPO) at every distance,
  Fab/Megascans billboard last LODs (oversized and spidery far away), heavy single LODs, shadows and collision on
  thousands of small plants, and scatter left as separate actors. Fix with `ue foliage fix billboards`,
  `fix cull --distance 15000`, `fix wpo --distance 4000`, and `ue foliage convert <regex>` (actors to instances).
  It also covers landscape grass (density is per 100 m^2, whatever the header says), Fab M_MS_Foliage instances
  with "use Distance Based WPO Fade" on (that permutation does not compile), Megaplants without a TransformProvider,
  materials that failed to compile, and usage flags the editor had to set (save those materials).
- Landscape grass builds over many frames after a camera move or a landscape material change: `ue wait grass`
  before a shot, or the ground looks bare. Grass instances are GPU-only (counts read 0 from Python).
- Floating things: `ue water` checks buoyant actors (the overlap-at-load flag they need, lift against weight).
  Buoyancy lift is shared across pontoons, so tune the coefficient for the whole body, not per pontoon.
- Real resolutions from the small offscreen viewport: `ue gpu --res 2560x1440`, `ue perf --res 2560x1440` (screen
  percentage to that pixel count) and `ue shot --res 1920x1080` (in PIE, or a windowed editor).
- A short event or an animated material: `ue shot --frames 6 --interval 0.1 [--after "<console command>"]` makes
  one contact sheet labelled with times; in PIE the game slows while capturing, so a 0.1 s flash still lands.
  `--burst N --every S` keeps the series as separate full-size PNGs (a spectator camera is placed once).
- Why a frame looks or costs what it does: `ue shot --view shadercomplexity|quadoverdraw|lightcomplexity|wireframe|
  unlit|lods|nanite|lumen ...` (the view is put back afterwards). Post-process at a spot: `ue post [x,y,z|actor]`
  lists the volumes in effect and the merged settings with their source. An editor tool tab or window:
  `ue shot --window "<title>"`.
- Try something in the running game without editing the level: `ue spawn /Game/X/BP_Buoy --pie --at x,y,z`.
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
- `ue log --since pie` reads from the last PIE start (`--since 2m` for the last two minutes); `--grep` searches
  every line (Verbose included) unless `--level` is given, and keeps the lines in order.
- `ue log --summary`: the whole session's warnings and errors grouped by message shape with counts. Noise the
  project knows about goes in Config/Tether/log-ignore.json (`ue log --ignore '^LogX: known thing' --why ...`);
  ue log, pie and ci then hide it and say how many lines they hid (`--all` shows them). Add an entry only for
  noise the user agrees is noise, never to make a real error go away.
- `ue reload`: hot-reloads `Plugins/Tether/Content/Python/tether` after you edit or add a tool.
  To make a throwaway inspection script reusable, add it there as a `@toolset_registry.tool_call`.

## C++ changes
- `ue build`: if the editor is up it uses Live Coding (~5 s, no restart); if not, UBT. Output is only
  your own errors and warnings, one line each. Never run Build.bat while an editor is open: one Live
  Coding session blocks UBT for every project on that engine.
- With other sessions sharing the project, Live Coding refuses (exit 5) when files another session edited in the
  last 15 minutes would be compiled, and names them; ask that session, or pass `--include-others`. Compile and
  link errors name each file's (and each missing function's header's) last writer.
- `ue lint --cpp` before a package or a restart: file-private names (anonymous namespace, `static`) defined in two
  .cpp files of a module collide in a unity build, and reflected types whose names differ only by prefix
  (`FSDThing` / `ASDThing`) fail UHT. It also lists BindKey keys an input mapping context maps (the mapping
  consumes the key, so the BindKey never fires).
- Function bodies go live. A change to what UHT sees (a new or changed UFUNCTION, UPROPERTY, UCLASS specifier)
  makes `ue build` say "restart required" with the declarations (exit 3): patched into a class with live
  instances it can crash the next PIE (a new UFUNCTION on a world subsystem asserted in GetSubsystem).
  `ue build --live` patches anyway; `pie start` then refuses until `ue build --restart` (`--patched` overrides).
  Header edits that leave the declarations alone (comments, inline bodies) still go through Live Coding.
- New UPROPERTYs, class layout or constructor defaults, Build.cs/.uproject changes, or Blueprint/Python
  wrappers that need the new API: run `ue build --restart`. It closes the editor, builds, reopens the
  same map and waits until ready. It refuses if there is unsaved work unless you pass `--save` or
  `--discard`; ask the user before discarding (`ue changes` and `ue diff <asset>` show what would go).
  Exit code 3 from `ue build` means a restart is required. Batch reflection changes so you restart once.
- Before closing, `--restart` compiles the game target (UBT, ~20 s): if that fails, the editor stays open
  for everyone sharing it. `ue build --check` runs the same check alone, with the editor up and Live
  Coding untouched (it misses editor-only modules and WITH_EDITOR code). `ue build --check --files
  Source/X/A.cpp Source/X/B.h` compiles just those files (a header: the .cpp files that include it), with no
  link and no lease needed, for checking your own work while another session holds the editor. Compiled alone a
  file sees only its own includes: errors on lines you did not change since the last commit are listed apart
  (often includes a unity build supplied); yours come first. A "redefinition" error names the other .cpp that
  defines the name (unity groups change when files are added; `ue lint --cpp` finds them all). New classes
  while another session's Live Coding builds? Keep them out of Source/ (its builds compile every changed file
  there): stage them in your own folder, Saved/Tether/staging/<session>/Source/<Module>/X.h, and run `ue build
  --check --overlay` (no folder: that one), which compiles a copy of the project with them on top (incremental
  after the first run). The copy takes the live Source/ under your files, so another session's half-written
  files can fail it: those are listed with who wrote them, and when none of the errors is in your files it checks
  yours alone (as `--files`) and reports that. A folder another session compiled is refused (on Windows staging/
  and Staging/ are one folder). Do not
  compile the editor target with UBT `-Module=` while the editor runs: Live Coding breaks until a restart.
- Live Coding patches every editor open on the project at once (the user's own and any PIE there included;
  `ue build` names them), and live only in those processes: `ue status` names editors running without them, and
  `ue build --when-closed` queues the UBT build for when every editor has closed.

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
  on materials, which shows parameter arrays raw. On a base material (M_, not MI_) it changes the parameter's
  saved default, which every instance without an override sees and a generator rebuilding the material can
  keep: it prints the command that puts the old value back; run it before saving a temporary debug value.
- Settings: `ue config` lists sections; `ue config maps` / `ue config rendering lumen` reads one;
  `ue config maps gameDefaultMap=/Game/Maps/L` sets and saves it (to Config/Default*.ini for project settings).
  Prefer it to editing .ini files by hand: names and values are checked, and the editor applies them live.
- Budgets: `ue size [folder]` (biggest assets on disk), `ue size /Game/Maps/L --deps` (what a level pulls in,
  by folder), `ue mem` (what the editor or PIE holds in memory, by class and mesh; ~20 s).
- `ue diff /Game/Path/Asset [rev]` shows what changed in a binary asset since a git revision (default HEAD,
  compared with the editor's current version) or between two (`--to rev2`): Blueprint variables, components,
  defaults and graph text, DataTable rows, map actors. Use it to review your own asset edits before
  committing, and to understand someone else's commit (`ue diff X <sha>~1 --to <sha>`). Perforce revisions work
  too: `#have` (the default in a Perforce workspace), `#head`, `#3`, `@1234` (`ue diff X @1233 --to @1234`).
  World Partition actor files (`__ExternalActors__/...`) are refused: diff their map, which lists the changed
  actors. If `ue changes` warns that an actor belongs to a Tether diff copy, do not save that level; reopen it
  with `ue map open <map> --discard` (ask first: it drops the level's unsaved edits).
- `ue find <regex> [--class StaticMesh] [--under /Game/x]` (or a path: `ue find "/Game/X/Presets/*"`) to locate assets; `ue refs <asset>` before deleting
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
- "Why didn't my event fire?": `ue bp trace --do tests/play/steps.txt --match BP_Door` lists the Blueprint
  events and functions that ran during PIE, in order with counts (an Insights trace with named events;
  about 20 s). If the event is missing, the trigger never reached it; then check input (`ue input`), overlaps
  and the values involved (`ue pie watch`).
- After renaming or removing C++ functions, properties or classes (and `ue build --restart`), run
  `ue bp check` (or `ue bp check /Game/Some/Folder`): it lists only Blueprints that no longer compile, with why.
  "missing or NULL parent class: /Script/Old.Class" means a renamed C++ class or module needs a redirect
  ([CoreRedirects] in DefaultEngine.ini); `ue open` reports no error for it, and PIE falls back to defaults.
- `ue table /Game/Path/DT_X` shows rows; `--export rows.json|.csv`, edit, then `--import rows.json
  --dry-run` to see the diff and `--import rows.json --save` to apply (upsert; `--prune` deletes missing
  rows).
- Does the shipped game boot? `ue package` (minutes to an hour: it is a background job it waits on) packages,
  then runs the packaged game headless for 20 s and reports crashes and errors; `--boot-only` re-tests the last
  package. `ue package --perf 20 --map /Game/Maps/L` measures frame times in the packaged game (editor numbers
  are not the shipped game's).
- Real multiplayer outside the editor (separate processes, real network, the dedicated-server code path):
  `ue game start --clients 2 [--server dedicated] [--map M] [--game MODE]` waits until the clients join;
  `ue game status` (crashes, errors per process), `ue game log 1` (client 1), `ue game stop`. `--packaged` (or
  `ue package --play 2`) runs the shipped build. Prefer `ue pie start --clients N` for quick checks.
  `--server none` runs each client on its own (a solo `-game` run, the game's start-up included), `--connect
  host:port` joins a server you run, and `--python` keeps editor Python as Play > Standalone does (start-up
  scripts such as init_unreal.py run). `ue game shot 1 --windowed` captures a game window, loading screens
  included (the movie player is off in PIE).
- Cooks and other long commands: `ue job cook` or `ue job start -- <cmd>`, then `ue job wait` (errors only)
  or `ue job status`. Do not block a shell on them.

## Visual checks
- To see what an asset looks like before using or changing it: `ue look /Game/Path/SM_Rock` (mesh, Blueprint or
  material) writes one PNG with six views; open it only if you need to. In a night level the level's light
  makes materials unreadable: `--studio` lights it neutrally instead.
- In dense maps a `--at` camera can land inside a building: `ue shot --at ... --look-at ... --clear` moves it
  along the view line to just in front of whatever blocks the view.
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
  GPU bound: `ue gpu` (needs a rendering editor) splits GPU time by kind (Lumen, shadows, base pass, post...)
  and names the top passes; `ue perf --scalability` measures low/medium/high/epic in one table. What a change
  costs: `ue gpu --ab "mat MI_X UseWindows=false" "mat MI_X UseWindows=true" --restore "..."` (in PIE, at a
  camera that shows it) alternates the two states and prints the difference by pass. A fixed view for timings:
  `ue gpu --at x,y,z --look-at x,y,z` (or `--camera <shot baseline>`; `ue perf` takes the same).
  Slow builds: `ue build --timing` (slowest files, modules and headers; game target, editor untouched). Slow editor
  start: `ue perf startup`. Which ue commands fail most here: `ue stats`.
  Settings behind it: `ue cvar r.Shadow` lists console variables with who set them; `ue cvar --changed` shows
  everything the project and device profile changed; to try a value, `ue cvar r.X=1 --for "gpu"` sets it only
  for that one command.
- Input: `hold IA_Move 1,0` in `pie do` injects the action's value (mapping modifiers like dead zones are
  skipped); `key W down`/`key W up`, `mouse 100 0` and `stick Gamepad_Right2D 0.25,0 1` go in as real key
  presses through the mappings. `ue input --live IA_Look` (PIE running) shows the action's value while you or a
  step file play; `pie watch IA_Look` puts it in a column. A background editor runs PIE at a few fps: `ue pie
  start` lifts that for its sessions, and `wait 2.5 game` waits on game time.
- Reading state: `ue props` also lists UPROPERTYs that are not Blueprint-visible; `pie watch` reads them, plus
  `player.control` and getters like `player.GetVelocity()`.
- When `ue py` prints `engine: ...` lines, that is why a call returned None (e.g. Editor Scripting refusing in PIE).
- UI checks: `ue shot --hud` in PIE captures the game view with its UMG HUD (plain `ue shot` has the 3D view only).
  A PIE `ue shot` is the frame as played (motion blur included); `--res` is a HighResShot rendered with the game
  stopped, so judge motion blur, TSR and other effects built over frames without `--res`.
  After static meshes were rebuilt in place (a generator's bake), `ue shot` waits for builds still running and
  renders a few frames at the view first, saying so: an offscreen editor renders only when asked, and shots right
  after a bake showed missing meshes and wrong materials. `--res` in a hand-opened editor whose viewport takes no
  HighResShot (minimised, tab hidden) falls back to the viewport's own size and says so.
  A headless editor's PIE viewport is a part of its main window, which can be small (the project's saved
  resolution sets it): `ue open --headless --render --size 2560x1440` gives a viewport big enough to read menus.
- To see what happened between checks: `ue pie do -f steps.txt --film 0.25` adds a contact sheet of the game's view
  (with HUD), four frames a second with times on them; read that one image instead of many screenshots.
- A side view during play (an enemy's wind-up, a door from outside): `ue shot --at x,y,z --look-at Enemy_1` in PIE
  takes one spectator shot from there and puts the player's view back.
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
- Texture memory: `ue tex /Game/Folder` (largest textures, estimated memory, oversized/uncompressed/never
  streaming ones) and `ue tex --streaming` (what is resident now, pool use, textures held back).
  `ue perf --trace` adds which scopes (functions, Blueprint events, render passes) take the time per thread.
  Limits the project must stay within live in Config/Tether/budget.json; `ue budget` checks them all (exit 1
  when over), and `ue ci --budget` makes CI fail on a regression.
  Stutter while moving through a level: `ue perf load [/Game/Maps/L] -f tests/play/route.txt` lists hitches
  with the packages that finished loading in each (and sync-load flushes), plus World Partition streaming time.
  Run it on a freshly opened editor: packages already in memory do not load again.
  When it says the frame rate is capped, compare the per-frame game/render/GPU work, not the fps.
- To play the game like a player would: `ue pie do "at ItemPickup_Key -150,0,100" "look ItemPickup_Key"
  "tap InteractAction" "until player.Inventory.Items ~ Key within 2" "gone ItemPickup_Key"` (or `-f steps.txt`).
  Steps: at, look, move, press/tap/hold/release (Enhanced Input actions: asset names, or actions the
  character creates in C++), call, expect/until (`= != < > <= >= ~`), log, exists/gone, wait, exec, shot
  (`ue help pie` lists them all with their arguments). A `log` step matches lines after the previous `log`
  step's match (a toggle checked twice proves the second toggle); `log <regex> anywhere` searches the whole run (`within N` before or after it).
  Each step prints ok/FAIL; it stops at the first failure. Write the step file once and rerun it after changes.
  Keep step files in `tests/play/` with a `# map: /Game/...` line: `ue ci --play` runs them all as regression tests.
- To turn the user's own play-through into a test: ask them to run `ue pie record -o tests/play/<name>.txt` in a
  windowed editor and play (it stops when they stop PIE). The file replays with `ue pie do -f`: keys become
  `tap`/`hold`/`release` of their input actions, mouse look becomes `look` steps, ending with a `near` check.
  Add `expect` steps for what should have happened.
- Visual checkpoints in a step file: `shot-check gate_view` records tests/play/shots/gate_view.png the first time
  and fails later runs that look different (diff image in the output); put a `look` step before it.
- To lock in how play unfolds (not just that steps pass): add `# watch: BP_Enemy_C_1.State player.Health player`
  to a step file in tests/play/. Its first `ue pie do -f` records tests/play/baselines/<file>.json (commit it);
  later runs and `ue ci --play` fail on differences (states reached late, values ending elsewhere, paths drifting).
  Re-record on purpose with `--baseline <file stem>`.
- To see how values change during play (instead of adding print nodes): `ue pie watch player.Health
  BP_Enemy_C_1.State BP_Enemy_C_1 --seconds 10` (an actor alone = its location; `-f steps.txt` plays a step file
  while watching). Prints each value's range, path or changes with times; the full samples go to a CSV (rotators
  as .pitch/.yaw/.roll). Getters work there and in `expect`/`until` steps alike: `until
  player.Movement.GetSpeedKmh() > 100 within 15`, `expect player.GetActorRotation().yaw > 80`. Keys a step file
  holds (`key W down`) are released when it ends, failed or not.
- Soaks and repeated console commands: `ue pie loop "SD.SpawnWave 4; wait 30; stat unit; wait 30" --seconds 600
  --every 60 "memreport"` runs them from the game thread on a fixed schedule (no drift, no round trip each);
  it waits for the end, and a killed `ue` leaves it running (`ue pie loop status` / `stop`).
- Which input actions exist and what keys reach them: `ue input` (by mapping context, and who adds each context);
  in PIE it also says which contexts are active. `ue input Move` shows matching actions only. A `pie do` step
  that presses an action no active context maps still works, but warns: a player could not do it.
- Game UI in PIE: `ue ui` lists what is on screen (Slate and UMG texts, buttons, text boxes); `ue ui click
  "Close"`, `ue ui type "Name" hello`, `ue ui key Escape` (in PIE, keys and chords such as `Ctrl+G` go to the
  game's input, so Escape opens its pause menu instead of stopping PIE; `--slate` sends to the focused widget).
  When a UMG widget has keyboard focus (a menu reading keys in NativeOnKeyDown, or a title screen holding focus
  itself), other single keys go to it
  through Slate on their own (`--game` forces the game's input); step files take `key --slate Enter` or
  `ui-key Enter`, and split steps follow the same rule: `key E down` on a focused screen presses E through Slate
  right there (Slate sends KeyDown and KeyUp together, so its `key E up` sends nothing); `click --event LABEL` (fires OnClicked, for a covered window). `ue ui --rects` prints each
  widget's screen rectangle (what `ue ui at x,y` takes); a plain `--headless` editor (-nullrhi) lays out no
  geometry, so rectangles, `ui at` and arrow-key menu navigation need `ue open --headless --render`, while clicks
  there fire the UMG button's OnClicked. A button with no text child (a transparent UButton over
  a text row) is labelled by the text under it; better, give it an accessible name
  (`SetAccessibleBehavior(EAccessibleBehavior::Custom, Text)`), which screen readers use too.
  One tap is one press, as on a keyboard, and taps in a row (two `key Down tap` steps) stay two presses. A legacy BindKey chord on a key an Enhanced Input mapping also uses
  (Ctrl+G with G mapped) usually never fires: the mapping consumes the key; `ue ui key` warns when it sees that.
  Several buttons with one label (nine ON/OFF toggles): `ue ui click "OFF" --near "DEBUG HUD: DIRECTOR"` (the one
  closest to that text) or `ue ui click "OFF#3"` (the third, in `ue ui`'s order).
  In `ue pie do`: `click <label>`, `expect-ui <text>`,
  `ui-gone <text>`. Clicks are real mouse clicks at the widget, so whatever is on top receives them; labels are
  stable, refs (b12) change between reads. Canvas-drawn HUD text is not a widget: check it with `ue shot`.
  Step files also take `log <regex> count >= 6` and `log in order: lap 1, lap 2, finished`, and `shot` with
  `ue shot`'s options (`shot --at x,y,z --rot -89,0,0`).
- `ue pie watch Actor.Component.Property` samples a component's property (the component by its property name,
  e.g. `SDPlayerCharacter_0.CharacterMovement.MovementBase`); `ue props X --match Name --pie` reads properties
  Python calls protected. After Live Coding patched class layouts, `ue pie start` warns: UMG and replication
  can misbehave (a HUD drawing nothing) until `ue build --restart`.
- Long PIE-side tests (a tick callback driving inputs for 10-30 s): end the script's work with
  `tether.finish({"result": ...})` and run it with `ue pie run test.py --wait 60`, which prints the value; no
  polling for files. `tether.spawn(cls_or_asset, (x, y, z), (pitch, yaw, roll), label="Pad")` puts an actor in the
  PIE world from a script; `ue spawn --pie --label Pad --replace` / `ue delete --pie Pad` from the shell. Paths in
  editor Python are Windows paths: `D:/Projects/...`, not Git Bash's `/d/Projects/...` (that lands in `C:\d\...`).
  For scripts: `ue pie status` exits 0 running / 1 stopped, `ue actors <re>` and `ue find <re>` exit 1 on no match.
- Prefer `ue pie run check.py --expect '<log regex>' --reject 'Error|no subsystem'` (or `-c "<code>"`) for verifying gameplay:
  it starts PIE, runs the script in the PIE world, waits for the log lines, stops PIE and exits 0/1.
  Write the script once and rerun it after every change.
- Animation in PIE: `ue anim <actor|player>` (anim BP and its variables, montage, curves, movement mode and
  speed). Python cannot read state machine states; the variables the transitions use usually tell you.
- Audio in PIE: `ue audio` lists what is playing (needs `ue open --headless --sound`, which stays silent);
  `expect-sound <regex>` as a `ue pie do` step checks a sound fires. `ue audio --log 10` (or `--do steps.txt`)
  records how each sound that started was actually played (3D or 2D, attenuation, class) and flags stereo sounds
  played in 3D: trust it over asset settings when code builds attenuation at runtime. To hear what the mix did
  (a crossfade, a voice that zeroes everything): `ue audio --bounce 10` (or `-f steps.txt`) writes PIE's master
  submix (or `--submix X`) to a WAV with the speakers silent and prints level per second, active channels, bands,
  dropouts to exact silence and clipping.
- Character animation logic: `ue anim graph /Game/.../ABP_Hero` (state machines, what each state plays,
  transition rules and blend times); `ue anim player` shows the live anim instance in PIE.
- AI built on StateTree: `ue statetree /Game/AI/ST_Enemy` prints the whole tree as text (states, enter conditions,
  tasks, transitions); read it before changing AI behaviour. `ue statetree --pie` shows each component's run status.
- AI in PIE: `ue ai` (one line per AI), `ue ai <pawn>` (movement, focus, perception, brain, blackboard, and the
  project's getters). State a C++ class keeps in plain members is invisible: suggest exposing it as a
  `UFUNCTION(BlueprintPure)` getter.
- No navmesh in the editor (headless editors do not build it by themselves): `ue world build nav` rebuilds it
  and says what it changed (it waits out a navigation build lock and says which). `ue world build
  lighting|hlod` for lightmaps and World Partition HLODs.
- AI that does not move: `ue nav` (is there a navmesh, and if not why), `ue nav check` (spawn/patrol points
  off the navmesh), `ue nav path <a> <b> [<a> <b> ...]` (reachable? how long, how high, and where a partial
  path stops; exit 1 if any pair is not a full path; `--m` for metres, `--agent <pawn>`); add `--pie` for
  runtime-generated navmeshes. Run the pairs for new routes after every `world build nav`.
- Spawn points, pickups, teleports: `ue world probe x,y,z|actor ...` says whether each point is inside geometry,
  what is within `--radius` (distance to the nearest collision), and whether the navmesh reaches it (exit 1 if
  any is inside or off the navmesh).
- Before a package or a hand-over: `ue game smoke` opens each map in a headless `-game` process for a few seconds
  and reports errors, Accessed None and crashes that PIE hides (editor-only components are null there).
- A short effect from a console command: `ue exec "SD.Flash" --shot` captures the frame right after it.
- `ue wait 4 game` waits 4 s of game time in PIE between `ue exec` calls (`ue wait 4`: wall-clock seconds).
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
error (one-line message), 2 editor unavailable, 3 restart required, 4 timeout, 5 another session holds the
editor lease (`ue lease`).
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
- Another project's editor with Live Coding on (same engine install) does not block `ue build`: it passes
  `-NoHotReloadFromIDE` when this project's editor is closed. It does block an IDE or hand-run Build.bat build
  ("Unable to build while Live Coding is active"); add that flag there too.
- Wrap edits in `with unreal.ScopedEditorTransaction("Agent: ...")` so the user can undo them.
- Tool calls run on the game thread. Keep each call short and split long jobs into steps.
