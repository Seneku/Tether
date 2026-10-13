#include "TetherProbeTerrain.h"

#include "Landscape.h"
#include "LandscapeLayerInfoObject.h"
#include "LandscapeProxy.h"

#if WITH_EDITOR
#include "AssetRegistry/AssetRegistryModule.h"
#include "Editor.h"
#include "Misc/PackageName.h"

static ULandscapeLayerInfoObject* MakeLayerInfo(const FString& Folder, FName Name)
{
	const FString PackageName = Folder / FString::Printf(TEXT("LI_%s"), *Name.ToString());
	UPackage* Package = CreatePackage(*PackageName);
	ULandscapeLayerInfoObject* Info = NewObject<ULandscapeLayerInfoObject>(Package, *FPackageName::GetShortName(PackageName),
		RF_Public | RF_Standalone | RF_Transactional);
	Info->SetLayerName(Name, false);
	FAssetRegistryModule::AssetCreated(Info);
	Package->MarkPackageDirty();
	return Info;
}
#endif

ALandscape* UTetherProbeTerrainLibrary::CreateTestLandscape(int32 ComponentsPerSide, float HillHeight,
	const FString& LayerFolder, FName FirstLayerName, FName SecondLayerName)
{
#if WITH_EDITOR
	ULandscapeLayerInfoObject* FirstLayer = FirstLayerName.IsNone() ? nullptr : MakeLayerInfo(LayerFolder, FirstLayerName);
	ULandscapeLayerInfoObject* SecondLayer = SecondLayerName.IsNone() ? nullptr : MakeLayerInfo(LayerFolder, SecondLayerName);
	UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
	if (!World || ComponentsPerSide < 1)
	{
		return nullptr;
	}
	constexpr int32 QuadsPerSection = 63;
	const int32 Verts = ComponentsPerSide * QuadsPerSection + 1;
	const double Scale = 100.0; // 1 m per quad
	const double HalfSize = (Verts - 1) * Scale * 0.5;

	// Heights are 16-bit around 32768 = z 0; with a z scale of 100, one unit is 1/128 cm.
	TArray<uint16> Heights;
	Heights.SetNumUninitialized(Verts * Verts);
	for (int32 Y = 0; Y < Verts; ++Y)
	{
		for (int32 X = 0; X < Verts; ++X)
		{
			const double U = double(X) / (Verts - 1), V = double(Y) / (Verts - 1);
			const double Z = HillHeight * (0.5 * FMath::Sin(U * 2 * PI) * FMath::Cos(V * 2 * PI) + 0.5 * U);
			Heights[Y * Verts + X] = uint16(FMath::Clamp(32768.0 + Z * 128.0 / Scale * (Scale / 100.0), 0.0, 65535.0));
		}
	}
	TMap<FGuid, TArray<uint16>> HeightData;
	HeightData.Add(FGuid(), MoveTemp(Heights));

	TArray<FLandscapeImportLayerInfo> Layers;
	if (FirstLayer)
	{
		FLandscapeImportLayerInfo Layer(FirstLayer->GetLayerName());
		Layer.LayerInfo = FirstLayer;
		Layer.LayerData.Init(255, Verts * Verts);
		Layers.Add(MoveTemp(Layer));
	}
	if (SecondLayer)
	{
		FLandscapeImportLayerInfo Layer(SecondLayer->GetLayerName());
		Layer.LayerInfo = SecondLayer;
		Layer.LayerData.Init(0, Verts * Verts);
		Layers.Add(MoveTemp(Layer));
	}
	TMap<FGuid, TArray<FLandscapeImportLayerInfo>> LayerData;
	LayerData.Add(FGuid(), MoveTemp(Layers));

	FActorSpawnParameters Params;
	Params.Name = MakeUniqueObjectName(World->PersistentLevel, ALandscape::StaticClass(), TEXT("TetherTerrain"));
	ALandscape* Landscape = World->SpawnActor<ALandscape>(FVector(-HalfSize, -HalfSize, 0.0), FRotator::ZeroRotator, Params);
	if (!Landscape)
	{
		return nullptr;
	}
	Landscape->SetActorRelativeScale3D(FVector(Scale, Scale, Scale));
	Landscape->Import(FGuid::NewGuid(), 0, 0, Verts - 1, Verts - 1, 1, QuadsPerSection, HeightData, nullptr, LayerData,
		ELandscapeImportAlphamapType::Additive, TArrayView<const FLandscapeLayer>());
	Landscape->SetActorLabel(TEXT("TetherTerrain"));
	return Landscape;
#else
	return nullptr;
#endif
}
