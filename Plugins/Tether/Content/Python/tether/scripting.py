"""Helpers for scripts run in PIE (`ue pie run script.py`, `ue py`): `import tether` and use tether.world() and
friends. They follow `ue pie target <n>` in multiplayer PIE, where unreal's get_game_world() always returns world 0.
"""
import unreal


def world():
    """The PIE world Tether's tools act on (`ue pie target`), else the first PIE world, else the editor world."""
    from tether import toolsets
    game = toolsets._game_world()
    return game if game is not None else unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()


def world_subsystem(cls, pie=None):
    """A UWorldSubsystem of a world, to call its UFUNCTIONs: 5.8's Python has no SubsystemBlueprintLibrary. pie=None
    takes the world above (PIE when it runs), False the editor world, True the PIE world. None if it has none."""
    if pie is False:
        target = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    else:
        target = world()
        if pie and target == unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world():
            raise RuntimeError("PIE is not running")
    cls = getattr(unreal, cls) if isinstance(cls, str) else cls
    return next((o for o in unreal.ObjectIterator(cls) if o.get_outer() == target), None)


def controller(index=0):
    """That world's player controller (index for split-screen players)."""
    return unreal.GameplayStatics.get_player_controller(world(), index)


def player(index=0):
    """That world's player pawn."""
    return unreal.GameplayStatics.get_player_pawn(world(), index)


def actors(cls=None):
    """Every actor of a class (default: all) in that world."""
    return list(unreal.GameplayStatics.get_all_actors_of_class(world(), cls or unreal.Actor))


def console(command):
    """Run a console command in that world as its player typed it. In multiplayer PIE it runs on the world's next
    tick, because a command run straight from Python makes every RPC local (a client's Server RPC would never
    reach the server); its output reaches the log a frame later."""
    from tether import toolsets
    game = world()
    if toolsets._networked(game):
        toolsets._queue_command(game, command)
    else:
        unreal.SystemLibrary.execute_console_command(game, command)
