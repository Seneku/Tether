using UnrealBuildTool;

public class ToolSuiteUnrealSharp : ModuleRules
{
	public ToolSuiteUnrealSharp(ReadOnlyTargetRules Target) : base(Target)
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
