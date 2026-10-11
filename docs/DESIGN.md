# ToolSuite — an agent-native workflow for Unreal Engine 5.8

Status: design v0.1, 2026-10-09. Numbers marked *measured* come from this machine (UE 5.8.3,
`D:\Projects\Unreal\Horror` = IslandHorror, and the scratch project in `Sandbox/`).

## 1. Summary

- **Compiling is not the bottleneck — the engine round trip is.** Incremental UBT builds take
  1–8 s. But every question about project state currently costs a ~24 s headless editor boot that
  emits ~1,900 log lines, and every reflection change costs a ~53 s editor restart plus manual steps.
- **Epic has already built the in-editor half.** UE 5.8 ships an experimental MCP server on top of
  a `ToolsetRegistry`: 52 toolsets, ~830 tools — Blueprint graph DSL read/write, reflection
  get/set, PIE, viewport capture, DataTables, materials, automation tests, even Slate UI automation.
  Toolsets can be authored in Python.
- **What is missing is the out-of-editor half and agent ergonomics** — the part Unity's `unity` CLI
  adds on top of its Pipeline package: editor lifecycle, build routing (Live Coding vs UBT), compact
  output, log cursors, unrestricted Python, console commands, PIE smoke tests, and Claude Code glue.
- **Proposal:** a token-frugal `ue` CLI (Unity CLI parity) + a small hot-reloadable Python toolset
  registered into Epic's registry to fill the gaps + a Claude Code skill and hooks. Build *on*
  Epic's server rather than adding yet another bespoke MCP bridge.
- **Proven by a working spike in this repo** (§2): unrestricted editor Python in 37 ms (vs a 24 s
  commandlet), property reads with values in ~50–70 ms, new tools hot-loaded into a running
  editor, C++ body edits live in ~4.5 s via `ue build` (Live Coding, one-line diagnostics), and
  reflection changes through a fully automated 24 s restart cycle — no clicks.

## 2. Proof of concept (measured on `Sandbox/`, 2026-10-09)

`Plugins/ToolSuite` (content-only, pure Python) + `cli/ue.py` (stdlib only), against Epic's MCP
server with `AllToolsets` enabled:

| Command | Time | Output |
|---|---|---|
| `ue status` | 15 ms | one line from the beacon file (state, map, PID, port, heartbeat) — no HTTP |
| `ue py "import sys; print(sys.version.split()[0]); 6*7"` | 37 ms | `3.11.8` / `42` — unrestricted `unreal` Python, persistent namespace |
| `ue actors light` | 50 ms | 3 lines |
| `ue props DirectionalLight.LightComponent0 "^intensity\|^temperature"` | 67 ms | 3 lines with values |
| `ue call toolsuite.level_summary` | 46 ms | 2 lines (138 actors, 12 classes) vs ~3.9k tokens of `find_actors` paths |
| `ue call scene.find_actors` (any Epic tool) | 75 ms | required blanks auto-filled; common prefix stripped; capped |
| `ue tools toolsuite` | 66 ms | one-line signatures instead of a full `describe_toolset` dump |
| `ue log --level Warning` | 68 ms | 7 de-duplicated entries out of 2,089 new log lines |
| `ue reload` | 40 ms | edited Python tools live in the running editor (added `level_summary` this way) |
| `ue open --headless` | 10.6 s | engine from `EngineAssociation`, per-project port (18160), waits for beacon + MCP, prints boot errors de-duplicated |
| `ue close` | 2.6 s | checks unsaved packages first (refuses without `--save` / `--discard`), graceful quit |

C++ iteration, measured on a minimal C++ module added to `Sandbox/` (editor running headless):

| Scenario | Result |
|---|---|
| Function-body edit → `ue build` (routes to Live Coding) | **4.4–4.7 s**, change live, verified from Python |
| First Live Coding compile of a session | 13.7 s (spawns `LiveCodingConsole` on first use) |
| Compile error via Live Coding | 3.4 s. The editor log only says "Live coding failed, please see Live console"; the real error is only in `%LOCALAPPDATA%\UnrealBuildTool\Log.txt` (139 lines). `ue build` prints 1 line: `Source\Sandbox\SandboxProbe.cpp:11: C2065 'UndeclaredThing': undeclared identifier` |
| New `UFUNCTION` via Live Coding | 7.0 s, "succeeded" with a packaging warning. The UFunction exists in reflection and is callable via `cdo.call_method('GetProbeCount')`, but the Python wrapper stays stale until a restart |
| UBT while Live Coding is active | fails in 0.9 s: "Unable to build while Live Coding is active". The mutex is keyed by the engine's `UnrealEditor.exe` path, so one editor blocks UBT for **every** project on that engine |
| `Build.cs` edited while the editor runs | `ue build` refuses with exit 3, "restart required" |
| `ue build --restart` | close → UBT 8.2 s → reopen (same mode) 13.1 s = **24.0 s total, zero clicks**; new UFUNCTION then visible to Python. IslandHorror estimate ≈ 65 s, given its 53 s boot |
| First full UBT build of the module | 42 s, 142 output lines, all engine-header deprecation warnings; `ue build` hides and counts these |

C# iteration with **UnrealSharp** (trial 2026-10-11, commit `06769c4`, Sandbox editor headless and
attended, auto-reload off, reloads driven by `ue build` through the `ToolSuiteUnrealSharp` bridge):

