"""ue selftest and ue ci: the toolset checked against a live editor."""
import json
import os
import sys
import time

from .core import (
    CliError, UE_SCRIPT, editor_live, editor_mode, host_platform, read_json, state_dir, subprocess, write_json,
)
from .transport import catalog
from .images import png_write
from .assets import package_file
from .setup import tether_plugins_dir, tether_version


FIRST_ASSET = """
import unreal
reg = unreal.AssetRegistryHelpers.get_asset_registry()
hits = [str(a.package_name) for a in reg.get_assets_by_class(unreal.TopLevelAssetPath('/Script/Engine', '{cls}'))
        if str(a.package_name).startswith('/Game/')]
print(sorted(hits)[0] if hits else '')
"""


PLANT_VALIDATOR = """
import sys
if not hasattr(sys, '_tether_validator_cls'):  # a Python uclass can be defined once per editor session
    @unreal.uclass()
    class TetherSelftestValidator(unreal.EditorValidatorBase):
        @unreal.ufunction(override=True)
        def k2_can_validate_asset(self, asset):
            return asset.get_outermost().get_name() == getattr(sys, '_tether_validator_target', '')
        @unreal.ufunction(override=True)
        def k2_validate_loaded_asset(self, asset):
            self.asset_fails(asset, 'tether selftest: planted failure')
            return unreal.DataValidationResult.INVALID
    sys._tether_validator_cls = TetherSelftestValidator
subsystem = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
if {plant}:
    sys._tether_validator_target = '{target}'
    sys._tether_validator = sys._tether_validator_cls()
    subsystem.add_validator(sys._tether_validator)
elif getattr(sys, '_tether_validator', None):
    subsystem.remove_validator(sys._tether_validator)
    sys._tether_validator = None
print('ok')
"""


SAVE_PROBE = """
import sys
if not hasattr(sys, '_tether_save_cls'):  # a Python uclass can be defined once per editor session
    @unreal.uclass()
    class TetherSelftestSave(unreal.SaveGame):
        score = unreal.uproperty(int)
    sys._tether_save_cls = TetherSelftestSave
if {make}:
    s = unreal.GameplayStatics.create_save_game_object(sys._tether_save_cls)
    s.set_editor_property('score', 7)
    print(unreal.GameplayStatics.save_game_to_slot(s, 'TetherSelftest', 0))
else:
    import glob, os
    unreal.GameplayStatics.delete_game_in_slot('TetherSelftest', 0)
    for f in glob.glob(os.path.join(unreal.Paths.project_saved_dir(), 'SaveGames', 'TetherSelftest.sav.bak-*')):
        os.remove(f)
    print(True)
"""


