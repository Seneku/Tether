"""Tether editor-side package. Hot-reload with the TetherCore.reload tool (or `ue reload`).

Scripts run in PIE can `import tether` and use tether.world(), player(), controller(), actors() and console():
they follow `ue pie target` in multiplayer PIE (see tether.scripting)."""
from .scripting import actors, console, controller, player, world  # noqa: F401
