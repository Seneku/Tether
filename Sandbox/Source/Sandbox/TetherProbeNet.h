#pragma once

#include "GameFramework/GameModeBase.h"
#include "GameFramework/PlayerController.h"
#include "TetherProbeNet.generated.h"

/**
 * Networking fixture for Tether's multiplayer checks: a console command that sends a Server RPC, so a test can tell
 * whether `ue exec --pie` on a client really reaches the server (it did not while commands ran from editor Python).
 */
UCLASS()
class ATetherProbeNetController : public APlayerController
{
	GENERATED_BODY()

public:
	/** Console: TetherPing <n>. Logs on the calling side, then asks the server to log back. */
	UFUNCTION(Exec)
	void TetherPing(int32 Nonce);

	/** Logs "TetherProbe: server got ping <n> from <controller>" with the world's net mode. */
	UFUNCTION(Server, Reliable)
	void ServerTetherPing(int32 Nonce);
};

/** Game mode that uses ATetherProbeNetController; PIE runs it with ?game=/Script/Sandbox.TetherProbeNetGameMode. */
UCLASS()
class ATetherProbeNetGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ATetherProbeNetGameMode();
};
