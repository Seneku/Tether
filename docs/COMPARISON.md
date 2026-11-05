# Tether vs Epic's official Claude Code plugin

Head-to-head on the same five tasks, run in the same Claude Code session on 2026-10-11. It was re-checked on
2026-11-05: see [Re-check](#re-check-2026-11-05) for what still holds and what changed. The sections after that one
are the original run.

## Re-check, 2026-11-05

UE 5.8.4, plugin 3.1.1 and Tether 0.36.3 on the same machine and `Sandbox/`, with the MCP port now :18873. The Epic
arm replays the original calls over raw MCP rather than through a live agent; its discovery sizes match the original
run. Timings are medians of 3–20 runs with no other Unreal editor open.

**What an agent reads for T1–T3:**

| | Tether | Epic plugin |
|---|---|---|
| Skill | 12.3 KB core, plus up to 26.8 KB for the three references these tasks touch | 7.2 KB |
| Tool discovery and output | ~1 KB | ~94 KB (the original ~97 KB) |
| **Total** | **13–40 KB** | **~101 KB** |

The original's 629 B against 97 KB left the skills out, which was close to fair while Tether's was 6 KB. It had
reached 60.8 KB by November (~62 KB against ~101 KB, and more than Epic on the first task) before being split into a
core and references. Once Epic's schemas are loaded, its calls are as small as `ue`'s: T1's read and T2's set return
239 B (the move call not counted) against Tether's 314 B for T1 and T2. The gap is discovery, a one-time cost per
session, and it grows with each toolset an agent needs (Niagara's system toolset alone is 278 KB).

**Timings:**

| Loop | Result | Original |
|---|---|---|
| Read state: same script as a commandlet / `ue py -f` | 11.6 s, 1,935 log lines / 0.33 s | ~24 s (mid-size game) / 0.85 s |
| `ue status`, `level_summary`, `props` | 0.20 s, 0.24 s, 0.29 s | 37–78 ms in earlier figures (`DESIGN.md` §1) |
| 20 reads in one `ue batch` | 0.64 s (0.03 s each) | |
| C++ body change, the session's first / later | 8–17 s / 5.1 s | 13.7 s / 4.4–4.7 s |
| C# reload through `ue build` | 0.58 s (0.39–1.19 s) | 0.6–1.2 s |
| `ue build --restart`, new UFUNCTION, with compile check | 51 s (140 s with a cold game target) | not comparable: 24 s, before the check existed and on an older build |
| The same with `--no-check` | 34.6 s | |
| `ue build --restart`, no code change | 22 s, or 30 s when the close hangs (below) | 22.4 s |
| First `ue` call after a restart | 0.48 s (0.39–1.04 s) | 377 ms |
| Editor ready, headless / `--render` | 14.9 s / 16.1 s | |
| `ue open --second` | 12.8 s | |
| PIE start, single / 2 clients, headless | 1.07 s / 1.21 s | 0.8 s / |
| `ue shot` in an offscreen editor | 1.34 s | |
| `ue test`, 32 tests | 1.67 s | ~1 s |

The README used to set 24 s against a 53 s manual restart. Those were different projects (Sandbox and the mid-size
game), and a restart compiles the same code as doing it by hand: it saves the hands, not the build.

**Behaviour:**

- **T3 (C#):** still fails on Epic's side. `CompileLiveCoding` returned `Result: NoChanges`, and every coin in the next
  PIE logged "no score subsystem" (see note 6 below). Tether's reload and PIE check passed, with the new cap live.
- **Restart with a live MCP session (T5, now tested for Epic's server):** the old session gets HTTP 404, "Unknown
  session id ... client should reinitialize". The next `ue` call simply worked. How Claude Code reacts to that 404
  was not tested; field reports say it needs a manual `/mcp reconnect` (`DESIGN.md` §5.2).
- **Editor gone:** `ue status`, `ue log` and `ue crash` answered in ~0.2 s after a `taskkill`. A hung game thread
  answered `ue status` in 0.25 s; `ue log` took 10 s, waiting to flush.
- **Several sessions:** with a lease held, a second session's `build --restart`, `close`, `pie start` and `save` were
  refused with exit 5, and its reads worked. Epic's `StartPIE` over MCP went straight through the lease. 8 parallel
  readers made 120 calls with no failures, at 0.33 s median against 0.23 s alone. `ue open --second` was isolated from
  the shared editor, but refuses while another session holds the lease unless given `--force`.
- **Closing right after boot:** an editor told to quit within seconds of booting often hangs in shutdown, at 0% CPU on
  every thread, after "Preparing to exit" and its asset registry write (2 of 10 to 6 of 8 by run; 0 of 10 after 15 s
  of uptime; only `Sandbox/` tested). The cause inside the shutdown is not identified, and waiting for that write
  before quitting did not help. `ue close` used to wait out 30 s, or fail with exit 4 without `--discard`. It now ends
  the editor after 10 s of silence: a stuck close takes ~12 s instead of 31 s, and a clean one 4–5 s.
- **Headless dialogs:** `EditorDialog.show_message` returned its default in 0.35 s without blocking.
- **Perforce (local rig):** an edit in a map another user held was undone and named them, and a bulk edit over the
  limit was refused until `--bulk`. This re-check also found `ue bp edit` not checking out unsaved edits, fixed in 0.36.4.
- **`ue selftest`:** 137 passed, 0 failed, 3 skipped, in 194 s. 384 unit tests passed.

## Setup

- **Project:** `Sandbox/` (UE 5.8.3) with the UnrealSharp coin feature. The editor runs headless on
  :18160. Coins are placed by `Scripts/place_coins.py`; this setup is shared and not measured.
- **Epic arm:** plugin `unreal-engine-skills-for-claude-code` 3.1.1. The agent follows its `unreal-mcp`
  skill over the `unreal-mcp` connection (direct HTTP, because the restart-surviving proxy is not
  shipped in the 5.8.3 launcher build). Best case: `LiveCodingToolset` is enabled, which the skill
  recommends but Epic's documented setup (`AllToolsets`) leaves out. No `ue`, no `tether.*` tools.
- **Tether arm:** the `ue` CLI, the `tether` toolset and the `unreal-tether` skill.
- **Both arms:** file edits use Claude Code's Edit tool.
- **Measures:**
  - *calls*: tool invocations against the editor;
  - *time*: wall clock, including process start;
  - *output*: characters of tool output the agent has to read (about 4 per token).

## Tasks

- **T1:** read Coin_3's `Value`, `RespawnSeconds`, `CoinId` and `bCollected`.
- **T2:** set Coin_3's `Value` to 25 and raise it 200 units.
- **T3:** change C# gameplay code (combo cap) and verify the new scoring in PIE.
- **T4:** C++ compile-error loop: get the diagnostic, fix it, confirm the fix is live.
- **T5:** restart the editor, then make the next successful query.

## Results

| Task | Tether | Epic plugin |
|---|---|---|
| T1 read state | 1 call · 0.2 s · 145 B | 6 calls · 2.1 s · 49.9 KB (49.7 KB of it is skill and schema discovery) |
| T2 set + move | 1 call · 0.2 s · 18 B | 4 calls · 1.3 s · 17.4 KB (17.1 KB of it is the `ActorTools` schema) |
| T3 C# change + PIE verify | 5 calls · 3.0 s · 466 B (reload 1.1 s) | **Failed:** 8 calls · 8.5 s · 30.1 KB, and the change never loaded (see note 2) |
| T4 C++ error loop | 3 calls · 10.3 s · 156 B; exact error line (`SandboxProbe.cpp:10: C2065 'MissingCounter'`) | not run |
| T5 restart + next query | 2 calls · 22.4 s · 815 B; next query worked at once (377 ms), no reconnect | not run; Epic has no restart tool |

Epic's times are per-call tool time taken from the session transcript. Tether's are process wall clock,
which includes Python start-up.

T4 and T5 were not run for Epic: T1–T3 already settled the comparison.

## Notes

1. **Discovery cost dominates Epic's output.** Its skill requires discovery first:
   - `ListSkills` returns 6.3 KB;
   - `list_toolsets` returns 13.5 KB;
   - each `describe_toolset` returns 2.6–24 KB.

   Over T1–T3 the agent read about 97 KB (about 24k tokens) with Epic, against 629 B with Tether.
   Most of that is a one-time cost per session, but it stays in context.
2. **Epic cannot reload UnrealSharp C#.** `LiveCodingToolset.CompileLiveCoding` only compiles C++. After
   the C# edit it reported "Live coding succeeded, no code changes detected", so an agent would assume
   the change was live.

   A PIE run afterwards still scored with the old cap: ×3 and a score of 165, where the new code would
   give 185. The only Epic route to a C# change is an editor restart.
3. **`GetLogEntries` rejects UnrealSharp's managed log categories.** With `category` set it fails with
   "Log category 'LogCoins' not found". The workaround is `category: ""` plus a `pattern`.
4. **Epic's edit tools log errors during PIE.** Actor and object edits run `require_editable`, which logs
   two editor errors on every call while PIE runs ("The Editor is currently in a play mode" and
   "GetCurrentLevel … no world"). The edit still works, but the errors land in the log the agent reads.
5. **What works on Epic's side:**
   - `find_actors` and `set_actor_transform` do reach PIE actors (refPaths under `/Temp/UEDPIE_0_…`).
   - `ProgrammaticToolset.execute_tool_script` batches calls, at one editor frame per nested call.
6. **An UnrealSharp bug, not Epic's tools.** In Epic's PIE run every pickup logged "no score subsystem
   in this world". Later PIE sessions scored normally.

   Root cause, found afterwards (see `DESIGN.md` §2):
   - The C# edit was saved but never reloaded.
   - UnrealSharp recompiles such a type in place on its first lookup.
   - That recompile drops the type's subsystems from every running world.

   Epic's arm hit this because it has no way to reload C#. Tether's T3 reloaded before PIE, and
   `ue pie start` now always does.
7. **Where Epic is ahead: breadth.** It has 50+ toolsets, covering Niagara, Sequencer, GAS, StateTree,
   PCG, materials, Blueprints and more. Tether is the fast path for the edit, build and play loop and
   complements Epic's catalog rather than replacing it.
