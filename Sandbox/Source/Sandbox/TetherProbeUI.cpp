#include "TetherProbeUI.h"

#include "Blueprint/WidgetTree.h"
#include "Components/Border.h"
#include "Components/Button.h"
#include "Components/Overlay.h"
#include "Components/OverlaySlot.h"
#include "Components/TextBlock.h"
#include "Components/VerticalBox.h"
#include "GameFramework/PlayerController.h"
#include "InputCoreTypes.h"
#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "Kismet/GameplayStatics.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SEditableTextBox.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/Text/STextBlock.h"

static int32 GProbeUIClicks = 0;
static TWeakPtr<SWidget> GProbePanel;
static TWeakObjectPtr<UUserWidget> GProbeWidget;

void UTetherProbeWidget::NativeOnInitialized()
{
	Super::NativeOnInitialized();
	UVerticalBox* Box = WidgetTree->ConstructWidget<UVerticalBox>(UVerticalBox::StaticClass(), TEXT("ProbeBox"));
	UTextBlock* Title = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("ProbeTitle"));
	Title->SetText(FText::FromString(TEXT("UMG probe widget")));
	UButton* Button = WidgetTree->ConstructWidget<UButton>(UButton::StaticClass(), TEXT("ProbeButton"));
	UTextBlock* Label = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("ProbeButtonLabel"));
	Label->SetText(FText::FromString(TEXT("UMG Probe Button")));
	Button->AddChild(Label);
	Button->OnClicked.AddDynamic(this, &UTetherProbeWidget::HandleClicked);
	Status = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("ProbeStatus"));
	Status->SetText(FText::FromString(TEXT("UMG clicks: 0")));
	Box->AddChild(Title);
	Box->AddChild(Button);
	Box->AddChild(Status);
	WidgetTree->RootWidget = Box;
}

void UTetherProbeWidget::HandleClicked()
{
	++GProbeUIClicks;
	Status->SetText(FText::FromString(FString::Printf(TEXT("UMG clicks: %d"), GProbeUIClicks)));
	UE_LOG(LogTemp, Display, TEXT("TetherProbeUI: UMG button clicked"));
}

void UTetherProbeMenuRow::NativeOnInitialized()
{
	Super::NativeOnInitialized();
	UOverlay* Row = WidgetTree->ConstructWidget<UOverlay>(UOverlay::StaticClass(), TEXT("RowOverlay"));
	UBorder* Background = WidgetTree->ConstructWidget<UBorder>(UBorder::StaticClass(), TEXT("RowBackground"));
	Background->SetBrushColor(FLinearColor(0.05f, 0.05f, 0.1f, 0.8f));
	RowText = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("RowLabel"));
	RowButton = WidgetTree->ConstructWidget<UTetherProbeMenuButton>(UTetherProbeMenuButton::StaticClass(), TEXT("RowButton"));
	RowButton->SetBackgroundColor(FLinearColor(1.f, 1.f, 1.f, 0.f));
	RowButton->OnClicked.AddDynamic(this, &UTetherProbeMenuRow::HandleClicked);
	Row->AddChild(Background);
	Row->AddChild(RowText);
	Row->AddChild(RowButton);
	for (UPanelSlot* RowSlot : Row->GetSlots())
	{
		if (UOverlaySlot* OverlaySlot = Cast<UOverlaySlot>(RowSlot))
		{
			OverlaySlot->SetHorizontalAlignment(HAlign_Fill);
			OverlaySlot->SetVerticalAlignment(VAlign_Fill);
		}
	}
	WidgetTree->RootWidget = Row;
}

void UTetherProbeMenuRow::NativeConstruct()
{
	Super::NativeConstruct();
	RowText->SetText(FText::FromString(Label));  // the label is set after CreateWidget has initialized the row
}

void UTetherProbeMenuRow::HandleClicked()
{
	++GProbeUIClicks;
	UE_LOG(LogTemp, Display, TEXT("TetherProbeMenu: %s clicked"), *Label);
}

void UTetherProbeMenu::NativeOnInitialized()
{
	Super::NativeOnInitialized();
	UVerticalBox* Column = WidgetTree->ConstructWidget<UVerticalBox>(UVerticalBox::StaticClass(), TEXT("MenuColumn"));
	for (const TCHAR* Name : {TEXT("GARAGE"), TEXT("FREE ROAM"), TEXT("QUIT")})
	{
		UTetherProbeMenuRow* Row = CreateWidget<UTetherProbeMenuRow>(this, UTetherProbeMenuRow::StaticClass());
		Row->Label = Name;
		Column->AddChild(Row);
	}
	WidgetTree->RootWidget = Column;
}

void UTetherProbeMenu::NativeConstruct()
{
	Super::NativeConstruct();
	TArray<UWidget*> All;
	WidgetTree->GetAllWidgets(All);
	for (UWidget* Widget : All)
	{
		UTetherProbeMenuRow* Row = Cast<UTetherProbeMenuRow>(Widget);
		if (Row && Row->RowButton)
		{
			Row->RowButton->SetKeyboardFocus();  // the first row, as menus focus their first entry
			break;
		}
	}
}

