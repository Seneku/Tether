#include "SandboxProbe.h"
#include "Misc/AutomationTest.h"

#if WITH_DEV_AUTOMATION_TESTS

// Small, fast tests that `ue test Sandbox.` runs in the live editor.

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FSandboxProbeValueTest, "Sandbox.Probe.Value",
	EAutomationTestFlags_ApplicationContextMask | EAutomationTestFlags::ProductFilter)

bool FSandboxProbeValueTest::RunTest(const FString& Parameters)
{
	TestEqual(TEXT("probe value"), USandboxProbeLibrary::GetProbeValue(), FString(TEXT("probe")));
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FSandboxProbeCountTest, "Sandbox.Probe.Count",
	EAutomationTestFlags_ApplicationContextMask | EAutomationTestFlags::ProductFilter)

bool FSandboxProbeCountTest::RunTest(const FString& Parameters)
{
	TestEqual(TEXT("probe count"), USandboxProbeLibrary::GetProbeCount(), 42);
	return true;
}

#endif
