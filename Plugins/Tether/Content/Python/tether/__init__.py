"""Tether editor-side package. Hot-reload with the TetherCore.reload tool (or `ue reload`).

Scripts run in PIE can `import tether` and use tether.world(), player(), controller(), actors(), console() and
world_subsystem(cls), and in the editor world rerun(actor): they follow `ue pie target` in multiplayer PIE (see tether.scripting)."""
from .scripting import actors, console, controller, finish, player, rerun, spawn, world, world_subsystem  # noqa: F401
