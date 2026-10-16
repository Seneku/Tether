# Tether: next possible additions (for review)

Status: draft for review, 2026-10-14, after v0.19.0. Nothing here is committed to; mark each item **yes / later / no**.

Tether today covers the editor loop end to end. That means status, open/close/build, Python and console, props and
set, actors and spawning, Blueprints, materials, DataTables and data assets, tests, PIE driving (steps, raw input,
watch, record, film, spectator shots), perf/GPU/memory/budgets, packaging (boot, perf, multiplayer sessions),
content checks (lint, mesh, skel, sound, textures, unused), world tools (landscape, foliage, PCG, WP, nav, map),
Fab, and audio. The list below comes from three sources:

- **[field]**: asked for by the sessions using Tether (ShooterDomination port, its audio session, IslandHorror).
- **[gap]**: something we found missing or broken while building it.
- **[idea]**: not asked for yet, but a likely next need for an agent working in Unreal.

Effort: **S** = under half a day, **M** = about a day, **L** = several days or needs C++/engine work.

**Done in 0.19.1** (after this list was written): 19a, 19b, 19c, 19d, 19e and 13a–13d. Their rows are kept below
for reference.

**Done in 0.19.2** (ShooterDomination Missions notes, not rows here): `world build nav --save` waits for tiles and
never saves a map without its navmesh; `build --restart --discard` no longer reopens on a "Restore Packages" dialog,
and `open`/`build`/`status` name a dialog that stops a boot; `build --restart` compiles the game target before it
closes the editor, and `ue build --check` does that alone; Live Coding failures re-read the logs before saying
"0 errors"; `ue exec` folds only repeats in a row and says so; `pie run` scripts get a fresh namespace; `ue py`
suggests the Python property names closest to a missing one; the unsaved-work refusal points at `ue changes`/`ue diff`.

**Done in 0.20.0**: 1, 2 and 4 as `ue lease` (one lease for the editor rather than per-kind locks) and `ue who`,
with the PIE owner in `ue status`; from the 0.19.1 front-end and Spire notes: `world build nav` retries a build the
navigation system refused as locked and names the lock; build errors say who last edited each file; `ue wait N
[game]`; `ue nav path` takes several pairs and reports the highest point and where a partial path stops (part of
27a; "search limit hit" and islands are still open); `ue world probe`.

**Done in 0.21.0** (the terrain round, for IslandHorror): 19f, 59 (with `fix billboards` hiding the impostor LOD by a
screen size of 0 instead of rebuilding LODs, plus `fix cull|wpo`), 60 (override materials kept in their own foliage
type), 61, 62, 63 (`paint` backs up the layer by rendering it at the vertex resolution) and 64; 66 as `ue wait
compile`, which watches shader-worker and editor CPU because neither compile queue is visible to Python (`ue open`
does not report the backlog yet). 65 (a C++ module) is still open.

