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
token and log (`Saved/Logs/<Project>_2.log` for the second), so neither breaks the other. `ue` talks to yours, says
which one it is using whenever it changes anything, and `ue status` lists the other with how to reach it
(`ue --pid <pid> ...`) or close it (`ue close` closes the one `ue open` started). Still, saving the same assets
from both loses work: close one. `TETHER_NO_MCP=1` in the editor's environment stops Tether starting the server.

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
| `ue overview [--brief]` | A one-screen brief of the project from its files (no editor needed): modules and their game mode, controller, character, AI, HUD, subsystem and component classes, startup map and game mode, maps (World Partition marked), input actions, plugins, content size by folder, last commit. The session-start hook adds the brief version. |
| `ue doctor` | Checks the setup and says how to fix what is wrong: Python, the engine install, the project's plugins and Tether copy (as `ue init` would change them), the editor (ready, hung, stuck on a dialog, crashed since it last ran), its MCP port, other editors on the same engine (they can hold the Live Coding lock that blocks UBT), remote execution before 5.8, the .NET SDK for UnrealSharp, git and LFS, disk space; the Tether version the project has and the one the running editor loaded, against the CLI's (and whether a restart would load the new one). |
| `ue status` | Editor state from the beacon: map, PIE, PID, port, heartbeat. No HTTP. Names the dialog a hung windowed editor is waiting on. |
| `ue init [--dry-run]` | Set up the project: enable the engine plugins Tether needs (Epic's MCP server, Editor, AutomationTest, SlateInspector and ConfigSettings toolsets, plus the Niagara, PCG and StateTree toolsets when the project uses those features), copy the Tether editor plugin into `Plugins/`. A command whose Epic toolset is missing names the plugin to enable. A project's own copy of the skill (`.claude/skills/unreal-tether/SKILL.md`) is brought up to date too, keeping its first paragraph (how that project reaches `ue`); `ue doctor` flags a stale one. |
| `ue help [command]` | The same as `ue <command> -h`. |
| `ue ci [--tests F] [--bp PATH] [--play [GLOB]] [--validate [PATH]] [--cook] [--report FILE]` | A project's checks in one pass/fail command: opens the editor headless if needed, runs the project's tests (`^<Project>.` by default), compiles all Blueprints, optionally runs scripted play tests (`ue pie do` step files, `tests/play/*.txt` by default; a `# map: /Game/...` line picks the map), validates content and cooks, closes again. IslandHorror: 64 s. Example GitHub Actions workflow (self-hosted runner): [docs/ci/unreal-ci.yml](docs/ci/unreal-ci.yml). |
| `ue selftest` | Check every capability against the running editor: one line of pass/fail/skip per feature (~35 s). Leaves nothing behind. Paste its output into bug reports. |
| `ue open [--headless [--render]] [--map M]` | Launch on a per-project port and wait until ready. Prints boot errors, de-duplicated. `--headless` has no window and no renderer (`-nullrhi`); add `--render` to render offscreen for `ue shot`. |
| `ue close [--save\|--discard\|--force]` | Quit gracefully. Refuses to drop unsaved work unless told to. |
| `ue lease take --for <what> [--minutes 15]` / `release` / `ue lease` | Sessions sharing one editor: an advisory lease, shown in `ue status`. While another session holds it, `build --restart`, `close` and `pie start` (and `pie run/do/watch/record`) refuse with exit 5 and say who holds it, for what and for how long; `py` and `exec` note it. Without a lease, a restart or close also refuses when another session used the editor in the last 5 minutes, naming them. `--ignore-lease` overrides. |
| `ue who [--minutes 15] [--as NAME]` | The sessions that ran `ue` commands on this editor recently (last command, how long ago), the lease, and who started PIE (a session, or by hand in the editor; `ue status` shows that too). `--as` names your session for the others. |
| `ue build [--restart]` | Uses Live Coding if the editor is up and UBT if not, plus C# hot reload. `--restart` covers reflection and `Build.cs` changes: save, close, build, reopen the same map, as the editor ran before (your own windowed editor stays windowed; `--pid` picks it). Before closing, it compiles the game target (the editor's binaries and Live Coding are untouched) and keeps the editor open if that fails; `--no-check` skips this, and `ue build --check` runs the check alone. When the full build still fails the editor stays closed and the next `--restart` reopens it. After `--discard`, the reopened editor does not offer the discarded packages back in a "Restore Packages" dialog. A failed Live Coding patch reports the compiler and linker errors from the Live Coding console's own log (`error LNK2019` unresolved externals with a hint about the missing Build.cs module). Each file with an error says who last wrote it: the session that edited it (recorded by the post-edit hook, with "(not you)" when it was another session), or else its git state and modification time. |
| `ue py "<code>"` / `-f file [args]` / `-` | Unrestricted editor Python with a persistent namespace; words after `-f file` are the script's `sys.argv[1:]`. A trailing expression is printed. What the script printed before an exception keeps its lines. Warns before known engine traps (rebuilding a mesh that already has render data with `build_from_static_mesh_descriptions` crashes the editor; editing elements of an `unreal.Array` of structs in a `for` loop changes copies). |
| `ue exec "<console cmd>" [--pie]` | Run a console command and print the log lines it produced. In multiplayer PIE the command runs on the target world's next tick, as its player typed it: run straight from editor Python, every RPC would run locally (a client's Server RPC never reaching the server). |
| `ue actors [regex] [--pie]` | Actors in the level (or the PIE world): label, class, location. |
| `ue props <target> [regex] [--pie]` | Reflected properties with **values**, for an actor, a component or a `/Game/...` asset; then the UPROPERTYs that are not Blueprint-visible (a component's internal `Current`), read through the engine's own export. `pie watch` reads those too. |
| `ue set <target> name=value [name+=delta ...] [--pie]` | Write properties as one undoable transaction and print `old -> new`. Takes numbers, booleans, enum names, object paths, JSON, or tuples for structs (`location+=0,0,200`). Actors also take `location`, `rotation` and `scale`. All-or-nothing: if one value fails, nothing changes. |
| `ue crash [N] [--list]` | Why the editor died: the error, the module chain (and which of your modules were on it), and the last log lines before the crash. `ue status`, `ue open` and the call that was running all mention a recent crash. |
| `ue validate [paths\|folders] [--changed [REV]] [--strict]` | Unreal's data validation (every registered validator, including your project's, plus each asset's `IsDataValid`) and Map Check, one line per problem. With no paths: the assets changed in git (uncommitted, plus `--changed REV` for commits since REV) and the unsaved ones, then Map Check of the open level. Deleted assets that something still references are errors. Exit 1 on errors (`--strict`: on warnings too). The end-of-turn hook suggests it once after assets were saved. |
| `ue diff <asset> [REV] [--to REV2]` | What changed in a `.uasset`/`.umap` between a git revision (default `HEAD`) and the editor's current state (unsaved changes included), or between two revisions. Blueprints: parent, variables, dispatchers, components, class defaults, and each changed graph as a text diff. DataTables: rows added, removed and changed by column. Maps: actors added, removed and changed (property by property); for World Partition maps, the actors whose packages changed, by label. Anything else: its properties. LFS works. As a difftool: `git config difftool.ue.cmd 'ue diff --files "$LOCAL" "$REMOTE" --as "$MERGED"'`, then `git difftool -t ue`. |
| `ue changes` / `ue save [paths]` | Review unsaved work (dirty packages; the level's actors added, removed, moved or changed since it was loaded or saved), then save it. Untitled `/Temp` maps are skipped. `ue status` shows the unsaved count. |
| `ue spawn <class\|asset> [--at x,y,z] [--rot p,y,r] [--label L]` | Place an actor from a class (`PointLight`, `/Script/Module.Class`) or an asset (a mesh, a Blueprint). Undoable. |
| `ue drop <actor>... [--align]` / `ue spawn ... --ground [--align]` | Put actors on the ground: each moves straight down (or up, if buried) until the bottom of its bounds rests on the first surface with collision below it (landscape, meshes), optionally tilted to the surface. `--group` moves them, and what is attached to them, as one unit (a hut of separate wall pieces keeps its shape on a slope): the group's lowest point rests on the lowest ground under it (`--rest highest\|mean`), and the output says how much the ground varies. One undo step. |
| `ue water` | Water zones and bodies (extent, water-info resolution in m/texel, local tessellation, materials, waves), with checks: a water body that edits the landscape (its brush flattens a hand-made island), a coarse shoreline fade, a stock material left on the distant LOD mesh, bodies without a zone. Exit 1 on an issue. |
| `ue scatter <asset\|class> --count N --area x,y,r \| --line A B [--spacing cm] [--yaw a,b] [--scale a,b] [--seed n] [--align]` | Dress a level: N copies at random points in a circle (or evenly along a line), each dropped onto the first surface below it, with random yaw and uniform scale, a minimum spacing, and a seed for a repeatable layout. One undo step; reports copies that found no ground. |
| `ue duplicate <actor> [--offset x,y,z] [--count N]` / `ue delete <actor>...` | Duplicate or delete actors; each command is one undo step. |
| `ue net <actor\|player\|Class\|/Game/...BP> [--pie] [--all]` / `ue net compare <Actor.Prop>` | Replication at a glance. What the class replicates, read from its C++ headers and sources (the project's and the engine's): replicated properties with their conditions and RepNotify functions, RPCs (server, client, multicast; unreliable and validated ones marked), the class itself in full and its parents counted; a Blueprint's replicated variables. For an actor in multiplayer PIE: in each world, its role and remote role, owner, update rate, relevancy and dormancy. `compare` puts one property's value in the server's and each client's world side by side (the same-named actor in the targeted world, the nearest of its class in the others); exit 1 when they differ. |
| `ue tree <actor\|player\|/Game/...BP> [--pie]` | An actor's or Blueprint's components as an indented tree: class, mesh or other asset, attach socket, relative transform (when not identity), mobility, and visibility, shadow and collision flags; inherited components marked; components outside the tree (movement, input, AI) listed after it, as are attached actors. |
| `ue class <Name\|/Game/...BP>` | A class at a glance: C++ or Blueprint, its parent chain, its own properties (type) and functions (C++), or a Blueprint's components, variables, functions and events; the C++ classes and Blueprints deriving from it (unloaded ones too, from the asset registry); and what references a Blueprint, maps included. Names work with or without the A/U prefix, or as a Blueprint name. |
| `ue trace [--from player\|camera\|actor\|x,y,z] [--to look\|actor\|x,y,z] [--channel C] [--sphere r] [--editor]` / `ue collision <actor>` | Why a trace does or does not hit: traces like gameplay code (by default from the PIE player's camera along the view: "what am I looking at"), on any trace channel including the project's own, and lists what it passes through and the first thing that blocks it, with each component's profile and response to that channel. `ue collision` shows an actor's components: profile, collision enabled state, object type, the response to every channel, overlap events, physics, and a static mesh's simple collision. |
| `ue set --assets <folder\|folder/glob> [--class C] [--match GLOB] Name=value ... [--dry-run] [--save]` | The same property change on many assets: every asset under the folder (recursively) whose class matches the regex and whose name matches the pattern, each changed like `ue set` (validated, one undoable step each), printing old -> new per asset and skipping (with the reason) those without the property. `--dry-run` prints the current values instead. |
| `ue undo --editor [N]` / `ue redo [N]` | The editor's own undo history, so any transacted edit can be taken back (foliage, scatter, drop, material graph edits, property sets, deletes), not only `ue set`: undoes or redoes the last N steps and names each one. Refused while PIE runs. Python cannot list the history without changing it, so the names come as the steps are undone. |
| `ue mv <asset> <path\|folder/>` / `ue rm <asset> [--force]` | Move or rename an asset (references are updated; it lists referencers left unsaved), or delete one, refusing while anything references it. Not undoable: version control is the undo. |
| `ue undo` | Undo Tether's last change (`set`, `spawn`, `duplicate`, `delete`), only if nothing else entered the editor's undo history since (it never undoes your edits). |
| `ue watch <regex>` / `ue watch --prop <target> <name> --until <regex>` | Wait for a log line or a property value, up to `--seconds` (exit 4 on timeout). |
| `ue props <target> --export F` / `ue set <target> --from F [--dry-run]` | Round-trip any object or asset (e.g. a DataAsset) through JSON; `--from` applies only what differs. |
| `ue shot [--actor A \| --at x,y,z --look-at A] [--annotate]` | Save a PNG of the viewport to `Saved/Tether/shots/` and print the path, so the agent opens it only when needed. During PIE it captures the player's view (`--hud`: with the game's UMG HUD on it, cut from a capture of the editor window), or with `--at`/`--look-at`/`--actor` a spectator shot from anywhere (the engine's debug camera, for that one capture; the game keeps running and the player's view comes back); during Simulate, the simulated world, with a free camera. `--annotate` overlays Epic's world grid and actor labels. |
| `ue shot ... --baseline NAME` / `ue shot --compare NAME [--tolerance %]` | Visual regression: save a shot and its exact camera as a named baseline; later re-shoot that camera and report the share of changed pixels and the changed areas (lone aliasing pixels ignored, separate changes listed apart), with a diff image (changes in red). Exit 1 above the tolerance (0.5%). Stdlib PNG decoding, about 2 s. |
| `ue look <asset> [--tile W]` | An asset (static or skeletal mesh, Blueprint, or a material on a sphere) rendered from six angles into one contact sheet: front, right, back, left, top, three-quarter. It is staged as transient actors high above the level with a soft fill light and removed afterwards; nothing is saved. Needs a rendering editor (`--render`). |
| `ue test <filter> [--list] [-v]` | Run automation tests in the live editor. Prints one summary line, plus each failure with its source line. Exits 1 on any failure. Uses Epic's filter syntax; anchor project tests (`^MyGame.`) so engine tests don't get pulled in. |
| `ue tools [toolset[:tool]]` | Browse Epic's and Tether's tools as one-line signatures instead of full schema dumps. |
| `ue call <toolset>.<tool> k=v ...` | Call any registered tool. Required blank parameters are filled in for you. |
| `ue log [--level L] [--cat C] [--grep re] [--since start\|pie\|90s\|2m\|1h]` | New log entries since the last call, de-duplicated (the cursor is kept on disk), or the whole session, since the last PIE session's start (a map travel inside PIE does not count as a new one), or the last few seconds, minutes or hours. Warnings and up by default; a `--grep` searches every line from Log up unless `--level` says otherwise. Reading events (a `--grep`, or `--level Display`/`Log`) keeps them in order and folds only repeats in a row; warnings and errors are grouped by message. |
| `ue log --summary` / `ue log --ignore REGEX [--why TEXT]` / `--all` | The whole session's warnings and errors grouped by message shape (numbers, paths, quoted names and instance suffixes folded), errors first, each with a count and its first line. Known noise goes in `Config/Tether/log-ignore.json` (a list of regexes on `Category: message`, or `{"pattern", "why"}` entries); `ue log`, `ue pie` and `ue ci` leave those lines out and say how many they hid, and `--all` shows them. |
| `ue pie start\|stop\|status [--simulate]` | Play-In-Editor. Prints only new warnings and errors. |
| `ue pie start [--clients N] [--net listen\|client\|standalone] [--game GameMode]` / `ue pie target <n>` | Multiplayer PIE: N clients with a listen server (default) or a dedicated server, in one process and without windows on a headless editor. `ue pie status` lists each PIE world with its role and players; `ue pie target <n>` picks the world that `--pie` reads, `ue pie do`, `ai`, `anim` and the console act on (0 = the server). `--game` plays with another game mode (the project default, changed in memory only; a map's own override still wins). `ue pie stop` puts your play settings back. |
| `ue pie run <script.py> --expect <regex> [--reject <regex>] [--seconds N]` | A scripted gameplay check: start PIE, run the script in the game world (a fresh namespace each run), wait for the expected log lines, fail on rejected ones or a script exception, stop PIE. Exit 0/1. Scripts can `import tether` and use `tether.world()`, `player()`, `controller()`, `actors()` and `console()`, which follow `ue pie target` (unreal's `get_game_world()` is always world 0) and keep RPCs working. |
| `ue pie do "<step>" ... [-f steps.txt] [--reject <regex>]` | Drive the player in PIE and check the results, one line per step, exit 0/1. Steps: `at <x,y,z\|actor> [offset]`, `look <target>`, `move <target>` (navmesh), `press <action> [seconds]`, `tap`/`hold`/`release <action> [x,y]` (Enhanced Input: an asset, or an action the character creates in C++), `call <target> <Function> [json args]`, `expect <target>.<prop> <op> <value>` (`= != < > <= >= ~regex`), `until <expectation> [within N]`, `log <regex>`, `exists`/`gone <actor>`, `wait <s>` (or `wait <s> game` for game time, whatever the frame rate), `exec <cmd>`, `shot`. Real input: `key <Key> down [value]\|up\|tap`, `mouse <dx> <dy>` (one frame of mouse movement), `stick <Key> x,y [seconds]`; these go in as key presses (Enhanced Input's forced keys), so mappings, triggers and modifiers (dead zones, scalars) apply, while `hold`/`tap <action>` inject the action's value and skip the mappings' modifiers. Targets include `player`, `controller` and `player.<Component>`. Starts and stops PIE unless it is already running. `--film S` also screenshots the game's view (with its UMG HUD, through the Slate inspector: about 0.05 s a frame, so 0.25 s works) every S seconds while the steps run, into one contact sheet with times on each frame (`--film-output`), so you can see what happened between the checks. |
| `ue pie do -f steps.txt` with `# watch: Actor.Prop ...` / `ue pie watch ... --baseline NAME` / `--compare NAME` | Gameplay regression tests: a step file under `tests/play` with a `# watch:` line records the watched values' timelines as a baseline the first time (`tests/play/baselines/<file>.json`, to commit), and every later run (also under `ue ci --play`) compares with it and fails on differences: a number ending or peaking elsewhere (beyond 5% of its range), a position ending more than 1.5 m away or travelling a quarter further or shorter, a different sequence of states, or a state reached more than 1.5 s earlier or later. |
| `shot-check NAME [tolerance%]` (a `ue pie do` step) | A visual checkpoint in a play test: screenshots the player's view and compares it with `tests/play/shots/NAME.png`, recording that the first time (commit it); fails beyond the tolerance (2% of pixels by default, since live frames vary a little) with a diff image that marks the changes in red. Catches a missing light, changed fog or wrong material under `ue ci --play`. |
| `ue pie record [-o file] [--seconds N]` | Play once, get a replayable `ue pie do` step file. While you play in PIE (a windowed editor; it starts PIE if needed), the editor records every key of the project's mapping contexts, analog sticks, and the player's position and view; when PIE stops (or after N seconds) the keys become the input actions they trigger (through the mappings' swizzle/negate/scalar modifiers): short presses `tap`, longer ones `hold`/`release`, movement `hold IA_Move x,y`; turning the camera becomes `look` steps, with one before each press so interactions aim where you aimed. Starts with `at` and a `# map:` line, ends with a `near` check. Python cannot read Enhanced Input action values, so actions triggered only by mouse movement are not recorded. |
| `ue pie watch <Actor.Prop> ... [--seconds N] [-f steps.txt] [--every S]` / `ue pie do <steps> --watch ...` | Sample values while PIE runs, on the editor's own tick (default every 0.1 s; a background editor's CPU throttling is lifted meanwhile), for N seconds or while a step file plays. An actor alone means its location; `player`, `controller` and `player.<Component>` work as in `pie do`, and actors that spawn later are picked up. Also `player.control` (the control rotation), getters (`player.GetVelocity()`), properties that are not Blueprint-visible, and input actions (`IA_Look`: the value worked out from the keys behind it). Prints one line per value: a number's range with when it peaked, a position's path length and top speed, a rotation's yaw and pitch range, another vector's size (a velocity's peak), or every change of anything else with its time; all samples go to a CSV. Baselines only with `--baseline`, or for step files under `tests/play`. |
| `ue input [regex]` | Enhanced Input at a glance: each input action with its value type and the keys that reach it, by mapping context (with modifiers such as swizzle and negate, and per-key triggers), then each context with what adds it (assets that reference it, and code that names it). In PIE it adds which contexts are active and the keys each action really has. `ue pie do` warns when a step presses an action no active context maps. `ue input --live IA_Look [--seconds N]` (PIE running) prints the action's value over time while you play or a step file runs, worked out from its keys through negate, swizzle, scalar and dead zone modifiers. |
| `ue ui [regex]` / `ue ui click <label>` / `type <textbox> <text>` / `key <Key>` | The game's on-screen UI in PIE, Slate and UMG alike: texts, buttons, text boxes and their state, as a compact tree, through Epic's SlateInspectorToolset (`ue init` enables it), with the editor's own viewport controls filtered out. Clicks go to a widget by its label; when another window (a floating Message Log) covers its centre on screen, a UMG button's `OnClicked` is fired instead, or the click fails naming that window (`--event` always fires `OnClicked`). `ue ui at <x,y>` lists the windows and widgets at a point. Without the toolset it falls back to UMG only. Canvas-drawn HUD text (`AHUD::DrawText`) is not a widget; use `ue shot` for that. `ue pie do` takes the same as steps: `click`, `type`, `key`, `expect-ui`, `ui-gone`. |
| `ue anim <actor\|player> [--editor]` | Animation state in PIE: for each skeletal mesh, its animation mode and Anim Blueprint (or single animation, or the leader pose it follows), the playing montage with section, position and rate, root motion mode, non-zero curves, and the Anim Blueprint's own variables (the values its state machine transitions read); plus the character's movement mode and speed. |
| `ue audio mix` | The sound mix as the game will hear it: the Sound Class tree with volume and pitch, the Sound Mixes (base and pushed) and their adjusters, submixes and their effects. |
| `ue sound <asset>` | A Sound Wave or Cue: length, channels, sample rate, compression and loading, looping, attenuation (and its falloff), sound class, concurrency; a Cue's node tree. What the asset itself lacks (attenuation, a class) is said as such, since code can supply both when it plays the sound. `ue lint` has `sound-stereo-3d` for stereo waves with attenuation. |
| `ue sound <data asset\|Blueprint\|folder>... [--all]` / `--stats [--sort loud\|quiet\|length\|name]` | Every wave the assets reach through hard and soft references (through cues, MetaSounds and data assets; nothing loaded): channels, length, sample rate and who uses each (down to the property path or map key, e.g. `DA_Sounds: Cues[Explosion].Sounds[0]`), with the stereo ones called out as "if played 3D". `--stats` exports the waves once (cached) and measures peak, RMS and loudest 50 ms in dBFS, attack time and brightness (spectral centroid), plus the level "as played" when the referencing struct has a Volume next to the sound, sorted, plus the median level per folder, for levelling packs against one another. |
| `ue audio --log N [--do steps.txt]` | Records the sounds that start on audio components over N seconds of PIE (or while a step file plays), with what the mixer resolved: spatialized or not, the attenuation in effect (component override, the component's asset, or the sound's), the sound class in effect (SoundClassOverride or the sound's), the waves' channels, owner. Counts starts cut off at once (a concurrency limit) and virtualized ones. Flags stereo sounds played spatialized with their channels spread apart (a StereoSpread of 0 folds them onto the source and is fine) and sounds attached to actors with no attenuation, from what actually played, so code-driven setups are checked too. |
| `ue audio [regex]` | Sounds playing in PIE, fire-and-forget ones included (from the audio device's own list): sound, owner, distance from the player's camera, volume, attenuation. `ue pie do` gains `expect-sound <regex>`. Headless editors have no audio device unless opened with `ue open --headless --sound`, which keeps one but silent (PIE at volume 0, editor volume 0). |
| `ue anim graph <AnimBlueprint>` | An Animation Blueprint as text: what feeds the output pose (slots, cached poses, Control Rig, blend conditions), then each state machine with its entry state, each state with what it plays (sequence, blend space and its inputs, cached pose), state aliases with the states they cover, and every transition with its rule as an expression ("NOT Boolean(Get IsFalling)", "its animation ends (automatic rule)") and blend time. State machine nodes have no pins in Python, so links come from the graph's text export. |
| `ue statetree [asset] [--pie]` | A StateTree as indented text: each state with its enter conditions, tasks and transitions ("-> Attack Enemy on delegate", "-> the next state on success", "-> Dead every tick if Actor.Current HP <= 0"), plus evaluators and global tasks, described the way the StateTree editor describes them (Epic's StateTree editor data). Without an asset, the project's StateTrees; with `--pie`, each StateTree component in play with its tree and run status (Python cannot read the active state). |
| `ue ai [pawn]` | AI in PIE: one line per AI (movement, focus, what it perceives, brain), or a block for one: path-following status and destination, focus, perceived actors, behavior tree or StateTree component, blackboard values, and the values of the no-argument getters the project's own pawn and controller classes add (`get_*`, `is_*`, `has_*`). |
| `ue soak --minutes N [-f steps.txt] [--max-growth MB]` | A long PIE run: loops a `ue pie do` step file (or wanders the player on the navmesh, or hops it about without one), sampling the editor process's memory and the frame rate, and collecting new errors. Reports the memory trend in MB per minute, the lowest frame-rate window and the errors; exit 1 on a crash, a failing step file, or growth over `--max-growth` (20 MB/min). |
| `ue bp <blueprint> [graph] [--diff] [--apply FILE] [--save]` | Blueprint graphs as text (Epic's graph DSL). `--diff` shows only what changed since the last read; `--apply` writes edited text back, compiles, and prints the diff. |
| `ue bp edit <bp> add-var Name Type [default] [--category C] [--instance-editable] [--array]` / `set-default Name=value ...` / `add-component Name Class [prop=value ...] [--parent P]` / `add-function Name` / `add-dispatcher Name` / `remove-var Name` | Blueprint authoring in one line, through Epic's Blueprint tools: variables of basic types, classes or structs (with defaults, category and instance-editable), components attached and configured, function stubs, event dispatchers, class defaults. Compiles and reports the result; refuses duplicate names. `--save` saves. |
| `ue bp trace [--seconds N] [--do steps.txt] [--match REGEX] [--keep]` | Which Blueprint events and functions ran during PIE, in order of first call with counts: answers "why didn't my event fire". Records an Unreal Insights trace with named events on (every UFunction call through ProcessEvent becomes a scope named after the function), starting PIE inside it so BeginPlay is caught, playing a step file or waiting, then exports the game thread with Insights headless. Event entry points (BeginPlay, input, overlaps, timers, custom events, delegates) go through ProcessEvent and appear; calls a Blueprint makes into its own functions may not get a scope of their own. `stat namedevents` is a toggle; if it was already on, run again. |
| `ue bp check [/Game/path] [--warnings]` | Compile every Blueprint under a path and list only the broken ones, with the compiler's reason (e.g. a C++ function that was renamed). Exit 1 if any fail. |
| `ue table <datatable> [--export F] [--import F [--prune] [--dry-run]] [--save]` | DataTable round-trips through JSON or CSV. Import shows a row-level diff (`+ Row`, `~ Row.col: old -> new`) and upserts; `--prune` also removes rows missing from the file. |
| `ue perf --trace [--top N]` | Also records an Unreal Insights trace over the same window and exports it headlessly with Unreal Insights: the most expensive CPU scopes per frame on the game, render and RHI threads (exclusive ms, with waiting shown apart), and the change since the last trace. The `.utrace` is kept (last three) for the full timeline. |
| `ue gpu [--frames N] [--tree [DEPTH]] [--min MS]` | GPU time per pass from `ProfileGPU` (PIE when it runs, else the editor's viewports), averaged over three frames: the time by kind (Lumen, shadows, Nanite, base pass, lighting, reflections, translucency, fog and sky, post processing, UI, ...) and the top passes, plus the async compute pipeline. `--tree` prints the last frame's pass tree. Sets the profiler to plain text and no window while it runs (`r.ProfileGPU.ShowUI=0`, so nothing opens on your screen), then restores it. Needs a rendering editor. |
| `ue perf --scalability [low,medium,high,epic,cinematic] [--seconds N] [--shots]` | The same PIE capture at each scalability level, as one table of frame (avg and p95), game, render and GPU time, hitches and what bounds the frame; the levels are set as console overrides and taken off again, so the project's own settings come back exactly. `--shots` saves a screenshot per level. `budget.json` can name the level its limits are measured at (`"scalability": "epic"`). |
| `ue perf [--seconds N]` | Frame-time capture in PIE through Unreal's CSV profiler: frame time, game/render/GPU work per frame, what bounds it, hitches, and the change since the last capture. Lifts the editor's background throttle for the capture. GPU and thread timings need a renderer (`--render`). |
| `ue budget` / `ue ci --budget` | Performance and size limits per project, in `Config/Tether/budget.json`: `frame_ms_p95`, `game_ms_p95`, `render_ms_p95`, `gpu_ms_p95`, `hitches`, `memory_mb`, `textures_mb`, `map_mb`, `content_mb`, `pie_start_s`, `streaming_s`, `load_hitches`, plus `map` (measured there), `route` (a `pie do` file to play while loading) and `seconds`. Runs only the measurements the limits need (`perf load`, `perf`, `mem`, `size`) and prints each against its limit; exit 1 when anything is over, so `ue ci --budget` fails the run. |
| `ue perf load [map] [-f route.txt] [--seconds N] [--hitch MS]` | Loading and streaming: starts PIE itself (on `map`, opened first if given) and reports the time to the first PIE frame and until World Partition streaming completes, then plays a `ue pie do` route (or N seconds) and lists the hitches (frames over 50 ms, or three times the median) with what loaded in each: packages that finished loading in that frame, and whether the game thread waited on loading (a flush). Ends with the slowest package loads, request to done. Timing comes from the editor's own tick and a verbose LogStreaming log tied to it, with background CPU throttling lifted. Packages the editor already has in memory do not load again, so a fresh editor gives the truest numbers. |
| `ue mesh check <mesh\|folder>... [--height CM] [--strict]` | Import problems a static mesh carries into the game: size against an expected height (the wrong unit, x100 or /100, lying on its side), where the pivot is (centre, bottom, or far off), UV channels and the lightmap UV, simple collision, faces pointing inward (the mesh's signed volume), empty material slots, sockets. `ue import` runs it on imported meshes. `--strict` exits 1 on any issue. |
| `ue skel <skeletal mesh\|skeleton>` | A skeleton at a glance: the bone tree (deep branches folded to fit), sockets with their bones, virtual bones, materials, morph targets, the physics asset's bodies and constraints, and the Control Rigs and IK Rigs that use it. |
| `ue data <class\|folder\|glob> [--columns A,B] [--all] [--csv F] [--import F [--dry-run] [--save]]` | Data-driven content as a table: every DataAsset of a class (or Blueprint subclass defaults, e.g. every enemy), or the assets in a folder, side by side with only the properties that differ (`--all` for every one). `--csv` writes the table; `--import` reads an edited CSV back and sets the changed values (`--dry-run` lists them first). |
| `ue asset <path>...` | A few lines per asset, by type: mesh triangles per LOD, Nanite, size, collision, materials; texture size, compression, streaming; material parameters and cost; instance parent chain and overrides; Blueprint parent and own variables; DataTable rows; map actor storage; plus disk size and reference counts. |
| `ue mat <material\|instance\|actor> [Name=value ...] [--reset Name] [--compile]` | Material parameters (scalar, vector, texture, static switch) with values and where each comes from (an instance's overrides and the parent value, a material function); for an actor, the material in each component slot. `Name=value` sets them as one undoable transaction (an instance's overrides, or a material's defaults); `--reset` drops an override; `--compile` recompiles and lists compile errors (a saved material is compiled from a copy, so it is not left modified). |
| `ue nav [path <a> <b> [<a> <b> ...] \| check [regex]] [--pie]` | Navigation: whether the level has a usable navmesh and, if not, why (navigation off in World Settings, no bounds volume, nothing built yet); `path` gives, per pair of actors or points, the path length, point count and highest point, where a partial path stops and how far short (height included), or which end is off the navmesh (exit 1 unless every pair has a full path; `--m` takes metres, `--agent <pawn>` paths for that pawn's navigation agent); `check` lists actors (player starts, spawn and patrol points by default) standing off the navmesh. `--pie` reads the runtime navmesh. |
| `ue size [folder\|asset] [--deps] [--top N]` | Disk usage from the files themselves (no editor needed): a folder's total, per subfolder, and its biggest assets (with their class when the editor is up). `--deps` on an asset totals everything it loads with it (hard dependencies, recursively; a World Partition map's actors too), by folder. |
| `ue mem [--full]` | The running editor's (and PIE's) memory from a memreport, in a few lines: process memory, the classes using the most, the biggest skeletal and static meshes, and (with `--full`) textures. Prints the report's path for the rest. |
| `ue cvar [regex]` / `--changed` / `ue cvar Name=value ... [--for 'ue command']` / `--reset Name ...` | Console variables with their value, who set it last (Constructor, Scalability, ProjectSetting, SystemSettingsIni, DeviceProfile, Commandline, Code, Console) and the built-in default when it differs; `--changed` lists only the ones that differ from their default. Setting works like the console (highest priority, not saved); `--for` sets them only while another `ue` command runs and then restores them exactly (`UnsetCVar`), and `--reset` takes console-set values off again. A misspelt name gets suggestions. |
| `ue config [section] [regex \| Name=value ...]` | Project and editor settings, through the editor's own settings registry (Epic's ConfigSettingsToolset): every section by Container/Category, one section's settings with values, or typed writes (numbers, booleans, enum names, paths) saved where the Settings window saves them (`Config/Default*.ini` for project settings), with the readback checked. Sections match by name (`maps`, `rendering`, `Engine/General`). |
| `ue import <file\|folder> ... <dest> [--collision box\|convex\|complex\|...] [--lods N] [--nanite\|--no-nanite] [--texture-group G] [--name N] [--save]` / `ue import --reimport <asset\|folder> ... [--force]` | Bring files in without dialogs: meshes (FBX, OBJ, glTF, USD, Alembic), textures and sounds, through whichever importer the engine uses for the type; folders are walked and keep their subfolders. Then applies the options: a static mesh's collision replaced, LODs generated, Nanite on or off; a texture's LOD group, and textures named `_ORM`, `_Mask`, `_AO`, `_Roughness`... made linear masks. Prints each asset with its vertices, LODs, size and collision, or texture format, plus the importer's warnings. Warns first when an FBX or glTF holds Unity-style `Name_LOD0..n` meshes, which Interchange stacks into a single LOD. `--reimport` reimports the assets whose source file is newer than the saved asset (one import per source file) and re-applies the options. Refuses while PIE runs: the editor builds no meshes in play mode. |
| `ue mat graph <material\|function>` / `... add <Class> [name] [value]` / `connect <from>[.Out] <to>.<In>\|<Output>` / `set <node> prop=value` / `delete <node>` | A material's node graph as text: what feeds each material output, every node with its key setting (parameter names and defaults, textures, functions, UV channels) and wiring, and nodes not connected to anything. Small edits by node (`#n` from the listing, a parameter name, or a class used once): add an expression (parameters get a name and default), connect outputs to inputs or to a material output (`BaseColor`, `Roughness`, ...), set properties, delete; each recompiles and reports errors. |
| `ue wp` / `ue wp actors [regex\|x,y,r]` / `ue wp load <x,y,r\|regex>` / `ue wp unload ...` | World Partition in the editor: actors in total and loaded, always-loaded ones, data layers and runtime grids; every actor including unloaded ones (from actor descriptors, `*` = loaded; generated HLOD proxies only when named); and loading actors by area or name so other commands (`actors`, `props`, `level check`, `foliage`) see them. `unload` releases what `ue wp load` loaded; actors the editor's own loaded regions hold stay. |
| `ue tex [folder\|texture] [--top N] [--max PX] [--strict]` / `ue tex --streaming` | Textures from the asset registry (nothing loaded): size, format, compression, LOD group, sRGB, streaming, virtual and mip flags, and memory at full resolution with mips (max size, cubes, arrays and volumes counted), the largest first; issues from `ue lint`'s texture rules (oversized, not a power of two, normal or mask settings, large uncompressed textures, large never-streaming ones). `--streaming` reads what the engine holds now (`ListTextures`, `ListStreamingRenderAssets`): resident memory, streaming pool use, the largest textures, and the ones that want more mips than they have. `budget.json` can limit `texture_assets_mb`. |
| `ue fx <system>` / `ue fx [actor\|regex] [--pie]` / `ue fx <actor> Name=value ...` | Niagara, read through Epic's Niagara toolsets and written compactly: a system's user parameters with defaults, and per emitter its sim target, renderers and modules per stage (system/emitter spawn and update, particle spawn and update; disabled ones marked). The level's or PIE's Niagara components with their system and active state, and one component's user parameter values. Sets user parameters on a placed component (float, int, bool, vectors, colour, position; the type comes from the parameter), as one undo step. Live particle counts are not reachable from Python. |
| `ue pcg [regex]` / `ue pcg <graph>` / `ue pcg generate <actor> [--seed N]` / `ue pcg cleanup <actor>` | PCG: a graph as text through Epic's PCG toolset (nodes in dataflow order with what feeds each, their changed settings, the meshes each Static Mesh Spawner picks from, nodes feeding nothing, graph parameters); the level's PCG components with graph, seed, trigger, state and what they spawned (instances per mesh, actors); regenerate one, with a new seed as an undoable change, waiting until it is done, or clean it up. |
| `ue lights` | The level's lighting in one view: every light with type, mobility, intensity and units, colour or temperature, radius, cone, shadows, IES profile and light function; the sky light, height fog, and the exposure post process volumes set (method, EV100 range, compensation, bloom) next to the project's default. Notes what commonly goes wrong: auto exposure lifting intended darkness (for a dark game: Manual exposure, or a higher minimum EV100), lights reaching over 50 m, many movable shadow-casting lights. |
| `ue level check [--gap cm] [--strict]` | Placed-actor problems Map Check does not report: props floating above whatever is below them (not hanging from something, not movable or simulating), props buried under the landscape, identical actors stacked in the same place (copy-paste leftovers), actors below Kill Z or far outside the landscape, actors hidden in game that still block pawns (invisible walls), and spots where more than four shadow-casting stationary lights overlap. Grouped by kind; `--strict` exits 1 when anything is found. |
| `ue map [x,y,r] [--match REGEX] [--pie] [--px 800] [-o png]` | A top-down picture of the level from what is in it (no rendering needed, so it works headless): landscape height, mesh footprints shaded by height, and markers (player starts, spawn points, AI, triggers, lights) in colours, with a legend and numbered labels for up to 60 of them (`--numbers`). `--pie` maps the PIE world. North is up (+Y), East is right (+X). |
| `ue landscape [info]` / `at x,y` / `map [x,y,size] [-o png]` / `flatten\|raise\|smooth x,y,r [--height z\|--by cm] [--strength s]` / `restore --backup file` | Terrain: size, components, resolution and paint layers; the height, slope and paint-layer weights at a point; a PNG of an area (brightness is height, colour the dominant layer) to see the shape of the ground; and sculpting a circle (the outer quarter fades). Python cannot sculpt, so edits go through the heightmap: exported to a render target, only the circle's square rewritten, read back to check it is exact (or nothing changes), then imported; each writes a backup of the square that `restore` puts back. |
| `ue landscape export <file.png\|.r16>` / `import <file>` / `paint <layer> --mask <picture>` (`--flip-y`) | Whole heightmaps and paint layers in and out of the existing landscape (it must render: `ue open --headless --render`, or a windowed editor). `export` writes the landscape's raw 16-bit heights (32768 = the actor's z; rows along +Y, columns along +X, as Unreal imports them) as a 16-bit PNG or `.r16`; `import` takes one of exactly the vertex grid's size, checks the render target reads back exactly, imports it, waits for collision to follow, and prints the largest change and a `.r16` backup to import back; `paint` replaces a layer's weights from a greyscale picture stretched over the landscape (white = full weight), after saving the layer's current weights (rendered at the vertex resolution, 8-bit as the engine keeps them) as a PNG to paint back. Creating a new landscape is not offered: `ALandscape::Import` is not script-callable. |
| `ue foliage [--area x,y,r]` / `ue foliage add <FoliageType\|mesh> --area x,y,r [--density N\|--count N] [--slope a,b] [--height a,b] [--scale a,b] [--align]` / `ue foliage remove [type] --area x,y,r` | Real foliage (instanced, as the foliage tool paints it, not actors like `ue scatter`): instance counts per type, in an area if given; add instances over a circle, each dropped onto the ground and filtered by slope and height, with random yaw and scale, using the foliage type's own settings for anything not given (a mesh gets a new foliage type in `--folder`); remove a type's instances from an area (Python can only clear a whole type, so the rest are put back). Same `--seed`, same layout. `add --mask <picture>` takes the density from a greyscale PNG, `.r16` or `.u8` over the landscape (or `--mask-area`), thinning before any trace, and the area may then be left out; `--avoid REGEX:RADIUS` keeps instances away from actors whose label, class or tag matches. |
| `ue foliage check [mesh regex]` / `convert <regex>` / `fix billboards\|cull\|wpo [--distance cm] [--scope regex\|/Game/folder]` | Per instanced or repeated mesh (foliage, other instanced components, and 20 or more separate actors of one mesh): instances, LOD triangle counts, Nanite, cull distance, and the problems: a Fab/Megascans billboard last LOD (4–12 triangles, renders oversized far away), no cull distance, wind (WPO) at every distance, one heavy LOD without Nanite, shadows and collision on thousands of small instances, scatter left as actors. `convert` turns StaticMeshActors (label, tag or mesh matching) into foliage instances, keeping override materials in a foliage type of their own. `fix billboards` hides the billboard LOD by setting its screen size to 0 (the engine never picks it; the authored LODs stay), on the level's meshes or every mesh in a folder; `fix cull\|wpo` sets the distance on the components and their foliage types. All undoable. |
| `ue lint [folder] [--rule R] [--ignore GLOB] [--strict]` | Content rules Epic's validators do not have, read from asset registry tags so nothing is loaded (5,000 assets in about half a second): `naming` (prefix by class: SM_, SK_, T_, M_, MI_, BP_, WBP_, ABP_, S_, NS_, DT_, IA_, IMC_, L_/Lvl_ for maps, ...), `characters` (spaces or non-ASCII in names), `texture-pow2` (no mips or streaming; UI textures exempt), `texture-size`, `texture-normal` (named _N but not compressed as a normal map), `texture-mask-srgb` (packed masks with sRGB on), `texture-uncompressed` (512 px and up stored uncompressed when its compression setting does not ask for it), `texture-never-stream` (1024 px and up kept fully in memory), `mesh-collision` (no simple collision, so characters and physics pass through), `mesh-lods` (many triangles, one LOD, no Nanite), `missing-ref` (references to packages that no longer exist: they load as None), `mesh-no-material` (a mesh that depends on no material: empty slots). `ue lint --cpp` reads the C++ instead: names declared file-private (in an anonymous namespace, `static`, or a type defined in a .cpp) in more than one .cpp of a module, which collide in a unity build or when Live Coding regroups files. Per project in `Config/Tether/lint.json`: `{"prefixes": {"World": ["Lvl_"]}, "ignore": ["/Game/Fab/*"], "disable": ["naming"], "max_texture_size": 4096, "lod_triangles": 10000}`. `--strict` exits 1 when anything is reported, for CI. `--fix [--dry-run] [--save]` fixes what has a safe fix: normal-map compression, sRGB off on masks, a box collision on meshes without simple collision; renames (missing prefix, bad characters, references fixed up) only with `--rule naming` or `--rule characters`, since they touch every referencing asset. |
| `ue fab [regex] [--all]` | Fab content in the project, no editor needed: Megascans (type, quality tiers), packs and the shared Fab materials, with the listing each came from (the Fab plugin's own record), size on disk and, with an editor, how much of each the game uses. `ue asset` names the Fab pack an asset came from, with its fab.com link. |
| `ue fab library [regex] [--from PROJECT]` | Fab content you already have: packs the Epic Games Launcher downloaded (its vault: title, size, supported engines, whether this project has it) and Fab content in other local projects (`--from` lists one). Read from files only: Tether never reads your account, tokens or cookies, so content you own but never downloaded is in the Fab tab's My Library. |
| `ue fab add <name> [--from PROJECT] [--dry-run] [--skip-existing]` | Copy content you have into the project: a downloaded vault pack (what the launcher's Add to Project does), or a Megascans asset or pack from another local project (Megascans bring the shared `/Game/Fab` materials they use). Checks the engine version (content from newer engines will not load), never overwrites a file that differs, and has a running editor scan the new folders. |
| `ue fab open <name\|listing\|/Game/asset>` / `--library` | The fab.com page for a listing (or a search, or the user's library) for the user to look at, claim or buy. Tether stops there: it never acquires anything, and while a Fab tab is open in the editor it refuses every click, key and text input (`ue ui`, `pie do`, `ue call`, and Epic's MCP tools through the plugin's hook), since the store page is a web view nothing can see into. |
| `ue fab settings [quality=Low\|Medium\|High\|Raw] [format=GLTF\|FBX] [cache=DIR]` / `ue fab clean` | Fab's download settings (Megascans quality tier, model format, cache folder), live through the editor or in the per-user config. `clean`: Fab content nothing uses, Megascans imported at more than one tier, files stored twice, two Megascans material libraries, and the size of the launcher vault and the Fab cache (report only). |
| `ue unused [folder] [--top N]` / `ue unused --redirectors [--fix]` | Assets nothing uses: everything the asset registry cannot reach (hard and soft references) from every map, Primary Asset Labels, editor utilities, the `/Game/...` paths named in the project's config and code, and the folders the Asset Manager scans or always cooks. Lists the biggest first with their size on disk, then totals by folder. Assets loaded by a path built at runtime look unused, so check with `ue usages` before deleting. `--redirectors` lists redirectors; `--fix` resaves what references each one and deletes it (through revision control). |
| `ue new <kind> <path> [--parent P] [--save]` | Create an asset in one line: `blueprint` (from a class name, `/Script/Module.Class` or another Blueprint), `material`, `mi` (from a parent material), `datatable` (with its row struct), `dataasset` (of a class), `level`, or `folder`. Refuses to overwrite. |
| `ue usages <symbol\|/Game/asset>` | Where a C++ function, property or class (or an asset) is used: whole-word lines in the project's C++, C# and config (interface `Execute_`/`_Implementation` forms included), and the assets whose name tables mention it, scanned from disk; with an editor up, each Blueprint's matching graph lines (within `--budget` seconds). Run it before renaming or deleting C++ API. |
| `ue world build nav [--save]` / `lighting [--quality Q]` / `hlod\|minimap\|nav-wp\|<Builder> [--map M]` | World builds. `nav` rebuilds the navmesh in the live editor (retrying while the navigation system refuses the build as locked, and saying which lock), waits until every bounds volume has tiles, and reports `ue nav` status and the packages it changed (`--save` refuses to save a map whose navmesh is still missing); `lighting` builds lightmaps and reports Lightmass's result (or why it skipped); the World Partition builders run as a commandlet job (refused while the map is open in the editor) and report what they built. |
| `ue world probe <x,y,z\|actor> ... [--radius 50] [--m] [--pie]` | What is at each point: the actors whose collision contains it (the point is inside geometry), those within the radius, and whether the navmesh reaches it or how far away it is. For spawn points, pickups and teleports; exit 1 if any point is inside geometry or off the navmesh. |
| `ue wait <seconds> [game]` / `ue wait compile [--max 900]` | Wait wall-clock seconds, or seconds of game time in the running PIE (as the `pie do` step `wait N game`), between `ue exec` calls. `compile` waits until shader and asset compilation is done (after opening a big level or editing materials): neither queue is visible to editor Python, so it watches the CPU use of the editor's ShaderCompileWorkers and of the editor itself until both have been quiet for 3 s (Windows). |
| `ue savegame [slot] [regex \| Name=value ...]` | Save slots: the list (from `Saved/SaveGames`, no editor needed); one slot's SaveGame with every saved property (through a text export, so plain `UPROPERTY()`s show too; arrays summarised); or edits written back to the slot, with the old file kept as `.bak-<time>`. Edits need properties exposed to Blueprints or the editor. |
| `ue find [regex] [--class C] [--under /Game/x]` | Search assets by name and class through the asset registry. |
| `ue refs <asset> [--deps]` | What references an asset (or what it depends on); World Partition actors are grouped per level. Check before deleting or renaming. |
| `ue api <Class[.member]> [regex]` | The `unreal` Python API in a few lines: method signatures, editor properties with types, enum values, or one member's docs. |
| `ue package [--config C] [--seconds N] [--boot-only] [--strict]` | Package the game (BuildCookRun build/cook/stage/pak/archive as a background job, archived under `Saved/Tether/package/`), then boot the packaged executable headless for N seconds (20) and report a crash (with its module chain), fatal errors, an early exit, and logged errors; the boot test forces a garbage collection, so GC problems show up too. Packaged games still running from an earlier package or boot are closed first. Exit 0/1. |
| `ue package --perf S [--map M]` / `ue package --play N` | `--perf`: frame times in the packaged game, through its CSV profiler, for about S seconds on a map (loading frames left out), next to the last `ue perf` capture in the editor. `--play`: a listen server and N clients from the packaged build (`ue game start --packaged`). |
| `ue game start [--clients N] [--server listen\|dedicated\|none] [--map M] [--game MODE] [--packaged] [--windowed]` / `status` / `log [n]` / `stop` | A real multiplayer session outside the editor: a server and N clients as separate game processes (the editor binary with `-game`/`-server`, or the packaged build), headless by default, each with its own log, on a free port from 17777 up (PIE and other projects' games use it too; clients follow the port the server reports); `start` waits until the clients join. `status` reports each process (running, exited, crashed) and its errors, `log 1` reads client 1's log, `stop` ends them all. `--server-args`/`--client-args` pass anything else. |
| `ue job start -- <cmd>` / `cook` / `status` / `wait` / `log` | Long work (cooks, commandlets, any command) in the background, with a log and exit code; `wait` prints only the errors. |
| `ue batch [file]` | Run many `ue` command lines (from a file or stdin, `#` comments) in one process and one editor session: about 0.04 s per command instead of about 0.15 s. `--stop-on-error`; exit code is the worst. |
| `ue reload` | Hot-reload the Tether Python package in the running editor, after compiling it (a syntax error is reported and the running code kept), and confirm the reload. |

Exit codes: `0` ok, `1` tool or command failed, `2` editor unavailable, `3` restart required, `4` timeout, `5` another session holds the editor lease.

`--json` works on every command: it prints a single document, `{"command", "ok", "exit", "error", "lines",
"data"}`, where `lines` is the text output and `data` is structured for status, doctor, validate, audio,
usages, size, ui, perf, fab and overview. `ue call`, `ue test` and `ue selftest` keep their own JSON.

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
| `cli/` | `ue.py` (the entry point) and `tether_cli/`, the stdlib-only CLI package (one module per area: transport, build, editor, assets, play, hooks, ...) |
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