| Scenario | Result |
|---|---|
| First UBT build with UnrealSharp (7 C++ modules + generated C# glue) | 129 s, once |
| Create the C# project headlessly (UAT `GenerateProject`, same as the GUI dialog) | 5.8 s, no GUI |
| Pre-build user C# (UAT `BuildUserSolution`, run by `ue open`) | 2.4–2.7 s |
| Function-body edit → `ue build` | **0.6 s** end to end (UnrealSharp 0.42 s) |
| Compile error → `ue build` | 0.1 s, one line (`ToolSuiteProbe.cs:16: CS0103 ...`); editor stays responsive; last good C# stays loaded |
| Fix + new `UFunction` + new `UProperty` (constructor default) + brand-new `UClass` in one reload | **1.2 s** (UnrealSharp 0.80 s); all four live, no restart |
| Editor boot with UnrealSharp | 9.6–11 s warm (37.6 s on the very first boot) |

UnrealSharp gotchas for agents, and how ToolSuite handles them:
- **Under `-unattended`, the hot-reload subsystem is not created** (`ShouldCreateSubsystem`), and the
  startup user build is skipped. `ue open --headless` therefore runs UnrealSharp projects attended,
  and pre-builds C# itself.
- **Compile failures raise a modal dialog** (`FMessageDialog`, "C# Compilation Failed"), which freezes
  the editor and every bridge. The bridge runs `PerformHotReload()` under `GIsRunningUnattendedScript`,
  so the dialog returns at once, and the compiler output is still logged (`Message dialog closed ...
  text: <errors>`), where `ue build` parses it.
- **Reload is not scriptable:** `PerformHotReload` is C++ only and the subsystem's `StaticClass()` is
  not exported. The bridge (one small C++ editor module) resolves the subsystem by class path and
  exposes `reload_csharp` / `csharp_reload_state` to Python. These are the only C++ lines in ToolSuite.
- **The project wizard is a GUI**, but it only wraps a UAT command, which `ue` can call directly.
- **Auto-reload on save is off** for agent sessions (`AutomaticHotReloading=Off`), so multi-file edits
  reload once, on `ue build`.
- C# types register as `/Script/UnrealSharp.<Name>_C`. Call new UFunctions via
  `cdo.call_method('Name', (args,))`. Reloads are deferred while PIE runs.
- **A saved but unreloaded C# type is recompiled in place on its first lookup** (root-caused
  2026-10-11).
  - *Cause:* the file watcher parses each saved file and marks every type whose declaration changed as
    structurally dirty (a constant change counts). `FCSManagedTypeDefinition::GetDefinition()` then
    compiles dirty types on access, from the old reflection data. For a subsystem class, that compile
    calls `DeactivateExternalSubsystem`, which deinitializes and removes its instance in every live
    world, PIE included. It does not come back in that PIE session.
  - *Symptom:* `GetWorldSubsystem<T>()` returns null even though begin-play ran. In the Epic comparison
    every coin logged "no score subsystem in this world".
  - *Reproduction:* save a C# change without reloading, start PIE, look the subsystem up. The same
    on-access recompile is the likely cause of "saving C# during PIE crashes PIE".
  - *ToolSuite's handling:* `ue pie start` hot-reloads pending C# first and refuses to start PIE if the
    reload fails.
  - *Epic's `StartPIE`:* a PreToolUse hook (`ue hook pre-mcp`) denies it while C# is pending.
  - *Gap:* the editor's Play button is not guarded, and `AutomaticHotReloading=Off` widens that window.
