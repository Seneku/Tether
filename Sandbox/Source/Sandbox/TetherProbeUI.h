#pragma once

#include "Blueprint/UserWidget.h"
#include "Components/Button.h"
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

/** A menu button as C++-built menus make them: a UButton subclass with no child, laid transparent over a text row. */
UCLASS()
class UTetherProbeMenuButton : public UButton
{
	GENERATED_BODY()
};

/** One menu row: Overlay[background, label text, transparent button on top]. */
UCLASS()
class UTetherProbeMenuRow : public UUserWidget
{
	GENERATED_BODY()

public:
	FString Label;

	UPROPERTY()
	TObjectPtr<UTetherProbeMenuButton> RowButton;

	UPROPERTY()
	TObjectPtr<UTextBlock> RowText;

protected:
	virtual void NativeOnInitialized() override;
	virtual void NativeConstruct() override;

private:
	UFUNCTION()
	void HandleClicked();
};

/** The menu screen: a column of rows (user widgets inside a user widget). */
UCLASS()
class UTetherProbeMenu : public UUserWidget
{
	GENERATED_BODY()

protected:
	virtual void NativeOnInitialized() override;
	virtual void NativeConstruct() override;
};

/** A title screen that takes keyboard focus itself (focusable, no buttons) and reads Enter in NativeOnKeyDown. */
UCLASS()
class UTetherProbeTitle : public UUserWidget
{
	GENERATED_BODY()

protected:
	virtual void NativeOnInitialized() override;
	virtual void NativeConstruct() override;
	virtual FReply NativeOnKeyDown(const FGeometry& Geometry, const FKeyEvent& KeyEvent) override;
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

	/** A front end as hand-built UMG menus make it: a title screen holding keyboard focus (Enter goes on to the
	 *  menu), then rows of transparent buttons over text, inside nested user widgets. Logs what was pressed. */
	UFUNCTION(BlueprintCallable, Category = "Sandbox", meta = (WorldContext = "WorldContextObject"))
	static bool ShowProbeMenu(UObject* WorldContextObject);

	/** Clicks on either probe button since the editor started. */
	UFUNCTION(BlueprintCallable, Category = "Sandbox")
	static int32 GetProbeUIClicks();
};