void UTetherProbeTitle::NativeOnInitialized()
{
	Super::NativeOnInitialized();
	SetIsFocusable(true);
	// Two lines, so the inspector does not fold the widget into one text: it then leaves the focused user widget out
	UVerticalBox* Column = WidgetTree->ConstructWidget<UVerticalBox>(UVerticalBox::StaticClass(), TEXT("TitleColumn"));
	UTextBlock* Name = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("TitleName"));
	Name->SetText(FText::FromString(TEXT("TETHER PROBE")));
	UTextBlock* Text = WidgetTree->ConstructWidget<UTextBlock>(UTextBlock::StaticClass(), TEXT("TitlePrompt"));
	Text->SetText(FText::FromString(TEXT("PRESS ENTER")));
	Column->AddChild(Name);
	Column->AddChild(Text);
	WidgetTree->RootWidget = Column;
}

void UTetherProbeTitle::NativeConstruct()
{
	Super::NativeConstruct();
	SetKeyboardFocus();
}

FReply UTetherProbeTitle::NativeOnKeyDown(const FGeometry& Geometry, const FKeyEvent& KeyEvent)
{
	if (KeyEvent.GetKey() == EKeys::Enter)
	{
		UE_LOG(LogTemp, Display, TEXT("TetherProbeMenu: title dismissed"));
		APlayerController* Controller = GetOwningPlayer();
		RemoveFromParent();
		if (UTetherProbeMenu* Menu = CreateWidget<UTetherProbeMenu>(Controller, UTetherProbeMenu::StaticClass(), TEXT("TetherProbeMenu")))
		{
			Menu->AddToViewport(30);
		}
		return FReply::Handled();
	}
	return Super::NativeOnKeyDown(Geometry, KeyEvent);
}

bool UTetherProbeUILibrary::ShowProbeMenu(UObject* WorldContextObject)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::LogAndReturnNull) : nullptr;
	APlayerController* Controller = World ? UGameplayStatics::GetPlayerController(World, 0) : nullptr;
	if (!Controller)
	{
		return false;
	}
	UTetherProbeTitle* Title = CreateWidget<UTetherProbeTitle>(Controller, UTetherProbeTitle::StaticClass(), TEXT("TetherProbeTitle"));
	if (!Title)
	{
		return false;
	}
	Title->AddToViewport(30);
	FInputModeUIOnly Mode;
	Mode.SetWidgetToFocus(Title->TakeWidget());
	Controller->SetInputMode(Mode);
	return true;
}

bool UTetherProbeUILibrary::ShowProbeUI(UObject* WorldContextObject)
{
	UWorld* World = GEngine ? GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::LogAndReturnNull) : nullptr;
	UGameViewportClient* Viewport = World ? World->GetGameViewport() : nullptr;
	if (!Viewport)
	{
		return false;
	}
	if (TSharedPtr<SWidget> Old = GProbePanel.Pin())
	{
		Viewport->RemoveViewportWidgetContent(Old.ToSharedRef());
	}
	if (GProbeWidget.IsValid())
	{
		GProbeWidget->RemoveFromParent();
	}

	TSharedRef<SWidget> Panel = SNew(SBox)
		.HAlign(HAlign_Right)
		.VAlign(VAlign_Top)
		.Padding(40.f)
		[
			SNew(SVerticalBox)
			+ SVerticalBox::Slot().AutoHeight()
			[
				SNew(STextBlock).Text(FText::FromString(TEXT("Tether probe panel")))
			]
			+ SVerticalBox::Slot().AutoHeight()
			[
				SNew(SButton)
				.Text(FText::FromString(TEXT("Slate Probe Button")))
				.OnClicked_Lambda([]()
				{
					++GProbeUIClicks;
					UE_LOG(LogTemp, Display, TEXT("TetherProbeUI: Slate button clicked"));
					return FReply::Handled();
				})
			]
			+ SVerticalBox::Slot().AutoHeight()
			[
				SNew(SEditableTextBox).HintText(FText::FromString(TEXT("Probe input")))
			]
		];
	Viewport->AddViewportWidgetContent(Panel, 10);
	GProbePanel = Panel;

	if (APlayerController* Controller = UGameplayStatics::GetPlayerController(World, 0))
	{
		if (UUserWidget* Widget = CreateWidget<UTetherProbeWidget>(Controller, UTetherProbeWidget::StaticClass(), TEXT("TetherProbeWidget")))
		{
			// Top-left and its own size, so it does not cover the Slate panel (clicks land on what is on top).
			Widget->SetAnchorsInViewport(FAnchors(0.f, 0.f));
			Widget->SetAlignmentInViewport(FVector2D(0.f, 0.f));
			Widget->SetDesiredSizeInViewport(FVector2D(260.f, 120.f));
			Widget->SetPositionInViewport(FVector2D(40.f, 40.f), false);
			Widget->AddToViewport(20);
			GProbeWidget = Widget;
		}
	}
	return true;
}

int32 UTetherProbeUILibrary::GetProbeUIClicks()
{
	return GProbeUIClicks;
}
