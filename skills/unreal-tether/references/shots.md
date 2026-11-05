# Seeing the result: ue shot, ue look, visual comparisons

## Before a shot

- After opening a big level or editing materials, `ue wait compile` waits until shaders and assets have
  compiled, before screenshots, timings or a nav build. It says what it waits on every 30 s; in a rendering
  editor, whose frames keep its own CPU busy, it ends once the shader workers have been quiet for 60 s
  (`--workers-only` waits for the workers alone).

## Framing and views

- `ue shot`: PNG of the viewport (the player's view during PIE) saved under `Saved/Tether/shots/`; prints
  the path and size. Open it only when you need to see it. Frame a view with `--actor <A>`,
  `--at x,y,z --look-at <A|x,y,z>` or `--rot p,y,r`; `--annotate` overlays a world grid and actor labels.
  Needs a rendering editor: `ue open --headless --render` (plain `--headless` is `-nullrhi`).
- `ue shot` sets its own view per capture: `ue exec "viewmode unlit"` does not carry over; use
  `ue shot --view unlit` (or wireframe, lightingonly, ...).
- In dense maps a `--at` camera can land inside a building: `ue shot --at ... --look-at ... --clear` moves it
  along the view line to just in front of whatever blocks the view.
- Why a frame looks or costs what it does: `ue shot --view shadercomplexity|quadoverdraw|lightcomplexity|wireframe|
  unlit|lods|nanite|lumen ...` (the view is put back afterwards). Post-process at a spot: `ue post [x,y,z|actor]`
  lists the volumes in effect and the merged settings with their source. An editor tool tab or window:
  `ue shot --window "<title>"`.
- Real resolutions from the small offscreen viewport: `ue gpu --res 2560x1440`, `ue perf --res 2560x1440` (screen
  percentage to that pixel count) and `ue shot --res 1920x1080` (in PIE, or a windowed editor).

## Assets

- To see what an asset looks like before using or changing it: `ue look /Game/Path/SM_Rock` (mesh, Blueprint or
  material) writes one PNG with six views; open it only if you need to. In a night level the level's light
  makes materials unreadable: `--studio` lights it neutrally instead.

## Short events

- A short event or an animated material: `ue shot --frames 6 --interval 0.1 [--after "<console command>"]` makes
  one contact sheet labelled with times; in PIE the game slows while capturing, so a 0.1 s flash still lands.
  `--burst N --every S` keeps the series as separate full-size PNGs (a spectator camera is placed once).
- A short effect from a console command: `ue exec "SD.Flash" --shot` captures the frame right after it.

## Comparing with a baseline

- Before a change that should not alter a view (refactors, optimisations), `ue shot --at ... --look-at ... --baseline
  NAME`; after it, `ue shot --compare NAME` reports what changed and where (diff image in red). Animated things
  (clouds, water, particles) show up as separate areas; judge by the largest.

## In PIE

- UI checks: `ue shot --hud` in PIE captures the game view with its UMG HUD (plain `ue shot` has the 3D view only).
  An editor `ue shot` lifts "Use Less CPU when in Background" for the capture, so edit modes tick and their
  previews are current. A PIE `ue shot` is the frame as played (motion blur included); `--res` is a HighResShot rendered with the game
  stopped, so judge motion blur, TSR and other effects built over frames without `--res`.
  After static meshes were rebuilt in place (a generator's bake), `ue shot` waits for builds still running and
  renders a few frames at the view first, saying so: an offscreen editor renders only when asked, and shots right
  after a bake showed missing meshes and wrong materials. `--res` in a hand-opened editor whose viewport takes no
  HighResShot (minimised, tab hidden) falls back to the viewport's own size and says so.
  A headless editor's PIE viewport is a part of its main window, which can be small (the project's saved
  resolution sets it): `ue open --headless --render --size 2560x1440` gives a viewport big enough to read menus.
- A side view during play (an enemy's wind-up, a door from outside): `ue shot --at x,y,z --look-at Enemy_1` in PIE
  takes one spectator shot from there and puts the player's view back.
