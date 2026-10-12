r"""PIE smoke test for the coin feature: walk the player pawn through every coin, report score.

As a pass/fail check (from Sandbox/, after `ue py -f Scripts/place_coins.py`):
    ue pie run Scripts/coin_smoke.py --expect "Coin run started" --expect "score \d+, 5 coins" \
        --reject "no score subsystem"
Or by hand while PIE runs:  ue py -f Scripts/coin_smoke.py
"""
import unreal

world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()
assert world, "PIE is not running (ue pie start)"
pawn = unreal.GameplayStatics.get_player_pawn(world, 0)
coin_class = unreal.load_class(None, "/Script/UnrealSharp.CoinPickup_C")
coins = sorted(unreal.GameplayStatics.get_all_actors_of_class(world, coin_class), key=lambda c: c.get_actor_label())

for coin in coins:
    pawn.set_actor_location(coin.get_actor_location(), sweep=False, teleport=True)

# Score is reported by the C# subsystem itself: ue log --cat LogCoins
collected = sum(1 for c in coins if c.get_editor_property("bCollected"))
print(f"pawn {pawn.get_class().get_name()} visited {len(coins)} coins; collected {collected}")
