#include "TetherProbeNet.h"

namespace
{
	const TCHAR* NetModeName(const UWorld* World)
	{
		switch (World ? World->GetNetMode() : NM_Standalone)
		{
		case NM_DedicatedServer: return TEXT("dedicated server");
		case NM_ListenServer: return TEXT("listen server");
		case NM_Client: return TEXT("client");
		default: return TEXT("standalone");
		}
	}
}

void ATetherProbeNetController::TetherPing(int32 Nonce)
{
	UE_LOG(LogTemp, Display, TEXT("TetherProbe: ping %d sent from %s on the %s"), Nonce, *GetName(), NetModeName(GetWorld()));
	++PingsSent;
	ServerTetherPing(Nonce);
}

void ATetherProbeNetController::ServerTetherPing_Implementation(int32 Nonce)
{
	UE_LOG(LogTemp, Display, TEXT("TetherProbe: server got ping %d from %s on the %s"), Nonce, *GetName(), NetModeName(GetWorld()));
}

ATetherProbeNetGameMode::ATetherProbeNetGameMode()
{
	PlayerControllerClass = ATetherProbeNetController::StaticClass();
}