- **A syntax error in a saved file raises a modal dialog at save time** ("C# Hot Reload Error", from
  the file watcher). That happens outside any reload the bridge can guard.
  - *Why it hangs:* in a `-nullrhi` editor the dialog is an invisible Slate modal. The engine tick and
    the MCP server stop, while Slate, and so the beacon, keep ticking.
  - *ToolSuite's handling:* `ue open --headless` starts UnrealSharp editors with `-RUNNINGUNATTENDEDSCRIPT`.
    Every `FMessageDialog` then logs its text and takes its default answer, and hot reload stays
    available, because UnrealSharp only checks `-unattended`.
  - *Reporting:* `ue build` and `ue pie start` turn the logged text into a failed reload ("UnrealSharp
    rejected the saved files", one line per error), and `ue open` lists any dialogs answered at boot.
- **.NET is found only through a `PATH` entry containing `Program Files\dotnet\`, trailing backslash
  included.** Git Bash drops that backslash. The editor then shows "An installation of .NET 10.0.0 SDK
  can't be found". The dialog comes back after every OK, because `FUnrealSharpCoreModule::StartupModule()`
  calls itself whenever the check fails, with no exit condition. With dialogs suppressed, that recursion
  would not wait at all. ToolSuite handles it in `ue open`:
  - it restores the backslash in the editor's environment;
  - it refuses to launch when no SDK exists;
  - on Windows, it kills a headless editor that stops on a native modal dialog during startup, and
    prints the dialog's text.
- Filed upstream on 2026-10-11:
  - [#774](https://github.com/UnrealSharp/UnrealSharp/issues/774): recompile on lookup;
  - [#775](https://github.com/UnrealSharp/UnrealSharp/issues/775): .NET discovery and the `StartupModule` recursion;
  - [#776](https://github.com/UnrealSharp/UnrealSharp/issues/776): reload under `-unattended`, logged errors, scriptable reload.
- Each of these is a reasonable upstream contribution:
  - an unattended-safe reload path;
  - logged errors instead of modals, including the save-time syntax check;
  - a scriptable reload;
  - recompiling dirty types only on reload, not on lookup;
  - `DOTNET_ROOT`-aware .NET discovery.

**C# gameplay feature, built and verified through the agent loop (2026-10-11).** Coin pickups in
`Sandbox/Script/ManagedSandbox/Coins/`:
- `ACoinPickup`: sphere trigger, mesh, spin, respawn timer;
- `UCoinScoreSubsystem`: a world subsystem with a score delegate;
- a combo multiplier (×1 to ×5 within 1.5 s);
- a `[CustomLog]` category.

Content placement (`Sandbox/Scripts/place_coins.py`) and the PIE smoke test
(`Sandbox/Scripts/coin_smoke.py`) both run through `ue py -f`. Verification goes through
`ue log --cat LogCoins`.

| Step | Time |
|---|---|
| Pre-build catches a C# compile error before launch (`ue open`) | 3.8 s, 1 line + cascade |
| Place 5 coins in the live editor (`ue py -f place_coins.py`) | 57 ms (a commandlet would take ~24 s) |
| `ue pie start` / smoke test / `ue log` / `ue pie stop` | 0.8 s / 66 ms / 132 ms / 0.2 s |
| C# fix → `ue build` hot reload | 0.5 s |
| Full restart (`ue build --restart`: close, UBT, C#, headless boot) | 20.2 s |

Agent testing found two gameplay bugs that reading the code had missed:
- A coin respawning under a standing player was re-collected every 3 s, an endless score farm
  (the score reached 230).
- `Respawn()` logged after re-collection, so the log lied.

Both were fixed and re-verified: combo 10+20+30+40+50 = 150, and the occupied coin waits until the
player leaves.

New UnrealSharp hazards found this way:
- **Saving C# while PIE runs can kill PIE.** The file watcher reprocesses types even with reloads
  deferred. The next `GetWorldSubsystem<T>()` returned null, and UnrealSharp turns an unhandled
  managed exception in PIE into "Fatal error detected", which ends the session. Rule: stop PIE
  before editing C#.
- **Hot-reloading a structurally changed `UCSWorldSubsystem` corrupts its class default object.**
  Every new world (PIE start, new map) then throws `InvalidCastException` in
  `K2_DoesSupportWorldType`, and only a restart (20 s) clears it. `ue pie start` now detects this
  and suggests `ue build --restart`.

Tooling fixed along the way:
- `ue pie start|stop|status`. Status says *why* a session died, with the user-code frame
  (`@ CoinPickup.cs:61 (ACoinPickup.ActorBeginOverlap)`).
- Error log lines carry their first user-code stack frame.
- `ue log` / `exec` / `pie` flush the editor log first (`FLUSHLOG`), so just-logged lines are never
  missed.
- The beacon no longer spams `LogUtils` errors during PIE.
- `console` targets the PIE world while PIE runs.
- `close --discard` terminates instead of stalling on "Save Content?".
- Headless editors pass `-AutoDeclinePackageRecovery`, so no restore prompt can block a boot.

Lessons from the spike:
- Hot reload re-executes modules in place, so state that must survive (beacon port, REPL
  namespace) lives on `sys`; the reload routine re-binds tick callbacks afterwards.
- Editor boot for the sandbox was 25.7 s cold and 9.6 s warm — another argument for never
  paying it per question.

## 3. Where the time and tokens go today (measured, IslandHorror)

| Loop | Current method | Wall time | Agent cost |
|---|---|---|---|
| Read asset/actor state | write `Scripts/inspect_*.py`, run `-run=pythonscript` headless | ~24 s per run | ~1,940 log lines per run; stdout unreliable so results go to `last_run_report.txt`; property names found by trial and error (`MISS 'Fog Amount'`) |
| Mutate content | same headless commandlet | ~24 s per run | `fill_data_table_from_json_string` wipes rows, so updates re-declare everything; `new_level` / `spawn_actor_from_object` crash headless |
| C++ body change | `Build.bat` | 1–8 s | fails while the editor (Live Coding) is open, so the agent runs `tasklist` first |
| C++ reflection / header change | close editor, `Build.bat`, reopen | ~53 s boot + clicks | not automatable today |
| Verify behaviour | a human plays in PIE | minutes | invisible to the agent |
| Load project knowledge | `CLAUDE.md` | — | 49 KB ≈ 12k tokens every session |

## 4. Why Unreal is harder for agents than Unity

1. **State is binary.** `.uasset` / `.umap` / Blueprints are only legible through a running engine.
2. **The engine is expensive to start, and agents keep starting it.** Commandlets pay the 24–53 s
   boot every time instead of talking to the editor that is already open.
3. **Reflection forces restarts, and nothing orchestrates them.** Live Coding patches function
   bodies; UPROPERTY/UFUNCTION/layout/Build.cs changes need a restart, and UBT refuses to build
   while Live Coding is active.
4. **No agent-shaped CLI.** Unity has `unity status | cmd | recompile | test | job | shell | watch`.
   Unreal has `Build.bat`, `UnrealEditor-Cmd`, and (new in 5.8) a raw MCP endpoint.
5. **Output is huge and unfiltered.** Logs, UBT spew, full object paths, JSON nested in JSON.
6. **Huge, drifting API surface.** Python naming quirks (bool `b` prefix dropped, `Rotator`
   positional order is roll/pitch/yaw) turn into guess-and-check loops.
7. **Verification gap.** "Compiles" is not "works"; PIE, screenshots and tests are not wired
   into the agent's loop.

## 5. Landscape (October 2026)

### 5.1 Epic: UE 5.8 `ModelContextProtocol` + `ToolsetRegistry` (Experimental, measured)

- In-editor MCP server, Streamable HTTP + SSE, JSON-RPC, protocol `2025-06-18`. Defaults:
  `http://localhost:8000/mcp`, auto-start **off**. Start with `-ModelContextProtocolStartServer`
  (and `-ModelContextProtocolPort=N`) or the `ModelContextProtocol.StartServer [port]` console
  command; settings live in `EditorPerProjectUserSettings`. New in 5.8 (absent from 5.7).
- `bEnableToolSearch=true` (default) exposes only three meta-tools — `list_toolsets`,
  `describe_toolset`, `call_tool` — so tool schemas are discovered lazily.
- With `AllToolsets`: 52 toolsets, ~830 tools, 1.3 MB of schemas (~325k tokens if registered
  natively). Calls round-trip in 13–40 ms; the sandbox editor was serving 25.7 s after launch.
- Highlights: `BlueprintTools` (53 tools incl. `read_graph_dsl` / `write_graph_dsl`), `ObjectTools`
  (reflection list/get/set), `SceneTools`, `ActorTools`, `AssetTools`, `DataTableTools` (row-level
  upserts), `MaterialTools`, `EditorAppToolset` (Start/StopPIE, `CaptureViewport` with annotations,
  camera, selection), `AutomationTestToolset`, `SlateInspectorToolset` (click/type/snapshot any
  editor UI), `LogsToolset`, `ProgrammaticToolset` (sandboxed batching scripts), plus PCG, Niagara,
  UMG, GAS, StateTree, Sequencer/ControlRig, BehaviorTree and more.
- Shipped but not part of `AllToolsets`: `LiveCodingToolset`, MVVM, MetaHumanGenerator, ChaosCloth.
- **Extensible from Python, with hot reload.** A toolset is `@unreal.uclass() class X(unreal.ToolsetDefinition)`
  with `@toolset_registry.tool_call` static methods; type hints and Google-style docstrings become
  the MCP schema and description (`ToolsetRegistry/Content/Python/toolset_registry/tests/demo_toolset.py`).
  `toolset_registry.Registration([...]).register()` adds them; `toolset_registry/_reload.py`
  re-imports modules and re-registers changed toolsets in a running editor. C++ tools are
  `UFUNCTION(meta=(AICallable))` on a toolset class.
- **Live Coding is scriptable:** console commands `LiveCoding.Compile` / `LiveCoding.CompileSync`
  (`LiveCodingModule.cpp`), and `LiveCodingToolset` wraps `ILiveCodingModule::Compile(WaitForCompletion)`.
- **Security:** binds to `127.0.0.1` only and validates `Origin` against DNS rebinding; there is no
  auth token, so any local process can call it.
- Also useful: `ModelContextProtocol.GenerateClientConfig ClaudeCode` writes a `.mcp.json` for
  Claude Code; `UToolsetRegistrySettings` has `AllowedNames` / `BlockedNames` to trim toolsets;
  `MCPClientToolset` lets Epic's own assistant call *external* MCP servers.
- Epic's in-editor **AIAssistant** is an embedded browser for the Epic Developer Assistant (LLM
  hosted by Epic). It calls the ToolsetRegistry directly, not via MCP.
