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

---

## Priority 1: asked for, or blocking real work

### 1A. Several sessions sharing one editor
Two Claude sessions now share the ShooterDomination editor, and they have already collided: PIE started under
another session's builder script, and a `build --restart` ran while another session was waiting.

| # | Addition | Why | Effort |
|---|---|---|---|
| 1 | **`ue lock <pie\|build\|edit> [--by NAME] [--for MIN]` / `ue unlock`**: an advisory lock in `Saved/Tether/locks/` that `pie start`, `build --restart`, `close`, `py -f` and `import` respect (they wait, or refuse with who holds it and since when) | [field] a builder ran into another session's PIE; a restart ran while another session waited | M |
| 2 | **`ue who`**: which clients used the editor's MCP port in the last few minutes (session token use, last command, PIE owner) | [field] "a restart that warns when another client used the MCP port in the last few minutes" | S |
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
