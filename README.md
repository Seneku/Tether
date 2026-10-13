# Tether

**Drive a running Unreal Editor from a coding agent: fast, token-frugal, and with no editor restarts.**

Tether is agent-native tooling for Unreal Engine 5.8+. It is built on Epic's experimental in-editor MCP
server (`ModelContextProtocol` + `ToolsetRegistry`) and adds the parts that are missing for agent work:

- editor lifecycle;
- build routing;
- compact output;
- log cursors;
- unrestricted editor Python;
- PIE smoke tests;
- Claude Code glue.

It works with Claude Code out of the box. Any agent that can run a shell command can use it.

```
$ ue status
Sandbox | ready | UE 5.8.3-58210709 | pid 16348 | map /Temp/Untitled_1 | mcp :18873 up | heartbeat 0.6s ago
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
| Run editor Python / read project state | ~24 s headless commandlet, ~1,900 log lines | **37 ms – 0.85 s**, a few lines |
| C++ function-body change | Live Coding by hand; the real error only appears in a separate UBT log | **~4.5 s** Live Coding via `ue build`, one-line diagnostics |
| Reflection / `Build.cs` change | ~53 s restart plus manual clicks | **24 s** automated `ue build --restart`, zero clicks |
| C# change ([UnrealSharp](https://github.com/UnrealSharp/UnrealSharp)) | no agent-callable reload; restart the editor | **0.6–1.2 s** hot reload via `ue build` |
| Run a project's automation tests | separate headless test run, thousands of log lines | **~1 s** for a small suite in the live editor: one summary line plus `file:line` per failure |

In a head-to-head on the same tasks against Epic's official Claude Code plugin, the agent read **629 B**
of tool output with Tether and **~97 KB** with Epic's plugin. Epic's tools also could not reload C#: they
reported success while the old code kept running. See [docs/COMPARISON.md](docs/COMPARISON.md).

Numbers come from the author's machine (Windows 11, UE 5.8.3). They cover the bundled `Sandbox/` project
and a private mid-size C++/Blueprint game. Methodology is in [docs/DESIGN.md](docs/DESIGN.md).

## How it works

```
 agent ──shell──▶ ue (cli/ue.py) ──HTTP──▶ Epic MCP server ──▶ ToolsetRegistry
                    │                         (in editor)       ├─ Epic's ~830 tools
                    │                                           └─ tether toolset (Plugins/Tether)
                    └── reads Saved/Tether/editor.json (beacon) and Saved/Logs directly
