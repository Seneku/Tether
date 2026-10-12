# Tether vs Epic's official Claude Code plugin

Head-to-head on the same five tasks, run in the same Claude Code session on 2026-10-11.

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
