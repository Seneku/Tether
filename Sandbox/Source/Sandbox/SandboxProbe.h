#pragma once

#include "Kismet/BlueprintFunctionLibrary.h"
#include "SandboxProbe.generated.h"

/** Tiny reflected surface used to measure Live Coding / restart behaviour from the ue CLI. */
UCLASS()
class USandboxProbeLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	UFUNCTION(BlueprintCallable, Category = "Sandbox")
	static FString GetProbeValue();

	UFUNCTION(BlueprintCallable, Category = "Sandbox")
	static int32 GetProbeCount();
};
