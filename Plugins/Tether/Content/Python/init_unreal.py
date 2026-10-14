"""Tether startup: register the toolset with Epic's ToolsetRegistry and start the editor beacon."""
import re

import unreal

# A game or server process started from the editor binary (`ue game`, -game/-server) runs Python startup scripts
# too, but has no editor to drive and no toolset registry: stay out of its way.
if re.search(r"(^|\s)-(game|server)\b", unreal.SystemLibrary.get_command_line() or "", re.I):
    unreal.log("Tether: not an editor session (-game/-server); Tether stays inactive")
else:
    from tether import beacon, toolsets

    if toolsets.registration is None:
        unreal.log("Tether: no ToolsetRegistry (UE < 5.8); tools are reachable through Python remote execution")
    elif toolsets.registration.register():
        unreal.log("Tether: toolsets registered")
    else:
        unreal.log_warning("Tether: ToolsetRegistry unavailable; toolsets not registered")
    beacon.start()
