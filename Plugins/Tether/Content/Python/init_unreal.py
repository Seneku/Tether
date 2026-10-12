"""Tether startup: register the toolset with Epic's ToolsetRegistry and start the editor beacon."""
import unreal

from tether import beacon, toolsets

if toolsets.registration is None:
    unreal.log("Tether: no ToolsetRegistry (UE < 5.8); tools are reachable through Python remote execution")
elif toolsets.registration.register():
    unreal.log("Tether: toolsets registered")
else:
    unreal.log_warning("Tether: ToolsetRegistry unavailable; toolsets not registered")
beacon.start()
