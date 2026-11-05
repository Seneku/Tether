# Performance: frame time, GPU, memory, loading, budgets

## Frame time and GPU

- Performance: `ue perf` before and after a change; each run prints the difference from the previous one.
  GPU bound: `ue gpu` (needs a rendering editor) splits GPU time by kind (Lumen, shadows, base pass, post...)
  and names the top passes; `ue perf --scalability` measures low/medium/high/epic in one table. What a change
  costs: `ue gpu --ab "mat MI_X UseWindows=false" "mat MI_X UseWindows=true" --restore "..."` (in PIE, at a
  camera that shows it) alternates the two states and prints the difference by pass. A fixed view for timings:
  `ue gpu --at x,y,z --look-at x,y,z` (or `--camera <shot baseline>`; `ue perf` takes the same).
  Slow builds: `ue build --timing` (slowest files, modules and headers; game target, editor untouched). Slow editor
  start: `ue perf startup`. Which ue commands fail most here: `ue stats`.
  Settings behind it: `ue cvar r.Shadow` lists console variables with who set them; `ue cvar --changed` shows
  everything the project and device profile changed; to try a value, `ue cvar r.X=1 --for "gpu"` sets it only
  for that one command.

## Textures, traces, budgets, hitches

- Texture memory: `ue tex /Game/Folder` (largest textures, estimated memory, oversized/uncompressed/never
  streaming ones) and `ue tex --streaming` (what is resident now, pool use, textures held back).
  `ue perf --trace` adds which scopes (functions, Blueprint events, render passes) take the time per thread.
  Limits the project must stay within live in Config/Tether/budget.json; `ue budget` checks them all (exit 1
  when over), and `ue ci --budget` makes CI fail on a regression.
  Stutter while moving through a level: `ue perf load [/Game/Maps/L] -f tests/play/route.txt` lists hitches
  with the packages that finished loading in each (and sync-load flushes), plus World Partition streaming time.
  Run it on a freshly opened editor: packages already in memory do not load again.
  When it says the frame rate is capped, compare the per-frame game/render/GPU work, not the fps.
