# Tether

**Drive a running Unreal Editor from a coding agent: fast, token-frugal, and with no editor restarts.**

Tether is agent-native tooling for Unreal Engine 5.8+. It started as the missing pieces on top of Epic's
experimental in-editor MCP server (editor lifecycle, build routing, compact output, log cursors). It now covers
the whole loop a game team runs in the editor:

- building C++ and C# without restarts;
- reading and editing levels, Blueprints, materials and data as text;
- play testing in single-player and multiplayer PIE, scripted or recorded;
- screenshots and visual regression;
- world building (landscape, foliage, navigation, lighting);
- content checks, performance budgets and CI;
- audio, packaging and standalone multiplayer sessions;
- several agents and people sharing one editor and one Perforce depot.

All of it is one shell command, `ue`, with about 100 subcommands. Each prints a few lines, not a log dump.
It works with Claude Code out of the box. Any agent that can run a shell command can use it.

```
$ ue status
Sandbox | ready | UE 5.8.4-58902052 | pid 16348 | map /Temp/Untitled_1 | mcp :18873 up | heartbeat 0.6s ago
$ ue call tether.level_summary
/Temp/Untitled_1: 74 actors, 11 classes, PIE off
LandscapeStreamingProxy x64, WorldDataLayers x1, WorldPartitionMiniMap x1, DirectionalLight x1, ...
$ ue props DirectionalLight.LightComponent0 "^intensity|^temperature"
DirectionalLight.LightComponent0 (DirectionalLightComponent, 118 props, 2 match)
temperature: number = 6500
intensity: number = 6
```

## Why

Compiling isn't what makes Unreal slow for agents; the round trip into the engine is. Without a live
editor, every question about project state costs a headless commandlet boot, and every reflection change
costs a full editor restart. Tether keeps one editor running and talks to it.