- **Blueprint graphs from Python are new in 5.8:** experimental `unreal.BlueprintGraphEditor`
  (`create_node_from_name`, ...) and `BlueprintGraphPinLibrary.try_create_connection`. Through 5.7,
  Python could not create nodes or connect pins.

Other engine facts that shape the design (Epic docs + 5.8 source):
- **Live Coding defaults (Windows):** on, with Reinstancing on, so UCLASS/USTRUCT/UENUM/UFUNCTION
  changes are reinstanced ("may cause packaging to fail"). Constructor defaults do not update
  existing instances. The first C++ class in a Blueprint-only project needs a restart. Patches land
  in `<Module>.patch_N.dll`, so always do a real UBT build with the editor closed before relaunching
  (which `ue build --restart` does). `-unattended` skips Live Coding auto-start; it starts lazily on
  the first `LiveCoding.Compile` (seen in the spike), or pass `-LiveCoding`.
- **UBT `-SingleFile=<path>`** runs a compile-only pass-through: a candidate for a fast `ue check`
  syntax gate that does not link.
- **Modal dialogs pause the engine tick**, which stalls MCP, Remote Control and Python remote
  execution at once. Only `FSlateApplication::GetOnModalLoopTickEvent` keeps firing.
- **Headless boots refuse to prompt:** under `-unattended`, a "modules are missing or built with a
  different engine version, rebuild?" check quits the editor instead of asking.
- **Python remote execution** (`remote_execution.py`: UDP discovery on 239.0.0.1:6766, then a
  client-hosted TCP channel, bound to 127.0.0.1 by default in 5.8) needs no plugin beyond Python,
  once "Enable Remote Execution" is ticked in project settings. It returns Python output but not
  C++ `UE_LOG`. This is the fallback route for UE 5.4–5.7, which lack Epic's MCP server.
- **Screenshots need an RHI:** `unreal.AutomationLibrary.take_high_res_screenshot` (async; poll
  `is_task_done()`) or `HighResShot` work in the editor viewport and in PIE, but not under
  `-nullrhi`.
- **Remote Control API** (HTTP 30010): in 5.8 calls need a function allowlist, and Python and
  console-command execution are off by default. Not a good foundation.

What an agent actually experiences (measured):

| Friction | Evidence |
|---|---|
| Discovery payloads | `list_toolsets` 13 KB (~3.3k tokens); one `describe_toolset` up to 306 KB (~77k tokens, Niagara System) |
| Verbose results | `find_actors` on an empty level: 145 refPaths, 15.6 KB (~3.9k tokens), ~45% repeated path prefix. `list_properties` on one light: 21 KB (~5.3k tokens) of JSON schema — types, no values — double-encoded JSON |
| Strict schemas | every string parameter is required (`"tag": ""`); a validation error echoes the full schema (~800 tokens) |
| Missing capabilities | no general Python (`ProgrammaticToolset` is sandboxed: no `unreal`, no exec/eval, imports limited to json/math/datetime/copy/re/time), no console commands, no log cursor or severity filter (`GetLogEntries` re-reads the whole log), no C++ build/restart orchestration, no editor lifecycle, no undo |
| Blocking | tool calls run synchronously on the game thread with no server-side timeout; cancelling is a no-op for registry tools, so a slow tool freezes both the editor and the HTTP server |
| Images | `CaptureViewport` / `CaptureEditorImage` return base64 PNG inside the text result, not as image content — a direct MCP client pays for the whole base64 string as text |
| Live Coding | `LiveCodingToolset` compiles synchronously on the game thread (editor frozen) and scrapes compiler errors from `Engine/Programs/UnrealBuildTool/Log.txt`, which does not exist on launcher installs (UBT logs to `%LOCALAPPDATA%\UnrealBuildTool`), so on this machine it returns no compiler diagnostics; no handling of header/reflection changes |
| Silent failures | `save_assets` returns `True` even when a save fails |
| Schema quirk | Python parameters whose default is `""` are still marked required — the source of the `"tag": ""` noise |
| Lifecycle | the endpoint vanishes whenever the editor restarts or crashes; sessions must be re-initialised; `tools/list_changed` effectively never reaches clients (new toolsets do appear at once via `list_toolsets`) |
| Telemetry | the plugin sends analytics by default; `ModelContextProtocol.EnableAnalytics 0` turns them off |
| Port | default 8000 collides with common dev servers and with a second editor |
| Licensing | the plugin logs a UE EULA reminder: data sent to an LLM provider is Licensed Technology, so make sure your provider is not allowed to train on it |

