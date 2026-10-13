# Contributing to Tether

Thanks for your interest. Tether is an early spike, so bug reports with exact commands and output are
just as valuable as code.

## Reporting bugs and asking for features

Use the [issue templates](https://github.com/Seneku/Tether/issues/new/choose). For bugs, include:

- the exact `ue` command and its full output, and `ue selftest` output if the editor runs;
- your UE version (`ue status` prints it), OS and shell (PowerShell, cmd or Git Bash);
- whether the editor was headless (`ue open --headless`) or windowed;
- whether the project uses UnrealSharp.

Some problems belong upstream:

- **Epic's MCP server or editor tools** (`ModelContextProtocol`, `ToolsetRegistry`, Epic's toolsets):
  report them to Epic. Open an issue here too if Tether could work around them.
- **UnrealSharp:** report to [UnrealSharp](https://github.com/UnrealSharp/UnrealSharp/issues). Known
  issues Tether works around are listed in [docs/DESIGN.md](docs/DESIGN.md) §2.

## Development setup

You need:

- Windows, with Unreal Engine 5.8+ (a launcher install is fine) and Visual Studio's C++ toolchain;
- Python 3 on `PATH`;
- for C# work: the .NET 10 SDK, with UnrealSharp cloned into `Sandbox/Plugins/UnrealSharp` (see the
  README; it's gitignored).

`Sandbox/` is the development project. It loads the plugins from `../Plugins` through
`AdditionalPluginDirectories`, so edits to the plugins take effect without copying anything.

To work on the Claude Code plugin (skill, hooks, `bin/`), load it from your checkout instead of the
published copy: `claude --plugin-dir .` for one session, or `claude plugin marketplace add . --scope local`
then `claude plugin install tether@tether --scope local`. Check it with `claude plugin validate . --strict`.
Users only receive a new plugin release when `version` in `.claude-plugin/plugin.json` changes, so bump it
(and `VersionName` in `Plugins/Tether/Tether.uplugin`) when releasing.

```
cd Sandbox
python ../cli/ue.py build            # first build (UBT)
python ../cli/ue.py open --headless  # ~10-25 s; no window, no dialogs
python ../cli/ue.py status
```

The iteration loop for each part:

| You changed | Make it live with |
|---|---|
| `cli/tether_cli/` | nothing: each call is a fresh process |
| `Plugins/Tether/Content/Python/tether/*.py` | `ue reload` (no editor restart) |
| `Plugins/TetherUnrealSharp` C++ (function bodies) | `ue build` (Live Coding) |
| `.uplugin`, `Build.cs`, new `UPROPERTY`/`UFUNCTION` | `ue build --restart` |
| `Sandbox/Script/**/*.cs` | `ue build` (C# hot reload) |

Close the editor with `ue close` when you're done.

### Where CLI code goes

`cli/tether_cli/` is split by layer; each module imports only from the ones listed before it in
`__init__.py` (core, transport, build, editor, calls, images, testing, assets, jobs, play, playtest, hooks, setup,
selftest, cli). A new command is a `cmd_<name>(ctx, args)` function in the module that fits, plus its
subparser in `cli.py`'s `build_parser()`. Set `ctx.data` to something structured if a script would want it
from `--json`. Helpers two modules need go in the lower of the two, so imports never go back up. In tests,
patch a name in the module that defines it (`ue.core.editor_live`), not on the package.

## Testing

Unit tests cover the parts that need no editor: log filtering, diagnostics, change classification,
hooks, and the toolset's value parsing (with `unreal` stubbed). There is no hosted CI: run them yourself, ideally
on Python 3.11 (the version bundled with UE 5.8) and a current 3.x. Before opening a PR:

1. `python -m unittest discover -s tests`, and add tests for new parsing or routing logic.
2. Against the Sandbox editor (`ue open --headless --render`), run the commands your change touches, then
   `ue selftest`. It should report 0 failed; skips must have a reason that fits your setup.
3. If you touched build, lifecycle or PIE code, also run `ue build`, `ue build --restart`,
   `ue pie start` / `ue pie stop`, and `ue close`.
4. For C# changes, `Sandbox/Scripts/coin_smoke.py` exercises the UnrealSharp coin feature during PIE.
   From `Sandbox/`: `ue py -f Scripts/place_coins.py`, `ue pie start`, then `ue py -f Scripts/coin_smoke.py`.

Paste the relevant output into the PR. If a change makes output shorter or faster, before/after numbers
help.

## Design rules

These keep Tether cheap for agents. [docs/DESIGN.md](docs/DESIGN.md) §7 has the full list.

- **The CLI is stdlib-only Python in a single file.** Don't add third-party dependencies.
- **Output is for agents.** Keep it short, line-oriented and de-duplicated. Strip common prefixes,
  never nest JSON in JSON, and keep errors to one line, not schema dumps. Full detail goes behind a
  flag (`--json`, `-v`).
- **Never re-read.** Logs use on-disk cursors; state queries return deltas or summaries.
- **Keep the editor alive.** Pick the cheapest route automatically: Python, then Live Coding, then a
  restart.
- **Exit codes are an API:** `0` ok, `1` failed, `2` editor unavailable, `3` restart required,
  `4` timeout. Don't change their meaning.
- **No surprise dialogs.** Agent-launched editors must never block on a modal dialog, and headless
  sessions must stay headless.
- **Build on Epic's server.** New editor capabilities go into the `tether` toolset (Python first), not
  into a separate bridge. Prefer an existing Epic tool when it's already compact enough.

## Pull requests

- Keep each PR to one focused change, and explain what it does and why.
- Update `README.md`, the skill (`skills/unreal-tether/SKILL.md`) and `docs/DESIGN.md` when
  behaviour or commands change. The skill is what agents read, so keep it accurate and short.
- Match the style of the surrounding code.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