```

- **`cli/ue.py`** is the out-of-editor half. It's a single stdlib-only Python file. It launches and closes
  the editor on a per-project port, routes builds (Live Coding when the editor is up, UBT when it isn't),
  and prints compact, de-duplicated output. Status and logs come from files, so they keep working when the
  editor is hung or gone.
- **`Plugins/Tether`** is a content-only (pure Python) editor plugin. It registers a `tether` toolset into
  Epic's registry: unrestricted Python, console commands, compact level, actor and property reads, and
  C# reload state. It also writes a heartbeat beacon. It hot-reloads with `ue reload`.
- **`Plugins/TetherUnrealSharp`** is optional. It's a small C++ editor plugin that makes UnrealSharp's C#
  hot reload callable from Python with modal dialogs suppressed.
- **The Claude Code plugin** (`.claude-plugin/`, `skills/`, `hooks/`, `bin/`) puts `ue` on the agent's
  PATH, adds the `unreal-tether` skill that teaches the workflow, and installs hooks (see below).

## Requirements

- Unreal Engine 5.8 or later for everything; 5.4–5.7 for a subset (see [Older engines](#older-engines-ue-54-57)).
  A launcher install is fine; no source build needed.
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
   `Tether`, plus `TetherUnrealSharp` for UnrealSharp projects), and copies the Tether editor plugin into
   the project's `Plugins/`. `ue init --dry-run` shows the changes first. Run it again after updating
   the plugin to refresh the copy.
3. `ue open --headless` (or `--headless --render` for screenshots), then `ue status`.

**Moving from a copied skill** (projects set up before the plugin existed): delete the project's
`.claude/skills/unreal-tether` folder (the plugin's skill replaces it, and two copies would compete), install
the plugin, and keep or replace the project's `AdditionalPluginDirectories` entry; `ue init` reports what is
missing either way.

### Hooks

The plugin's hooks stay silent outside Unreal projects, and never block a tool call because they failed.

- **Session start:** two lines of editor state (running or not, map, engine, port).
- **Before an edit:** blocks C# edits while PIE runs (UnrealSharp would reprocess them mid-session).
- **Before Epic's `StartPIE`:** blocks it while saved C# has not been hot-reloaded.
- **After a C++/C# edit:** one line saying whether `ue build` (Live Coding or C# reload) will pick it
  up, or a restart is needed (reflected declarations, `Build.cs`).
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

3. **For other agents:** `skills/unreal-tether/SKILL.md` is a plain-Markdown guide to the workflow;
   point your agent at it.

## Commands

| Command | What it does |
|---|---|
| `ue status` | Editor state from the beacon: map, PIE, PID, port, heartbeat. No HTTP. Names the dialog a hung windowed editor is waiting on. |
| `ue init [--dry-run]` | Set up the project: enable the engine plugins Tether needs, copy the Tether editor plugin into `Plugins/`. |
| `ue ci [--tests F] [--bp PATH] [--validate [PATH]] [--cook] [--report FILE]` | A project's checks in one pass/fail command: opens the editor headless if needed, runs the project's tests (`^<Project>.` by default), compiles all Blueprints, optionally validates content and cooks, closes again. IslandHorror: 64 s. Example GitHub Actions workflow (self-hosted runner): [docs/ci/unreal-ci.yml](docs/ci/unreal-ci.yml). |
| `ue selftest` | Check every capability against the running editor: one line of pass/fail/skip per feature (~35 s). Leaves nothing behind. Paste its output into bug reports. |
| `ue open [--headless [--render]] [--map M]` | Launch on a per-project port and wait until ready. Prints boot errors, de-duplicated. `--headless` has no window and no renderer (`-nullrhi`); add `--render` to render offscreen for `ue shot`. |
| `ue close [--save\|--discard\|--force]` | Quit gracefully. Refuses to drop unsaved work unless told to. |
| `ue build [--restart]` | Uses Live Coding if the editor is up and UBT if not, plus C# hot reload. `--restart` covers reflection and `Build.cs` changes: save, close, build, reopen the same map. |
| `ue py "<code>"` / `-f file` / `-` | Unrestricted editor Python with a persistent namespace. A trailing expression is printed. |
| `ue exec "<console cmd>" [--pie]` | Run a console command and print the log lines it produced. |
| `ue actors [regex] [--pie]` | Actors in the level (or the PIE world): label, class, location. |
| `ue props <target> [regex] [--pie]` | Reflected properties with **values**, for an actor, a component or a `/Game/...` asset. |
| `ue set <target> name=value [name+=delta ...] [--pie]` | Write properties as one undoable transaction and print `old -> new`. Takes numbers, booleans, enum names, object paths, JSON, or tuples for structs (`location+=0,0,200`). Actors also take `location`, `rotation` and `scale`. All-or-nothing: if one value fails, nothing changes. |
| `ue crash [N] [--list]` | Why the editor died: the error, the module chain (and which of your modules were on it), and the last log lines before the crash. `ue status`, `ue open` and the call that was running all mention a recent crash. |
| `ue validate [paths\|folders] [--changed [REV]] [--strict]` | Unreal's data validation (every registered validator, including your project's, plus each asset's `IsDataValid`) and Map Check, one line per problem. With no paths: the assets changed in git (uncommitted, plus `--changed REV` for commits since REV) and the unsaved ones, then Map Check of the open level. Deleted assets that something still references are errors. Exit 1 on errors (`--strict`: on warnings too). The end-of-turn hook suggests it once after assets were saved. |
| `ue diff <asset> [REV] [--to REV2]` | What changed in a `.uasset`/`.umap` between a git revision (default `HEAD`) and the editor's current state (unsaved changes included), or between two revisions. Blueprints: parent, variables, dispatchers, components, class defaults, and each changed graph as a text diff. DataTables: rows added, removed and changed by column. Maps: actors added, removed and changed (property by property); for World Partition maps, the actors whose packages changed, by label. Anything else: its properties. LFS works. As a difftool: `git config difftool.ue.cmd 'ue diff --files "$LOCAL" "$REMOTE" --as "$MERGED"'`, then `git difftool -t ue`. |
| `ue changes` / `ue save [paths]` | Review unsaved work (dirty packages; the level's actors added, removed, moved or changed since it was loaded or saved), then save it. Untitled `/Temp` maps are skipped. `ue status` shows the unsaved count. |
| `ue spawn <class\|asset> [--at x,y,z] [--rot p,y,r] [--label L]` | Place an actor from a class (`PointLight`, `/Script/Module.Class`) or an asset (a mesh, a Blueprint). Undoable. |
| `ue drop <actor>... [--align]` / `ue spawn ... --ground [--align]` | Put actors on the ground: each moves straight down (or up, if buried) until the bottom of its bounds rests on the first surface with collision below it (landscape, meshes), optionally tilted to the surface. One undo step. |
| `ue duplicate <actor> [--offset x,y,z] [--count N]` / `ue delete <actor>...` | Duplicate or delete actors; each command is one undo step. |
| `ue mv <asset> <path\|folder/>` / `ue rm <asset> [--force]` | Move or rename an asset (references are updated; it lists referencers left unsaved), or delete one, refusing while anything references it. Not undoable: version control is the undo. |
| `ue undo` | Undo Tether's last change (`set`, `spawn`, `duplicate`, `delete`), only if nothing else entered the editor's undo history since (it never undoes your edits). |
| `ue watch <regex>` / `ue watch --prop <target> <name> --until <regex>` | Wait for a log line or a property value, up to `--seconds` (exit 4 on timeout). |
| `ue props <target> --export F` / `ue set <target> --from F [--dry-run]` | Round-trip any object or asset (e.g. a DataAsset) through JSON; `--from` applies only what differs. |
| `ue shot [--actor A \| --at x,y,z --look-at A] [--annotate]` | Save a PNG of the viewport to `Saved/Tether/shots/` and print the path, so the agent opens it only when needed. During PIE it captures the player's view; during Simulate, the simulated world. `--annotate` overlays Epic's world grid and actor labels. |
| `ue shot ... --baseline NAME` / `ue shot --compare NAME [--tolerance %]` | Visual regression: save a shot and its exact camera as a named baseline; later re-shoot that camera and report the share of changed pixels and the changed areas (lone aliasing pixels ignored, separate changes listed apart), with a diff image (changes in red). Exit 1 above the tolerance (0.5%). Stdlib PNG decoding, about 2 s. |
| `ue test <filter> [--list] [-v]` | Run automation tests in the live editor. Prints one summary line, plus each failure with its source line. Exits 1 on any failure. Uses Epic's filter syntax; anchor project tests (`^MyGame.`) so engine tests don't get pulled in. |
| `ue tools [toolset[:tool]]` | Browse Epic's and Tether's tools as one-line signatures instead of full schema dumps. |
| `ue call <toolset>.<tool> k=v ...` | Call any registered tool. Required blank parameters are filled in for you. |
| `ue log [--level L] [--cat C] [--grep re] [--since start]` | New log entries since the last call, de-duplicated. The cursor is kept on disk. |
| `ue pie start\|stop\|status [--simulate]` | Play-In-Editor. Prints only new warnings and errors. |
| `ue pie run <script.py> --expect <regex> [--reject <regex>] [--seconds N]` | A scripted gameplay check: start PIE, run the script in the game world, wait for the expected log lines, fail on rejected ones or a script exception, stop PIE. Exit 0/1. |
| `ue pie do "<step>" ... [-f steps.txt] [--reject <regex>]` | Drive the player in PIE and check the results, one line per step, exit 0/1. Steps: `at <x,y,z\|actor> [offset]`, `look <target>`, `move <target>` (navmesh), `press <action> [seconds]`, `tap`/`hold`/`release <action> [x,y]` (Enhanced Input: an asset, or an action the character creates in C++), `call <target> <Function> [json args]`, `expect <target>.<prop> <op> <value>` (`= != < > <= >= ~regex`), `until <expectation> [within N]`, `log <regex>`, `exists`/`gone <actor>`, `wait <s>`, `exec <cmd>`, `shot`. Targets include `player`, `controller` and `player.<Component>`. Starts and stops PIE unless it is already running. |
| `ue ui [regex]` / `ue ui click <label>` / `type <textbox> <text>` / `key <Key>` | The game's on-screen UI in PIE, Slate and UMG alike: texts, buttons, text boxes and their state, as a compact tree, through Epic's SlateInspectorToolset (`ue init` enables it), with the editor's own viewport controls filtered out. Clicks go to a widget by its label. Without the toolset it falls back to UMG only. Canvas-drawn HUD text (`AHUD::DrawText`) is not a widget; use `ue shot` for that. `ue pie do` takes the same as steps: `click`, `type`, `key`, `expect-ui`, `ui-gone`. |
| `ue ai [pawn]` | AI in PIE: one line per AI (movement, focus, what it perceives, brain), or a block for one: path-following status and destination, focus, perceived actors, behavior tree or StateTree component, blackboard values, and the values of the no-argument getters the project's own pawn and controller classes add (`get_*`, `is_*`, `has_*`). |
| `ue bp <blueprint> [graph] [--diff] [--apply FILE] [--save]` | Blueprint graphs as text (Epic's graph DSL). `--diff` shows only what changed since the last read; `--apply` writes edited text back, compiles, and prints the diff. |
| `ue bp check [/Game/path] [--warnings]` | Compile every Blueprint under a path and list only the broken ones, with the compiler's reason (e.g. a C++ function that was renamed). Exit 1 if any fail. |
| `ue table <datatable> [--export F] [--import F [--prune] [--dry-run]] [--save]` | DataTable round-trips through JSON or CSV. Import shows a row-level diff (`+ Row`, `~ Row.col: old -> new`) and upserts; `--prune` also removes rows missing from the file. |
| `ue perf --trace [--top N]` | Also records an Unreal Insights trace over the same window and exports it headlessly with Unreal Insights: the most expensive CPU scopes per frame on the game, render and RHI threads (exclusive ms, with waiting shown apart), and the change since the last trace. The `.utrace` is kept (last three) for the full timeline. |
| `ue perf [--seconds N]` | Frame-time capture in PIE through Unreal's CSV profiler: frame time, game/render/GPU work per frame, what bounds it, hitches, and the change since the last capture. Lifts the editor's background throttle for the capture. GPU and thread timings need a renderer (`--render`). |
| `ue asset <path>...` | A few lines per asset, by type: mesh triangles per LOD, Nanite, size, collision, materials; texture size, compression, streaming; material parameters and cost; instance parent chain and overrides; Blueprint parent and own variables; DataTable rows; map actor storage; plus disk size and reference counts. |
| `ue mat <material\|instance\|actor> [Name=value ...] [--reset Name] [--compile]` | Material parameters (scalar, vector, texture, static switch) with values and where each comes from (an instance's overrides and the parent value, a material function); for an actor, the material in each component slot. `Name=value` sets them as one undoable transaction (an instance's overrides, or a material's defaults); `--reset` drops an override; `--compile` recompiles and lists compile errors. |
| `ue nav [path <a> <b> \| check [regex]] [--pie]` | Navigation: whether the level has a usable navmesh and, if not, why (navigation off in World Settings, no bounds volume, nothing built yet); `path` gives the path length and point count between two actors or points, or which end is off the navmesh; `check` lists actors (player starts, spawn and patrol points by default) standing off the navmesh. `--pie` reads the runtime navmesh. |
| `ue size [folder\|asset] [--deps] [--top N]` | Disk usage from the files themselves (no editor needed): a folder's total, per subfolder, and its biggest assets (with their class when the editor is up). `--deps` on an asset totals everything it loads with it (hard dependencies, recursively; a World Partition map's actors too), by folder. |
| `ue mem [--full]` | The running editor's (and PIE's) memory from a memreport, in a few lines: process memory, the classes using the most, the biggest skeletal and static meshes, and (with `--full`) textures. Prints the report's path for the rest. |
| `ue config [section] [regex \| Name=value ...]` | Project and editor settings, through the editor's own settings registry (Epic's ConfigSettingsToolset): every section by Container/Category, one section's settings with values, or typed writes (numbers, booleans, enum names, paths) saved where the Settings window saves them (`Config/Default*.ini` for project settings), with the readback checked. Sections match by name (`maps`, `rendering`, `Engine/General`). |
| `ue new <kind> <path> [--parent P] [--save]` | Create an asset in one line: `blueprint` (from a class name, `/Script/Module.Class` or another Blueprint), `material`, `mi` (from a parent material), `datatable` (with its row struct), `dataasset` (of a class), `level`, or `folder`. Refuses to overwrite. |
| `ue find [regex] [--class C] [--under /Game/x]` | Search assets by name and class through the asset registry. |
| `ue refs <asset> [--deps]` | What references an asset (or what it depends on); World Partition actors are grouped per level. Check before deleting or renaming. |
| `ue api <Class[.member]> [regex]` | The `unreal` Python API in a few lines: method signatures, editor properties with types, enum values, or one member's docs. |
| `ue package [--config C] [--seconds N] [--boot-only] [--strict]` | Package the game (BuildCookRun build/cook/stage/pak/archive as a background job, archived under `Saved/Tether/package/`), then boot the packaged executable headless for N seconds (20) and report a crash (with its module chain), fatal errors, an early exit, and logged errors. Exit 0/1. |
| `ue job start -- <cmd>` / `cook` / `status` / `wait` / `log` | Long work (cooks, commandlets, any command) in the background, with a log and exit code; `wait` prints only the errors. |
| `ue reload` | Hot-reload the Tether Python package in the running editor. |

Exit codes: `0` ok, `1` tool or command failed, `2` editor unavailable, `3` restart required, `4` timeout.

## Revision control (Perforce and others)

`ue save`, `ue mv` and `ue rm` go through the revision control provider the editor is connected to
(Perforce, Plastic/Unity Version Control, ...), silently, because the editor's own prompts are modal
dialogs that would stall an agent:

- files are checked out before they are written; a file checked out by someone else is refused, naming them;
- new files (a saved new asset, the target of a move) are marked for add, and a moved or deleted file for delete;
- without a connected provider, read-only files (an unconnected Perforce workspace) are refused up front
  instead of raising the editor's "make writable?" dialog. `ue save` exits 1 when something was not saved.

With Git nothing is checked out or staged. `ue status` names the provider. This was tested without a
Perforce server (unit tests with a fake provider, and read-only files on a live editor); reports from
Perforce users are welcome.

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
| `cli/` | `ue.py`, the CLI (one stdlib-only file) |
| `bin/` | `ue` and `ue.cmd` launchers; the Claude Code plugin puts this folder on the agent's PATH |
| `.claude-plugin/`, `skills/`, `hooks/` | Claude Code plugin manifest (the repo is also its own marketplace), the `unreal-tether` skill, and the hooks |
| `tests/` | Unit tests for the editor-free parts (`python -m unittest discover -s tests`) |
| `Plugins/Tether/` | Python toolset plugin |
| `Plugins/TetherUnrealSharp/` | Optional C++ bridge for UnrealSharp hot reload |
| `.claude/`, `.mcp.json` | This repo's own Claude Code settings: Epic's plugin, wired to the sandbox editor for comparison runs |
| `Sandbox/` | Scratch UE 5.8 project used for development and measurements (includes a small C# coin feature) |
| `docs/DESIGN.md` | Analysis, measurements, architecture, roadmap |
| `docs/COMPARISON.md` | Head-to-head against Epic's official Claude Code plugin |

## Status

This is a working spike (roadmap phases 0 and 1). It's used daily on the bundled sandbox and on a real game
project, but expect rough edges. Commands and output formats may still change. The roadmap and open
design decisions are in [docs/DESIGN.md](docs/DESIGN.md) §8–9.

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
