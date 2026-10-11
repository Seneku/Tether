using UnrealSharp;
using UnrealSharp.Attributes;
using UnrealSharp.Engine;
using UnrealSharp.UnrealSharpCore;

namespace ManagedSandbox.Coins;

[UMultiDelegate]
public delegate void FOnCoinScoreChanged(int newScore, int delta);

/// <summary>
/// Per-world score for collected coins. Pickups within <see cref="ComboWindowSeconds"/> of each
/// other raise a combo multiplier (capped at <see cref="MaxCombo"/>). Lives in game and PIE worlds.
/// </summary>
[UClass]
public partial class UCoinScoreSubsystem : UCSWorldSubsystem
{
    public const double ComboWindowSeconds = 1.5;
    public const int MaxCombo = 5;

    private double lastPickupTime = double.NegativeInfinity;

    [UProperty(PropertyFlags.BlueprintReadOnly)]
    public partial int Score { get; set; }

    [UProperty(PropertyFlags.BlueprintReadOnly)]
    public partial int CoinsCollected { get; set; }

    [UProperty(PropertyFlags.BlueprintReadOnly)]
    public partial int ComboMultiplier { get; set; }

    [UProperty(PropertyFlags.BlueprintAssignable)]
    public partial TMulticastDelegate<FOnCoinScoreChanged> OnScoreChanged { get; set; }

    public override bool DoesSupportWorldType(ECSWorldType worldType)
    {
        return worldType == ECSWorldType.Game || worldType == ECSWorldType.PIE;
    }

    public override void OnWorldBeginPlay()
    {
        Score = 0;
        CoinsCollected = 0;
        ComboMultiplier = 0;
        lastPickupTime = double.NegativeInfinity;
        LogCoins.Log("Coin run started");
    }

    /// <returns>The points awarded after the combo multiplier.</returns>
    [UFunction(FunctionFlags.BlueprintCallable)]
    public int AddScore(int baseValue, string sourceId)
    {
        double now = UGameplayStatics.TimeSeconds;
        ComboMultiplier = now - lastPickupTime <= ComboWindowSeconds ? Math.Min(ComboMultiplier + 1, MaxCombo) : 1;
        lastPickupTime = now;

        int points = baseValue * ComboMultiplier;
        Score += points;
        CoinsCollected++;
        LogCoins.Log($"{sourceId}: +{points} (x{ComboMultiplier}) -> score {Score}, {CoinsCollected} coins");
        OnScoreChanged.Invoke(Score, points);
        return points;
    }
}