def cmd_selftest(ctx, args):
    """Run every capability against the live editor, one CLI process per check, and report pass/fail/skip."""
    import tempfile
    project = ctx.project
    base = [sys.executable, UE_SCRIPT, "--project", project]
    results, notes = [], {}

    def ue(*argv, timeout=180):
        started = time.time()
        proc = subprocess.run(base + list(argv), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip(), time.time() - started

    def check(name, argv, ok=lambda code, out: code == 0, skip=None, timeout=180):
        if skip:
            results.append(("SKIP", name, skip, 0.0))
            return None
        try:
            code, out, took = ue(*argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            results.append(("FAIL", name, f"no answer within {timeout}s", float(timeout)))
            return None
        passed = ok(code, out)
        detail = "" if passed else (out.splitlines() or [f"exit {code}"])[-1][:160]
        results.append(("PASS" if passed else "FAIL", name, detail, took))
        return out if passed else None

    beacon = ctx.beacon or {}
    print(f"Tether {tether_version()} | {os.path.basename(project)} | UE {beacon.get('engine', '?').split('+')[0]} | "
          f"{host_platform()} | Python {sys.version.split()[0]} | transport {ctx.transport}")
    if not check("status", ["status"]):
        for status, name, detail, _ in results:
            print(f"{status} {name}: {detail}")
        raise CliError("the editor is not running or not answering; start it with `ue open --headless --render`", 2)
    remote = ctx.transport == "remote"
    launch = editor_mode(project, editor_live(project))
    check("doctor", ["doctor"], ok=lambda c, o: "checks:" in o)
    check("py", ["py", "6*7"], ok=lambda c, o: c == 0 and o.endswith("42"))
    check("exec", ["exec", "stat fps"], ok=lambda c, o: c == 0 and "Cmd: stat fps" in o)
    check("actors", ["actors", "--limit", "3"], ok=lambda c, o: c == 0 and "actors" in o.splitlines()[0])
    check("class", ["class", "Character"], ok=lambda c, o: c == 0 and ": Pawn : Actor" in o and "own functions" in o)
    check("api", ["api", "Actor", "set_actor_location"], ok=lambda c, o: c == 0 and "set_actor_location" in o)
    check("find", ["find", "Cube", "--class", "StaticMesh", "--under", "/Engine/BasicShapes"],
          ok=lambda c, o: c == 0 and "/Engine/BasicShapes/Cube" in o)
    check("refs", ["refs", "/Engine/BasicShapes/BasicShapeMaterial", "--limit", "3"],
          ok=lambda c, o: c == 0 and "reference" in o)
    check("asset", ["asset", "/Engine/BasicShapes/Cube"], ok=lambda c, o: c == 0 and "triangles" in o)
    check("log", ["log", "--level", "Error"])
    check("log --summary", ["log", "--summary"], ok=lambda c, o: c == 0 and "group(s)" in o)
    check("help", ["help", "log"], ok=lambda c, o: c == 0 and "--since" in o)
    check("log --tail", ["log", "--tail", "3"], ok=lambda c, o: c == 0 and "this session" in o)
    check("lint --cpp", ["lint", "--cpp"], ok=lambda c, o: c == 0 and "file-private name(s)" in o)
    has_net = (ue("py", "hasattr(unreal, 'TetherProbeNetController')")[1].splitlines() or [""])[-1] == "True"
    check("props (not Blueprint-visible)", ["props", "/Script/Sandbox.Default__TetherProbeNetController", "PingsSent"],
          skip=None if has_net else "no Sandbox net fixture", ok=lambda c, o: c == 0 and "PingsSent = 0" in o)
    check("net (class)", ["net", "Character"], ok=lambda c, o: c == 0 and "Character : Pawn" in o and "replicates" in o)
    check("cvar", ["cvar", "r.Shadow.MaxResolution"], ok=lambda c, o: c == 0 and "r.Shadow.MaxResolution = " in o)
    check("cvar --for", ["cvar", "r.Shadow.MaxResolution=777", "--for", "cvar r.Shadow.MaxResolution"],
          skip="needs UE 5.8+" if remote else None,
          ok=lambda c, o: c == 0 and "r.Shadow.MaxResolution = 777  (Console" in o and "restored r.Shadow.MaxResolution" in o)
    check("tex", ["tex", "/Engine/EngineSky", "--top", "2"], ok=lambda c, o: c == 0 and "textures under /Engine/EngineSky" in o)
    check("tex --streaming", ["tex", "--streaming", "--top", "2"], ok=lambda c, o: c == 0 and "textures in memory" in o)
    toolsets = set() if remote else set(catalog(ctx))
    check("fx (system)", ["fx", "/Niagara/DefaultAssets/Templates/Systems/RadialBurst"],
          skip=None if "NiagaraToolsets.NiagaraToolset_System" in toolsets else "no Epic Niagara toolset",
          ok=lambda c, o: c == 0 and "emitter(s)" in o and "particle update:" in o)
    check("fx (list)", ["fx"], ok=lambda c, o: (c == 0 and "Niagara component(s)" in o) or "NiagaraComponent" in o)
    check("pcg (list)", ["pcg"], ok=lambda c, o: (c == 0 and "PCG component(s)" in o) or "PCGComponent" in o)
    check("pcg (graph)", ["pcg", "/PCG/EdMode/DrawSpline/SpacingTool"],
          skip=None if "PCGToolset.PCGToolset" in toolsets else "no Epic PCG toolset",
          ok=lambda c, o: c == 0 and "node(s)" in o)
    check("changes", ["changes"], ok=lambda c, o: c == 0 and "unsaved package" in o)
    check("size", ["size", "--top", "3"], ok=lambda c, o: c == 0 and "packages" in o)
    check("lint", ["lint", "/Engine/BasicShapes"], ok=lambda c, o: c == 0 and "naming" in o)
    check("mesh check", ["mesh", "check", "/Engine/BasicShapes/Cube", "--height", "100"],
          ok=lambda c, o: c == 0 and "100 x 100 x 100 cm" in o and "lightmap UV" in o)
    check("skel", ["skel", "/Engine/EngineMeshes/SkeletalCube"], ok=lambda c, o: c == 0 and "2 bones" in o and "Bone02" in o)
    check("sound", ["sound", "/Engine/EngineSounds/WhiteNoise"], ok=lambda c, o: c == 0 and "mono, 44100 Hz" in o)
    check("sound (folder: the waves it uses)", ["sound", "/Engine/EngineSounds"], ok=lambda c, o: c == 0 and "sound wave(s)" in o)
    check("sound --stats", ["sound", "/Engine/EngineSounds/WhiteNoise", "--stats"], timeout=300,
          ok=lambda c, o: c == 0 and "dBFS" in o and "WhiteNoise" in o)
    check("audio mix", ["audio", "mix"], ok=lambda c, o: c == 0)
    check("data", ["data", "/Engine/BasicShapes", "--limit", "3"], ok=lambda c, o: c == 0 and "row(s) for" in o)
    check("map", ["map", "--px", "200", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-map.png")],
          ok=lambda c, o: c == 0 and "x right, y up" in o)
    check("log --since 1m", ["log", "--since", "1m"], ok=lambda c, o: c == 0 and "in the last 1m" in o)
    check("fab", ["fab"], ok=lambda c, o: c == 0 and "Fab content" in o)
    check("fab library", ["fab", "library"], ok=lambda c, o: c == 0 and "does not read your account" in o)
    check("fab settings", ["fab", "settings"], ok=lambda c, o: c == 0 and "Megascans quality" in o)
    check("fab open", ["fab", "open", "mossy rock"], ok=lambda c, o: c == 0 and "fab.com/search?q=mossy%20rock" in o)
    check("unused", ["unused", "--top", "3"], ok=lambda c, o: c == 0 and "are unused" in o)
    check("unused --redirectors", ["unused", "--redirectors"], ok=lambda c, o: c == 0 and "redirector" in o)
    check("mem", ["mem", "--top", "3"], timeout=300, ok=lambda c, o: c == 0 and "process:" in o)
    check("config", ["config", "Project/Project/Maps", "DefaultMap"], skip="needs UE 5.8+" if remote else None,
          ok=lambda c, o: c == 0 and "gameDefaultMap" in o)
    check("usages", ["usages", "PrintString", "--budget", "5"], ok=lambda c, o: c == 0 and "line(s)" in o)
    check("input", ["input"], ok=lambda c, o: c == 0 and ("input action" in o or "no Enhanced Input" in o))
    saved = check("savegame: make a slot", ["py", SAVE_PROBE.format(make=True)], ok=lambda c, o: c == 0 and o.endswith("True"))
    check("savegame", ["savegame", "TetherSelftest"], skip=None if saved else "no slot",
          ok=lambda c, o: c == 0 and "score = 7" in o)
    check("savegame edit", ["savegame", "TetherSelftest", "score=42"], skip="needs UE 5.8+" if remote else None if saved else "no slot",
          ok=lambda c, o: c == 0 and "7 -> 42" in o)
    ue("py", SAVE_PROBE.format(make=False))
    check("nav", ["nav"], ok=lambda c, o: c == 0 and ("bounds" in o or "NavMeshBoundsVolume" in o))
    check("call tether.level_summary", ["call", "tether.level_summary", "limit=3"],
          ok=lambda c, o: c == 0 and "actors" in o)

    # A transient actor to read and edit; never saved, destroyed at the end.
    spawned = check("spawn probe actor", ["py", "a = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)"
                    ".spawn_actor_from_class(unreal.TargetPoint, unreal.Vector(0, 0, 100000), transient=True)\n"
                    "a.set_actor_label('TetherSelftestProbe')\nprint(a.get_actor_label())"],
                    ok=lambda c, o: c == 0 and "TetherSelftestProbe" in o)
    probe_skip = None if spawned else "probe actor missing"
    check("props", ["props", "TetherSelftestProbe", "hidden"], skip=probe_skip)
    check("set", ["set", "TetherSelftestProbe", "location+=0,0,50"], skip=probe_skip,
          ok=lambda c, o: c == 0 and "-> (0, 0, 100050)" in o)
    check("undo", ["undo"], skip=probe_skip, ok=lambda c, o: c == 0 and "undone: TetherSelftestProbe" in o)
    check("set rollback", ["set", "TetherSelftestProbe", "location=0,0,0", "no_such_property_xyz=1"],
          skip=probe_skip, ok=lambda c, o: c != 0 and "no property" in o)
    check("drop", ["drop", "TetherSelftestProbe"], skip=probe_skip,
          ok=lambda c, o: c == 0 and ("-> " in o or "no ground below" in o))
    check("collision", ["collision", "TetherSelftestProbe"], skip=probe_skip,
          ok=lambda c, o: c == 0 and "primitive component(s)" in o and "profile" in o)
    check("trace --editor", ["trace", "--editor", "--from", "0,0,5000", "--to", "0,0,-5000"],
          ok=lambda c, o: c == 0 and "line trace on Visibility" in o)
    check("undo --editor", ["undo", "--editor"], skip=probe_skip, ok=lambda c, o: c == 0 and "undone:" in o)
    check("redo", ["redo"], skip=probe_skip, ok=lambda c, o: c == 0 and "redone:" in o)
    check("mat", ["mat", "/Engine/BasicShapes/BasicShapeMaterial"], ok=lambda c, o: c == 0 and "vector  Color" in o)
    check("mat graph", ["mat", "graph", "/Engine/BasicShapes/BasicShapeMaterial"],
          ok=lambda c, o: c == 0 and "BaseColor <- #1" in o)
    instance = check("mat: transient instance", ["py",
        "mi = unreal.find_object(None, '/Engine/Transient.MI_TetherSelftest') or "
        "unreal.new_object(unreal.MaterialInstanceConstant, name='MI_TetherSelftest')\n"
        "unreal.MaterialEditingLibrary.set_material_instance_parent(mi, unreal.load_asset('/Engine/BasicShapes/BasicShapeMaterial'))\n"
        "mi.get_path_name()"], ok=lambda c, o: c == 0 and o.endswith("MI_TetherSelftest"))
    check("mat set", ["mat", "/Engine/Transient.MI_TetherSelftest", "Color=1,0,0"],
          skip=None if instance else "no transient instance", ok=lambda c, o: c == 0 and "-> (1, 0, 0, 1)" in o)
    check("undo mat", ["undo"], skip="before 5.8, ue undo covers ue set only" if remote else
          None if instance else "no transient instance", ok=lambda c, o: c == 0 and "undone: mat" in o)
    created = check("new", ["new", "bp", "/Game/TetherSelftest/BP_SelftestNew", "--parent", "Actor"],
                    ok=lambda c, o: c == 0 and "created /Game/TetherSelftest/BP_SelftestNew" in o)
    edit_skip = "needs UE 5.8+ (Epic's BlueprintTools)" if remote else None if created else "nothing created"
    check("bp edit add-var", ["bp", "edit", "/Game/TetherSelftest/BP_SelftestNew", "add-var", "Health", "float", "75"],
          skip=edit_skip, ok=lambda c, o: c == 0 and "-> 75" in o and ": compiled" in o)
    check("bp edit add-component", ["bp", "edit", "/Game/TetherSelftest/BP_SelftestNew", "add-component", "Body",
                                    "StaticMesh", "StaticMesh=/Engine/BasicShapes/Cube"],
          skip=edit_skip, ok=lambda c, o: c == 0 and "added Body" in o and "BasicShapes/Cube" in o)
    check("tree (Blueprint)", ["tree", "/Game/TetherSelftest/BP_SelftestNew"], skip=edit_skip,
          ok=lambda c, o: c == 0 and "Body: StaticMeshComponent" in o)
    check("class (Blueprint)", ["class", "/Game/TetherSelftest/BP_SelftestNew"], skip=None if created else "nothing created",
          ok=lambda c, o: c == 0 and "Health" in o and ": Actor (C++)" in o)
    check("rm (new asset)", ["rm", "/Game/TetherSelftest/BP_SelftestNew"], skip=None if created else "nothing created",
          ok=lambda c, o: c == 0 and "deleted" in o)
    check("anim graph", ["anim", "graph", "/Engine/Tutorial/SubEditors/TutorialAssets/Character/TutorialTPP_AnimBlueprint"],
          ok=lambda c, o: (c == 0 and "Output Pose <-" in o) or "is not an Animation Blueprint" in o)
    check("statetree", ["statetree"], ok=lambda c, o: (c == 0 and "StateTree asset(s)" in o) or "needs the StateTree" in o)
    check("wp", ["wp"], ok=lambda c, o: (c == 0 and "actors" in o and "loaded in the editor" in o)
          or "not a World Partition level" in o)
    check("lights", ["lights"], ok=lambda c, o: c == 0 and "light(s)" in o and "project default" in o)
    check("level check", ["level", "check"], ok=lambda c, o: c == 0 and "actors checked" in o)
    check("landscape", ["landscape"], ok=lambda c, o: (c == 0 and "paint layers" in o) or "no landscape" in o)
    check("foliage", ["foliage"], ok=lambda c, o: c == 0 and "foliage instance(s)" in o)
    planted = check("foliage add", ["foliage", "add", "/Engine/BasicShapes/Cube", "--area", "0,0,800", "--count", "12",
                                    "--scale", "0.2,0.3", "--folder", "/Game/TetherSelftest"],
                    ok=lambda c, o: c == 0 and "added" in o)
    check("foliage remove", ["foliage", "remove", "/Game/TetherSelftest/FT_Cube", "--area", "0,0,100000"],
          skip=None if planted else "nothing added", ok=lambda c, o: c == 0 and "removed" in o)
    png = os.path.join(tempfile.gettempdir(), "T_TetherSelftestImport.png")
    png_write(png, 8, 8, bytes(range(192)))
    imported = check("import", ["import", png, "/Game/TetherSelftest"],
                     ok=lambda c, o: c == 0 and "Texture2D" in o)
    check("rm (imported)", ["rm", "/Game/TetherSelftest/T_TetherSelftestImport"], skip=None if imported else "nothing imported",
          ok=lambda c, o: c == 0 and "deleted" in o)
    ue("py", "unreal.EditorAssetLibrary.delete_directory('/Game/TetherSelftest')")
    scattered = check("scatter", ["scatter", "/Engine/BasicShapes/Cube", "--count", "3", "--area", "0,0,400",
                                  "--label", "TetherSelftestScatter"],
                      ok=lambda c, o: c == 0 and ("placed 3 of 3" in o or "had no ground" in o))
    check("undo scatter", ["undo"], skip=None if scattered and "placed 3" in scattered else "nothing was placed",
          ok=lambda c, o: c == 0 and "undone: scatter" in o)
    check("destroy probe actor", ["py", "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
          "found = [a for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor) "
          "if a.get_actor_label() == 'TetherSelftestProbe']\n"
          "print(len(found), unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actors(found))"],
          skip=probe_skip, ok=lambda c, o: c == 0 and o.endswith("1 True"))

    rendering = launch.get("render", True) or not launch.get("headless")
    shot_out = check("shot", ["shot", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest.png")],
                     skip="needs 5.8 (CaptureViewport)" if remote else None if rendering else
                     "headless with -nullrhi; reopen with `ue open --headless --render`",
                     ok=lambda c, o: c == 0 and ".png" in o)
    if shot_out:
        notes["shot"] = shot_out.split()[1] if len(shot_out.split()) > 1 else ""
        based = check("shot --baseline", ["shot", "--at", "0,-500,300", "--rot", "-20,90,0", "--baseline", "tether_selftest",
                                          "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-base.png")],
                      ok=lambda c, o: c == 0 and "baseline 'tether_selftest' saved" in o)
        check("shot --compare", ["shot", "--compare", "tether_selftest", "--tolerance", "5",
                                 "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-cmp.png")],
              skip=None if based else "no baseline", ok=lambda c, o: c == 0 and "of pixels changed" in o)
        for ext in (".png", ".json"):
            with __import__("contextlib").suppress(OSError):
                os.remove(os.path.join(state_dir(project), "shots", "baselines", "tether_selftest" + ext))
    check("look", ["look", "/Engine/BasicShapes/Cube", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-look.png")],
          skip="needs 5.8 (CaptureViewport)" if remote else None if rendering else
          "headless with -nullrhi; reopen with `ue open --headless --render`",
          ok=lambda c, o: c == 0 and ".png" in o)
    tests_available = not remote and "AutomationTestToolset.AutomationTestToolset" in catalog(ctx)
    check("test", ["test", "StartsWith:System.Core.Math.Evaluate"], timeout=600,
          skip=None if tests_available else "needs Epic's AutomationTestToolset (UE 5.8+, `ue init` enables it)",
          ok=lambda c, o: c == 0 and "passed" in o)
    blueprint = (ue("py", FIRST_ASSET.format(cls="Blueprint"))[1].splitlines() or [""])[-1]
    check("bp", ["bp", blueprint or ""], skip="needs 5.8 (Epic's BlueprintTools)" if remote else
          None if blueprint and blueprint.startswith("/Game/") else "no Blueprint under /Game",
          ok=lambda c, o: c == 0 and ("==" in o or "no graphs" in o))
    if blueprint and blueprint.startswith("/Game/"):
        planted = check("validate: plant", ["py", PLANT_VALIDATOR.format(plant=True, target=blueprint)],
                        ok=lambda c, o: c == 0 and o.endswith("ok"))
        check("validate", ["validate", blueprint], skip=None if planted else "could not plant a validator",
              ok=lambda c, o: c == 1 and "planted failure" in o)
        check("validate: unplant", ["py", PLANT_VALIDATOR.format(plant=False, target="")],
              ok=lambda c, o: c == 0 and o.endswith("ok"))
    else:
        check("validate", [], skip="no Blueprint under /Game")
    blueprint_file = package_file(project, blueprint) if blueprint and blueprint.startswith("/Game/") else None
    check("diff", ["diff", "--files", blueprint_file or "", blueprint_file or ""],
          skip="needs UE 5.8+" if remote else None if blueprint_file and os.path.isfile(blueprint_file) else
          "no Blueprint file under Content/", ok=lambda c, o: c == 0 and o.startswith(blueprint or "?"))
    table = None if remote else (ue("py", FIRST_ASSET.format(cls="DataTable"))[1].splitlines() or [""])[-1]
    check("table export", ["table", table or "", "--export", os.path.join(tempfile.gettempdir(), "tether-selftest.json")],
          skip="needs 5.8 (Epic's DataTableTools)" if remote else
          None if table and table.startswith("/Game/") else "no DataTable under /Game",
          ok=lambda c, o: c == 0 and "exported" in o)
    check("perf", ["perf", "--seconds", "2", "--no-record"], timeout=300, skip="needs 5.8" if remote else None,
          ok=lambda c, o: c == 0 and "frames in" in o)
    check("perf --trace", ["perf", "--seconds", "2", "--no-record", "--trace"], timeout=600,
          skip="needs 5.8" if remote else None if rendering else "headless with -nullrhi (no render timings)",
          ok=lambda c, o: c == 0 and "top scopes" in o)
    check("perf --scalability", ["perf", "--scalability", "low,epic", "--seconds", "1"], timeout=600,
          skip="needs 5.8" if remote else None, ok=lambda c, o: c == 0 and "original levels restored" in o and "epic" in o)
    check("perf load", ["perf", "load", "--seconds", "1"], timeout=300, skip="needs 5.8" if remote else None,
          ok=lambda c, o: c == 0 and "PIE start:" in o and "frames, median" in o)
    has_budget = os.path.isfile(os.path.join(os.path.dirname(project), "Config", "Tether", "budget.json"))
    check("budget", ["budget"], timeout=900, skip=None if has_budget else "no Config/Tether/budget.json",
          ok=lambda c, o: c in (0, 1) and ("within budget" in o or "over budget" in o))
    check("pie start", ["pie", "start"], ok=lambda c, o: c == 0 and "OK" in o, timeout=300)
    check("pie status", ["pie", "status"], ok=lambda c, o: c == 0 and "running" in o)
    check("actors --pie", ["actors", "--pie", "--limit", "1"])
    check("pie do", ["pie", "do", "at 100,200,300", "near 100,200,300 50", "expect player.location ~ ."],
          ok=lambda c, o: c == 0 and "PASS" in o)
    check("pie watch", ["pie", "watch", "player", "--seconds", "1"],
          ok=lambda c, o: c == 0 and "sample(s)" in o and "player  " in o)
    check("pie record", ["pie", "record", "--seconds", "1", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-rec.txt")],
          ok=lambda c, o: (c == 0 and "recorded" in o) or "no keys to record" in o)
    check("tree (player)", ["tree", "player"], ok=lambda c, o: c == 0 and "component(s)" in o)
    check("pie status (frame rate)", ["pie", "status"], ok=lambda c, o: c == 0 and "fps" in o)
    check("pie do wait (game time)", ["pie", "do", "wait 0.5 game"], ok=lambda c, o: c == 0 and "of game time" in o)
    check("pie do --film", ["pie", "do", "wait 4", "--film", "1", "--film-output",
                            os.path.join(tempfile.gettempdir(), "tether-selftest-film.png")], timeout=300,
          skip="needs 5.8" if remote else None if rendering else "headless with -nullrhi",
          ok=lambda c, o: c == 0 and "film: " in o and "frame(s)" in o)
    check("map --pie", ["map", "--pie", "--px", "200", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-map.png")],
          ok=lambda c, o: c == 0 and "x right, y up" in o)
    check("py (engine's own message)", ["py", "print(unreal.EditorAssetLibrary.load_asset('/Engine/BasicShapes/Cube'))"],
          ok=lambda c, o: c == 0 and "engine:" in o and "play mode" in o)
    imc = "/Game/TetherFixtures/Input/IMC_TetherProbe"
    # unreal.load_asset: EditorAssetLibrary refuses to run while PIE is up
    has_imc = (ue("py", f"unreal.load_asset('{imc}') is not None")[1].splitlines() or [""])[-1] == "True"
    if has_imc:
        ue("py", f"from tether import toolsets as t\nt._input_subsystem(tether.world()).add_mapping_context("
                 f"unreal.load_asset('{imc}'), 0, unreal.ModifyContextOptions())")
    check("pie do raw keys -> action value", ["pie", "do", "key W down", "wait 0.5", "key W up", "wait 0.3",
                                               "--watch", "IA_TetherMove"],
          skip=None if has_imc else "no input fixture (Sandbox's IMC_TetherProbe)",
          ok=lambda c, o: c == 0 and "(0, 1)" in o)
    check("ui at", ["ui", "at", "5,5"], skip=None if "SlateInspectorToolset.SlateInspectorToolset" in toolsets
          else "no Epic Slate inspector", ok=lambda c, o: c == 0 and "window" in o)
    check("log --since pie", ["log", "--since", "pie", "--level", "Display"],
          ok=lambda c, o: c == 0 and "since the last PIE start" in o)
    check("gpu", ["gpu", "--frames", "1"], timeout=300, skip="needs 5.8" if remote else None if rendering else
          "headless with -nullrhi (nothing renders)", ok=lambda c, o: c == 0 and "GPU " in o and "by kind:" in o)
    check("trace (player view)", ["trace", "--distance", "2000"], ok=lambda c, o: c == 0 and "trace on Visibility" in o)
    check("ui", ["ui"], ok=lambda c, o: c == 0)
    check("ai", ["ai"], ok=lambda c, o: c == 0 and ("AI" in o))
    check("audio", ["audio"], skip=None if launch.get("sound", not launch.get("headless")) else "no audio device (ue open --headless --sound)",
          ok=lambda c, o: c == 0 and "sound(s) playing" in o)
    check("audio --log", ["audio", "--log", "1"], skip=None if launch.get("sound", not launch.get("headless")) else
          "no audio device (ue open --headless --sound)", ok=lambda c, o: c == 0 and "audio log, " in o)
    check("anim", ["anim", "player"], ok=lambda c, o: c == 0 and ("skeletal mesh" in o or "anim instance" in o
                                                                   or "mesh " in o))
    fixture = (ue("py", "hasattr(unreal, 'TetherProbeUILibrary')")[1].splitlines() or [""])[-1] == "True"
    check("ui click (Sandbox fixture)", ["pie", "do", "call /Script/Sandbox.Default__TetherProbeUILibrary ShowProbeUI",
                                         "expect-ui Tether probe panel", "click UMG Probe Button", "expect-ui UMG clicks: "],
          skip=None if fixture else "no UI fixture (Sandbox's TetherProbeUILibrary)", ok=lambda c, o: c == 0 and "PASS" in o)
    menu = (ue("py", "hasattr(unreal.TetherProbeUILibrary, 'show_probe_menu')")[1].splitlines() or [""])[-1] == "True" \
        if fixture else False
    # a title screen holding focus itself, then rows of text-less buttons inside nested user widgets
    check("ui menu keys and clicks (Sandbox fixture)",
          ["pie", "do", "call /Script/Sandbox.Default__TetherProbeUILibrary ShowProbeMenu", "key Enter",
           "expect-ui GARAGE", "key Down", "key Enter", "click --event QUIT", "click GARAGE",
           "log TetherProbeMenu: FREE ROAM clicked", "log TetherProbeMenu: QUIT clicked",
           "log TetherProbeMenu: GARAGE clicked"],
          skip=None if menu else "no menu fixture (Sandbox's ShowProbeMenu)", ok=lambda c, o: c == 0 and "PASS" in o)
    check("shot --hud", ["shot", "--hud", "-o", os.path.join(tempfile.gettempdir(), "tether-selftest-hud.png")],
          skip="needs 5.8" if remote else None if rendering else "headless with -nullrhi",
          ok=lambda c, o: c == 0 and "(PIE, with HUD)" in o)
    check("shot --at (PIE spectator)", ["shot", "--at", "0,-300,200", "--look-at", "0,0,0", "-o",
                                        os.path.join(tempfile.gettempdir(), "tether-selftest-spectator.png")],
          skip="needs 5.8" if remote else None if rendering else "headless with -nullrhi",
          ok=lambda c, o: c == 0 and "(PIE)" in o and "camera at (0, -300, 200)" in o)
    check("pie status (view restored)", ["py", "from tether import toolsets as t\n"
                                         "type(unreal.GameplayStatics.get_player_controller(t._game_world(), 0)).__name__"],
          ok=lambda c, o: c == 0 and not o.rstrip().endswith("DebugCameraController"))
    check("bp trace", ["bp", "trace", "--seconds", "1", "--keep"], timeout=600, skip="needs 5.8" if remote else None,
          ok=lambda c, o: c == 0 and "Blueprint function(s)/event(s) ran" in o)
    check("pie stop", ["pie", "stop"], ok=lambda c, o: c == 0 and "OK" in o)
    net_fixture = (ue("py", "hasattr(unreal, 'TetherProbeNetGameMode')")[1].splitlines() or [""])[-1] == "True"
    clients = check("pie --clients 2", ["pie", "start", "--clients", "2"]
                    + (["--game", "/Script/Sandbox.TetherProbeNetGameMode"] if net_fixture else []),
                    timeout=300, skip="needs UE 5.8+" if remote else None, ok=lambda c, o: c == 0 and "1: client" in o)
    check("pie target 1", ["pie", "target", "1"], skip=None if clients else "no multiplayer session",
          ok=lambda c, o: c == 0 and "PIE world 1" in o)
    check("exec --pie: client RPC reaches the server", ["exec", "--pie", "TetherPing 42"],
          skip=None if clients and net_fixture else "no net fixture (Sandbox's TetherProbeNetGameMode)",
          ok=lambda c, o: c == 0 and "server got ping 42" in o and "on the listen server" in o)
    check("net compare", ["net", "compare", "player.location"], skip=None if clients else "no multiplayer session",
          ok=lambda c, o: c in (0, 1) and "2 PIE world(s)" in o)
    check("pie stop (restores)", ["pie", "stop"], skip=None if clients else "no multiplayer session",
          ok=lambda c, o: c == 0 and "restored" in o)
    game = check("game start (listen + 1 client)", ["game", "start", "--clients", "1", "--wait", "180"]
                 + (["--map", "/Game/TetherFixtures/L_TetherProbe", "--game", "/Script/Sandbox.TetherProbeNetGameMode"]
                    if net_fixture else []), timeout=400,
                 skip=None if net_fixture else "no net fixture (Sandbox's TetherProbeNetGameMode)",
                 ok=lambda c, o: c == 0 and "1 of 1 client(s) joined" in o)
    check("game stop", ["game", "stop"], skip=None if game else "no game session",
          ok=lambda c, o: c == 0 and "stopped" in o)
    if args.reload:
        check("reload", ["reload"])
        time.sleep(1.5)
        check("py after reload", ["py", "1+1"], ok=lambda c, o: c == 0 and o.endswith("2"))

    width = max(len(name) for _, name, _, _ in results)
    for status, name, detail, took in results:
        print(f"{status}  {name.ljust(width)}  {took:5.1f}s" + (f"  {detail}" if detail else ""))
    counts = {s: sum(1 for r in results if r[0] == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    if args.json:
        print(json.dumps([{"check": n, "status": s, "detail": d, "seconds": round(t, 2)} for s, n, d, t in results]))
    return 1 if counts["FAIL"] else 0


def cmd_ci(ctx, args):
    """A project's checks in one pass/fail command: open (if needed), tests, Blueprint compile, cook, close."""
    project, name = ctx.project, os.path.splitext(os.path.basename(ctx.project))[0]
    base = [sys.executable, UE_SCRIPT, "--project", project]
    steps, started = [], time.time()

    def step(label, argv, timeout=3600, required=True):
        t0 = time.time()
        try:
            proc = subprocess.run(base + argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
            code, out = proc.returncode, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            code, out = 4, f"no result within {timeout}s"
        steps.append({"step": label, "ok": code == 0, "exit": code, "seconds": round(time.time() - t0, 1),
                      "output": out[-4000:], "required": required})
        status = ("PASS" if code == 0 else "FAIL") if required else ("INFO" if code == 0 else "WARN")
        summary = out.splitlines()[0] if code == 0 and out else (out.splitlines()[-1] if out else f"exit {code}")
        print(f"{status}  {label:<12} {time.time() - t0:6.1f}s  {summary[:150]}")
        if code and args.verbose:
            for line in out.splitlines()[-15:]:
                print("      " + line[:200])
        return code == 0

    opened = False
    if not editor_live(project):
        opened = step("open", ["open", "--headless"], timeout=1800)
        if not opened:
            return finish_ci(args, steps, started)
    try:
        test_filter = args.tests or f"^{name}."
        listing = subprocess.run(base + ["test", "--list", test_filter], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=600)
        if listing.returncode == 0 and not listing.stdout.startswith("0 tests"):
            step("tests", ["test", test_filter])
        else:
            print(f"SKIP  tests        no tests match {test_filter}" + (" (--tests to choose)" if not args.tests else ""))
        step("blueprints", ["bp", "check", args.bp or "/Game"])
        if args.play is not None:  # scripted play tests: `ue pie do` step files
            import glob
            root = os.path.dirname(project)
            pattern = args.play or next((p for p in ("tests/play/*.txt", "Tests/Play/*.txt")
                                         if glob.glob(os.path.join(root, p))), "tests/play/*.txt")
            files = sorted(glob.glob(pattern if os.path.isabs(pattern) else os.path.join(root, pattern)))
            if not files:
                print(f"SKIP  play         no step files match {pattern}")
            for path in files:
                step(f"play {os.path.splitext(os.path.basename(path))[0]}", ["pie", "do", "-f", path], timeout=900)
        if getattr(args, "budget", False):
            step("budget", ["budget"], timeout=1800)
        if args.validate:
            step("validate", ["validate", args.validate, "--map-check"])
        if args.cook:
            if step("cook start", ["job", "cook"], timeout=120):
                step("cook", ["job", "wait", "--wait", str(args.cook_timeout)], timeout=args.cook_timeout + 60)
        step("errors", ["log", "--since", "start", "--level", "Error"], required=False)
    finally:
        if opened:
            step("close", ["close", "--discard"], required=False)
    return finish_ci(args, steps, started)


def finish_ci(args, steps, started):
    failed = [s["step"] for s in steps if s["required"] and not s["ok"]]
    print(f"{'FAILED' if failed else 'PASSED'} in {time.time() - started:.0f}s" + (": " + ", ".join(failed) if failed else ""))
    if args.report:
        write_json(os.path.abspath(args.report), {"passed": not failed, "failed": failed, "steps": steps,
                                                  "seconds": round(time.time() - started, 1)})
    return 1 if failed else 0
