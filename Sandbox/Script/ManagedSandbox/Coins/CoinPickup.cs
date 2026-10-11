using UnrealSharp.Attributes;
using UnrealSharp.CoreUObject;
using UnrealSharp.Engine;

namespace ManagedSandbox.Coins;

/// <summary>A spinning coin that scores when a pawn touches it, then respawns after a delay.</summary>
[UClass]
public partial class ACoinPickup : AActor
{
    public ACoinPickup()
    {
        Value = 10;
        RespawnSeconds = 3.0f;
    }

    [UProperty(DefaultComponent = true, RootComponent = true)]
    public partial USphereComponent Collision { get; set; }

    [UProperty(DefaultComponent = true, AttachmentComponent = nameof(Collision))]
    public partial UStaticMeshComponent Mesh { get; set; }

    [UProperty(DefaultComponent = true)]
    public partial URotatingMovementComponent Spinner { get; set; }

    [UProperty(PropertyFlags.EditAnywhere | PropertyFlags.BlueprintReadOnly)]
    public partial int Value { get; set; }

    [UProperty(PropertyFlags.EditAnywhere | PropertyFlags.BlueprintReadOnly)]
    public partial float RespawnSeconds { get; set; }

    [UProperty(PropertyFlags.EditAnywhere)]
    public partial UStaticMesh? CoinMesh { get; set; }

    /// <summary>Stable id for logs and tests (set by the placement script); falls back to the object name.</summary>
    [UProperty(PropertyFlags.EditAnywhere | PropertyFlags.BlueprintReadOnly)]
    public partial string CoinId { get; set; }

    [UProperty(PropertyFlags.BlueprintReadOnly)]
    public partial bool bCollected { get; set; }

    private string DisplayId => string.IsNullOrEmpty(CoinId) ? Name.ToString() : CoinId;

    public override void BeginPlay()
    {
        Collision.SetSphereRadius(60.0f);
        Collision.SetCollisionProfileName("OverlapAllDynamic");
        Mesh.SetCollisionProfileName("NoCollision");
        Spinner.RotationRate = new FRotator(0, 180, 0);
        if (CoinMesh is not null)
        {
            Mesh.SetStaticMesh(CoinMesh);
        }
        base.BeginPlay();
    }

    public override void ActorBeginOverlap(AActor otherActor)
    {
        if (bCollected || otherActor is not APawn)
        {
            return;
        }

        UCoinScoreSubsystem? score = GetWorldSubsystem<UCoinScoreSubsystem>();
        if (score is null)
        {
            LogCoins.LogWarning($"{DisplayId}: no score subsystem in this world; pickup ignored");
            return;
        }

        bCollected = true;
        ActorHiddenInGame = true;
        ActorEnableCollision = false;
        score.AddScore(Value, DisplayId);
        SystemLibrary.SetTimer(Respawn, RespawnSeconds, false);
    }

    [UFunction]
    public void Respawn()
    {
        // Re-enabling collision under a standing player would re-collect at once (an endless
        // score farm), so wait until the spot is clear.
        if (IsPlayerOnSpot())
        {
            SystemLibrary.SetTimer(Respawn, 0.5f, false);
            return;
        }

        LogCoins.Log($"{DisplayId} respawned");
        bCollected = false;
        ActorHiddenInGame = false;
        ActorEnableCollision = true;
    }

    private bool IsPlayerOnSpot()
    {
        APawn? player = UGameplayStatics.GetPlayerPawn(0);
        return player is not null && FVector.Distance(player.ActorLocation, ActorLocation) < Collision.ScaledSphereRadius + 50.0;
    }
}
