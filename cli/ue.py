#!/usr/bin/env python3
"""ue: a token-frugal CLI for driving a running Unreal Editor from coding agents.

This file only starts the CLI: the implementation lives in the tether_cli package next to it. Python caches
an imported module's bytecode but recompiles the script it runs on every start, which for the whole CLI
cost about 0.14 s per call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Agent shells often set PYTHONDONTWRITEBYTECODE, which would make every call recompile the package; its cache
# (cli/tether_cli/__pycache__, git-ignored) is worth writing.
sys.dont_write_bytecode = False

import tether_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(tether_cli.main())
