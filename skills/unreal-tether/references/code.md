# Code: C++, C#, automated tests

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
- Another project's editor with Live Coding on (same engine install) does not block `ue build`: it passes
  `-NoHotReloadFromIDE` when this project's editor is closed. It does block an IDE or hand-run Build.bat build
  ("Unable to build while Live Coding is active"); add that flag there too.

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

## Automated tests

- `ue test <filter>` runs automation tests in the live editor and prints one summary line plus each
  failure with its source line; exit code 1 if any failed. Filter syntax is Epic's: a substring,
  `^Prefix`, `Suffix$`, `StartsWith:X`, `Group:Name`, joined with `+`. Never run without a filter (the
  engine has ~9k tests). Anchor project filters (`^MyGame.`): a bare substring also matches engine
  tests, and some of those change the open level. `ue test --list <substring>` lists names; `-v` adds
  warnings.
- New tests compiled with `ue build` are picked up automatically.
