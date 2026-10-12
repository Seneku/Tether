#include "TetherCSharpLibrary.h"

#include "Editor.h"
#include "HotReload/CSHotReloadSubsystem.h"

namespace
{
	UCSHotReloadSubsystem* FindHotReloadSubsystem()
	{
		// UnrealSharp does not export UCSHotReloadSubsystem::StaticClass(), so resolve the class by path.
		UClass* SubsystemClass = FindObject<UClass>(nullptr, TEXT("/Script/UnrealSharpEditor.CSHotReloadSubsystem"));
		if (!GEditor || !SubsystemClass)
		{
			return nullptr;
		}
		return static_cast<UCSHotReloadSubsystem*>(GEditor->GetEditorSubsystemBase(SubsystemClass));
	}
}

FString UTetherCSharpLibrary::ReloadCSharp()
{
	UCSHotReloadSubsystem* Subsystem = FindHotReloadSubsystem();
	if (!Subsystem)
	{
		return TEXT("unavailable");
	}
	if (GEditor->PlayWorld)
	{
		return TEXT("pie"); // UnrealSharp defers reloads until PIE ends
	}
	if (Subsystem->IsHotReloading())
	{
		return TEXT("busy");
	}
	if (!Subsystem->HasPendingHotReloadChanges())
	{
		return TEXT("nothing pending");
	}

	// FMessageDialog returns its default instead of blocking while this is set, and still logs the text.
	TGuardValue<bool> SuppressDialogs(GIsRunningUnattendedScript, true);
	Subsystem->PerformHotReload();

	// A successful reload clears the pending set; a failed or paused one leaves it in place.
	return Subsystem->HasPendingHotReloadChanges() ? TEXT("not reloaded") : TEXT("reloaded");
}

FString UTetherCSharpLibrary::GetCSharpReloadState()
{
	UCSHotReloadSubsystem* Subsystem = FindHotReloadSubsystem();
	if (!Subsystem)
	{
		return TEXT("unavailable");
	}
	if (Subsystem->IsHotReloading())
	{
		return TEXT("reloading");
	}
	return Subsystem->HasPendingHotReloadChanges() ? TEXT("pending") : TEXT("idle");
}
