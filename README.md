# ToolSuite

Agent-native tooling for Unreal Engine 5.8+: drive a **running** editor from Claude Code (or any
agent) quickly and with few tokens, instead of booting headless commandlets or restarting the editor.

- `cli/ue.py`: token-frugal CLI (stdlib-only Python). It talks to Epic's in-editor MCP server
  and reads the editor beacon and logs directly.
- `Plugins/ToolSuite`: content-only (pure Python) editor plugin. It registers a `toolsuite` toolset
  into Epic's `ToolsetRegistry` (unrestricted Python, console commands, compact reads, hot reload)
  and writes `Saved/ToolSuite/editor.json` as a heartbeat.
- `Plugins/ToolSuiteUnrealSharp`: optional, tiny C++ editor plugin for UnrealSharp projects. It
  makes C# hot reload callable from Python/MCP with modal dialogs suppressed, so `ue build`
  reloads C# in about 1 s.
- `.claude/skills/unreal-toolsuite`: Claude Code skill that teaches the workflow. It loads
  automatically in sessions started at this repo's root; copy it into another project's
  `.claude/skills/` to use it there.
- `.claude/settings.json`: two PreToolUse hooks:
  - one blocks C# edits while the project's editor is in PIE;
  - one blocks Epic's `StartPIE` while saved C# has not been hot-reloaded.

  It also enables Epic's official `unreal-engine-skills-for-claude-code` plugin for side-by-side
  comparison, with its `unreal-mcp` connection to the sandbox editor configured in `.mcp.json`.
- `Sandbox/`: scratch UE 5.8 project used for development.
- `docs/DESIGN.md`: analysis, measurements, architecture and roadmap.

Status: working spike (Phase 0/1). See `docs/DESIGN.md` §2a for measured results.

## Quickstart (any UE 5.8 project)

1. Enable plugins in the `.uproject`: `ModelContextProtocol`, `EditorToolset` (or all of Epic's
   toolsets via `AllToolsets`, which also pulls in plugins such as GameFeatures) and `ToolSuite`.
   - To get `ToolSuite`, either copy `Plugins/ToolSuite` into the project's `Plugins/`, or add
     `"AdditionalPluginDirectories": ["<path to this repo>/Plugins"]`.
   - The entry must point at `Plugins`, not at a plugin folder: UnrealBuildTool only searches subfolders.
   - Both ToolSuite plugins are opt-in (`EnabledByDefault: false`), so list `ToolSuite` explicitly.
     Projects without UnrealSharp simply leave `ToolSuiteUnrealSharp` out. IslandHorror's setup is the
     example.
2. Put this repo's `cli/` folder on `PATH` (or call `<repo>\cli\ue.cmd`). Then run these from the project folder, or with `UE_PROJECT` set:

```
cli\ue.cmd open                 # launches with the MCP server on a per-project port, waits until ready
cli\ue.cmd status
cli\ue.cmd actors light
cli\ue.cmd props DirectionalLight.LightComponent0 intensity
cli\ue.cmd py "print(unreal.SystemLibrary.get_engine_version())"
cli\ue.cmd tools blueprint
cli\ue.cmd log --level Warning
cli\ue.cmd pie start            # Play-In-Editor; `pie status` explains a session that died
cli\ue.cmd build                # Live Coding if the editor is up (~5 s), UBT if not; one-line diagnostics
cli\ue.cmd build --restart      # reflection/Build.cs changes: save, close, UBT, reopen same map
cli\ue.cmd close                # refuses to drop unsaved work unless --save or --discard
```

## C# with UnrealSharp (optional)

1. Clone UnrealSharp into the project's `Plugins/UnrealSharp` and enable `ToolSuiteUnrealSharp`.
   This needs .NET SDK 10.0.5 or later. `Sandbox/` uses commit `06769c4`.
2. Run `ue build` with the editor closed (UBT; the first build takes about 2 minutes).
3. Create the C# project without the GUI wizard:
   `RunUAT.bat GenerateProject -ScriptDir=<proj>\Plugins\UnrealSharp\Build\Scripts -Project=<proj>\<P>.uproject -ProjectName=Managed<P> -ProjectFolder=<proj>\Script\Managed<P> -GenerateSolution=true -RunUSharpProjectSetup=true -CreateModuleClass=true`
4. Add `AutomaticHotReloading=Off` under `[/Script/UnrealSharpEditor.CSUnrealSharpEditorSettings]`
   in `Config/DefaultEditorPerProjectUserSettings.ini`.
5. `ue open` pre-builds the C#. After that, `ue build` hot-reloads C# changes.

Note: Epic's MCP plugin sends analytics by default. Run `ModelContextProtocol.EnableAnalytics 0` in the
editor console to turn them off.
