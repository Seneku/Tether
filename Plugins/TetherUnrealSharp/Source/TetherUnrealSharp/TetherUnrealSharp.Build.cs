using UnrealBuildTool;

public class TetherUnrealSharp : ModuleRules
{
	public TetherUnrealSharp(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine" });
		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"UnrealEd",
			"EditorSubsystem",
			"DirectoryWatcher",
			"UnrealSharpCore",
			"UnrealSharpEditor",
		});
	}
}