The same information rendered compactly (measured on the captured payloads): `find_actors`
15.6 KB → 1.5 KB (9%) with a 30-row cap; `list_properties` 21.3 KB → 2.1 KB (10%) as
`name: type` lines. Rendering alone is a 5–10× token cut.

### 5.2 Epic's Claude Code plugin, community bridges, commercial tools (researched 2026-10-09)

**Epic official, beyond the engine:**
- **`EpicGames/unreal-engine-skills-for-claude-code-plugin`** (created 2026-06-08, ~330 stars,
  ~870 installs): an `unreal-mcp` skill, a SessionStart hook, and an optional proxy that keeps
  Claude Code's MCP session alive while the editor is down. Its rules: save before and after bulk
  edits, run dependent calls one at a time, treat anything short of explicit success as failure.
  ToolSuite should *complement* it (CLI, lifecycle, build routing, compaction, unrestricted
  Python), not duplicate its skill.
- **UEFN** has had Unreal MCP in beta since 2026-08-20 (it can edit and compile Verse).
- At **State of Unreal 2026-06-17**, Epic showed a live Claude demo, said UE6 integrates models
  like Claude and Gemini, and announced Lore, an open-source VCS. Epic first called 5.8 the last
  major UE5 release, then announced an **AI-focused 5.9** at Unreal Fest Seoul (2026-08-20). No
  date or preview build yet; the in-editor AI Assistant is slated to gain project context and
  screenshots.

**Community bridges** (GitHub stars and last push as of 2026-10-09):

| Repo | Stars / push | Approach | Notes |
|---|---|---|---|
| chongdashu/unreal-mcp | 2.1k / 2025-04 | Python stdio → C++ TCP | the classic; unmaintained, 5.4–5.7 fixes unmerged |
| flopperam/unreal-engine-mcp | 1.1k / 2026-06 | Python stdio → C++ TCP | ~42 tools; acquired by Aura |
| ChiR24/Unreal_mcp | 0.9k / 2026-10 | TS stdio → C++ WebSocket | 23 grouped tools; per-engine binaries |
| Natfii/UnrealClaude | 0.9k / 2026-06 | editor panel runs `claude -p` | 5.7 only |
| kevinpbuckley/VibeUE | 0.7k / 2026-10 | plugs into Epic's 5.8 registry | 6 tools + ~36 on-demand skills, batches via Python |
| db-lyon/ue-mcp | 0.4k / 2026-10 | Node stdio → C++ WebSocket | 26 tools / 1,975 actions, wraps Epic's tools |
| tumourlove/monolith | 0.3k / 2026-09 | C++ HTTP + restart-surviving stdio proxy | ~1,400 actions behind ~26 query tools |
| runreal/unreal-mcp | 0.1k / 2025-06 | Python remote execution, no plugin | `editor_run_python` |
| Italink/UnrealClientProtocol | 0.1k / 2026-08 | Python **CLI + skills**, no MCP | closest in spirit to ToolSuite |
| ZiggyMar/unreal-mcp | <0.1k / 2026-09 | C++ TCP + Node | measured: 4-tool profile 2.4k vs 34.8k tokens; Blueprint text 126k → 3.1k tokens for 807 nodes |

**Commercial:** **Aura** (Ramen; Claude-based agents including a playtesting "verification"
agent; 1.0 on 2026-09-09 at $10/$40/$200 a month; acquired Coplay and Flopperam). Its C++ agent
compiles through Live Coding, and on failure the *user* closes the editor and rebuilds by hand —
the step `ue build --restart` automates. **Ludus AI** (credits; UE 5.4–5.8; MCP from $25 a month).
StraySpark and ClaudeUnreal (16 MCP tools + CLI) make unverified token-saving claims.

**Field reports from Claude Code users:**
- After an editor restart, Epic's in-memory sessions return 404 until a manual `/mcp reconnect`.
- Starting Claude Code while the editor is down leaves the session with no Unreal tools.
- A dead editor blocks calls until a 30-minute timeout, and a hung one caused a change to be
  applied twice.
- Puget Systems measured Epic's MCP with Claude at 5–15 minutes per response and 15% of a weekly
  quota for its tests.
- Screenshots arrive as multi-MB base64.

A CLI that connects per call (as `ue` does) is immune to the reconnect problems, and compact
rendering plus Python batching attacks the cost.

### 5.3 Unity parity baseline (the `unity` CLI installed on this machine)

`unity` CLI + the Unity **Pipeline package** (an in-editor command registry):
`status` (live editors: port, project, PID, state) · `open` / `close` / `run` / `build` ·
`cmd` (list commands with query/tag/detail/paging; execute; `--detach`; `--result-only`) ·
`recompile` (with compile errors) · `test` · `job` · `shell` (warm REPL) · `watch` ·
`skill install` · `mcp` · `--format human|json|tsv|ndjson|github`.

Mapping: Unity Pipeline package ≈ Epic `ToolsetRegistry` + MCP server (exists). Unity CLI ≈
**nothing yet** — that is the main thing to build.

Verified details (Unity docs/blog, as of 2026-10-09):
- **Timeline:** CLI beta 2026-04-04, editor control via Pipeline in beta.5 (2026-05-21), official
  launch 2026-07-20, latest 1.0.0-beta.13 (2026-10-07). "Pipeline Bridge" is not a product name;
  reviewers call CLI + `com.unity.pipeline` "the bridge".
