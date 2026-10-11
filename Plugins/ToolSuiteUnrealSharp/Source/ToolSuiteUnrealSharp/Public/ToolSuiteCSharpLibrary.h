#pragma once

#include "Kismet/BlueprintFunctionLibrary.h"
#include "ToolSuiteCSharpLibrary.generated.h"

/**
 * Agent-facing control over UnrealSharp C# hot reload, called from the ToolSuite Python toolset.
 * UnrealSharp exposes reload only to C++ and reports compile failures through a modal dialog;
 * these wrappers make reload callable from Python and keep the editor responsive when it fails.
 */
UCLASS()
class UToolSuiteCSharpLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Reloads pending C# changes now, with modal dialogs suppressed. A compile failure is logged as
	 * "Message dialog closed, result: Ok, title: C# Compilation Failed, text: <compiler output>".
	 * @return "reloaded", "not reloaded" (failed, or paused during the initial C# load),
	 *         "nothing pending", "busy", "pie" or "unavailable".
	 */
	UFUNCTION(BlueprintCallable, Category = "ToolSuite|CSharp", meta = (ScriptName = "reload_csharp"))
	static FString ReloadCSharp();

	/** @return "idle", "pending", "reloading" or "unavailable". */
	UFUNCTION(BlueprintCallable, Category = "ToolSuite|CSharp", meta = (ScriptName = "csharp_reload_state"))
	static FString GetCSharpReloadState();
};