**Done in 0.22.0** (IslandHorror's visual pass, 67-77): 67 (`shot --hud` checks its window crop against the
viewport's own capture and falls back to it), 68 (through the cheat manager's `summon`), 69, 70-72 (plus materials
that failed to compile and usage flags the editor set, both from the log; collision-only components skip the render
checks), 73, 74, 75 (screen percentage to the pixel count for gpu/perf, verified in the upscaler's line; HighResShot
for shot, which in the editor needs a level viewport), 76 (the grass builder's Verbose batch lines), 77. Measured on
Lvl_Island at its player start: 1440p ~15 ms GPU (BasePass most of it), 1080p ~11.5 ms.

**Done in 0.23.0** (ShooterDomination's 0.20 notes): 78-89 and 35a (`ue map --footprints`). `ue props` already read
protected properties; `ue py`'s error now says so. 90 (collision-profile lint) and 91 (a `-game` smoke step) are
still open; the gotchas behind them are in the skill.

**Done in 0.23.1**: `ue ui key` taps fired a legacy BindKey once per frame held (forced keys repeat IE_Pressed
every frame): taps are now sequenced in whole frames, one press each, chords included, with a warning for a chord
on a key an Enhanced Input mapping uses.

**Done in 0.24.0** (ShooterDomination's 0.23 notes): 92-100 and 102. `build --restart` checks the editor came back
and says why not; `shot --res` redraws a windowed editor's viewport and retries; `build --check --files`;
`ui click --near` / `LABEL#N`; `log --grep` includes Verbose; `shot --frames` (time dilation in PIE);
`pie loop` (a game-thread runner in the plugin: the plugin copy needs updating); `git stage --mine` (the edit hook
now records every project file and each session that wrote it); BindKey/IMC conflicts in `lint --cpp`; the
missing-HUD note on `pie start`. 101 (`ui key --viewport`) is still open.

**Done in 0.24.1**: `ue init` updating a plugin copy whose only changes are its Python (and version fields) says
`ue reload` instead of `ue build --restart`.

**Done in 0.25.0** (UnrealRacing's notes, ShooterDomination's rainy night): 103-125. `ue new project` (from the
engine's templates, checked by building the Vehicle and Blank C++ ones), `ue template add`; 105: Tether's UBT
builds already pass `-NoHotReloadFromIDE`, so another project's Live Coding only blocks IDE builds (doctor and
docs say so now); `lint --mpc [--fix]`, `look --studio`, `shot --clear`, `wait idle`; `git stage --mine` reads
session transcripts when the edit hook has no record (108: ShooterDomination's sessions do not load the plugin,
so it had none; `init --claude-plugin` enables it). Plugin changes (111-115, 117, 119, mpc_check) need the
projects' plugin copies updated (`ue init`, then `ue reload`).

**Done in 0.26.0** (UnrealRacing's notes 126-141, plus older open items): 126 `build --check --overlay` (a
shadow copy of the project's code under Saved/Tether/shadow, ~30 s incremental on Sandbox); 127 `lease request`/
`grant`; 128 `find` path globs; 129 `class --functions|--vars|... --grep` with Blueprint function signatures
(from the function graph's pins: a UFunction's parameters are not readable from Python); 130, 131 (each session's
editor is sticky), 132, 133 (`status` names editors without the Live Coding patches; `build --when-closed`), 134
(`ue collide`; footprints per procedural component and instance), 135, 136 (`obj dump` prints arrays one element
per line, `Name(i)=`, which the hidden-property reader skipped; empty arrays print nothing), 137-141; and from
older lists 3 (`ue py` in PIE), 5 (`exec --shot`), 9b (`pie watch Actor.Mesh@bone`), 90 (collision profile
mismatch in `level check`), 91 (`ue game smoke`). 18 was already done (`mat --compile` compiles a clean copy).
9 (`ui click --at` inside a widget) needs a C++ helper: Epic's Slate inspector clicks widgets only by ref, and
Python cannot send Slate mouse events (see 65). 101 stays open.

**Done in 0.27.0** (UnrealRacing's notes 142-159): 142 `ue open --second` (a private headless editor: the
beacon names its owner session, other sessions never pick it by default, and work in it needs no lease); 143
`fab add` prints the listing link to check "Allows usage with AI" (the vault does not record it, and fab.com's
listing data sits behind a bot check, so Tether does not read it); 144 `ue fab remove` (deletes through the editor;
files it still holds are deleted at `ue close`/`ue open`); 145 keys go through Slate when a UMG widget has
keyboard focus, plus `key --slate`/`--game` and `ui-key` steps; 146 clicks check the OS window under the point
(another program's window) and fire `OnClicked` instead, and step files take `click --event`; 147 unlabeled
buttons take the text under them (and the plugin's UMG list takes accessible or sibling text); 148 requests said
once (again on change or every 10 min); 149 `ue ui --rects` and rects in `--json`; 150 `collide` reads a band of
rows per call (160x160 with hits in ~4 s), and non-JSON replies are reported in one line; 151 `ue wait lease`;
152 `tether.world_subsystem()` and a `ue py` hint; 153 `validate` without git takes what the editor saved since
it started; 154 Tether's own `shot`/`HighResShot` console lookups leave `ue log`; 155 `expect`/`until` read
values as `pie watch` does (getters, hidden properties, `.z`/`.yaw` parts); 156 keys a step file left down are
released at its end; 157 watch CSVs name rotator columns `.pitch/.yaw/.roll`; 158 `--overlay` with no folder uses
`Saved/Tether/staging/<session>/`, and a folder another session compiled is refused; 159 a lease lasts 5 min past
its holder's last command. Also: the "several editors are open" note is said once per set of editors. Not tested
live: the Slate-focus key route and text-under-button labels (Sandbox has no UMG menu), and the OS-window click
check (needs a covered windowed editor).

**Done in 0.28.0** (UnrealRacing's notes 160-164, from its live test of 0.27): 160 a user widget holding keyboard
focus itself (a title screen) is not in the inspector's snapshot, so `press_key` asks UMG (`HasKeyboardFocus` /
`HasFocusedDescendants`) before sending a key to the game; 161 the plugin's UMG walk follows nested user widgets'
own trees (rows inside a screen), so `click --event` finds C++-built buttons; 147's labels go to the button whose
centre is nearest the text (overlapping rows took their neighbours' text); 162 `mat graph` reads each input's
OutputIndex from the engine's export (`obj dump`) when two inputs share a source (`#1.R` and `#1.A`); 163 `mat
graph M prune` (one undo step) and `ue lint --materials`; 164 `ue gpu --ab "A" "B" [--restore]`. Also: link errors
name the missing symbol rather than the function that uses it, and for another module's symbol the module to add
to Build.cs; Sandbox gained a menu fixture (`ShowProbeMenu`: a focusable title screen, then rows of text-less
UButton subclasses in nested user widgets) and the selftest a check that drives it. All checked live in Sandbox;
`prune` first crashed the editor by asking for every MaterialProperty value's input (MP_MAX among them), now only
the real outputs.

**Done in 0.29.0** (UnrealRacing's notes 165-179): 165 `ue log` keeps an indented reason line with its entry and
marks "Failed to compile Material" stale when the material compiles clean now; 166 `gpu`/`perf --at --look-at
--rot --actor --camera`; 167 a startup crash after a good build says so, with "the same crash happened at 09:45"
when it did; 168 overlays take only Source/, Config/ and Plugins/ and list what they staged; 169 `actors`/`find`
exit 1 on no match (and `actors` no longer echoes the pattern); 170 `delete --pie`, `spawn --pie --replace`; 171
`tether.spawn()`; 172 `pie status` prints `stopped` and exits 1; 173 `pie run --wait` with `tether.finish(value)`,
`--wait-file`, and a note on Git Bash paths in scripts; 174 `call` builds struct arguments from C++ or Python field
names or UE-order lists and fails on unknown fields (types from the method's docstring); 175 the default overlay
folder is found by `<name>-<id>`, `<name>` or `<id>` and never created by a check; 176 `pie run` prints a JSON
result; 177 `pie do -f -`; 178 `--second` refuses while the shared editor has unsaved work or another session's
lease (`--force`), and a failed `ue save` names the editor holding the files (reproduced live: the shared editor
could not save the map the second one had loaded); releasing the second editor's file handles would need engine
code (ResetLoaders), so it stays a guard; 179 the boot report reads the new editor's own log and lists the shared
editor's errors meanwhile separately. All checked live in Sandbox except 165's stale check, 167 (unit-tested) and
the `--wait-file` path.

---

## Priority 1: asked for, or blocking real work

### 1A. Several sessions sharing one editor
Two Claude sessions now share the ShooterDomination editor, and they have already collided: PIE started under
another session's builder script, and a `build --restart` ran while another session was waiting.

| # | Addition | Why | Effort |
|---|---|---|---|
| 1 | **`ue lock <pie\|build\|edit> [--by NAME] [--for MIN]` / `ue unlock`**: an advisory lock in `Saved/Tether/locks/` that `pie start`, `build --restart`, `close`, `py -f` and `import` respect (they wait, or refuse with who holds it and since when) | [field] a builder ran into another session's PIE; a restart ran while another session waited | M |
| 2 | **`ue who`**: which clients used the editor's MCP port in the last few minutes (session token use, last command, PIE owner); plus `started by` (a session, or "user") on PIE in `ue status` / `ue pie status` | [field] "a restart that warns when another client used the MCP port in the last few minutes"; 0.19.1: three sessions on one editor had to message each other to learn PIE was the user's own run | S |
| 3 | **`ue py` warns when PIE is running** before it runs (Editor Scripting refuses in PIE, and scripts then take the wrong branch) | [field] | S |
| 4 | **Restart and close guards**: `build --restart` and `close` name the other sessions' recent activity and need `--force` | [field] | S |

### 1B. Seeing short-lived game state
| # | Addition | Why | Effort |
|---|---|---|---|
| 5 | **`ue exec "a; b" --shot out.png`** (and a `pie do` step `exec+shot`): run commands and take the shot on the next tick | [field] a 1.8 s damage indicator had faded before the next `shot --hud` | S |
| 6 | **Canvas-drawn HUD in fast captures**: the widget capture misses AHUD `DrawText` and debug text; offer `--film-mode window` and say which mode ran | [gap] | S |
| 7 | **Real-time film (`--film-fps 30`)**: frames rendered into a ring buffer inside the editor (scene capture or the viewport's back buffer) and written out after the steps | [field] still misses a 0.45 s slash on big viewports; [gap] scene capture renders blank offscreen, so this needs a spike first | L |
| 8 | **Client-world screenshots (`pie target 1` + `ue shot`)**: confirm and fix the stale-frame report (HighResShot is global; target the client's viewport, or widget-capture its window) | [field] open since 0.17 | M |
| 9 | **`ui click --at x,y` inside a widget** (offset from its top left), for widgets the inspector sees as one block | [field] | S |
| 9a | **`ue shot --at ... --burst N --every S`**: a fixed spectator camera over time, with the debug camera turned on once instead of per shot (each spectator shot takes about 1.75 s now) | [field] 0.19 | S |
| 9b | **Bone and socket sampling in `pie watch`** (`Actor.Mesh@Hips`, `player.Mesh@hand_r`): world location per sample | [field] 0.19: a ue py loop run 14 times instead | S |
| 9c | **`ue shot --window "<title>"`**: an editor tab or tool window as a PNG, through `SlateInspectorToolset` (`Windows action=select`, then `Screenshot`, which returns base64) | [field] 0.19.1: the only way to see the Mission Board tool from the CLI; `ue shot` covers viewports only | S |

### 1C. Audio follow-ups (round 16 shipped the core)
| # | Addition | Why | Effort |
|---|---|---|---|
| 10 | **`ue audio` reports virtualized sounds** ("N virtualized") and keeps them out of the playing list | [field] | S |
| 11 | **Fire-and-forget sounds in `ue audio --log`** (PlaySoundAtLocation has no component): poll the device dump during the log, or use the Audio Insights trace | [gap] | M |
| 12 | **`ue metasound <asset>`**: a MetaSound graph as text (inputs, nodes, outputs), like `ue bp` | [idea] MetaSounds are UE5's default | M |
| 13 | **`ue sound --normalize <target dB> [--dry-run]`**: set each wave's volume to level a pack, from `--stats` | [field] levelling 5 packs 10 to 20 dB apart | S |
| 13a | **`ue sound <data asset>` names where each wave is referenced**: the property path or map key (`Cues[Explosion].Sounds[2]`, `FireSound`), which is what a person searches for | [field] 0.19 | M |
| 13b | **A real spectral centroid for `--stats` brightness**: zero crossings read noisy sounds about 2x high (concrete footsteps 12k against a ~5–6k centroid). Use a small pure-Python FFT on a few windows, or rename the column "zero-crossing rate" | [field] 0.19 | S |
| 13c | **Level "as played"**: when the referencing struct has a Volume-like float next to the sound (`FSDSoundCue.Volume`), report wave level + that gain, since the pack offsets are corrected there | [field] 0.19 | M |
| 13d | **`ue audio --log` counts starts lost to concurrency limits or virtualization** per sound (does a per-cue voice cap bite?) | [field] 0.19: 33 whizzes in 10 s with a voice cap | M |

### 1D. Known gaps in shipped features
| # | Addition | Why | Effort |
|---|---|---|---|
| 14 | **A packaging fixture without UnrealSharp**: a second minimal project (or a Sandbox target with UnrealSharp off) so `package`, `--play`, `--perf` and `game --packaged` run in the selftest here, not only in peer projects | [gap] Sandbox's packaged game crashes in UnrealSharp (exit 3) | M |
| 15 | **Selftest for the Fab input guard**: a fake "Fab" tab via a Slate test widget (no real store page), to check that click and type are refused | [gap] the guard is only unit-tested | S |
| 16 | **`shot --hud` at DPI scaling, verified in a windowed editor** at 125%, 150% and 200% | [field] fix shipped untested on a real 150% desktop | S |
| 17 | **Consistent actor names across commands** (`ue ai` prints labels, Python and `pie watch` use object names): print `Label (Name)` where they differ | [field] | S |
| 18 | **`ue mat --compile` leaves the material clean** when nothing changed | [field] | S |
| 19 | **`ue look` in an isolated preview world** (no level fog or lighting) | [field] previews washed out by Sector01's height fog | M |
| 19a | **`pie do at/look <actor>` aims at the wrong point**: `_pie_point` uses `get_actor_bounds(False)`, which counts hidden components (a robot's hidden laser mesh at the origin put the player 37 m away). Use the actor's location, or the bounds of visible components only, and print the point it resolved | [field] bug, 0.19 | S |
| 19b | **`build --restart` relaunches the user's own windowed editor with `-unattended`**: keep the flags it was opened with, and say so in the output | [field] bug, 0.19 | S |
| 19d | **`ue audio --log` flags stereo spread 0 as a problem**: spread 0 collapses both channels onto the source (it is the fix). Flag only spread > 0 (the default is 200), and drop "set StereoSpread" from the advice when it is already 0 | [field] bug, 0.19 | S |
| 19e | **`ue audio --log` says "3D ... on no actor (2D)"** for 3D sounds created with `CreateComponent(World)` plus SetLocation: say "no owner actor" and give the world position and distance instead | [field] bug, 0.19 | S |
| 19c | **`ue py` warns about `set_editor_properties` (plural) on AnimSequences**: it posts a change event without a property, so marking an import additive keeps the non-additive compressed data until a restart (the skeleton flew about 250 m in game) | [field] engine gotcha | S |
| 19f | **Project copies of the skill stay current**: `ue doctor` flags a project's `.claude/skills/unreal-tether/SKILL.md` older than the CLI's, and `ue init` updates it | [gap] IslandHorror's copy is at 0.19.0 (synced by hand each release until then) | S |

---

## Priority 2: likely next needs (functional areas)

### 2A. Multiplayer and networking
| # | Addition | Why | Effort |
|---|---|---|---|
| 20 | **Network emulation**: `ue pie start --clients 2 --net bad\|average\|custom(lag,loss)` and `ue game start --net ...` (PktLag, PktLoss, NetEmulation profiles) | [idea] ShooterDomination is multiplayer; replication bugs show up under lag | S |
| 21 | **`ue net stats`**: bandwidth per actor class and per property over N seconds (net.ListActorChannels, stat net, or Networking Insights) | [idea] | M |
| 22 | **`ue net check`**: replication lint (Replicated props without GetLifetimeReplicatedProps entries, RPCs without validation, server-only logic in client code paths) from source plus reflection | [idea] | M |
| 23 | **Desync finder**: `ue net compare` over time with a `--every` series, flagging values that diverge longer than N ms | [idea] extends the existing `net compare` | S |

### 2B. Gameplay systems
| # | Addition | Why | Effort |
|---|---|---|---|
| 24 | **`ue gas <actor\|player>`**: abilities, active effects, attribute values and tags in PIE (Epic's GAS inspector toolsets are already loaded) | [idea] | S |
| 25 | **`ue vislog`**: record the Visual Logger over a PIE run and print a per-actor text timeline (AI decisions, EQS, perception) | [idea] best AI debugging view, text-friendly | M |
| 26 | **`ue gdt <category>`**: Gameplay Debugger categories as text for a chosen actor | [idea] | M |
| 27 | **`ue eqs <query> --at <pawn>`**: run an EQS query and print the scored items | [idea] | M |
| 27a | **`ue nav reach <from> <to...>`** telling "no path" from "search limit hit" (A* stops at 2048 nodes by default, so long routes come back partial), and **`ue nav islands --from <point>`** listing navmesh pieces unreachable from it (e.g. floor sealed inside blocks, which Recast still rasterises) | [field] 0.19.1: two level problems in the Spire district came from navmesh connectivity | M |
| 28 | **`ue anim seq <asset>`**: notifies, curves, root motion, length and rate; `ue anim montage` sections and slots | [idea] animation debugging after `ue skel` | S |
| 28a | **`ue anim pose <asset> --at T [--bones Hips,...]`**: the pose as the runtime evaluates it (additive applied on its base, compressed data), not the raw source that `AnimationLibrary.get_bone_poses_for_time` reads; flags bones that move far from the reference pose | [field] 0.19: an additive slam sent the skeleton about 250 m; no check outside PIE could see it | M |

### 2C. UI (UMG)
| # | Addition | Why | Effort |
|---|---|---|---|
| 29 | **`ue umg <WBP>`**: the widget tree as text with bindings and anchors; edit through Epic's UMG toolset | [idea] | M |
| 30 | **`ue ui check --res 1280x720,1920x1080,2560x1080`**: HUD shots at several resolutions and aspect ratios, with overlap and off-screen detection | [idea] | M |
| 31 | **Gamepad focus walk**: press Tab/D-pad through a menu and list the focus order and dead ends | [idea] | M |

### 2D. Rendering and look
| # | Addition | Why | Effort |
|---|---|---|---|
| 32 | **`ue shot --view lit\|unlit\|wireframe\|shadercomplexity\|lightcomplexity\|nanite\|lumen`** | [idea] the agent can see why a frame is expensive | S |
| 33 | **`ue lights check`**: unbuilt lighting, overlapping stationary lights (more than 4), shadow-casting counts, Lumen settings in effect | [idea] | S |
| 34 | **`ue post`**: the post-process volumes in effect at a point, with merged settings | [idea] | S |
| 35a | **`ue map --footprints --json`**: per-mesh top-down hulls (outline, top and bottom z) over the play area, for tools that lay out a minimap or deploy map | [field] 0.20 menu session: a cyberpunk deploy map and minimap built from the level | S |
| 35 | **Rendered top-down `ue map --render`**: fix the offscreen scene-capture issue (or use a temporary orthographic viewport) | [gap] `ue map` is schematic only | M |

### 2E. Performance, deeper
| # | Addition | Why | Effort |
|---|---|---|---|
| 36 | **`ue perf compare <run A> <run B>`**: two Insights traces or CSV captures diffed by scope | [idea] | M |
| 37 | **`ue perf startup`**: editor and packaged startup time, by phase | [idea] | S |
| 38 | **Shader and PSO report**: shader compile counts, PSO cache coverage for the packaged game (hitches on first use) | [idea] | M |
| 39 | **`ue size --cooked`**: per map and chunk sizes from the cook output, with the biggest assets per chunk | [idea] | M |
| 40 | **`ue build --timing`**: slowest C++ files and modules from UBT timing data | [idea] Live Coding is fast, full builds are not | S |

### 2F. Content pipeline
| # | Addition | Why | Effort |
|---|---|---|---|
| 41 | **Blender round trip**: `ue import --from-blender <object>` via the Blender MCP on this machine (export FBX/glTF, import, `mesh check`) | [idea] Blender MCP is installed here | M |
| 42 | **`ue fix lods\|nanite <folder>`**: generate LODs or enable Nanite where `lint` flags them | [idea] | S |
| 43 | **Localization**: `ue loc` (gather, compile, missing translations, FText in code not marked for localization, string length overflow per language) | [idea] | M |
| 44 | **`ue move-pack <folder> /Game/ThirdParty/<name>`**: relocate a Fab or marketplace pack with redirectors fixed | [idea] packs land at the content root | S |

### 2H. Terrain, foliage and water
From IslandHorror's own scripts and C++ helpers (`docs/systems/terrain-water.md`): each one is something Tether
could not do when it was written. Its open follow-ups are re-placing content on slopes, re-levelling huts, rebuilding
nav, hero-placing cliffs, tuning scatter and turning scattered rocks into foliage for performance.

| # | Addition | Why | Effort |
|---|---|---|---|
| 59 | **`ue foliage check [type\|folder]`**: per foliage type or scattered mesh: instance count, cull distances, LODs (and the Fab/Megascans **billboard last LOD**, ≤12 triangles with `MI_*_Billboard`, which renders huge and spidery at distance), Nanite, WPO without a WPO disable distance, collision and shadows on small plants. With `ue fix lods --billboard` (part of 42) to strip that LOD | [field] IslandHorror wrote `fix_foliage_lod.py` and `fix_foliage_wpo.py` for exactly these | M |
| 60 | **`ue foliage convert <regex\|tag>`**: StaticMeshActors into foliage instances (a foliage type per mesh, transforms kept, undoable) | [field] "convert scatter to foliage/HISM for perf": ~180 rock actors; Python cannot build HISM actors, so IslandHorror wrapped it in C++ | M |
| 61 | **`ue foliage add --mask <png> [--avoid <regex>:<radius>]`**: density from a greyscale mask over the landscape, with keep-out around actors (play area, paths) | [field] the forest is placed from `forest_mask.u8` by a custom script | M |
| 62 | **`ue drop --group`**: ground several actors (or an actor and what is attached to it) as one unit, by the lowest contact, so a multi-piece hut does not warp on a slope | [field] "multi-piece structures get each piece grounded independently, so on a slope they warp" | S |
| 63 | **`ue landscape export\|import <file.png\|.r16> [--flip-y]`** and **`ue landscape paint <layer> --mask <png>`**: heightmap and paint layers in and out of an existing landscape through render targets (`LandscapeExportHeightmapToRenderTarget`, `LandscapeImportHeightmapFromRenderTarget`, `LandscapeImportWeightmapFromRenderTarget`); needs a rendering editor (`--render` or windowed). Creating a new landscape needs C++ (`ALandscape::Import` is not script-callable): see 65 | [field] png support and north/east orientation are open IslandHorror follow-ups | M |
| 64 | **`ue water`**: water bodies and zones as text, with checks: a water body that edits the landscape (`affects_landscape` flattens a hand-made island), shoreline resolution (zone extent / water-info texture size, local tessellation), materials on the near and distant LOD meshes | [field] IslandHorror's terrain RULES list each of these as a lesson learned | S |
| 65 | **A small Tether C++ editor module** for what Python cannot do: create a landscape from a heightmap file, assemble HISM actors. Optional, so Blueprint-only projects are not forced to compile it | [field] IslandHorror carries its own `IslandTerrainTools` / `IslandFoliageTools` for these | L |
| 66 | **`ue wait compile`**: wait until shader and asset compilation has finished (after opening a big level or editing a material), printing what remains; `ue open` reports the backlog | [field] Lvl_Island must be opened once for its landscape and water shaders; the nav lock (0.20) also waits on asset compilation | S |

### 2I. From IslandHorror's visual pass (0.21, terrain, grass, forest, water, buoyancy, night lighting)
| # | Addition | Why | Effort |
|---|---|---|---|
| 67 | **Bug: `ue shot --hud` in a headless `--render` editor during PIE** captures the editor's panels with a black game view (plain `ue shot` works, HUD included). `--hud` crops a `shot showui` of the editor window to the viewport; offscreen, that crop goes wrong | [field] repro: `ue open --headless --render`, `ue pie start`, `ue shot --hud` | S |
| 68 | **`ue spawn ... --pie`**: spawn into the PIE world (deferred spawn on the game world), gone when PIE stops | [field] an A/B of Epic's BP_BuoyancyExample took stop PIE, place, restart, delete | S |
| 69 | **`ue build` / `ue doctor` name the process holding the Live Coding mutex** (`Global\LiveCoding_` + the UnrealEditor.exe path, so one project's Live Coding blocks editor builds of every project on the install): which editor or LiveCodingConsole, and its project | [field] ShooterDomination's LiveCodingConsole blocked IslandHorror's builds | S |
| 70 | **Landscape grass in `ue foliage check`**: per LandscapeGrassType variety, density as the engine uses it (**per 100 m²**, though the header says per 10 m²), cull and WPO disable distances, and the instances in view | [field] the first grass pass came out 10x too sparse | S |
| 71 | **`foliage check` material warnings**: Fab `M_MS_Foliage` instances with "use Distance Based WPO Fade" on (that permutation fails to compile, `Missing function input 'Function Distance Input'`, and instances fall back to the grey default); materials whose shaders failed to compile in general | [field] verified on IslandHorror's grass | S |
| 72 | **`foliage check` for InstancedSkinnedMeshComponents with no TransformProvider** (Megaplants without a DynamicWind data asset: every Nanite branch assembly collapses to the root) | [field] | S |
| 73 | **Buoyancy in `ue water`**: placed actors with a BuoyancyComponent but `bGenerateOverlapEventsDuringLevelStreaming` off (actors present at load get no begin-overlap, so they never enter the water and sink; Epic's BP_BuoyancyExample does), and lift against body mass (lift is normalised across pontoons: about BuoyancyCoefficient x one pontoon's submerged volume) | [field] both verified | M |
| 74 | **Setting uint32 and Blueprint-variable properties**: `ue set` round-trips uint32 fields Python cannot set (e.g. a grass variety's InstanceWorldPositionOffsetDisableDistance) through export/import text; `ue py`'s property hint also suggests Blueprint variables by display name ("Time of Day": names with spaces resolve only that way) | [field] | S |
| 75 | **`--res WxH` for `ue gpu`, `ue shot`, `ue perf`**: measure pixel-bound passes (BasePass, VSM raster) at a real resolution from the 799x521 offscreen viewport, e.g. by screen percentage to the same pixel count, saying how it was done | [field] "is the dense forest affordable at 1440p" without a package | M |
| 76 | **`ue wait grass` (or `ue shot --settle`)**: wait until landscape grass for the current view has been built after a camera move or a landscape material change (grass instances are GPU-only, so `get_instance_count()` reads 0) | [field] shots showed bare ground; the masks were blamed before PIE proved the grass fine | M |
| 77 | **Gotchas for the skill / `ue explain`**: UDS copies "Exposure Brightness Range" into its PostProcessComponent only at startup (write the component); Live Coding is per engine install, not per project | [field] | S |

### 2J. From ShooterDomination's 0.20 notes (menu, objectives, robots, debug menu, intensity director)
| # | Addition | Why | Effort |
|---|---|---|---|
| 78 | **Bug: `ue ui key Escape` in PIE stops PIE** (the editor's shortcut wins): send keys to the game's input; and **chords** (`ue ui key Ctrl+G`) reach the game's `FInputChord` bindings | [field] no way to open the pause menu or the debug menu from the CLI | S |
| 79 | **Bug: `ue shot --hud` ignores `ue pie target`** (the host's view with target 1) | [field] | S |
| 80 | **Bug: `ue game start --server none` still connects the clients** to 127.0.0.1: launch them on the map, without an address | [field] a solo `-game` run sat in a pending connection | S |
| 81 | **Small CLI fixes**: `ue --version`; `ue who --as NAME` confirms the name; `ue lease take --reason` as an alias of `--for`; `ue pie run -c "<code>"` (inline code, and a clear message for a missing file); `ue exec "shot showui"` prints the screenshot it wrote | [field] | S |
| 82 | **`ue map open <path> [--save\|--discard]`**: load another map into the running editor (refused during PIE, or with unsaved changes unless told) | [field] took a `ue py` load_level | S |
| 83 | **`ue game start --python`** (and say that `-game` from Tether disables editor Python): start-up scripts such as `init_unreal.py` then run as in Play > Standalone | [field] a crash only Standalone hit | S |
| 84 | **`ue build` (Live Coding) names other sessions' changed files** before compiling them (from the post-edit ledger and `ue who`), and asks for `--include-others` when an active session changed them in the last few minutes | [field] Live Coding patched another session's half-written reflected headers | S |
| 85 | **Link errors name their owner**: an unresolved `Class::Function` maps to the header declaring it and that header's last writer | [field] six LNK2001 lines, no owner | S |
| 86 | **`ue pie start` warns after Live Coding layout changes** ("class layouts patched since the last restart: UMG and replication may misbehave"), and `ue ui` with 0 widgets in PIE says the same | [field] a HUD that silently stopped drawing | S |
| 87 | **`ue props --pie` reads protected properties** (ToolsetLibrary, not `get_editor_property`), and **`ue pie watch Actor.Component.Property`** samples component properties in the same CSV | [field] | S |
| 88 | **`ue lint --cpp` flags reflected type names that differ only by prefix** (`FSDSetPiece` / `ASDSetPiece` both become `SDSetPiece`, which UHT rejects); the existing unity-collision rule goes into the skill | [field] the same UHT trap twice | S |
| 89 | **`ue game shot <n>`**: a screenshot of a `-game` window, the launcher's child process resolved, the movie player's loading screen included | [field] loading screens are invisible to PIE and to FScreenshotRequest | M |
| 90 | **`ue lint`: StaticMeshActors whose CollisionEnabled disagrees with their profile** (editor Python's `set_collision_enabled` is undone by the next PostEditChange) | [field] every decorative piece in two generated maps was BlockAll | M |
| 91 | **A `-game` smoke step**: load each map once in `-game` (in `ue test` or `ue package`): editor-only components are null there | [field] an access violation only `-game` and packaged runs hit | M |

### 2K. From ShooterDomination's 0.23 notes (debug HUD, call-ins, level polish, core-kill shatter)
| # | Addition | Why | Effort |
|---|---|---|---|
| 92 | **Bug: `build --restart --save` ended on the UBT line with no editor running**: check the relaunched editor came up, and say why not (its log, a crash, a dialog) | [field] `ue status` "not running" afterwards; `ue open` brought it back | S |
| 93 | **Bug: `ue shot --res` in a windowed editor says "a headless editor has none"** after a builder reloaded the map: redraw the level viewport and retry, and only blame headless when the editor is headless | [field] failed for minutes after `load_level` | S |
| 94 | **`ue build --check --files <paths>`**: compile just those translation units (UBT `-SingleFile`, no link), allowed while another session holds the lease | [field] a session could not check its own half-written files while another held the editor | S |
| 95 | **`ue ui click "OFF" --near "DEBUG HUD: DIRECTOR"`** (the widget closest to a text) and **`OFF#11`** (the 11th of several same-label widgets) | [field] nine identical ON/OFF toggle buttons | S |
| 96 | **`ue log --grep` includes Verbose lines** (it stopped at Log) | [field] a Verbose "took the jump pad" never showed | S |
| 97 | **`ue shot --frames N --interval S [--after "<exec>"]`**: a contact sheet of a short event or an animated material, game time slowed while capturing so short intervals are reachable | [field] a 0.1 s flash fell between shots; light-strip pulses | M |
| 98 | **`ue pie loop "<cmd; wait N; cmd>" --seconds 300 [--every 60 "<cmd>"]`**: console commands and waits run on the game thread, for soaks that do not drift | [field] ~250 `ue exec` round trips from bash, bursts drifting | M |
| 99 | **`ue git stage --mine`**: stage the files this session last wrote (the post-edit hook's record), naming the ones another session also edited | [field] the riskiest step when two sessions commit from one checkout | S |
| 100 | **`ue lint --cpp`: `BindKey` keys that an input mapping context also maps** (the mapping consumes the key; the binding never fires), with the editor's mappings when it runs | [field] Ctrl+G never fired; the menu moved to Insert | S |
| 101 | **`ue ui key --viewport`**: keys through the game viewport the way a keyboard sends them (one InputKey per press) | [field] | M |
| 102 | **`ue pie start` notes a HUD that is gone**: no UMG widgets after warmup when the last PIE run had some | [field] the first PIE after `ue open` showed 0 widgets, the next 34 | S |

### 2L. From UnrealRacing's notes (new C++ project from an empty folder, Fab UDS, generated city)
| # | Addition | Why | Effort |
|---|---|---|---|
| 103 | **`ue new project <Name> [--cpp] [--engine 5.8]`**: write the .uproject, Target.cs files, a Build.cs that works on 5.8 (`PublicIncludePaths.Add(ModuleDirectory)` so `#include "Folder/File.h"` resolves from the module root), Config defaults (Enhanced Input classes, a game mode), then run `ue init` | [field] the project was hand-written; the first `ue build` failed with 21 `C1083 Cannot open include file` because 5.8 does not put the module root on the include path | S |
| 104 | **`ue template add <Pack>`** (Vehicles, ConceptCar, Building, LevelPrototyping...): copy an engine template's shared pack from `Templates/TemplateResources/<Standard\|High>/<Pack>/Content` to its mount path (`/Game/Vehicles`), listing what it brings | [field] the mount path is only visible inside the .uasset package paths (found by grepping the bytes); the car meshes there are the only free vehicle art in a launcher install | S |
| 105 | **Re-check the "another project's editor blocks UBT" claim**: with ShooterDomination's editor and its LiveCodingConsole running on the same engine, `ue build` (this editor closed, UBT) succeeded twice. UBT's check (`HotReload.CheckForLiveCodingSessionActive`) needs an active Live Coding session for the same executable and is skipped entirely under `-NoHotReloadFromIDE`: pass that flag when building with the project's editor closed, and narrow the wording in the skill/README to when it really blocks | [field] the warning made the session plan a workaround it did not need | S |
| 106 | **`ue log` noise after `ue class` on a big Blueprint**: Ultra_Dynamic_Sky produced ~80 `LogJson: Property "..." type ... unhandled during Json schema generation` warnings (Epic's toolset schema generation); ship a default ignore for that shape, or avoid the schema pass in `ue class` | [field] they buried the real warnings in the next `ue log` | S |
| 107 | **Python gotchas for the skill / `ue py` hints**: `unreal.MaterialProperty` has no `MP_CUSTOM_DATA0/1` (clear coat), so clear-coat materials need Make Material Attributes plus `use_material_attributes`; `CollectionScalarParameter.parameter_name` raises, `get_editor_property('parameter_name')` works; a FontSampleParameter compiles only with a default `font` set | [field] three failed runs of a material generator | S |
| 108 | **A session without the Claude Code plugin enabled has no `ue` on PATH and no session-start note**; `ue init` could add the plugin to the project's `.claude/settings.json` (or print the one line that does), so later sessions in that folder get the hooks | [field] this session found Tether by listing the parent folder | S |
| 109 | **`shot` file names collide within one second**: `shot-<date>-<HHMMSS>.png` has no milliseconds or counter, so two shots in the same second overwrite each other | [field] lost frames when shooting several landmarks in a row | S |
| 110 | **`ue py` timeouts on generator scripts**: rebuilding 41 materials with a big level loaded takes ~3 min, past the 120 s default. On a timeout, say the script is still running in the editor and how to wait for it (`ue wait compile`, or re-attach), rather than reading like a failure | [field] one run looked failed but had finished | S |
| 111 | **`pie do hold IA_X` can't find Input Actions the PlayerController creates at runtime**; look them up on the PC and its mapping contexts too (`key W down` worked) | [field] | S |
| 112 | **`player` in `--watch player.X` / `ue props player.X`**: `--watch` binds before the pawn is spawned (samples read zeros) and `props` reports `player` as ambiguous; resolve it lazily to the possessed pawn and say which object was used | [field] | S |
| 113 | **A PIE session with no possessed pawn reads as Simulate**: on a title/menu screen whose view target is a camera actor, `shot` captures the editor viewport labelled "(Simulate)" and `--hud` refuses with "--hud needs PIE (ue pie start)" while `ue pie status` says running. Detect PIE by the PIE world and shoot the player controller's view target | [field] the front-end title screen could not be captured; the workaround was entering free roam first | S |
| 114 | **`pie do` failed one step with "unauthorized: pass the session token from Saved/Tether/editors/<pid>.token"** 25 s into a script (a `wait 10 game` step; earlier steps fine, the re-run passed). Re-read the token and retry once on an unauthorized reply | [field] | S |
| 115 | **Consecutive `key X tap` steps can merge into one press** when they land in the same frame (two `key Down tap` lines moved a menu cursor once). Space taps a frame apart in `pie do`, or document `wait 0.2` between taps | [field] a front-end script selected the wrong menu item | S |
| 116 | **Rewriting a Material Parameter Collection's parameter array zeroes those parameters in every material compiled against the old IDs**, silently (every neon sign went dark; compile clean, no warning). `ue mat --compile` / `ue lint` could flag Collection Parameter nodes whose ParameterId no longer exists in the collection, and `ue py` could warn when a script sets `scalar_parameters` on an MPC | [field] caught only by eye in a screenshot | S |
| 117 | **GeometryScript `create_new_static_mesh_asset_from_mesh` puts every triangle in one section regardless of material IDs**; only a later `copy_mesh_to_static_mesh` with `replace_materials` splits them. A `ue mesh` check ("1 section, N material slots") and a skill note | [field] a ferris wheel's neon rims rendered as plain white until the mesh was built a second time | S |
| 118 | **A cube scaled to kilometres gets no Lumen reflections** (a 5 km x 12 km x 20 cm water slab stayed black; the same material on 1.6 km tiles mirrors the skyline). `ue lint` / `ue lights` could flag primitives with scale over ~1000 or bounds over a few km | [field] took several experiments to isolate | S |
| 119 | **`ue lights` says "0 light(s)" with Ultra Dynamic Sky in the level**: count light components on Blueprint and builder actors, not light actors. The exposure note should also list every unbound post process with its priority: UDS's own components (priority 1) overrode the level's volume (priority 0) | [field] the exposure override did nothing until the priority was raised | S |
| 120 | **`shot --at x,y,z` lands inside buildings in dense maps** (3 of 4 landmark shots). A `--clear` option that backs the camera along the view line until a trace to `--look-at` is unobstructed | [field] | S |
| 121 | **`ue look <material>` renders in the level's lighting**: in a night level the preview sphere is black and reflections cannot be judged; offer `--studio` (neutral HDRI) | [field] | S |
| 122 | **Boot-error noise in `ue build --restart`**: "LogAutomationTest: Condition failed x2" from the engine's own UnifiedErrorTest appears on every 5.8 boot; ignore it by default | [field] | S |
| 123 | **The struct-array copy hint fires on correct code**: `ue py` printed "elements of struct arrays are copies..." for a loop that appended each edited copy to a new array that was then written back; only warn when the edited element is never used | [field] | S |
| 124 | **Live Coding patches do not survive a restart**: `ue close` could warn "N Live Coding builds since the last UBT build; run `ue build` now", so a project handed over (opened from the launcher) loads the code that was tested | [field] the hand-over needed a final UBT build after a dozen Live Coding fixes | S |
| 125 | **`ue git stage --mine` from the session's own transcript**: in ShooterDomination (no hooks there, see 108) it staged nothing of 91 files the session wrote with Claude's file tools, Python scripts and `ue py` builders. Attribute a changed file to the session whose Edit/Write named it, or whose command (Bash, `ue py`) was running when it was written; when several sessions were, say so | [field] ShooterDomination's rainy-night pass | S |
| 126 | **Compile-check C++ while another session holds the editor**: a second session adding UCLASSes must not touch Source/ (the first one's Live Coding builds compile every file there). It staged the work outside Source/, copied Source + Config + plugin descriptors to a throwaway project, overlaid the staged files and ran UBT on the game target there (25 s incremental; it caught 9 compile errors before the shared editor was touched). `ue build --check --overlay <dir>` could do exactly that | [field] UnrealRacing's UI pass ran beside its race play-test session | M |
| 127 | **`ue lease request`**: a way to ask the session holding the lease for a restart window ("new UCLASSes, needs `build --restart`"), shown in `ue status`/`ue who` and to the holder on its next `ue` command, with `ue lease grant` to hand over | [field] the two sessions agreed it by cross-session messages instead | S |
| 128 | **`ue find` with a folder glob**: `ue find "/Game/UltraDynamicSky/Blueprints/Weather_Effects/Weather_Presets/*"` answered 0 assets (13 exist; `ls Content/...` found them). Match package paths when the pattern starts with `/`, or say that patterns match names | [field] | S |
| 129 | **`ue class` on a big Blueprint is 50 KB** (Ultra Dynamic Sky: 915 variables, 562 functions): add `--functions` / `--vars` / `--grep`, and print function signatures (parameter names and types), which is what calling a Blueprint function by reflection needs | [field] the output overflowed and had to be grepped from a saved file | S |
| 130 | **`shot` in a `pie do` step file takes the rest of the line as the output path**: `shot --at 90000,55500,3800 --rot -89,0,0` wrote a PNG named `--at 90000,55500,3800 --rot -89,0,0` into the project root and reported ok. Parse `ue shot`'s options there (`--at`, `--look-at`, `--rot`, `--actor`), or refuse a path that starts with `--` | [field] a junk file in the project root; overhead shots of a corner mid-race had to be taken with `ue shot` between `ue wait N game` calls | S |
| 131 | **An editor the user opens mid-session becomes every session's default target**: this session had worked for hours in its own `ue open --headless` editor (holding the lease) when the user opened the project from the launcher; its next `ue pie do -f ...` without `--pid` went to the user's editor, started PIE there, ran `exec NRRace 3` and failed on "unauthorized". `chosen_editor` prefers a hand-opened editor by design; keep a session's target sticky (the editor it opened or last used), and make `pie start`/`pie do` in a hand-opened editor need `--pid` while the session's own editor is up | [field] a race started and stopped in the user's editor; every later command needed `--pid` | M |
| 132 | **`--pid` lookups fail now and then: "no editor with pid 74540 is open on this project (running: 116488)"** one minute into a race test, with both editors running. The editor replaces `editors/<pid>.json` every heartbeat (`os.replace`, already retried on its side); a CLI read that lands on the replace gets an OSError on Windows, `read_json` returns None and the beacon is dropped. Retry beacon reads a few times (and never `forget_beacon` on a failed read) | [field] a 4-minute race test failed at step 17 and had to be rerun | S |
| 133 | **Which editors run stale code**: the user opened the project at 10:57 from the launcher and got the 10:47 UBT build, without the session's later Live Coding patches; a final UBT build cannot run until that editor closes. `ue status` could flag editors whose loaded module is older than the session's last Live Coding patch, and `ue build --when-closed` queue the UBT build for when the last editor exits (see 124) | [field] the hand-over had to ask the user to close their editor | M |
| 134 | **`ue collide x,y,r [--z cm] [--step cm]`: a picture of what blocks at a given height around a point**, coloured by class. `ue map --footprints` saw one footprint (the ground slab) in a generated city: road and junction meshes (ProceduralMeshComponent with collision) and HISM buildings are left out; include those too. The session built this by hand from 25,600 `box_overlap_actors` calls to find why AI racers got stuck: it showed a ring of wall across every elevated junction and raised medians where racers ran wide | [field] it found the junction-wall bug that wrecked every car on the expressway | M |
| 135 | **`ue world probe` distances are to actor origins**: "CityBuilder (NRCityBuilder, 142511 cm)" inside a 500 cm probe radius. For a builder or road actor spanning kilometres, give the distance to its nearest blocking geometry, or the component's, not the pivot's | [field] read as "nothing near" at a stuck car | S |
| 136 | **`ue props` leaves out specifier-less TArray properties**: `ue props NRRaceManager_0 Racers --pie` answered "74 props, 0 match" for `UPROPERTY(Transient) TArray<FNRRacerStatus> Racers` (and `Gates`, a `TArray<TObjectPtr<>>`), while the same actor's `UPROPERTY(Transient) TObjectPtr<> Markers` was listed and an `EditAnywhere` array elsewhere shows fine. Show them (count, plus the first elements' fields), so state like a race's standings can be read and `expect`ed without test-only mirror properties | [field] the session added `RacerCount`/`FinishedCount`/`PlayerCheckpoint` UPROPERTYs for its `expect` steps | S |
| 137 | **`expect`/`pie watch` on an actor with a dynamic multicast delegate log `LogJson: Warning: Property "OnPlayerFinished" type FNRRaceEvent unhandled during Json schema generation`** on reads of other properties (watching at 5 Hz repeated it). Skip delegates when building the schema, or build it once per class (see 106) | [field] | S |
| 138 | **`ue actors <regex>` matches labels only**: log lines name level actors by object name (`wrecked (NRRoadNode_1031)`), but `ue actors NRRoadNode_1031 --pie` found nothing (its label is `N_X_2_4`). Match object names too, and print both when they differ | [field] | S |
| 139 | **`ue py` hint for CallInEditor functions**: `node.rebuild_junction()` raised AttributeError (a `UFUNCTION(CallInEditor)` without BlueprintCallable has no Python name); `node.call_method('RebuildJunction')` works. When an AttributeError names a UFUNCTION the class has, print the `call_method` form | [field] | S |
| 140 | **The `--hud` fallback note is wrong for canvas HUDs**: `shot` steps under `pie do --hud` printed "this is the viewport's own capture, with UMG but without canvas-drawn text (AHUD, ...)" while the PNG showed the AHUD-drawn results screen. Check what the capture contains before saying so | [field] results screenshots that were fine looked suspect | S |
| 141 | **A step-file `log` step that counts**: race tests wanted "every rival finished" and checkpoint lines in order; `log` finds one match. `log <regex> count >= 6` (or `in order: re1, re2, ...`) would cover it without the game adding summary lines for tests | [field] the race manager gained a "complete: all N racers finished" line for the tests | S |
| 142 | **`ue open --second` (or `--new`)**: start a separate headless editor next to a shared one, on its own MCP port, for isolated work (scene captures on a studio map, a throwaway import). Today `ue open` always attaches to the open editor ("already open; using it"), so work that needs another map has to take the shared editor's lease and switch its level away and back | [field] room cubemap captures for window interiors had to queue behind two sessions' leases | M |
| 143 | **Fab's "Allows usage with AI: No" flag**: `ue fab library` / `fab add` copied a NoAI-tagged pack without a word, and the user then decided not to use it in an AI-driven project. The vault manifest does not carry the flag; `fab add` could print the listing link with "check its AI-usage terms first", or read the flag from the listing page when online | [field] Tokyo Stylized Environment was copied in and removed again | S |
| 144 | **`ue fab remove <pack>`**: deleting a copied pack while the editor runs left four files locked (`IA_*`/`IMC_*`: the editor loads every Input Action it discovers) and `ue rm` said "no asset at ..." because the asset registry had already dropped them. Unload the packages and delete through the editor, or queue the leftovers for when it closes | [field] | S |
| 145 | **Step files can't send Slate keys**: `key Enter` in a step file goes to the player controller (Enhanced Input forced keys), and `--slate` exists only on `ue ui key`. Hand-built UMG menus read keys in `NativeOnKeyDown` and move by Slate navigation, so they get nothing: `key Enter` left a title screen up, while `ue ui key --slate Enter` worked. Add `key --slate X` (or a `ui-key X` step) to step files; maybe choose Slate on its own when PIE's input mode is UI-only | [field] the UMG front-end script could not get past the title; the whole menu pass was driven by hand, one `ue ui key --slate` call at a time | S |
| 146 | **Synthetic clicks focus a button but don't fire it in a covered windowed editor** (likely cause, not proven): `ue ui click b123` in the user's windowed editor moved focus to the button twice and never clicked it. Epic's SimulateClick moves the real OS cursor, then sends move, down and up; SButton fires on mouse-up only if `IsHovered()`, and Slate updates hover only when the OS cursor is directly over a Slate window. Another app's window over the editor at that point gives a press without a click. The covering-window check only looks at Slate windows: also check the OS window under the point (WindowFromPoint) and fire OnClicked there (the `--event` path), or say why nothing happened; and accept `click --event LABEL` in step files | [field] the menu test fell back to Slate keys | S |
| 147 | **Buttons with no text child have no label in `ue ui`**: Epic's inspector names a widget by its accessible text or a single STextBlock child. A menu row built as Overlay[background, text row, transparent UButton on top] lists as `button (b120)`, so `click GARAGE` finds nothing (the text is a sibling). For an unlabeled button, fall back to the text widgets inside its rectangle. Skill note: give such buttons an accessible name (`SWidget::SetAccessibleBehavior(EAccessibleBehavior::Custom, Text)`), which screen readers use too | [field] the menus gained a UButton subclass with accessible labels so scripts can click by name | S |
| 148 | **Lease requests repeat on every command**: while holding the lease, each `ue` call printed both pending requests (two notes, wrapped over 4-6 lines) before its own output, through a 40-call menu pass. Print a request once per session, and again when it changes or has waited N minutes; `ue status`/`ue who` keep the full list | [field] the notes buried short outputs like `pressed Enter` | S |
| 149 | **`ue ui at <x,y>` points at positions `ue ui` doesn't print**: its usage error says "absolute screen pixels, as ue ui shows positions", but neither `ue ui` nor `ue ui --json` gives positions or rects. Add rects to `--json` (and a `--rects` flag), or reword the hint | [field] checking where a click landed needed the rect | S |
| 150 | **`ue collide` dies with a CLI traceback on its default grid**: `ue collide -39694,-83572,16000 --z 1265 --half 45` (160 cells across, 25,600 boxes) ended in `json.decoder.JSONDecodeError: Expecting value` in `py_json` (transport.py:353) at all four expressway corners, and so did radius 3000; `--step 400` (80x80) and `--step 40` (75x75) worked, `--step 25` at radius 1500 (120x120) failed. Find the limit (reply size or time budget) and chunk the grid or keep the default under it; when the editor's reply is not JSON, say so in one line with what it was | [field] checking deck clearance at four ring corners needed hand-picked `--step` values | S |
| 151 | **`ue wait lease`**: return when the lease is free or granted to this session and PIE has stopped (with `--timeout`), so a queued session can resume without polling `ue lease` and `ue pie status` itself. This session's hand-written loop never ended: `grep running` also matches "pie: not running" | [field] it waited ~25 min behind two leases (a restart + UMG play-test, then a material rebuild) | S |
| 152 | **World subsystems from editor Python**: 5.8's Python has no `unreal.SubsystemBlueprintLibrary`, so there is no obvious handle on a `UWorldSubsystem` to call its UFUNCTIONs (here `UNRRoadNetworkSubsystem::Rebuild`, to refresh the editor world's road graph after reshaping roads). `[o for o in unreal.ObjectIterator(unreal.X) if o.get_outer() == editor_world]` works; a `tether.world_subsystem(cls, pie=False)` helper, a skill note and a `ue py` hint on that AttributeError would save the detour | [field] | S |
| 153 | **`ue validate` with no paths needs git**: in a project that is not a git repository it stops at "git rev-parse: fatal: not a git repository ... pass asset paths or folders instead". Without git, fall back to the unsaved packages, the packages saved in this editor session, and Map Check of the open level | [field] UnrealRacing has no repository; `ue validate /Game/Maps/L_HikariBay` had to be named by hand | S |
| 154 | **`pie do --hud` shots leave Tether's own warning in `ue log`**: after two step files with 7 `shot` steps, `ue log --level Warning` listed `LogConsoleManager: Performance warning: Many (31) failed FindConsoleObject() for 'shot'` next to the game's warnings. core.py already drops the FLUSHLOG lookups; drop this one too, or capture without the console lookup | [field] the warning list after a clean race test had one entry that was Tether's | S |
| 155 | **`until`/`expect` can't call getters that `pie watch` samples**: `until player.Movement.GetSpeedKmh() > 100 within 15` failed with "NRVehicle_0.Movement has no property 'GetSpeedKmh()'", while `pie watch player.Movement.GetSpeedKmh()` in the same run read it fine. Use one expression reader for both, so a step file can wait for a speed, a gear or a state a getter exposes | [field] the handling tests fell back to fixed `wait N game` times measured from a separate acceleration run | S |
| 156 | **A failed `pie do` leaves keys held**: `key W down` followed by a failing step left W pressed, so the next step file's car was already doing 31 km/h when it started. Release every key and action the step file pressed when it ends or fails (or say which are still down) | [field] one baseline run had to be thrown away | S |
| 157 | **`pie watch` splits rotators into `.x/.y/.z` columns** (`player.GetActorRotation().x`), so which one is yaw is a guess; name them `.pitch/.yaw/.roll` (and vectors `.x/.y/.z`) | [field] the analysis script picked "the column that moves most" | S |
| 158 | **`--overlay staging/` collides between sessions**: the skill suggests `staging/`, which on Windows is the same folder as another session's `Staging/` (case-insensitive). That one held a menu pass's staged files plus an `integrate.py` that copies everything under it into Source/, and this session copied a header in before noticing. Default to a per-session overlay folder (e.g. `Saved/Tether/staging/<session>/`), or have `--overlay` refuse a folder holding files another session wrote | [field] UnrealRacing's handling session, beside the menus session | S |
| 159 | **The lease expires under an active holder**: it ran out while this session was between a C++ restart, handling tests and a city rebuild (it had issued `ue` commands every minute or two), and the next session in the queue only noticed by reading `ue status`. Extend the lease while the holder keeps issuing commands, and warn the holder a couple of minutes before it ends | [field] the queued session had to message to ask whether it could take the editor | S |
| 160 | **0.27's Slate key route (145) misses focus held by a user widget itself**: on the menu buttons it worked ("pressed Down through Slate (button "FREE ROAM" has keyboard focus)"), but a title screen that takes focus itself (a focusable UUserWidget with no buttons) never appears in the inspector snapshot, which skips containers with no role or label. So `key Enter` went to the game and the title stayed up, while `--slate` worked. Ask Slate for the focused widget path (`FSlateApplication::GetUserFocusedWidget`), and route through Slate when it is inside the PIE game layer and is not the SViewport | [field] first live test of 145, in UnrealRacing's menu re-check | S |
| 161 | **`click --event` finds no buttons in C++-built UMG, so 146's fallback can't fire**: the covered-window check worked and named the window ("covered by window "general-studio (Channel) - Slack""), but its OnClicked fallback failed. `ue ui click --event GARAGE` answered "no visible button 'GARAGE' (buttons: )" (an empty list) while `ue ui` showed `button "GARAGE"`. These buttons are a UButton subclass inside a row UUserWidget inside the screen's UUserWidget: the UMG walk seems to miss nested user widgets' trees, or subclasses of UButton. Walk every UserWidget's WidgetTree (recursing into child user widgets) and match on accessible text as `ue ui` does. 147's text-under-button labelling couldn't be tested: the menu buttons now carry accessible labels | [field] first live test of 146 | S |
| 162 | **`ue mat graph` names the wrong output when two inputs share a source node**: a Custom node fed `FO <- FacadeOverride.RGB` and `FOA <- FacadeOverride.A` printed both as `#23.RGB`, and so did `MaterialEditingLibrary.get_input_node_output_name_for_material_expression` (it returns the first link to that node). Read each input's `OutputIndex` from the FExpressionInput, so a wrong-channel wiring bug and a correct one look different | [field] a correct material looked miswired; a landmark screenshot plus the engine's output list settled it | S |
| 163 | **`ue mat graph` "not connected to an output" lists 20 stale nodes on a generator-built material** (M_NR_Building: 43 nodes, 23 used; M_NR_Neon 14 for ~8). The generator calls `MaterialEditingLibrary.delete_all_material_expressions` before rebuilding, so either that call leaves some behind or they come from older builds. A `ue mat graph M prune` (delete nodes that reach no output, one undo step) and a `ue lint` rule for materials with many unused nodes | [field] | S |
| 164 | **`ue gpu --ab "<cmd A>" "<cmd B>"`**: an A/B frame-time comparison at the same camera, e.g. a material parameter on and off. Done by hand it was gpu, `mat graph set`, wait compile, gpu, restore, save: 6 commands and a dirty asset to remember | [field] interior-mapped windows cost ~0.13 ms at a street view (6.89 vs 6.76 ms) | S |
| 165 | **`ue log` drops the reason line under a multi-line warning**: it showed `LogMaterial: [AssetLog] ...M_NR_SignPanel.uasset: Failed to compile Material for platform PCD3D_SM6, Default Material will be used in game. x24` but not the tab-indented next line, `(Node Custom) Custom material NR missing input 1 (WP)`, which is the whole explanation (and shows the 24 warnings were transient, logged while a generator wired a Custom node's inputs one by one). Keep indented continuation lines with their entry; when the material now compiles clean (`recompile_material` returns no errors), say the warning is stale | [field] a working material looked broken; the session compiled the HLSL standalone with DXC before finding the reason in the raw log | S |
| 166 | **`ue gpu` / `ue perf --editor` have no camera options**: a before/after timing at a fixed street view needed `ue py "...set_level_viewport_camera_info(...)"` before every run. Take `--at x,y,z --look-at/--rot` like `shot`, or `--camera <shot baseline name>` to reuse a baseline's camera | [field] UnrealRacing's street-dressing pass, 10 timing runs at two views | S |
| 167 | **`build --restart` blames the build for an unrelated startup crash**: UBT succeeded, then the reopened editor crashed in ProceduralMeshComponent rendering (the same signature as a 09:45 crash before any of this session's changes) and the command ended "error: editor exited during startup (code 3)" with a callstack tail. Say the build succeeded and the crash is at startup, compare its signature with earlier reports in Saved/Crashes ("seen before this build at 09:45"), and offer `ue open` | [field] the session had to read the crash folders to rule its own code out | S |
| 168 | **`build --check --overlay <dir>` counts files it shouldn't and doesn't name them**: "5 staged file(s)" for 4 source files plus a checksum file in the folder root. List the staged files, and only overlay Source/, Config/ and Plugins/ (copying the rest into the project is how stray files land there) | [field] | S |
| 169 | **`ue actors` exits 0 and echoes the pattern when nothing matches**: `ue actors --pie '^SkidPad$'` printed "0 actors matching /^SkidPad$/" with exit 0, so the handling suite's `... | grep -q SkidPad` saw the name and never spawned the pad (the car fell out of the world in every test). Exit 1 on no match (as `ue find` should too), or keep the pattern out of the no-match line | [field] UnrealRacing physics session: 20 min of nonsense test data before the cause was clear | S |
| 170 | **No `--pie` on `ue delete`** (`ue spawn --pie` exists): a test that spawns a wall in PIE cannot remove it, and repeated runs stacked walls until `ue set --pie CrashWall` failed with "ambiguous 'CrashWall'". Add `delete --pie`, and let `spawn --pie --replace` reuse a label | [field] crash tests on a PIE-only test wall | S |
| 171 | **Spawning from a `pie run` script**: Python has no way to spawn an actor in the PIE world (`GameplayStatics.begin_deferred_actor_spawn_from_class` is not exposed; EditorActorSubsystem spawns in the edited level). A `tether.spawn(cls_or_asset, transform, label=)` in the `pie run` namespace, the same thing `ue spawn --pie` does | [field] | S |
| 172 | **`pie status` text makes a fragile check**: "pie: not running" contains "running", so `grep -qi running` in a test runner thought PIE was up and the next `pie run` started (and stopped) its own PIE under the script. `ue pie status --json` (or exit 0/1 for running/not) for scripts | [field] | S |
| 173 | **Long PIE-side tests from `pie run`**: a handling test runs from a slate post-tick callback for 10-30 s (inputs every frame, samples, a JSON at the end) while the shell polls for the file. A `tether.run_until(tick_fn, timeout)` that keeps the call open (or a `ue pie run --wait-file PATH`) would replace the polling loops; and note in the skill that a POSIX path like /d/Projects/... in a script resolves to C:\d\... in the editor (results landed under C:\d) | [field] UnrealRacing tests/handling/drive.py, crash.py, traffic.py | S |
| 174 | **`pie do` `call` drops struct fields named as in C++**: `call player K2_SetActorRotation [{"Pitch":0,"Yaw":-3.66,"Roll":0}, true]` and `call player.Collision SetPhysicsLinearVelocity [{"X":2217,"Y":-142,"Z":0}, false, "None"]` both reported ok, but the car kept its yaw and got no velocity: only snake_case keys (`yaw`, `x`) reach the struct, other keys are silently ignored and the struct stays zero. Map keys case-insensitively and from the C++ names (`Yaw`, `X`), and fail the step on a key the struct does not have; also accept `[p,y,r]`/`[x,y,z]` lists in UE order | [field] UnrealRacing's signal-pole smash test missed its pole three runs in a row before the cause was clear | S |
| 175 | **`build --check --overlay` with no folder ignores `ue who --as`**: after `ue who --as signals` it looked in `staging/114fc78d` ("nothing under ...") and left that empty folder behind (it came back after `rmdir`), while sessions name their folders `<name>-<id>` (`dressing-9ad399cf`). Look for `<name>-<id>` too (or say which folder it expected and that `--overlay <dir>` takes one), and don't create the folder on a check | [field] | S |
| 176 | **`pie run` crashes when the script prints a JSON document**: a script ending in `print(json.dumps(out))` made `ue pie run checks.py` die with a CLI traceback (`play.py` pie_run: `'dict' object has no attribute 'rstrip'`), so the result was lost; `ctx.tether("py")` hands back the parsed JSON and pie_run assumes text. Prefixing the line (`print("RESULT " + json.dumps(out))`) worked around it. Print a dict/list result as JSON instead of assuming a string (and `ue py` likely has the same path) | [field] | S |
| 177 | **`pie do -f -` (steps from stdin)**: `ue pie do --keep -f - <<EOF ... EOF` answers "error: no step file -", so two-line throwaway step lists (`exec NRFreeRoam` / `wait 3 game`) need a temp file each. Read steps from stdin for `-`, as `ue py -` does | [field] | S |
| 178 | **A `--second` editor blocks saves in the shared editor**: while `ue open --second --render --map L_HikariBay` loaded the map, the shared editor (mid material build in another session) failed to save four materials in 33 s: `LogFileManager: Error moving file '.../M_NR_LandmarkSign.uasset' to 'Saved/...tmp'`, then "failed to save" dialogs answered by default. An editor keeps loaded packages' files open for lazy loading, and SavePackage can't move the original aside. Saves worked again 3 s after the second editor exited. `--second` should warn when another editor is building or saving content (and say saves there will fail while it runs). Better: load from a snapshot of the packages it needs, or make the loader let go of file handles (load bulk data eagerly, then `ResetLoaders`). The shared editor's save could also name the process holding the file | [field] it broke another session's material build; the four materials had to be saved again | M |
| 179 | **`ue open --second` reports the shared editor's errors as its own boot errors**: "ready in 25.2s ... 2 errors at boot" listed the other editor's `LogSavePackage` failures from `UnrealRacing.log`, while the new editor writes `UnrealRacing_2.log` (where `ue log --since start` correctly found nothing). Scan the new editor's own log. Errors in the shared log that start during the boot are still worth a separate line ("meanwhile the shared editor logged: ...") — that line is what exposed 178 | [field] | S |

### 2G. Code (C++ and C#)
| # | Addition | Why | Effort |
|---|---|---|---|
| 45 | **`ue modules`**: module dependency graph, with missing-dependency hints for link errors | [field] a missing module dependency was only visible in LiveCodingConsole.log | S |
| 46 | **Blueprint-to-C++ hot paths**: rank Blueprint functions by time from `bp trace` plus perf, and suggest which to move to C++ | [idea] | S |
| 47 | **Angelscript trial** | [roadmap] DESIGN.md §8 "later pass" | L |

---

## Priority 3: platform, release and infrastructure

| # | Addition | Why | Effort |
|---|---|---|---|
| 48 | **Verify UE 5.4 to 5.7 on a real install** (the remote-execution transport is only tested against 5.8 with MCP off) | [gap] DESIGN.md: "not yet on a real 5.4–5.7 install" | M |
| 49 | **macOS and Linux runs** of the CLI and selftest | [gap] Windows and WSL unit tests only | M |
| 50 | **Mobile packaging** (`ue package --platform Android`), device install and boot check over adb | [idea] | L |
| 51 | **Gauntlet and functional tests**: create and run AFunctionalTest maps, and run Gauntlet tests from `ue ci` | [idea] | M |
| 52 | **Scheduled local soak and CI** (Windows Task Scheduler, no hosted CI): nightly `ue ci --play --budget` with a report file | [idea] respects the no-billing rule | S |
| 53 | **Fab license report**: which Fab and marketplace content ends up in the cooked build, with the license type of each | [idea] shipping compliance | M |
| 54 | **Your full Fab library, without tokens**: a small C++ editor helper that reads the Fab plugin's own TEDS library rows after its own sync | [gap] Python cannot read TEDS; Tether must not handle the token | L |
| 55 | **Tether as an MCP server** (`ue mcp serve`) for agents other than Claude Code | [idea] | M |

---

## Agent ergonomics (any priority)
| # | Addition | Why | Effort |
|---|---|---|---|
| 56 | **`ue explain <error text>`**: known engine errors and gotchas (from TetherNotes, DESIGN.md and our fixes) with the fix | [idea] | S |
| 57 | **Local usage stats** (`ue stats`): which commands are run and fail most, kept on this machine only, to steer priorities | [idea] | S |
| 58 | **Shorter outputs by default** where commands can exceed about 40 lines, with `--all`; audit every command | [idea] token budget | S |

---

## Deliberately not proposed
- **Hosted CI** (GitHub Actions and similar): ruled out (no billable services).
- **Driving the Fab store page**, or reading Epic tokens or cookies: ruled out (no purchases or claims without the user).
- **Desktop-level input** (clicking the user's screen): agent-launched editors stay headless; the user's own editor
  is touched only when they ask.

## Questions for the review
0. The four 0.19 bugs (19a, 19b, 19d, 19e) are small: fix them in a 0.19.1 before the next round?
1. Is the shared-editor lock (1–4) the next round, as both sessions that collided suggest?
2. Should the real-time film (7) and the rendered map (35) share one spike into offscreen scene capture?
3. Networking (20–23) and GAS/AI (24–27): which first, given ShooterDomination's direction?
4. Is a second packaging fixture (14) acceptable as another small project in the repo?
