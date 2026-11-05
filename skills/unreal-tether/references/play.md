# Play testing in PIE: steps, watching, input, game UI, scripts, long runs

## Starting and waiting

- `ue pie start` / `ue pie stop` / `ue pie status`. Status explains a session that died (fatal error
  plus your source line).
- Try something in the running game without editing the level: `ue spawn /Game/X/BP_Buoy --pie --at x,y,z`.
- `ue wait 4 game` waits 4 s of game time in PIE between `ue exec` calls (`ue wait 4`: wall-clock seconds).

## Playing like a player: step files

- To play the game like a player would: `ue pie do "at ItemPickup_Key -150,0,100" "look ItemPickup_Key"
  "tap InteractAction" "until player.Inventory.Items ~ Key within 2" "gone ItemPickup_Key"` (or `-f steps.txt`).
  Steps: at, look, move, press/tap/hold/release (Enhanced Input actions: asset names, or actions the
  character creates in C++), call, expect/until (`= != < > <= >= ~`), log, exists/gone, wait, exec, shot
  (`ue help pie` lists them all with their arguments). A `log` step matches lines after the previous `log`
  step's match (a toggle checked twice proves the second toggle); `log <regex> anywhere` searches the whole run (`within N` before or after it).
  Each step prints ok/FAIL; it stops at the first failure. Write the step file once and rerun it after changes.
  Keep step files in `tests/play/` with a `# map: /Game/...` line: `ue ci --play` runs them all as regression tests.
- To turn the user's own play-through into a test: ask them to run `ue pie record -o tests/play/<name>.txt` in a
  windowed editor and play (it stops when they stop PIE). The file replays with `ue pie do -f`: keys become
  `tap`/`hold`/`release` of their input actions, mouse look becomes `look` steps, ending with a `near` check.
  Add `expect` steps for what should have happened.
- Visual checkpoints in a step file: `shot-check gate_view` records tests/play/shots/gate_view.png the first time
  and fails later runs that look different (diff image in the output); put a `look` step before it.
- To lock in how play unfolds (not just that steps pass): add `# watch: BP_Enemy_C_1.State player.Health player`
  to a step file in tests/play/. Its first `ue pie do -f` records tests/play/baselines/<file>.json (commit it);
  later runs and `ue ci --play` fail on differences (states reached late, values ending elsewhere, paths drifting).
  Re-record on purpose with `--baseline <file stem>`.

## Watching values

- To see how values change during play (instead of adding print nodes): `ue pie watch player.Health
  BP_Enemy_C_1.State BP_Enemy_C_1 --seconds 10` (an actor alone = its location; `-f steps.txt` plays a step file
  while watching). Prints each value's range, path or changes with times; the full samples go to a CSV (rotators
  as .pitch/.yaw/.roll). Getters work there and in `expect`/`until` steps alike: `until
  player.Movement.GetSpeedKmh() > 100 within 15`, `expect player.GetActorRotation().yaw > 80`. Keys a step file
  holds (`key W down`) are released when it ends, failed or not.
- `ue pie watch Actor.Component.Property` samples a component's property (the component by its property name,
  e.g. `SDPlayerCharacter_0.CharacterMovement.MovementBase`); `ue props X --match Name --pie` reads properties
  Python calls protected. After Live Coding patched class layouts, `ue pie start` warns: UMG and replication
  can misbehave (a HUD drawing nothing) until `ue build --restart`.
- Reading state: `ue props` also lists UPROPERTYs that are not Blueprint-visible; `pie watch` reads them, plus
  `player.control` and getters like `player.GetVelocity()`.

## Input

- Input: `hold IA_Move 1,0` in `pie do` injects the action's value (mapping modifiers like dead zones are
  skipped); `key W down`/`key W up`, `mouse 100 0` and `stick Gamepad_Right2D 0.25,0 1` go in as real key
  presses through the mappings. `ue input --live IA_Look` (PIE running) shows the action's value while you or a
  step file play; `pie watch IA_Look` puts it in a column. A background editor runs PIE at a few fps: `ue pie
  start` lifts that for its sessions, and `wait 2.5 game` waits on game time.
- Which input actions exist and what keys reach them: `ue input` (by mapping context, and who adds each context);
  in PIE it also says which contexts are active. `ue input Move` shows matching actions only. A `pie do` step
  that presses an action no active context maps still works, but warns: a player could not do it.

## Game UI

