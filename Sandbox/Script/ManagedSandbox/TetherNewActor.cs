using UnrealSharp.Attributes;
using UnrealSharp.Engine;

namespace ManagedSandbox;

/// <summary>A class added while the editor runs, to test hot reload of brand-new types.</summary>
[UClass]
public partial class ATetherNewActor : AActor
{
    [UFunction(FunctionFlags.BlueprintCallable)]
    public string Hello()
    {
        return "new class, no restart";
    }
}