- **Connection:** `com.unity.pipeline` auto-starts an editor HTTP/JSON server on
  127.0.0.1:7800–7849. Discovery uses `Library/Pipeline/.unity-pipeline-port` (pid, port,
  **token**, heartbeat) — the same shape as our `Saved/ToolSuite/editor.json` beacon, plus a bearer
  token we should add.
- **Commands:** 150+ built-ins; `unity command eval '<C#>'` runs Roslyn on the main thread without
  a recompile (our `ue py`); custom commands are static methods tagged `[CliCommand]` (our
  `@toolset_registry.tool_call`); async `recompile` with status polling; detached `job`s; exit code
  7 means "safe to retry".
- **Unity moved from MCP to the CLI:** the AI Assistant's MCP server is deprecated ("Use the Unity
  CLI instead"). `unity mcp` remains as a stdio wrapper over Pipeline commands, but Unity recommends
  calling `unity command` directly because it costs fewer tokens. Their Claude Code plugin
  (`unity-agent-plugin`, 2026-09-09, 29 skills) is skills-only, with editor access through the CLI.
  This is the same CLI-first, skills-for-knowledge split proposed here.
- **Reloads:** the Pipeline server is recreated on each domain reload, so clients must tolerate
  connection errors (the CLI retries through reloads). Unity 7.0 alpha (CoreCLR, 2026-10-05) removes
  domain reloads altogether. Unreal's equivalent pain is editor restarts, which `ue build --restart`
  automates.

Adopt from Unity: a per-session token in the beacon, a "safe to retry" exit code for busy or
restarting editors, and `ue setup claude` to install the skill.

### 5.4 Code-iteration options (researched 2026-10-09)