- Game UI in PIE: `ue ui` lists what is on screen (Slate and UMG texts, buttons, text boxes); `ue ui click
  "Close"`, `ue ui type "Name" hello`, `ue ui key Escape` (in PIE, keys and chords such as `Ctrl+G` go to the
  game's input, so Escape opens its pause menu instead of stopping PIE; `--slate` sends to the focused widget).
  When a UMG widget has keyboard focus (a menu reading keys in NativeOnKeyDown, or a title screen holding focus
  itself), other single keys go to it
  through Slate on their own (`--game` forces the game's input); step files take `key --slate Enter` or
  `ui-key Enter`, and split steps follow the same rule: `key E down` on a focused screen presses E through Slate
  right there (Slate sends KeyDown and KeyUp together, so its `key E up` sends nothing); `click --event LABEL` (fires OnClicked, for a covered window). `ue ui --rects` prints each
  widget's screen rectangle (what `ue ui at x,y` takes); a plain `--headless` editor (-nullrhi) lays out no
  geometry, so rectangles, `ui at` and arrow-key menu navigation need `ue open --headless --render`, while clicks
  there fire the UMG button's OnClicked. A button with no text child (a transparent UButton over
  a text row) is labelled by the text under it; better, give it an accessible name
  (`SetAccessibleBehavior(EAccessibleBehavior::Custom, Text)`), which screen readers use too.
  One tap is one press, as on a keyboard, and taps in a row (two `key Down tap` steps) stay two presses. A legacy BindKey chord on a key an Enhanced Input mapping also uses
  (Ctrl+G with G mapped) usually never fires: the mapping consumes the key; `ue ui key` warns when it sees that.
  Several buttons with one label (nine ON/OFF toggles): `ue ui click "OFF" --near "DEBUG HUD: DIRECTOR"` (the one
  closest to that text) or `ue ui click "OFF#3"` (the third, in `ue ui`'s order).
  In `ue pie do`: `click <label>`, `expect-ui <text>`,
  `ui-gone <text>`. Clicks are real mouse clicks at the widget, so whatever is on top receives them; labels are
  stable, refs (b12) change between reads. Canvas-drawn HUD text is not a widget: check it with `ue shot`.
  Step files also take `log <regex> count >= 6` and `log in order: lap 1, lap 2, finished`, and `shot` with
  `ue shot`'s options (`shot --at x,y,z --rot -89,0,0`).

## Verifying from scripts and logs

- Prefer `ue pie run check.py --expect '<log regex>' --reject 'Error|no subsystem'` (or `-c "<code>"`) for verifying gameplay:
  it starts PIE, runs the script in the PIE world, waits for the log lines, stops PIE and exits 0/1.
  Write the script once and rerun it after every change.
- Long PIE-side tests (a tick callback driving inputs for 10-30 s): end the script's work with
  `tether.finish({"result": ...})` and run it with `ue pie run test.py --wait 60`, which prints the value; no
  polling for files. `tether.spawn(cls_or_asset, (x, y, z), (pitch, yaw, roll), label="Pad")` puts an actor in the
  PIE world from a script; `ue spawn --pie --label Pad --replace` / `ue delete --pie Pad` from the shell. Paths in
  editor Python are Windows paths: `D:/Projects/...`, not Git Bash's `/d/Projects/...` (that lands in `C:\d\...`).
  For scripts: `ue pie status` exits 0 running / 1 stopped, `ue actors <re>` and `ue find <re>` exit 1 on no match.
- Drive the test from Python: in PIE, `w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()`,
  then `unreal.GameplayStatics.get_player_pawn(w, 0)`, then `set_actor_location(loc, sweep=False, teleport=True)`.
  Overlaps fire on teleport. Timers need real time, so wait between calls rather than inside one.
- Verify through a custom log category (`ue log --cat LogMyFeature --level Display`). Logs are flushed
  before each read, so lines from the last action are always there.
- "Can't interact with / pick up / shoot X": `ue pie start`, aim with `ue pie do "look X"`, then `ue trace --channel
  <the channel the code traces on>` (what blocks first, and each component's response); `ue collision X` shows its
  profiles and per-channel responses.

## Films, soaks, loops

- To see what happened between checks: `ue pie do -f steps.txt --film 0.25` adds a contact sheet of the game's view
  (with HUD), four frames a second with times on them; read that one image instead of many screenshots. When the
  game ends PIE, filming stops and `--keep-going` skips the steps left (a Slate capture of the PIE viewport being
  removed crashes the editor).
- Soaks and repeated console commands: `ue pie loop "SD.SpawnWave 4; wait 30; stat unit; wait 30" --seconds 600
  --every 60 "memreport"` runs them from the game thread on a fixed schedule (no drift, no round trip each);
  it waits for the end, and a killed `ue` leaves it running (`ue pie loop status` / `stop`).
- Leaks and rare failures: `ue soak --minutes 10 -f tests/play/x.txt` loops a step file (or wanders) and
  reports the memory trend, frame rate and errors. Run it in the background (it blocks for the duration).
