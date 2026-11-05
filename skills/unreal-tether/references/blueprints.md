# Blueprints and classes: reading, editing, tracing

## Before changing a class

- Before changing a class or Blueprint: `ue class AEnemyCharacter` (or `ue class BP_Door`) shows its parents, own
  properties and functions, a Blueprint's components/variables/events, what derives from it and what uses it.
  A big one (Ultra Dynamic Sky: 900 variables): `ue class X --functions --grep cloud` (parameters and types,
  what `call_method` needs), `--vars`, `--components`, `--events`.
- Component layout: `ue tree BP_Door` (or an actor label, or `player` in PIE) shows the component hierarchy
  with sockets, transforms, meshes and collision/visibility flags.
- Before renaming or removing C++ functions, properties or classes, `ue usages <Name>` lists the C++ lines and
  the Blueprints (with their graph lines) that use them.

## Reading and editing Blueprints

- `ue bp /Game/Path/BP_X [graph]` prints Blueprint graphs as text. To change one: write the edited text to
  a file, `ue bp /Game/Path/BP_X EventGraph --apply file.dsl --save` (compiles, prints the diff).
  `ue bp ... --diff` shows only what changed since the last read; `ue bp --docs` is the syntax reference.
- Build a Blueprint's shape in one-liners: `ue bp edit /Game/X/BP_Door add-var IsOpen bool false
  --instance-editable`, `... add-component Mesh StaticMesh StaticMesh=/Game/X/SM_Door`, `... add-function Open`,
  `... set-default IsOpen=true`; then write graph bodies with `ue bp ... --apply`.

## When a Blueprint misbehaves

- "Why didn't my event fire?": `ue bp trace --do tests/play/steps.txt --match BP_Door` lists the Blueprint
  events and functions that ran during PIE, in order with counts (an Insights trace with named events;
  about 20 s). If the event is missing, the trigger never reached it; then check input (`ue input`), overlaps
  and the values involved (`ue pie watch`).
- After renaming or removing C++ functions, properties or classes (and `ue build --restart`), run
  `ue bp check` (or `ue bp check /Game/Some/Folder`): it lists only Blueprints that no longer compile, with why.
  "missing or NULL parent class: /Script/Old.Class" means a renamed C++ class or module needs a redirect
  ([CoreRedirects] in DefaultEngine.ini); `ue open` reports no error for it, and PIE falls back to defaults.
