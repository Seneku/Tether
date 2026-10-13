"""ue: a token-frugal CLI for driving a running Unreal Editor (UE 5.8+) from coding agents.

Talks to Epic's in-editor MCP server (ModelContextProtocol plugin) plus the Tether
toolset, and reads Saved/Tether/editor.json and Saved/Logs directly, so status and
logs keep working when the editor is hung or gone. Stdlib only.

Exit codes: 0 ok, 1 tool/command failed, 2 editor unavailable, 3 restart required, 4 timeout.

Modules, lowest layer first (each imports only from those above it):
  core       project/beacon files, engine discovery, ports, dialogs, crash reports, git, rendering, logs
  transport  Epic's MCP server, Python remote execution (5.4-5.7), the toolset catalog
  build      UBT, Live Coding, the UnrealSharp C# build, compiler diagnostics
  editor     status, open, close, build, crash
  calls      thin commands over one editor tool each
  images     screenshots, ue look, PNG read/write/diff
  testing    ue test
  assets     Blueprints, data tables, diff, validate, size, usages, overview
  content    ue import, unused, lint, landscape
  jobs       ue job, ue package
  play       pie, ui, perf, mem, world, audio, soak
  playtest   ue input, pie record/watch, baselines, perf load, bp trace, budget
  hooks      Claude Code hooks
  setup      ue init, reload, doctor
  selftest   ue selftest, ue ci
  cli        argument parsing, ue batch, main()

Everything is re-exported here, so `import tether_cli` reaches any name; patch a name in the module that
defines it, since the others hold their own reference.
"""
from .core import *  # noqa: F401,F403
from .transport import *  # noqa: F401,F403
from .build import *  # noqa: F401,F403
from .editor import *  # noqa: F401,F403
from .calls import *  # noqa: F401,F403
from .images import *  # noqa: F401,F403
from .testing import *  # noqa: F401,F403
from .assets import *  # noqa: F401,F403
from .content import *  # noqa: F401,F403
from .jobs import *  # noqa: F401,F403
from .play import *  # noqa: F401,F403
from .playtest import *  # noqa: F401,F403
from .hooks import *  # noqa: F401,F403
from .setup import *  # noqa: F401,F403
from .selftest import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403
