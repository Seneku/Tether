"""ToolSuite startup: register the toolset with Epic's ToolsetRegistry and start the editor beacon."""
import unreal

from toolsuite import beacon, toolsets

if toolsets.registration.register():
    unreal.log("ToolSuite: toolsets registered")
else:
    unreal.log_warning("ToolSuite: ToolsetRegistry unavailable; toolsets not registered")
beacon.start()
