#pragma once

#include "Blueprint/UserWidget.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "TetherProbeUI.generated.h"

class UTextBlock;

/** UMG half of the game UI fixture: a text, a button that counts its clicks, and a status line. */
UCLASS()
class UTetherProbeWidget : public UUserWidget
{
	GENERATED_BODY()

protected:
	virtual void NativeOnInitialized() override;

private:
	UFUNCTION()
	void HandleClicked();

	UPROPERTY()
	TObjectPtr<UTextBlock> Status;
};

/** Game UI fixture for `ue ui` and `ue pie do click`: a raw Slate panel and a UMG widget on the PIE viewport. */
UCLASS()
class UTetherProbeUILibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/** Adds the probe UI to the game viewport (replacing an earlier one). */
	UFUNCTION(BlueprintCallable, Category = "Sandbox", meta = (WorldContext = "WorldContextObject"))
	static bool ShowProbeUI(UObject* WorldContextObject);

	/** Clicks on either probe button since the editor started. */
	UFUNCTION(BlueprintCallable, Category = "Sandbox")
	static int32 GetProbeUIClicks();
};
