"""Place a line of C# coin pickups ahead of the PlayerStart. Idempotent (re-run replaces them).

Run in the live editor:  ue py -f Sandbox/Scripts/place_coins.py
"""
import unreal

COUNT, SPACING, HEIGHT, TAG = 5, 250.0, 120.0, "coin_run"

actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
coin_class = unreal.load_class(None, "/Script/UnrealSharp.CoinPickup_C")
sphere = unreal.load_asset("/Engine/BasicShapes/Sphere")
start = next(a for a in actors.get_all_level_actors() if isinstance(a, unreal.PlayerStart))
origin, forward = start.get_actor_location(), start.get_actor_forward_vector()

with unreal.ScopedEditorTransaction("Agent: place coin run"):
    for old in [a for a in actors.get_all_level_actors() if a.actor_has_tag(TAG)]:
        actors.destroy_actor(old)
    for i in range(COUNT):
        loc = origin + forward * (SPACING * (i + 1))
        loc.z = origin.z + HEIGHT - 90.0
        coin = actors.spawn_actor_from_class(coin_class, loc, unreal.Rotator(pitch=0, yaw=0, roll=0))
        coin.set_actor_label(f"Coin_{i + 1}")
        coin.set_editor_property("CoinId", f"Coin_{i + 1}")
        coin.tags = [TAG]
        coin.set_actor_scale3d(unreal.Vector(0.4, 0.4, 0.4))
        coin.set_editor_property("CoinMesh", sphere)
        coin.set_editor_property("Value", 10)

placed = [a for a in actors.get_all_level_actors() if a.actor_has_tag(TAG)]
print(f"placed {len(placed)} coins from {origin} along {forward}")
