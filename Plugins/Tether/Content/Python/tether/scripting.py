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


def spawn(what, location=(0, 0, 0), rotation=(0, 0, 0), label=None):
    """An actor spawned in the PIE world, as `ue spawn --pie` does: a class (unreal.StaticMeshActor,
    '/Script/Module.Class') or an asset (a StaticMesh, a Blueprint), through the cheat manager's `summon` (Python
    cannot spawn into a game world), then placed. rotation is (pitch, yaw, roll). Returns the actor; not in
    multiplayer PIE, where the summon runs a tick later."""
    from tether import toolsets
    game = world()
    if toolsets._networked(game):
        raise RuntimeError("tether.spawn: in multiplayer PIE the summon runs a tick later; use `ue spawn --pie`")
    mesh = None
    if isinstance(what, str) and what.startswith("/") and not what.startswith("/Script/"):
        asset = unreal.load_asset(what)
        if isinstance(asset, unreal.StaticMesh):
            cls, mesh = unreal.StaticMeshActor.static_class(), asset
        elif isinstance(asset, unreal.Blueprint):
            cls = asset.generated_class()
        else:
            raise RuntimeError(f"tether.spawn: {what} is not a StaticMesh, a Blueprint or a class")
    elif isinstance(what, str):
        cls = unreal.load_class(None, what) if what.startswith("/Script/") else getattr(unreal, what, None)
    else:
        cls = what
    if isinstance(cls, type):
        cls = cls.static_class()
    if cls is None:
        raise RuntimeError(f"tether.spawn: no class '{what}'")
    before = {a.get_name() for a in unreal.GameplayStatics.get_all_actors_of_class(game, cls)}
    unreal.SystemLibrary.execute_console_command(game, f"summon {cls.get_path_name()}")
    new = [a for a in unreal.GameplayStatics.get_all_actors_of_class(game, cls) if a.get_name() not in before]
    if not new:
        raise RuntimeError(f"tether.spawn: summon {cls.get_path_name()} spawned nothing (the player needs a cheat manager)")
    actor = new[0]
    if mesh is not None:
        actor.static_mesh_component.set_mobility(unreal.ComponentMobility.MOVABLE)
        actor.static_mesh_component.set_static_mesh(mesh)
    loc = location if isinstance(location, unreal.Vector) else unreal.Vector(*location)
    rot = rotation if isinstance(rotation, unreal.Rotator) else \
        unreal.Rotator(pitch=rotation[0], yaw=rotation[1], roll=rotation[2])
    actor.set_actor_location_and_rotation(loc, rot, False, True)
    if label:
        actor.set_actor_label(label)
    return actor


def rerun(actor):
    """Rerun an editor-world actor's construction script (5.8's Python has no rerun_construction_scripts): a
    property change notification on the actor makes the engine rerun it, as editing any property in the Details
    panel does. For a Blueprint whose construction script applies a variable (Ultra Dynamic Sky's time of day)."""
    actor.modify()
    actor.set_editor_property("tags", actor.get_editor_property("tags"), unreal.PropertyAccessChangeNotifyMode.ALWAYS)
    return actor


def finish(value=None):
    """End a `ue pie run --wait` script that runs on over ticks (a tick callback driving a test): the CLI waits for
    this call and prints `value` (JSON-able) as the result."""
    import sys
    sys._tether_run_result = {"done": True, "value": value}


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