| Option | Engine change? | UE versions | Hot-reload scope | Tooling | Maturity |
|---|---|---|---|---|---|
| **Hazelight Angelscript** | **Yes:** private engine fork, built from source | not published (reportedly 5.8.1, unverified) | Outside PIE, anything (full reload + Blueprint reinstancing). In PIE, function bodies only; new members queue until PIE ends; removals/signature/layout changes rejected | VS Code LSP + debugger (v1.9.4, ~25k installs), Rider, unit tests re-run on every reload, coverage | Shipped (Split Fiction, ARC Raiders, ...) |
| TDGameStudio AS port | claims none, but documented on a 5.7 source build | 5.7 (5.8 host project) | Hazelight's reload logic | own VS Code extension | new (Apr 2026), small |
| **UnrealSharp (C#)** | No (plugin) | 5.6–5.8 | full type changes incl. new UClasses; reload deferred during PIE | any C# IDE, debugger, `llms.txt` | 1.9k stars; Win/macOS; build from source (.NET 10); FAQ: "not production ready yet" |
| UnrealCSharp | No | 5.0–5.8 | new UClass/UStruct/UEnum; compiles on editor focus, never in PIE | .NET 10 | v1.3.0 (Sep 2026) |
| PuerTS (TypeScript) | No | 4.22+ | live edits via V8 debugger; TS classes → Blueprints | TS declarations | v1.0.9 (Jul 2025) |
| UnLua / sluaunreal | No | older 5.x | Lua reload | stubs | low activity / dormant |
| C++ Live Coding | No | all | measured here: bodies 4.5 s, new UFUNCTIONs via reflection 7 s, the rest via 24–65 s automated restart | built in | built in |

- **Verse is not an option for UE5.** It remains UEFN-only, and the 5.8 notes contain nothing about
  it. Epic announced UE6 on 2026-06-17 (Unreal Fest Chicago) as a merge of UE5 and UEFN: Early
  Access "end of 2027-ish", stable 12–18 months later. Scene Graph will eventually replace Actors,
  with conversion tools promised. A `ue6-main` branch is public but "not any kind of alpha", and
  Verse is not wired up there yet.
- **UE5's runway is short:** after 5.8, Epic has announced only an AI-focused 5.9 (no date).
  Building on 5.8's MCP server targets the endpoint Epic is investing in, and the CLI/skill layer
  is engine-version-agnostic for UE6.
- **Angelscript has the best reload story, but it is a fork:** moving IslandHorror onto a source-built
  Hazelight engine and tracking their version cadence, in a niche language with thinner LLM training
  data. Its standout agent feature is that unit tests run on every reload.
- **UnrealSharp is the low-friction trial:** plugin-only, C# (a strong LLM language), supports 5.8,
  but must be built from source and is self-described as not production-ready. **Trialled
  2026-10-11 (§2):** C# edits hot-reload in 0.6–1.2 s including new properties and classes, once
  three agent hazards are worked around: no hot reload under `-unattended`, a modal dialog on compile
  errors, and no scriptable reload.

**Recommendation:** treat a scripting layer as an optimisation, not a prerequisite. The measured
Live Coding + automated restart loop already removes most restarts. If you want one, trial
UnrealSharp in `Sandbox/` first (no engine change; measure reload time and agent error rate), and
consider Angelscript only if a source-built fork is acceptable for IslandHorror.

## 6. Proposed architecture

```
Claude Code --Bash--> ue CLI --HTTP JSON-RPC--> Epic MCP server (in editor)
     |                   |                          |-- Epic toolsets (~830 tools)
     |                   |                          '-- toolsuite.* toolset (Python, hot-reload)
     |                   |-- reads Saved/ToolSuite/editor.json  (pid, port, state, heartbeat)
     |                   |-- runs UBT / Build.bat               (when the editor is closed)
     |                   |-- launches / quits UnrealEditor.exe  (open, close, restart)
     |                   '-- tails Saved/Logs/*.log             (works when hung or crashed)
     '-- optional: direct MCP over HTTP to the same server
```

### 6.1 `ue` CLI — the out-of-editor half

| Command | Does | Replaces | Status |
|---|---|---|---|
| `ue status` | editors, map, state (booting / ready / busy / pie / compiling / hung / crashed), port, last build | `tasklist` checks | spike (ready/pie/hung/not running) |
| `ue open [--map M] [--wait]` | launch with a per-project port and quiet flags, wait for ready, print boot time + new errors only | manual launch | spike |
| `ue close [--save\|--discard]` | graceful quit that never stalls on "Save changes?" | manual | spike |
| `ue build` | routes automatically: editor up + live-safe change → Live Coding; reflection/Build.cs change or editor down → (close →) UBT (→ reopen same map). Compact diagnostics | Build.bat + restart dance | spike |
| `ue py <code>` / `ue py -f x.py` | unrestricted Python in the live editor; headless fallback when no editor | `-run=pythonscript` (24 s → tens of ms) | spike (live editor) |
| `ue exec <cmd>` | console command in the editor or PIE world (`ih.GodMode 1`) | — | spike |
| `ue props <obj> [regex]`, `ue get`, `ue set` | reflected names, types **and values** as a compact table | `inspect_*.py` | props: spike |
| `ue actors [regex]`, `ue find assets <q>` | compact tables, short handles | — | actors: spike |
| `ue log [--since last] [--level warn] [--cat X] [--grep Y]` | cursor-based, de-duplicated; reads the file if the editor is gone | 2k-line logs | spike |
| `ue pie start\|stop\|status`, `ue smoke <map> [--cmd ...] [--secs N]` | PIE plus a "did anything error/ensure" verdict | a human playing | planned |
| `ue shot [--pie] [--w 960]` | viewport capture → PNG path; the agent opens it only when needed | — | planned |
| `ue test [filter]` | automation tests, compact pass/fail | — | planned |
| `ue tools [query]`, `ue call <toolset.tool> k=v ...` | discover and invoke any Epic or ToolSuite tool, defaults filled in, output compacted | raw MCP | spike |
| `ue job list\|wait\|cancel` | detached long-running operations | — | planned |
| `ue api <Symbol>` | exact UE 5.8 signatures from live reflection | hallucinated APIs | planned |

Output contract:
- Default "agent" text: tables with common prefixes stripped, numbers rounded, at most 30 rows then
  `... +N more (--limit)`. `--json` / `--ndjson` for machines.
- Diagnostics: `file:line: code: message`, de-duplicated, cascades collapsed, first cause first.
- Exit codes are a protocol: `0` ok · `1` failed · `2` editor unavailable · `3` restart required ·
  `4` timeout · `7` busy, safe to retry (planned, matching Unity's CLI).

### 6.2 ToolSuite editor plugin — fill the gaps, nothing more

v1 is a **content-only plugin (pure Python, zero C++)**: nothing to compile, it drops into any 5.8
project's `Plugins/`, and updates hot-reload without restarting the editor.

- **Python toolset `toolsuite.*`** registered into `ToolsetRegistry`, so the `ue` CLI, Claude
  Code's own MCP client and Epic's in-editor assistant all see it: `py`, `console`, `props`
  (names, types *and* values, filtered), `actors`, `level_summary`, `reload` (all working in the
  spike), then `live_compile`, `smoke`, `shot` and project-specific tools. Re-registered through
  `toolset_registry.reload_module`, so the agent can add tools without restarting the editor.
- **`live_compile` must not block the game thread:** fire `LiveCoding.Compile` (async), let the
  CLI follow `LogLiveCoding` through its log cursor, and parse compiler diagnostics from
  `%LOCALAPPDATA%\UnrealBuildTool\Log.txt` — the path Epic's toolset misses on launcher builds.
- **Screenshots go to files:** decode PNGs to `Saved/ToolSuite/shots/` and return the path, so
  the agent pays for an image only when it chooses to look.
- **Beacon:** a Slate post-tick callback writes `Saved/ToolSuite/editor.json` (pid, port, map,
  PIE state, heartbeat) every second. A stale heartbeat with a live PID means the game thread is
  hung — detectable without HTTP.
- **Log cursors live in the CLI**, as byte offsets into `Saved/Logs/<Project>.log`, so they keep
  working after a hang or crash and need no editor code.
- **Guard rails for `py` / `exec`:** a per-session token in `Saved/ToolSuite/` that the CLI sends
  with each call, on top of Epic's localhost-only binding.
- **Modal-dialog watchdog:** a dialog freezes every bridge, so detection must happen out of
  process. When the heartbeat is stale but the PID is alive, the CLI lists the editor's top-level
  windows (Win32 `EnumWindows`) and reports the dialog title, e.g. `blocked by modal: "Save
  Content"`, instead of timing out blind. A tiny C++ hook on `GetOnModalLoopTickEvent` can record
  the dialog in the beacon. In agent-only sessions, use `-unattended` /
  `WindowsPlatform.DisableMessageBox=1` so dialogs never block.
- **`ue open` pre-builds C++ projects** with UBT (about a 1 s no-op when up to date), so a stale
  module never triggers the rebuild prompt — which quits the editor outright in headless mode.
- **C++ only if a gap demands it** (e.g. the modal hook above, or Live Coding diagnostics that
  never reach a log). Kept small and optional.
- **Undo-safe by default:** every mutating tool runs inside an editor transaction, so you can
  Ctrl+Z agent work.
- Existing `Scripts/inspect_*.py` / `build_*.py` become named, reusable tools over time.

### 6.3 Claude Code integration

- **Skill `unreal-toolsuite`** (v0 in `.claude/skills/`, loaded on demand): command reference, the iteration decision table, and the
  Python gotchas currently buried in IslandHorror's CLAUDE.md.
- **Hooks, done:** two PreToolUse hooks.
  - `ue hook pre-edit` denies C# edits while PIE runs.
  - `ue hook pre-mcp` denies Epic's `StartPIE` while saved C# has not been hot-reloaded (see §2). It
    matches `call_tool` on the `unreal-mcp` connection and finds StartPIE in direct calls and in
    batch scripts.
- **Hooks, planned:** SessionStart injects a two-line editor status; PostToolUse on `Source/**` classifies
  the edit as live-codable or restart-required; Stop nudges `ue build` if C++ changed since the
  last green build.
- **Context diet (measured):** five subsystem sections of IslandHorror's CLAUDE.md (Terrain &
  water 10.3 KB, Player systems 8.5 KB, Adaptive cult 6.1 KB, Developer tooling 5.7 KB, Editor
  tooling 4.5 KB) are 73% of its 48 KB. Moving them to `docs/systems/*.md`, linked from a ~13 KB
  core, saves ~9k tokens in every session without losing any knowledge.

### 6.4 Iteration tiers

| Change | Route | Latency |
|---|---|---|
| Content, level, data | Python in the live editor | < 1 s |
| C++ function bodies | `ue build` → Live Coding | 4–5 s (measured) |
| New UFUNCTIONs | `ue build` → Live Coding, call via reflection; batch the restart for later | ~7 s (measured) |
| New UPROPERTYs / layout / CDO defaults / Build.cs | `ue build --restart`: save → close → UBT → reopen same map | 24 s sandbox (measured), ~65 s IslandHorror (estimated), zero clicks |
| C# gameplay code (UnrealSharp): bodies, new UFUNCTIONs, UPROPERTYs, classes | `ue build` → bridge hot reload | **0.6–1.2 s** (measured), no restart |
| Angelscript (later pass) | engine fork | not yet measured |

## 7. Token and speed rules

1. CLI first, MCP optional: zero standing context cost; the skill loads only when needed.
2. Never re-read: cursors for logs, deltas for state.
3. Filter in the editor; render compactly; strip common prefixes; never nest JSON in JSON.
4. Discovery is paged and summarised, never dumped (`ue tools blueprint` → names + one-liners;
   `ue tools <toolset>:<tool>` for a single schema).
5. Fill defaults client-side; errors are one line, not schema dumps.
6. Images only on request, downscaled.
7. Keep the editor alive and pick the cheapest route automatically (Python ≪ Live Coding ≪ restart).
8. Long operations are jobs, not blocking calls.

## 8. Roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 — CLI spine | **done in spike:** `status`, `open`, `close`, `build` (Live Coding / UBT routing, compact diagnostics, `--restart`), `tools`, `call`, `log`, per-project port, skill v0. **To do:** harden, then dogfood on IslandHorror | Sandbox and IslandHorror driven end-to-end without a manual editor step |
| 1 — Gap toolset | **done in spike:** `py`, `exec`, `props`, `actors`, `level_summary`, `reload`, beacon + heartbeat. **To do:** `get`/`set`, `shot`, `pie`, `test` | the `inspect_*.py` loop replaced; 24 s → sub-second |
| 2 — Restart-free loop | **done in spike:** Live Coding orchestration, coarse change classifier, automated restart. **To do:** finer classifier (diff reflected declarations), hooks, `smoke` | edit C++ → verified in PIE with no human step |
| 3 — Depth | Blueprint DSL ergonomics, DataTable/DataAsset round-trips, `api`, `job`, `watch` | Blueprint changes reviewed as text diffs |
| 4 — Scripting | **UnrealSharp trial done in Sandbox:** headless project creation, C# pre-build in `ue open`, bridge-driven reload in `ue build` (0.6–1.2 s, structural changes included), one-line C# errors. **Gameplay feature done** (coins + score subsystem + combo, verified in PIE through `ue`). **Since done:** PreToolUse hooks that block C# edits while PIE runs and Epic's `StartPIE` while C# is unreloaded; `ue pie start` reloads pending C# first (works around UnrealSharp's recompile-on-lookup, §2); headless editors log dialogs instead of hanging on them. **Reported upstream:** [#774](https://github.com/UnrealSharp/UnrealSharp/issues/774) (recompile on lookup), [#775](https://github.com/UnrealSharp/UnrealSharp/issues/775) (.NET discovery + `StartupModule` recursion), [#776](https://github.com/UnrealSharp/UnrealSharp/issues/776) (headless reload). **To do:** Angelscript in a later pass | C# features iterated with no restarts |

## 9. Decisions

Decided 2026-10-11:
- Build on Epic's 5.8 MCP + ToolsetRegistry.
- The CLI is stdlib-only Python.
- Dogfood on Sandbox first, alongside Epic's official Claude Code plugin.
- Trial **UnrealSharp** now; Angelscript in a later pass.

The options as they were laid out:

1. **Foundation:** build on Epic's 5.8 MCP + ToolsetRegistry (recommended; 5.8+ only and still
   Experimental) or a standalone bridge that also covers older engines. A middle path keeps the
   CLI engine-agnostic and adds a Python remote-execution transport for 5.4–5.7, with no plugin
   needed.
2. **CLI language:** stdlib-only Python (recommended: same language as the in-editor toolset,
   zero dependencies, can even run on the Python bundled with UE) vs TypeScript compiled with Bun
   vs Rust. Startup is a non-issue either way (measured 17–81 ms vs a 24,000 ms commandlet).
3. **Dogfooding order:** Sandbox first, then IslandHorror, alongside Epic's official Claude Code
   plugin (§5.2) to compare its MCP-proxy approach against the CLI on the same tasks.
4. **Scripting layer:** defer to Phase 4 (recommended), or try UnrealSharp in `Sandbox/` now.
   Angelscript only if moving IslandHorror to a source-built engine fork is acceptable. Verse is not
   available until UE6 Early Access (~end 2027).

## 10. Research notes

Six research passes ran on 2026-10-09: Epic MCP/registry source, toolset catalog, iteration
internals, scripting options, community/commercial landscape and the Unity baseline. Their findings
are folded into §5. Items marked unverified there rest on a single source, or on vendor claims.
Primary sources: dev.epicgames.com (Unreal MCP, Live Coding, Python, Remote Control docs), the
local UE 5.8.3 source, docs.unity.com (Unity CLI and Pipeline), the GitHub repos named above, and
press coverage of Unreal Fest 2026.
