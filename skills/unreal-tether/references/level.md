# Levels: seeing, placing, terrain, foliage, water, light, effects

## Seeing what is there

- Where things are in a level: `ue map` (or `ue map x,y,r`, `--pie` for the running game) draws it from above
  (works headless) with player starts, spawners, AI and triggers as numbered dots; read the PNG to plan placement.
  `ue map --footprints` writes the collidable geometry as data (each mesh's turned outline, top and bottom) for
  minimaps and layout checks. `ue map open /Game/Maps/X` switches the editor's map (`--save`/`--discard` when the
  current one has unsaved edits) and compares the level with its last good load: a WARNING (exit 1) means it came
  back broken (actors at the origin, meshes gone, empty splines), typically in an editor carrying many Live Coding
  patches. Don't save it from there (`ue save` refuses); `ue build --restart` and reopen.
- Why AI (or players) get stuck somewhere: `ue collide x,y,r --z 100` draws what blocks at that height around the
  point, coloured by class, from overlap tests: generated roads, junction walls and raised medians show up.
- After placing or scattering things: `ue level check` finds floating or buried props, stacked duplicates,
  invisible walls and overlapping stationary lights; `ue drop <actor>` fixes a floating one.
- World Partition levels: other commands only see loaded actors. `ue wp` says how many are loaded; `ue wp actors
  Door` finds unloaded ones; `ue wp load x,y,r` (or `ue wp load <regex>`) loads them before you work on them.

## Placing things

- On terrain, never guess heights: `ue spawn /Game/X/SM_Rock --at 1200,-300,0 --ground` (or `ue drop Rock1 Rock2`,
  `--align` to follow the slope) puts things on the surface below. A structure built of separate pieces (walls,
  roof) goes down as one: `ue drop Wall1 Wall2 Roof --group` (attached actors come along); it says how much the
  ground varies under it, so move it to flatter ground rather than letting a slope swallow one side.
- Many props at once: `ue scatter /Game/X/SM_Rock --count 30 --area 1200,-300,2000 --spacing 250 --scale 0.7,1.4
  --align` (one undo step; same `--seed`, same layout).
- Level edits: `ue spawn PointLight --at 0,0,300 --label Lamp` (or an asset path), `ue duplicate Lamp --count 3`,
  `ue delete Lamp`. Asset moves: `ue mv /Game/A/X /Game/B/` (then `ue save`), `ue rm /Game/A/X` (refuses while
  referenced; check `ue refs` first). Prefer these to raw `ue py`: they are undoable and checked.

## Terrain

- Terrain: `ue landscape` (size, layers), `ue landscape at x,y` (height, slope, layer weights: use it before placing
  things), `ue landscape map` (a PNG of the shape). `ue landscape flatten x,y,r [--height z]` (a pad for a
  building), `raise x,y,r --by cm`, `smooth x,y,r`; each prints a backup for `ue landscape restore --backup`.
  Whole heightmaps: `ue landscape export h.png` / `import h.png` (16-bit, the landscape's exact vertex grid;
  `--flip-y` for north-up files) and paint layers: `ue landscape paint Rock --mask rock.png`. These need a
  rendering editor (`ue open --headless --render`); each prints the file that undoes it. Python cannot create a
  new landscape; if the project has its own C++ helper for that, use it.

## Foliage and grass

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

## Water

- Water: `ue water` lists zones and bodies and flags the usual mistakes (an ocean whose brush flattens the
  terrain, a coarse shoreline, a stock material on the distant water mesh).
- Floating things: `ue water` checks buoyant actors (the overlap-at-load flag they need, lift against weight).
  Buoyancy lift is shared across pontoons, so tune the coefficient for the whole body, not per pontoon.

## Lighting, effects, procedural content

- Lighting and mood: `ue lights` lists lights, sky, fog and exposure with notes (e.g. auto exposure undoing a dark
  scene); check it before and after lighting changes, together with `ue shot`. It counts lights inside
  Blueprints (Ultra Dynamic Sky's sun and moon) and lists every unbound post process with its priority: a sky
  Blueprint's own post process (priority 1) beats a level volume at 0, so an exposure change there does nothing.
  A mesh kilometres across (a 5 x 12 km water slab) gets no Lumen reflections: tile it (~1 km).
- Effects: `ue fx /Game/FX/NS_Fire` reads a Niagara system (emitters, modules per stage, user parameters);
  `ue fx Torch_3 Intensity=4` sets a user parameter on a placed one. Procedural content: `ue pcg /Game/PCG/PCG_Forest`
  reads a PCG graph; `ue pcg` lists the level's PCG components; `ue pcg generate ForestVolume --seed 3`.
