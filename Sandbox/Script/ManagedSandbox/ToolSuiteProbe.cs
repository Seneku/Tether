using UnrealSharp.Attributes;
using UnrealSharp.Engine;

namespace ManagedSandbox;

/// <summary>Tiny reflected surface used to measure UnrealSharp hot reload from the ue CLI.</summary>
[UClass]
public partial class AToolSuiteProbe : AActor
{
    public AToolSuiteProbe()
    {
        ProbeLabel = "added by reload";
    }

    [UProperty(PropertyFlags.EditDefaultsOnly)]
    public partial int ProbeCount { get; set; }

    [UProperty(PropertyFlags.EditDefaultsOnly)]
    public partial string ProbeLabel { get; set; }

    [UFunction(FunctionFlags.BlueprintCallable)]
    public string GetProbeValue()
    {
        return "cs v4 after fix";
    }

    [UFunction(FunctionFlags.BlueprintCallable)]
    public int AddNumbers(int a, int b)
    {
        return a + b;
    }
}
