# Game systems and shipping: multiplayer, AI, navigation, animation, audio, saves, packaging

## Multiplayer

- Multiplayer: `ue pie start --clients 2` (listen server; `--net client` for a dedicated one), `ue pie status`
  for the worlds, `ue pie target 1` to make `--pie` reads, `pie do` and `ai` act on client 1 (0 = server).
  Compare a value on the server and a client to check replication.
- Multiplayer: `ue pie start --clients 2` (add `--game /Script/MyGame.MyGameMode` to try another game mode), then
  `ue pie target 1` to act on the first client. `ue exec --pie` and `pie do` exec steps run on that world's next
  tick, so RPCs replicate; a `call` step or your own Python calling a function directly runs any RPC locally
  (editor Python makes every RPC local). `ue pie run` scripts run in a fresh namespace each time: `tether.world()`, `player()`,
  `console("cmd")` follow the targeted world. A UWorldSubsystem (5.8's Python has no SubsystemBlueprintLibrary):
  `tether.world_subsystem(unreal.MySubsystem)` in `ue py` (PIE's while it runs; `pie=False` for the editor's). `ue net MyCharacter` lists replicated properties and RPCs;
  `ue net player --pie` shows role, owner and relevancy per world; `ue net compare player.Health` checks the
  server and clients agree.

## AI

- AI built on StateTree: `ue statetree /Game/AI/ST_Enemy` prints the whole tree as text (states, enter conditions,
  tasks, transitions); read it before changing AI behaviour. `ue statetree --pie` shows each component's run status.
- AI in PIE: `ue ai` (one line per AI), `ue ai <pawn>` (movement, focus, perception, brain, blackboard, and the
  project's getters). State a C++ class keeps in plain members is invisible: suggest exposing it as a
  `UFUNCTION(BlueprintPure)` getter.

## Navigation and points in the world

- No navmesh in the editor (headless editors do not build it by themselves): `ue world build nav` rebuilds it
  and says what it changed (it waits out a navigation build lock and says which). `ue world build
  lighting|hlod` for lightmaps and World Partition HLODs.
- AI that does not move: `ue nav` (is there a navmesh, and if not why), `ue nav check` (spawn/patrol points
  off the navmesh), `ue nav path <a> <b> [<a> <b> ...]` (reachable? how long, how high, and where a partial
  path stops; exit 1 if any pair is not a full path; `--m` for metres, `--agent <pawn>`); add `--pie` for
  runtime-generated navmeshes. Run the pairs for new routes after every `world build nav`.
- Spawn points, pickups, teleports: `ue world probe x,y,z|actor ...` says whether each point is inside geometry,
  what is within `--radius` (distance to the nearest collision), and whether the navmesh reaches it (exit 1 if
  any is inside or off the navmesh).

## Animation

- Animation in PIE: `ue anim <actor|player>` (anim BP and its variables, montage, curves, movement mode and
  speed). Python cannot read state machine states; the variables the transitions use usually tell you.
- Character animation logic: `ue anim graph /Game/.../ABP_Hero` (state machines, what each state plays,
  transition rules and blend times); `ue anim player` shows the live anim instance in PIE.

## Audio

- Audio in PIE: `ue audio` lists what is playing (needs `ue open --headless --sound`, which stays silent);
  `expect-sound <regex>` as a `ue pie do` step checks a sound fires. `ue audio --log 10` (or `--do steps.txt`)
  records how each sound that started was actually played (3D or 2D, attenuation, class) and flags stereo sounds
  played in 3D: trust it over asset settings when code builds attenuation at runtime. To hear what the mix did
  (a crossfade, a voice that zeroes everything): `ue audio --bounce 10` (or `-f steps.txt`) writes PIE's master
  submix (or `--submix X`) to a WAV with the speakers silent and prints level per second, active channels, bands,
  dropouts to exact silence and clipping.

## Save games

- Save games: `ue savegame` lists slots, `ue savegame <slot> [regex]` shows what is saved; `ue savegame <slot>
  Name=value` edits it (a .bak is kept) to set up a play test. Ask before editing a user's real save.

## Packaging and real multiplayer

- Before a package or a hand-over: `ue game smoke` opens each map in a headless `-game` process for a few seconds
  and reports errors, Accessed None and crashes that PIE hides (editor-only components are null there).
- Does the shipped game boot? `ue package` (minutes to an hour: it is a background job it waits on) packages,
  then runs the packaged game headless for 20 s and reports crashes and errors; `--boot-only` re-tests the last
  package. `ue package --perf 20 --map /Game/Maps/L` measures frame times in the packaged game (editor numbers
  are not the shipped game's).
- Real multiplayer outside the editor (separate processes, real network, the dedicated-server code path):
  `ue game start --clients 2 [--server dedicated] [--map M] [--game MODE]` waits until the clients join;
  `ue game status` (crashes, errors per process), `ue game log 1` (client 1), `ue game stop`. `--packaged` (or
  `ue package --play 2`) runs the shipped build. Prefer `ue pie start --clients N` for quick checks.
  `--server none` runs each client on its own (a solo `-game` run, the game's start-up included), `--connect
  host:port` joins a server you run, and `--python` keeps editor Python as Play > Standalone does (start-up
  scripts such as init_unreal.py run). `ue game shot 1 --windowed` captures a game window, loading screens
  included (the movie player is off in PIE).
- Cooks and other long commands: `ue job cook` or `ue job start -- <cmd>`, then `ue job wait` (errors only)
  or `ue job status`. Do not block a shell on them.
