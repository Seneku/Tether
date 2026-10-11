#include "SandboxProbe.h"

FString USandboxProbeLibrary::GetProbeValue()
{
	return TEXT("probe");
}

int32 USandboxProbeLibrary::GetProbeCount()
{
	return 42;
}