| Loop | Without Tether | With Tether |
|---|---|---|
| Run editor Python / read project state | 12–24 s headless commandlet, ~1,900 log lines | **0.2–0.85 s**, a few lines (~0.03 s each in `ue batch`) |
| C++ function-body change | Live Coding by hand; the real error only appears in a separate UBT log | **~5 s** Live Coding via `ue build`, one-line diagnostics (the session's first: 8–17 s) |
| Reflection / `Build.cs` change | close, rebuild and reopen by hand | **~50 s** automated `ue build --restart`, compile-checked first, zero clicks (~35 s with `--no-check`, 20–30 s when no code changed) |
| C# change ([UnrealSharp](https://github.com/UnrealSharp/UnrealSharp)) | no agent-callable reload; restart the editor | **0.4–1.2 s** hot reload via `ue build` |
| Run a project's automation tests | separate headless test run, thousands of log lines | **~2 s** for a small suite in the live editor: one summary line plus `file:line` per failure |

A restart saves no compile time: it builds the same code as doing it by hand. What it saves is the hands, and it
keeps the editor open when the build would fail.

In a head-to-head on the same three tasks against Epic's official Claude Code plugin, an agent reads **at most
~40 KB** with Tether (the skill, the references those tasks touch, ~1 KB of tool output) and **~101 KB** with Epic's
plugin (its skill, ~94 KB of tool discovery and output). The gap is discovery: once Epic's schemas are loaded, a
call returns about as little as `ue` does. Epic's tools also could not reload C#: `CompileLiveCoding` reported
`NoChanges`, and the next PIE lost the C# subsystem. See [docs/COMPARISON.md](docs/COMPARISON.md).

Numbers come from the author's machine (Windows 11, UE 5.8.4), re-measured on the bundled `Sandbox/` project on
2026-11-05. The 24 s commandlet and 0.85 s read are from a private mid-size C++/Blueprint game in October, on 5.8.3.
Methodology is in [docs/DESIGN.md](docs/DESIGN.md).

## What it does

A tour by area. Every command, with all its options, is in **[docs/COMMANDS.md](docs/COMMANDS.md)**, and
`ue help <command>` prints the same from the CLI.

**Code without restarts.** `ue build` uses Live Coding when the editor is up and UBT when it isn't, and
hot-reloads C#. `ue build --restart` handles reflection and `Build.cs` changes: it compiles the game target
first and keeps the editor open if that fails, then saves, closes, builds and reopens the same map.
`ue build --check --files A.cpp` compiles single files without linking. Compiler and linker errors come back one
per line: an unresolved symbol names the module to add to `Build.cs`, and each file with an error says who last
wrote it. `ue crash` explains why the editor died. `ue lint --cpp` catches unity-build and UHT name clashes before
the compiler does. [More](docs/COMMANDS.md#editor-lifecycle-and-builds)

**Read and change the editor.** `ue py` runs unrestricted editor Python with a persistent namespace. `ue exec`
runs console commands. `ue props` and `ue set` read and write reflected properties on actors, components and
assets, including ones Python can't reach (struct fields inside arrays, non-Blueprint UPROPERTYs). Each `ue set`
is all-or-nothing and one undo step. You can also spawn, drop to the ground, scatter, duplicate and delete actors.
`tree`, `class`, `net` (replication), `trace` and `collision` explain what an actor is and why it does what it
does. `ue call` reaches any of Epic's ~830 registered tools, and `ue tools` lists them as one-line signatures.
[More](docs/COMMANDS.md#inspecting-and-editing-the-level)

**Graphs as text.** Blueprints read and diff as text (`ue bp`), take edited text back and compile (`--apply`),
and gain variables, components, functions and dispatchers in one line (`ue bp edit`). `ue bp check` compiles every
Blueprint and lists only the broken ones; `ue bp trace` lists which events ran during PIE. Animation Blueprints
(`ue anim graph`), StateTrees, material graphs (`ue mat graph`, with small edits by node), PCG graphs and Niagara
systems all read as text too. [More](docs/COMMANDS.md#blueprints-anim-blueprints-and-statetrees)

**Play testing.** `ue pie start --clients 2` runs multiplayer PIE headless. `ue pie do` drives the player one
step per line: move along the navmesh, press input actions, send real keys, mouse and sticks, click and type in the
game's UI, call functions. It checks results with `expect`, `until` and `log`, and exits 0 or 1. `ue pie record`
turns a session you play into a replayable step file. `ue pie watch` samples values as timelines. Step files with
a `# watch:` line become gameplay regression tests against a committed baseline, and `shot-check` adds visual
checkpoints. Also: `ue ui`, `ue input`, `ue ai`, `ue anim`, `ue soak` for long runs, and `ue test` for automation
tests in the live editor. [More](docs/COMMANDS.md#play-testing)

**See the game.** `ue shot` saves a PNG and prints its path, so the agent opens it only when needed. It can capture
the player's view with the UMG HUD, a spectator camera anywhere, a debug view mode (wireframe, Nanite, Lumen,
shader complexity, ...), bursts and contact sheets, or any editor window. `--baseline` and `--compare` give pixel
diffs with a red-marked image. `ue look` renders an asset from six angles. `ue map` draws the level from above,
even in a headless editor with no renderer. [More](docs/COMMANDS.md#screenshots-and-visual-checks)

**Build worlds.** Sculpt, import, export and paint landscapes; add, check and fix foliage (billboard LODs, cull
distances, wind); scatter props onto the ground. Load World Partition regions, check water bodies, find navmesh
paths and the spots they can't reach, and build nav, lighting and HLODs. `ue lights` and `ue post` show what lights
a spot and why it looks dark; `ue level check` finds floating, buried and stacked props and invisible walls.
[More](docs/COMMANDS.md#world-building)

**Manage content.** Find assets, list their references and every place a C++ symbol is used. Import meshes,
textures and sounds without dialogs. Create assets, whole projects, or the engine's template content. Edit data
assets and DataTables as CSV or JSON with a row diff, set material parameters, and read meshes, skeletons, textures
and sounds by type. `ue unused` finds what nothing reaches, and `ue move-pack` relocates a marketplace pack with
its references fixed. [More](docs/COMMANDS.md#assets-and-content)

**Gate quality.** `ue validate` runs Unreal's data validation and Map Check on what changed. `ue lint` reads asset
rules from the registry (5,000 assets in about half a second) and fixes the safe ones. `ue ci` runs tests, compiles
Blueprints, plays scripted tests, validates and cooks as one pass/fail command; a sample self-hosted workflow is in
[docs/ci/unreal-ci.yml](docs/ci/unreal-ci.yml). [More](docs/COMMANDS.md#content-checks-and-ci)

**Measure performance.** `ue perf` captures frame, game, render and GPU time in PIE. `--trace` adds Unreal
Insights' most expensive scopes, `--scalability` compares levels, `perf load` lists streaming hitches and what
loaded in each, and `perf startup` shows where editor boot time went. `ue gpu` gives time per pass, and
`gpu --ab` compares two settings at the same camera. Limits in `Config/Tether/budget.json` make `ue ci --budget`
fail when the game gets slower or bigger. [More](docs/COMMANDS.md#performance)

**Hear it.** `ue audio --log` records what played in PIE and how the mixer resolved it, flagging stereo sounds
played in 3D and sounds with no attenuation. `ue audio --bounce` records PIE's mix to a WAV with the speakers
silent and reports levels, dropouts and clipping. `ue sound --stats` measures loudness across packs.
[More](docs/COMMANDS.md#audio)

**Ship it.** `ue package` packages the game, boots it headless and reports crashes and errors. `ue game start`
runs a real server and clients as separate processes, from the editor binary or the packaged build. `ue game smoke`
loads each map in `-game` to catch what PIE hides. Long work runs as background jobs.
[More](docs/COMMANDS.md#packaging-and-standalone-games)

**Work as a team.** With Perforce or Plastic, every edit checks out the files it touched. An edit to a file
someone else holds is undone, naming them (see [Revision control](#revision-control-perforce-and-others)).
`ue diff` shows what changed in a `.uasset` or `.umap` between revisions. Several agent sessions can share
one editor: a lease (`ue lease`, `ue who`) keeps them from restarting it under each other, `--second` gives
one its own editor, and `ue git stage --mine` stages only the files this session wrote. `ue fab` brings in
Fab content you already have and never buys anything. [More](docs/COMMANDS.md#several-sessions-on-one-editor)

### Built for agents

- **Compact by default.** Output is de-duplicated and summarised. Logs are read from a cursor, so each
  `ue log` shows only what is new. Screenshots and CSVs are written to files and their paths printed.
- **Scriptable.** Exit codes mean one thing each (`0` ok, `1` failed, `2` no editor, `3` restart needed,
  `4` timeout, `5` another session holds the editor). `--json` works on every command.
- **No stuck dialogs.** Headless editors answer modal dialogs with their default and log them. A windowed editor
  blocked on a modal fails at once, naming it. Revision control prompts are handled silently.
- **Works when the editor doesn't.** `ue status`, `ue log` and `ue crash` read files, so they still answer when
  the editor is hung or gone (`ue log` first gives a hung editor up to 10 s to flush). `ue doctor` checks the whole
  setup, and `ue selftest` checks every capability against the running editor in about 3 minutes.

## How it works

```
 agent ──shell──▶ ue (cli/ue.py) ──HTTP──▶ Epic MCP server ──▶ ToolsetRegistry
                    │                         (in editor)       ├─ Epic's ~830 tools
                    │                                           └─ tether toolset (Plugins/Tether)
                    └── reads Saved/Tether/editors/<pid>.json (beacons) and Saved/Logs directly
```

- **`cli/ue.py`** is the out-of-editor half: a launcher for the stdlib-only `cli/tether_cli` package. It launches and closes
  the editor on a per-project port, routes builds (Live Coding when the editor is up, UBT when it isn't),
  and prints compact, de-duplicated output. Status and logs come from files, so they keep working when the
  editor is hung or gone.
- **`Plugins/Tether`** is a content-only (pure Python) editor plugin. It registers a `tether` toolset into
  Epic's registry: unrestricted Python, console commands, compact level, actor and property reads, and
  C# reload state. It also writes a heartbeat beacon, and starts Epic's MCP server when the editor was opened
  without it. It hot-reloads with `ue reload`.
- **`Plugins/TetherUnrealSharp`** is optional. It's a small C++ editor plugin that makes UnrealSharp's C#
  hot reload callable from Python with modal dialogs suppressed.
- **The Claude Code plugin** (`.claude-plugin/`, `skills/`, `hooks/`, `bin/`) puts `ue` on the agent's
  PATH, adds the `unreal-tether` skill that teaches the workflow, and installs hooks (see below).

### Your own editor, and more than one

Open the project in the editor as you normally do and the agent works with that editor: Tether starts Epic's MCP
server in it (on the project's own port, unless something already started one), and `ue open` sees it is open
and uses it instead of starting another. `ue close` and `ue build --restart` will not close an editor you opened
yourself; they say so, and `--pid <pid>` overrides that. The session-start hook tells the agent whose editor it is.

Two editors on the same project (yours, and a headless one an agent started earlier) each have their own beacon,
token and log (`Saved/Logs/<Project>_2.log` for the second), so neither breaks the other. Each session keeps
talking to the editor it used last. A new session goes to the one the lease holder and the other sessions have
been using this past hour, else to yours; commands that only look (`status`, `who`, `lease`) do not make their
pick the session's own. `ue` says which one it is using whenever it changes anything, and `ue status` lists the other with how to reach it
(`ue --pid <pid> ...`) or close it (`ue close` closes the one `ue open` started). Still, saving the same assets
from both loses work: close one. `TETHER_NO_MCP=1` in the editor's environment stops Tether starting the server.

## Requirements

- Unreal Engine 5.8 or later for everything; 5.4–5.7 for a subset (see [Older engines](#older-engines-ue-5457)).
  A launcher install is fine; no source build needed. The 5.9 Preview passes `ue selftest` on a Blueprint-only
  project. Epic changed a lot of its agent tooling in 5.9; see [docs/UE59.md](docs/UE59.md).
- Python 3 on `PATH`, for the CLI. It uses the standard library only.
- Windows is the tested platform. macOS and Linux are supported by the CLI (engine discovery,
  `Build.sh`/`RunUAT.sh`, the editor binary, clang diagnostics, detached launch), and the unit tests
  pass there, but they have not been driven against a real Mac or Linux editor yet. Live Coding is
  Windows-only in UE, so C++ changes there go through `ue build --restart`.
- Optional: the .NET 10 SDK and UnrealSharp, for C#.

## Install

### With Claude Code (recommended)

1. Add the plugin. In Claude Code:

   ```
   /plugin marketplace add Seneku/Tether
   /plugin install tether@tether
   ```

   Or from a shell: `claude plugin marketplace add Seneku/Tether`, then
   `claude plugin install tether@tether` (add `--scope project` to share it with a repo's other users).
2. In your Unreal project folder, run `ue init`. It enables the engine plugins Tether needs in the
   `.uproject` (`PythonScriptPlugin`, `ModelContextProtocol`, `EditorToolset`, `AutomationTestToolset`,
   `SlateInspectorToolset`, `ConfigSettingsToolset`, `Tether`, plus the Niagara, PCG and StateTree toolsets
   when the project uses those features and `TetherUnrealSharp` for UnrealSharp projects), and copies the
   Tether editor plugin into the project's `Plugins/`. `ue init --dry-run` shows the changes first. Run it
   again after updating the plugin to refresh the copy.
3. `ue open --headless` (or `--headless --render` for screenshots), then `ue status`. `ue doctor` says what
   is wrong if anything is.

**Moving from a copied skill** (projects set up before the plugin existed): delete the project's
`.claude/skills/unreal-tether` folder (the plugin's skill replaces it, and two copies would compete), install
the plugin, and keep or replace the project's `AdditionalPluginDirectories` entry; `ue init` reports what is
missing either way.

### Hooks

The plugin's hooks stay silent outside Unreal projects, and never block a tool call because they failed.

- **Session start:** a brief of the project and two lines of editor state (running or not, whose editor, map,
  engine, port).
- **Before an edit:** blocks C# edits while PIE runs (UnrealSharp would reprocess them mid-session).
- **Before Epic's MCP tools:** blocks `StartPIE` while saved C# has not been hot-reloaded, and clicks and
  input while a Fab store tab is open.
- **After a C++/C# edit:** one line saying whether `ue build` (Live Coding or C# reload) will pick it
  up, or a restart is needed (reflected declarations, `Build.cs`). It also records which session wrote the
  file, for build errors and `ue git stage --mine`.
- **End of a turn:** if project code changed in the last hour since the last green build and the
  editor is running, Claude is asked once to run `ue build` or say why not.

### Manual (any agent)

1. **Enable the plugins** in your `.uproject`. Add `ModelContextProtocol`, `EditorToolset` and `Tether`,
   and point Unreal at this repo's plugins:

   ```json
   "AdditionalPluginDirectories": ["<path to this repo>/Plugins"],
   "Plugins": [
     { "Name": "PythonScriptPlugin", "Enabled": true },
     { "Name": "ModelContextProtocol", "Enabled": true },
     { "Name": "EditorToolset", "Enabled": true },
     { "Name": "Tether", "Enabled": true }
   ]
   ```

   - The path must point at `Plugins`, not at a plugin folder. UnrealBuildTool only searches its
     subfolders. You can also copy `Plugins/Tether` into your project's `Plugins/` folder.
   - Both Tether plugins are opt-in (`EnabledByDefault: false`), so list them explicitly.
   - `AllToolsets` instead of `EditorToolset` enables all of Epic's toolsets, but it also pulls in side
     plugins such as GameFeatures.

2. **Put `bin/` on your `PATH`** (`ue` for bash shells, `ue.cmd` for PowerShell/cmd). Then run commands
   from your project folder, or set `UE_PROJECT`:

   ```
   ue open --headless      # launch with the MCP server and wait until ready (~10-25 s warm)
   ue status
   ue call tether.level_summary
   ```

3. **For other agents:** `skills/unreal-tether/SKILL.md` is a plain-Markdown guide to the workflow, with area
   detail in `references/` beside it; point your agent at it.

## Revision control (Perforce and others)

Tether works through the revision control provider the editor is connected to (Perforce, Plastic/Unity
Version Control, ...), silently, because the editor's own prompts are modal dialogs that would stall an agent.
It is built for teams sharing binary assets with exclusive check-outs (Perforce `binary+l`), where the way to
avoid conflicts is to find out who holds a file before working on it, not at submit time:

- **Every edit claims its files.** Any Tether command that changes the editor's content (`ue set`, `bp edit`,
  `mat`, `spawn`, `ue py` code, ...) checks out the files of the packages it modified as soon as it returns, so
  teammates see them locked while the work goes on. With One File Per Actor maps that is the actor's own file.
  `ue call` does the same for Epic's tools, except the ones named as reads (`get_`, `list_`, `read_`, ...).
- **Edits that could not be submitted are undone.** If an edit touched a file someone else has checked out, or
  one behind the depot (someone submitted a newer revision: binary files don't merge), the command undoes it
  (its editor transactions, then a reload from disk of the assets it modified) and fails, naming the holder:
  `error: actor Crate1 (15G2LQ2749XAOZKMYEXXB3) is checked out by bob. Nothing was changed (the edit was undone)`.
  `ue set --assets`, `ue lint --fix`, `ue mv` and `ue move-pack` check their targets, and the assets that
  reference them, before changing anything; `ue bp edit`, `ue bp --apply` and `ue table --import` check their asset.
- **Bulk changes need `--bulk`.** One command may check out at most 20 files (`Config/Tether/vcs.json`:
  `{"bulk_limit": 20}`), so an agent's sweep can't lock half the project by accident; above that it is undone or
  refused with the count. Tell the team, then rerun with `--bulk`.
- `ue save`, `ue mv` and `ue rm` check files out before writing them, mark new files for add and moved or
  deleted ones for delete. Without a connected provider, read-only files (an unconnected Perforce workspace)
  are refused up front instead of raising the editor's "make writable?" dialog. `ue save` exits 1 when
  something was not saved.
- `ue locks [/Game/Folder ...]` lists who has which content checked out, your own check-outs, and what is
  behind the depot (a map includes its actors, by label). It asks the editor when one runs, else `p4` directly.
- `ue diff <asset> #have|#head|#3|@1234|@label [--to REV]` compares Perforce revisions (the default in a
  Perforce workspace is `#have`). As P4V's diff tool for `.uasset`/`.umap`: `ue diff --files %1 %2`.

The command-line `p4` uses the connection in the editor's `Saved/Config/<Platform>Editor/SourceControlSettings.ini`
(else p4's own settings). With Git nothing is checked out or staged. `ue status` names the provider. Tested
against a local P4 Server 2026.1 with two users and an exclusive-lock typemap: editing a file the other user
holds, editing behind the depot, bulk edits through `ue set --assets` and `ue py` loops, `ue mv` with a locked
referencer, a One File Per Actor map, and `ue selftest` (117/0/20) with the editor connected to Perforce.

## C# with UnrealSharp (optional)

1. Clone [UnrealSharp](https://github.com/UnrealSharp/UnrealSharp) into your project's
   `Plugins/UnrealSharp` and enable `TetherUnrealSharp`. You need the .NET SDK 10.0.5 or later; `Sandbox/`
   is tested against UnrealSharp commit `06769c4`.
2. Run `ue build` with the editor closed. This runs UBT; the first build takes about 2 minutes.
3. Create the C# project without the GUI wizard:

   ```
   RunUAT.bat GenerateProject -ScriptDir=<proj>\Plugins\UnrealSharp\Build\Scripts -Project=<proj>\<P>.uproject -ProjectName=Managed<P> -ProjectFolder=<proj>\Script\Managed<P> -GenerateSolution=true -RunUSharpProjectSetup=true -CreateModuleClass=true
   ```

4. Add this to `Config/DefaultEditorPerProjectUserSettings.ini`, so reloads only happen when the agent
   asks for them:

   ```ini
   [/Script/UnrealSharpEditor.CSUnrealSharpEditorSettings]
   AutomaticHotReloading=Off
   ```

5. `ue open` pre-builds the C# project. After that, `ue build` hot-reloads C# changes in about a second.

Tether also works around several UnrealSharp issues:

- **Unreloaded C# during PIE:** `ue pie start` reloads first, and a hook blocks Epic's `StartPIE` while
  saved C# hasn't been reloaded.
- **Save-time modal dialogs** in headless editors.
- **.NET discovery** from Git Bash.

Details are in [docs/DESIGN.md](docs/DESIGN.md) §2. Upstream reports:
[#774](https://github.com/UnrealSharp/UnrealSharp/issues/774),
[#775](https://github.com/UnrealSharp/UnrealSharp/issues/775),
[#776](https://github.com/UnrealSharp/UnrealSharp/issues/776).

## Older engines (UE 5.4–5.7)

Before 5.8 there is no MCP server or ToolsetRegistry. `ue` then talks to the editor through the Python
plugin's remote execution protocol, using the engine's own client, and picks this route from the
project's engine version. `ue init` enables remote execution in `Config/DefaultEngine.ini` (and `ue open`
switches it on for the session anyway).

| Works | Needs 5.8 |
|---|---|
| `status`, `open`, `close`, `build` (UBT and Live Coding), `log`, `py`, `exec`, `actors`, `props` (from the Python wrappers' property lists), `set` and `undo` (see below), `asset`, `find`, `refs`, `validate`, `size`, `mem`, `nav`, `mat`, `new`, `drop`, `ai`, `ui` (UMG only), `changes`, `save`, `spawn`/`delete`/`duplicate`/`mv`/`rm`, `package`, `call tether.*`, `pie start/stop/status/do`, `reload`, `init`, `selftest`, hooks | `set --from`, `diff`, `config`, `shot`, `test`, `bp`, `table`, `perf`, `tools`, and `call` for Epic's toolsets |

`ue set` on 5.4–5.7 writes through `set_editor_property` in one editor transaction, parsing each value by
the type of the property's current value (numbers, booleans, enum names, structs such as vectors and
colours, object paths) and putting everything back if one value fails. With no undo-history depth to check
before 5.8, `ue undo` there writes the old values back as a new transaction, and refuses if any of those
properties changed since; it covers `ue set` only.

Each call costs about 0.5 s more (UDP discovery), and the token gate adds nothing here: remote execution
itself runs any Python sent to it, so keep its bind address at the default `127.0.0.1`.

**Tested** on UE 5.7.4 (a Blueprint-only project set up with `ue init`): `ue selftest` passes 33 checks,
fails none and skips the 11 that need 5.8. 5.4–5.6 have not been run yet; if you have such a project,
please paste its `ue selftest` output into an issue.

## Repository layout

| Path | Contents |
|---|---|
| `cli/` | `ue.py` (the entry point) and `tether_cli/`, the stdlib-only CLI package (one module per area: transport, build, editor, assets, play, hooks, ...) |
| `bin/` | `ue` and `ue.cmd` launchers; the Claude Code plugin puts this folder on the agent's PATH |
| `.claude-plugin/`, `skills/`, `hooks/` | Claude Code plugin manifest (the repo is also its own marketplace), the `unreal-tether` skill, and the hooks |
| `tests/` | Unit tests for the editor-free parts (`python -m unittest discover -s tests`) |
| `Plugins/Tether/` | Python toolset plugin |
| `Plugins/TetherUnrealSharp/` | Optional C++ bridge for UnrealSharp hot reload |
| `.claude/`, `.mcp.json` | This repo's own Claude Code settings: Epic's plugin, wired to the sandbox editor for comparison runs |
| `Sandbox/` | Scratch UE 5.8 project used for development and measurements (includes a small C# coin feature) |
| `docs/COMMANDS.md` | Every command, grouped by area |
| `docs/DESIGN.md` | Analysis, measurements, architecture, roadmap |
| `docs/COMPARISON.md` | Head-to-head against Epic's official Claude Code plugin |
| `docs/NEXT.md` | Candidate additions, with what has shipped |
| `docs/ci/` | A sample GitHub Actions workflow for `ue ci` on a self-hosted runner |

## Status

Pre-1.0 (0.36). Tether is used daily on the bundled sandbox and on several private UE 5.8 games: C++, Blueprint
and UnrealSharp, single-player and multiplayer, Git and Perforce. Most features were added because a session
working on one of those games needed them. Expect rough edges: commands and output formats may still change.
The roadmap and open design decisions are in [docs/DESIGN.md](docs/DESIGN.md) §8–9, and candidate additions in
[docs/NEXT.md](docs/NEXT.md).

## Notes

- Epic's MCP server binds to `127.0.0.1` only and has no auth token, so any local process can call it.
  Tether's tools that run code or change state (`py`, `console`, `set_props`) also require a random
  per-session token, which the editor writes to `Saved/Tether/session.json` and `ue` sends automatically.
  So a caller must be able to read the project folder. Epic's own tools are not covered by the token.
- A modal dialog in a windowed editor freezes every call. `ue` detects it (Windows) and fails at once
  with `blocked by modal: "<title>"` instead of waiting for a timeout. Headless editors log dialogs and
  answer them with their default.
- Epic's MCP plugin sends analytics by default. To turn them off, run `ModelContextProtocol.EnableAnalytics 0`
  in the editor console.
- Tether is not affiliated with or endorsed by Epic Games. Unreal Engine is a trademark of Epic Games, Inc.

## Contributing

Bug reports, measurements and PRs are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup,
testing and the design rules.

## License

[MIT](LICENSE)
