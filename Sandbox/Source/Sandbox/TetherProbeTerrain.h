#pragma once

#include "Kismet/BlueprintFunctionLibrary.h"
#include "TetherProbeTerrain.generated.h"

class ALandscape;
class ULandscapeLayerInfoObject;

/** Terrain fixture for `ue landscape` and `ue foliage`: Python cannot create a landscape, so this does. */
UCLASS()
class UTetherProbeTerrainLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Spawn a small landscape of rolling hills (63-quad components, 1 m per quad) centred on the origin of the
	 * editor world, with two paint layers whose layer info assets (LI_<name>) are created in LayerFolder:
	 * FirstLayer starts fully painted, SecondLayer empty. Editor only; returns null elsewhere.
	 */
	UFUNCTION(BlueprintCallable, Category = "Tether|Fixtures")
	static ALandscape* CreateTestLandscape(int32 ComponentsPerSide, float HillHeight, const FString& LayerFolder,
		FName FirstLayer, FName SecondLayer);
};
