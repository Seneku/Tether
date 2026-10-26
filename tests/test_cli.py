"""Unit tests for the ue CLI (cli/tether_cli): the parts that need no editor (parsing, filtering, routing, hooks)."""
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import tether_cli as ue  # noqa: E402


_NAME_PATCH = unittest.mock.patch("tether_cli.core.process_name", return_value=None)


def setUpModule():
    # Tests stand in for editors with real pids (this process, its parent); don't let the image-name check
    # (python.exe is not an editor) call them dead. Beacons.test_* exercises that check itself.
    _NAME_PATCH.start()


def tearDownModule():
    _NAME_PATCH.stop()


def stamped(text):
    return f"[2026.10.12-10.13.00:858][398]{text}"


class Project:
    """A throwaway project folder with a .uproject, Source/ and Saved/."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.uproject = os.path.join(self.root, "Game.uproject")
        with open(self.uproject, "w") as f:
            f.write('{"EngineAssociation": "5.8"}')
        os.makedirs(os.path.join(self.root, "Saved", "Logs"))

    def write(self, rel, text="", mtime=None):
        path = os.path.join(self.root, os.path.normpath(rel))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def close(self):
        self.tmp.cleanup()


class Rendering(unittest.TestCase):
    def test_unwrap_return_value_holding_json(self):
        self.assertEqual(ue.unwrap('{"returnValue": "{\\"a\\": 1}"}'), {"a": 1})
        self.assertEqual(ue.unwrap('{"returnValue": 3}'), 3)
        self.assertEqual(ue.unwrap("plain text"), "plain text")
        self.assertEqual(ue.unwrap('{"x": 1, "y": 2}'), {"x": 1, "y": 2})

    def test_compact_error_drops_schema_and_whitespace(self):
        text = "Bad   input\n  here\nFunction schema Json {...huge...}"
        self.assertEqual(ue.compact_error(text), "Bad input here")
        self.assertLessEqual(len(ue.compact_error("x" * 2000)), 600)

    def test_strip_common(self):
        self.assertEqual(ue.strip_common(["/Game/A/B.B", "/Game/A/C.C"]), ("/Game/A/", ["B.B", "C.C"]))
        self.assertEqual(ue.strip_common(["only"]), ("", ["only"]))

    def test_render_ref_paths_and_limit(self):
        value = [{"refPath": f"/Game/Maps/L.L:PersistentLevel.Actor{i}"} for i in range(5)]
        out = ue.render(value, 2).splitlines()
        self.assertEqual(out[0], "5 items under /Game/Maps/L.L:PersistentLevel.")
        self.assertEqual(out[1:3], ["Actor0", "Actor1"])
        self.assertIn("+3 more", out[-1])
        self.assertEqual(ue.render({"a": 1, "b": [1, 2]}, 10), "a: 1\nb: [1,2]")


class ToolSchemas(unittest.TestCase):
    TOOL = {"name": "editor_toolset.toolsets.scene.SceneTools.find_actors",
            "description": "Find actors by tag. Returns paths. More text.",
            "inputSchema": {"properties": {"tag": {"type": "string"}, "classes": {"type": "array"},
                                           "world": {"type": "object", "properties": {"refPath": {}}},
                                           "limit": {"type": "integer"}},
                            "required": ["tag", "classes", "world"]}}

    def test_signature_marks_optional_and_keeps_first_sentence(self):
        self.assertEqual(ue.signature(self.TOOL), "find_actors(tag, classes, world, limit?) - Find actors by tag.")

    def test_fill_args_parses_json_wraps_refs_and_fills_blanks(self):
        args = ue.fill_args(self.TOOL, {"limit": "5", "world": "/Game/Maps/L.L"})
        self.assertEqual(args, {"limit": 5, "world": {"refPath": "/Game/Maps/L.L"}, "tag": "", "classes": []})

    def test_fill_args_keeps_string_params_as_text(self):
        self.assertEqual(ue.fill_args(self.TOOL, {"tag": '{"a": 1}'})["tag"], '{"a": 1}')
        self.assertEqual(ue.fill_args(self.TOOL, {"tag": "5"})["tag"], "5")

    @unittest.mock.patch.dict(os.environ, {"MSYSTEM": "MINGW64"})
    def test_undo_msys_paths(self):
        if os.name != "nt":
            self.skipTest("Git Bash rewriting only happens on Windows")
        with unittest.mock.patch("shutil.which", return_value=r"C:\Program Files\Git\usr\bin\bash.EXE"):
            out = ue.undo_msys_paths(["props", "C:/Program Files/Git/Game/Maps/L.L", "graph=C:/Program Files/Git/Game/BP.BP:G",
                                      "D:/Projects/Game/Source", "C:/Program Files/Git/usr/bin", "C:/Program Files/Git/Game",
                                      "under=C:/Program Files/Git/Engine", "C:/Program Files/Git/Niagara/DefaultAssets",
                                      "C:/Program Files/Git/mingw64/bin", "C:/Program Files/Git/game/x"])
        # Plugin mount points (/Niagara) are capitalised like /Game; Git's own folders are lowercase.
        self.assertEqual(out, ["props", "/Game/Maps/L.L", "graph=/Game/BP.BP:G", "D:/Projects/Game/Source",
                               "C:/Program Files/Git/usr/bin", "/Game", "under=/Engine", "/Niagara/DefaultAssets",
                               "C:/Program Files/Git/mingw64/bin", "/game/x"])


class Logs(unittest.TestCase):
    def test_filter_log_levels_dedup_and_noise(self):
        lines = [
            stamped("LogTemp: Warning: careful"),
            stamped("LogTemp: Warning: careful"),
            stamped("LogTemp: Error: broken"),
            stamped("LogTemp: Display: hello"),
            stamped("LogModelContextProtocol: Warning: transport chatter"),
            stamped('LogJson: Warning: Property "OnHit" type FHitSignature unhandled during Json schema generation.'),
            stamped("LogJson: Warning: a real problem"),
            stamped("Cmd: FLUSHLOG"),
            "not a log line",
        ]
        self.assertEqual(ue.filter_log(lines), ["W LogTemp: careful  x2", "E LogTemp: broken", "W LogJson: a real problem"])
        self.assertEqual(ue.filter_log(lines, level="Error"), ["E LogTemp: broken"])
        self.assertEqual(ue.filter_log(lines, level="Display", category="logtemp", pattern="hel+o"), ["D LogTemp: hello"])

    def test_error_gets_users_csharp_frame(self):
        lines = [stamped("LogUnrealSharp: Error: NullReferenceException"),
                 r"   at UnrealSharp.Glue.Thing() in D:\P\Plugins\UnrealSharp\Glue.cs:line 3",
                 r"   at ManagedGame.Coins.Pickup.OnOverlap(Actor a) in D:\P\Script\Coins\Pickup.cs:line 42"]
        self.assertEqual(ue.filter_log(lines, level="Error"),
                         ["E LogUnrealSharp: NullReferenceException @ Pickup.cs:42 (Pickup.OnOverlap)"])

    def test_logged_dialogs(self):
        lines = [stamped("LogSlate: Message dialog closed, result: Ok, title: Save, text: Could not save"),
                 "  second line", stamped("LogTemp: next")]
        self.assertEqual(ue.logged_dialogs(lines), [("Save", "Could not save\n  second line")])

    def test_read_new_log_cursor_keeps_partial_line(self):
        project = Project()
        try:
            log = ue.log_path(project.uproject)
            with open(log, "wb") as f:
                f.write(b"LogA: one\nLogA: two\nLogA: par")
            self.assertEqual(ue.read_new_log(project.uproject), ["LogA: one", "LogA: two"])
            with open(log, "ab") as f:
                f.write(b"tial\n")
            self.assertEqual(ue.read_new_log(project.uproject), ["LogA: partial"])
            self.assertEqual(ue.read_new_log(project.uproject), [])
            self.assertEqual(len(ue.read_new_log(project.uproject, from_start=True)), 3)
        finally:
            project.close()


class Builds(unittest.TestCase):
    def test_compact_diagnostics_relative_paths_hidden_engine_warnings_linker(self):
        project = Project()
        try:
            mine = os.path.join(project.root, "Source", "Game", "Thing.cpp")
            engine = os.path.join(os.sep, "Engine", "Source", "Runtime", "Core.h")
            lines = [f"{mine}(12): error C2065: 'Foo': undeclared identifier",
                     f"{mine}(12): error C2065: 'Foo': undeclared identifier",
                     f"{engine}(5): warning C4996: deprecated",
                     f"  [Compile] {mine}(30,7): warning C4101: unreferenced local [Game.vcxproj]",
                     "Thing.obj : error LNK2019: unresolved external symbol Bar"]
            errors, warnings, hidden = ue.compact_diagnostics(lines, project.root)
            rel = os.path.join("Source", "Game", "Thing.cpp")
            self.assertEqual(errors, [f"{rel}:12: C2065 'Foo': undeclared identifier",
                                      "LNK2019 unresolved external symbol Bar"])
            self.assertEqual(warnings, [f"{rel}:30: C4101 unreferenced local"])
            self.assertEqual(hidden, 1)
        finally:
            project.close()

    def test_clang_and_posix_linker_diagnostics(self):
        project = Project()
        try:
            mine = os.path.join(project.root, "Source", "Game", "Thing.cpp")
            lines = [f"{mine}:12:5: error: use of undeclared identifier 'Foo'",
                     f"{mine}:30:9: warning: unused variable 'x' [-Wunused-variable]",
                     "/Engine/Source/Runtime/Core.h:5:1: warning: deprecated [-Wdeprecated-declarations]",
                     "ld.lld: error: undefined symbol: Bar"]
            errors, warnings, hidden = ue.compact_diagnostics(lines, project.root)
            rel = os.path.join("Source", "Game", "Thing.cpp")
            self.assertEqual(errors, [f"{rel}:12: use of undeclared identifier 'Foo'", "ld.lld: undefined symbol: Bar"])
            self.assertEqual(warnings, [f"{rel}:30: -Wunused-variable unused variable 'x'"])
            self.assertEqual(hidden, 1)
        finally:
            project.close()

    def test_platform_paths(self):
        root = os.path.join(os.sep, "UE")
        cases = {"win32": ("Win64", "UnrealEditor.exe"), "darwin": ("Mac", "UnrealEditor"), "linux": ("Linux", "UnrealEditor")}
        for platform, (name, exe) in cases.items():
            with self.subTest(platform=platform), unittest.mock.patch.object(ue.sys, "platform", platform), \
                    unittest.mock.patch.object(ue.os, "name", "nt" if platform == "win32" else "posix"):
                self.assertEqual(ue.host_platform(), name)
                self.assertTrue(ue.editor_executable(root).endswith(exe))
                self.assertEqual(ue.live_coding_available(), platform == "win32")
                command = ue.ubt_command(root)
                if platform == "win32":
                    self.assertTrue(command[0].endswith("UnrealBuildTool.exe"))
                else:
                    self.assertEqual((command[0], os.path.basename(command[1])), ("sh", "Build.sh"))
                    self.assertIn(name, command[1])

    def test_unity_compile_detection(self):
        log = "[1/2] Compile [x64] Cheats.cpp\n[2/2] Compile [x64] Module.Game.cpp\n[1/1] Compile [x64] Module.Game.2.cpp"
        self.assertEqual(ue.UNITY_COMPILE.findall(log), ["Module.Game.cpp", "Module.Game.2.cpp"])

    def test_classify_changes(self):
        project = Project()
        try:
            self.assertIsNone(ue.classify_changes(project.uproject))  # no green build yet
            old = time.time() - 100
            os.utime(project.uproject, (old, old))
            project.write("Source/Game/Old.cpp", "", mtime=old)
            ue.mark_green(project.uproject)
            body = project.write("Source/Game/Body.cpp", "int f() { return 1; }")
            header = project.write("Source/Game/Thing.h", "UCLASS()\nclass UThing : public UObject {};")
            plain = project.write("Source/Game/Plain.h", "struct Plain {};")
            build = project.write("Source/Game/Game.Build.cs", "")
            future = time.time() + 5
            for path in (body, header, plain, build):
                os.utime(path, (future, future))
            changed, module, reflected = ue.classify_changes(project.uproject)
            self.assertEqual(sorted(changed), sorted([body, header, plain, build]))
            self.assertEqual(module, [build])
            self.assertEqual(reflected, [header])
        finally:
            project.close()


class Tests(unittest.TestCase):
    def test_location_regex(self):
        line = stamped(r"LogAutomationController: Error: Expected 'x' to be 1, but it was 2. "
                       r"[D:\P\Source\Game\Tests.cpp(23)]")
        m = ue.TEST_LOCATION.search(line)
        self.assertEqual(m.groups(), ("Expected 'x' to be 1, but it was 2.", r"D:\P\Source\Game\Tests.cpp", "23"))

    def test_test_locations_relative_to_project(self):
        project = Project()
        try:
            src = os.path.join(project.root, "Source", "Game", "Tests.cpp")
            with open(ue.log_path(project.uproject), "w", encoding="utf-8") as f:
                f.write(stamped(f"LogAutomationController: Error: boom [{src}(9)]") + "\n")
            ctx = argparse.Namespace(project=project.uproject, beacon=None)
            self.assertEqual(ue.test_locations(ctx, 0), {"boom": os.path.join("Source", "Game", "Tests.cpp") + ":9"})
        finally:
            project.close()


class FakeCtx:
    def __init__(self, reply=""):
        self.calls, self.reply = [], reply

    def tether(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        return self.reply


class Commands(unittest.TestCase):
    def test_set_builds_values_and_keeps_deltas(self):
        ctx = FakeCtx("Light\nintensity: 1 -> 2")
        args = argparse.Namespace(target="Light", assignments=["intensity=2", "location+=0,0,10", "name=a=b"], pie=False,
                                  from_file=None, dry_run=False)
        with unittest.mock.patch("builtins.print"):
            self.assertEqual(ue.cmd_set(ctx, args), 0)
        tool, kwargs = ctx.calls[0]
        self.assertEqual(tool, "set_props")
        self.assertEqual(json.loads(kwargs["values"]), {"intensity": "2", "location+": "0,0,10", "name": "a=b"})

    def test_set_rejects_missing_equals(self):
        args = argparse.Namespace(target="Light", assignments=["intensity"], pie=False, from_file=None, dry_run=False)
        with self.assertRaises(ue.CliError):
            ue.cmd_set(FakeCtx(), args)

    def test_set_error_drops_epics_object_path(self):
        class Failing(FakeCtx):
            def tether(self, tool, **kwargs):
                raise ue.CliError("SetObjectProperties on '/Temp/L.L:PersistentLevel.A.C' (LightComponent): the "
                                  "following properties could not be set: mobility invalid value (see ue log) (rolled back)")
        args = argparse.Namespace(target="A.C", assignments=["mobility=Wobbly"], pie=False, from_file=None, dry_run=False)
        with self.assertRaises(ue.CliError) as caught:
            ue.cmd_set(Failing(), args)
        self.assertEqual(str(caught.exception), "LightComponent.mobility: invalid value (see ue log) (rolled back)")


class Hooks(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        os.makedirs(os.path.join(self.project.root, "Plugins", "UnrealSharp"))
        self.project.write("Script/Managed.sln")
        self.cs = os.path.join(self.project.root, "Script", "Managed", "Coin.cs")

    def tearDown(self):
        self.project.close()

    def beacon(self, **fields):
        ue.write_json(os.path.join(ue.state_dir(self.project.uproject), "editor.json"),
                      dict({"pid": os.getpid(), "heartbeat": time.time()}, **fields))

    def test_pre_edit_denies_csharp_during_pie_only(self):
        payload = {"tool_input": {"file_path": self.cs}}
        self.beacon(pie=True)
        self.assertIn("Play-In-Editor", ue.hook_pre_edit(payload))
        self.assertIsNone(ue.hook_pre_edit({"tool_input": {"file_path": self.cs.replace(".cs", ".txt")}}))
        self.beacon(pie=False)
        self.assertIsNone(ue.hook_pre_edit(payload))

    def test_hook_command_prints_deny_json_and_never_raises(self):
        self.beacon(pie=True)
        out = []
        with unittest.mock.patch("sys.stdin", new=__import__("io").StringIO(json.dumps({"tool_input": {"file_path": self.cs}}))), \
                unittest.mock.patch("builtins.print", side_effect=out.append):
            self.assertEqual(ue.cmd_hook(None, argparse.Namespace(event="pre-edit")), 0)
        decision = json.loads(out[0])["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        with unittest.mock.patch("sys.stdin", new=__import__("io").StringIO("not json")), \
                unittest.mock.patch("builtins.print") as printed:
            self.assertEqual(ue.cmd_hook(None, argparse.Namespace(event="pre-edit")), 0)
        printed.assert_not_called()


class Init(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        with open(self.project.uproject, "w", encoding="utf-8", newline="") as f:
            f.write('{\r\n\t"FileVersion": 3,\r\n\t"Plugins": [\r\n\t\t{\r\n\t\t\t"Name": "PythonScriptPlugin",'
                    '\r\n\t\t\t"Enabled": false\r\n\t\t}\r\n\t]\r\n}\r\n')
        self.ctx = argparse.Namespace(project=self.project.uproject, transport="mcp")

    def tearDown(self):
        self.project.close()

    def test_older_engine_gets_remote_execution_not_mcp_plugins(self):
        self.ctx.transport = "remote"
        self.project.write("Config/DefaultEngine.ini", "[/Script/EngineSettings.GameMapsSettings]\nGameDefaultMap=/Game/M\n")
        out = self.run_init()
        self.assertIn("enable Python remote execution in Config/DefaultEngine.ini (how ue reaches UE < 5.8)", out)
        self.assertEqual(self.plugins(), {"PythonScriptPlugin": True, "Tether": True})
        with open(os.path.join(self.project.root, "Config", "DefaultEngine.ini"), encoding="utf-8") as f:
            ini = f.read()
        self.assertIn("GameDefaultMap=/Game/M", ini)
        self.assertIn("[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nbRemoteExecution=True\n", ini)
        self.assertEqual(self.run_init(), ["already set up: plugins enabled, Tether reachable"])

    def run_init(self, **kw):
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda *a: out.append(" ".join(map(str, a)))):
            ue.cmd_init(self.ctx, argparse.Namespace(**dict({"unrealsharp": False, "dry_run": False,
                                                            "plugin_check": False}, **kw)))
        return out

    def raw(self):
        with open(self.project.uproject, "rb") as f:
            return f.read()

    def plugins(self):
        with open(self.project.uproject, encoding="utf-8") as f:
            return {e["Name"]: e["Enabled"] for e in json.load(f)["Plugins"]}

    def test_dry_run_changes_nothing(self):
        before = self.raw()
        out = self.run_init(dry_run=True)
        self.assertIn("would re-enable PythonScriptPlugin (was disabled)", out)
        self.assertEqual(self.raw(), before)

    def test_enables_plugins_copies_tether_keeps_crlf_and_is_idempotent(self):
        self.run_init()
        self.assertEqual(self.plugins(), dict.fromkeys(ue.REQUIRED_PLUGINS + ["NiagaraToolsets"], True))  # Niagara is on by default
        self.assertTrue(os.path.isfile(os.path.join(self.project.root, "Plugins", "Tether", "Tether.uplugin")))
        self.assertFalse(os.path.exists(os.path.join(self.project.root, "Plugins", "TetherUnrealSharp")))
        self.assertIn(b"\r\n\t\"FileVersion\": 3", self.raw())
        self.assertEqual(self.run_init(), ["already set up: plugins enabled, Tether reachable"])

    def test_all_toolsets_covers_epic_toolsets(self):
        with open(self.project.uproject, "w", encoding="utf-8") as f:
            json.dump({"Plugins": [{"Name": "AllToolsets", "Enabled": True}]}, f)
        self.run_init(dry_run=True)
        out = self.run_init()
        self.assertNotIn("enable EditorToolset", out)
        self.assertNotIn("EditorToolset", self.plugins())


class RemoteResults(unittest.TestCase):
    """RemoteExec.tether against canned command_result payloads (the protocol's shape)."""

    def remote(self, data):
        rex = ue.RemoteExec("Game.uproject", 5)
        rex.mode = "ExecuteFile"
        rex.rex = types.SimpleNamespace(run_command=lambda code, **kw: dict(data, command=code))
        return rex

    def test_value_after_marker(self):
        rex = self.remote({"success": True, "output": [{"type": "Info", "output": "noise"},
                                                      {"type": "Info", "output": '\x1eTETHER"2 actors"\r'}]})
        self.assertEqual(rex.tether("actors", {"match": "light"}), "2 actors")

    def test_error_keeps_message_without_exception_type(self):
        rex = self.remote({"success": False, "output": [],
                           "result": 'Traceback (most recent call last):\n  File "<string>"\nRuntimeError: not found: X\n'})
        with self.assertRaises(ue.CliError) as caught:
            rex.tether("props", {"target": "X"})
        self.assertEqual(str(caught.exception), "not found: X")


class Phase3(unittest.TestCase):
    def test_flat_row(self):
        row = {"item": {"ItemId": "Battery", "DisplayName": 'NSLOCTEXT("DT_Items [F7]", "Battery_Name", "Battery")',
                        "Icon": "None", "MaxStack": 5}, "weight": 1.5}
        self.assertEqual(ue.flat_row(row), {"item.ItemId": "Battery", "item.DisplayName": "Battery",
                                            "item.MaxStack": 5, "weight": 1.5})

    def test_same_value(self):
        self.assertTrue(ue.same_value({"r": 1, "g": 0.8}, {"r": 1, "g": 0.8000001}))
        self.assertFalse(ue.same_value({"r": 1, "g": 0.8}, {"r": 1, "g": 0.9}))
        self.assertFalse(ue.same_value({"r": 1}, {"r": 1, "g": 0}))
        self.assertTrue(ue.same_value([1, 2.004], [1, 2]))
        self.assertFalse(ue.same_value(True, 1))
        self.assertTrue(ue.same_value("Movable", "Movable"))

    def test_profile_csv_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Profile.csv")
            with open(path, "w", newline="") as f:
                f.write("EVENTS,FrameTime,GameThreadTime,GPUTime\n")
                for ms in (8, 8, 9, 40, 8):
                    f.write(f",{ms},5.5,2.0\n")
                f.write("EVENTS,FrameTime,GameThreadTime,GPUTime\n[HasHeaderRowAtEnd],1,[platform],Windows\n")
            columns = ue.read_profile_csv(path)
        self.assertEqual(columns["FrameTime"], [8, 8, 9, 40, 8])
        stats = ue.frame_stats(columns["FrameTime"])
        self.assertEqual((stats["p50"], stats["max"]), (8, 40))
        self.assertAlmostEqual(stats["avg"], 14.6)

    def test_compiler_line(self):
        line = stamped(r"LogBlueprint: Error: [AssetLog] D:\P\Content\BP_X.uasset: [Compiler] Could not find a "
                       "function named \"GetProbeCount\" in 'SandboxProbeLibrary'.")
        self.assertEqual(ue.COMPILER_LINE.search(line).group(1), "Error")
        self.assertIn("BP_X", ue.COMPILER_LINE.search(line).group(2))

    def test_asset_ref(self):
        self.assertEqual(ue.asset_ref("/Game/A/BP_X"), "/Game/A/BP_X.BP_X")
        self.assertEqual(ue.asset_ref("/Game/A/BP_X.BP_X"), "/Game/A/BP_X.BP_X")
        self.assertEqual(ue.asset_ref("/Game/A/BP_X:EventGraph"), "/Game/A/BP_X.BP_X:EventGraph")

    def test_read_table_file_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            as_dict, as_list, as_csv = (os.path.join(tmp, n) for n in ("a.json", "b.json", "c.csv"))
            with open(as_dict, "w") as f:
                json.dump({"Health": {"maxValue": 100}}, f)
            with open(as_list, "w") as f:
                json.dump([{"Name": "Health", "maxValue": 100}], f)
            with open(as_csv, "w", newline="") as f:
                f.write('Name,maxValue,tag,nested\nHealth,100,boss,"{""x"": 1}"\n')
            expected = {"Health": {"maxValue": 100}}
            self.assertEqual(ue.read_table_file(as_dict), expected)
            self.assertEqual(ue.read_table_file(as_list), expected)
            self.assertEqual(ue.read_table_file(as_csv), {"Health": {"maxValue": 100, "tag": "boss", "nested": {"x": 1}}})

    def test_text_diff(self):
        diff = ue.text_diff("(event A)\n", "(event A\n  (Print))\n", "EventGraph")
        self.assertIn("-(event A)", diff)
        self.assertIn("+  (Print))", diff)

    def test_job_stuck_on_a_repeating_dialog_in_the_commandlet_log(self):
        with tempfile.TemporaryDirectory() as folder:
            cook_log = os.path.join(folder, "Cook.txt")
            with open(os.path.join(folder, "1.log"), "w") as f:
                f.write(f"Running UAT\nCommandlet log file is {cook_log}\n")
            with open(cook_log, "w") as f:
                f.write("LogInit: starting\n")
            self.assertIsNone(ue.job_stuck(folder, "1"))
            with open(cook_log, "a") as f:
                for _ in range(12):
                    f.write("Message dialog closed, result: Ok, title: Message, text: UnrealSharp can't be initialized.\n")
            self.assertIn("stuck on a repeating dialog (12x", ue.job_stuck(folder, "1"))

    def test_job_runs_in_background_and_records_exit(self):
        project = Project()
        out = []
        try:
            def run(*argv):
                with unittest.mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                    return ue.main(["--project", project.uproject, "job", *argv])
            self.assertEqual(run("start", "--name", "ok", "--", sys.executable, "-c", "print('hello')"), 0)
            self.assertEqual(run("wait", "--wait", "60"), 0)
            self.assertIn("  ok  done", out[-1])
            self.assertEqual(run("start", "--", sys.executable, "-c", "import sys; print('Error: bad'); sys.exit(4)"), 0)
            time.sleep(1.1)  # job ids have one-second resolution
            self.assertEqual(run("wait", "--wait", "60"), 1)
            self.assertTrue(any("FAILED (exit 4)" in line for line in out))
        finally:
            time.sleep(0.5)  # let the detached runner close its files before cleanup
            project.close()


class Ci(unittest.TestCase):
    def run_ci(self, codes, report, validate=None):
        """cmd_ci against a running editor, with each step's exit code scripted by subcommand."""
        def fake_run(argv, **kw):
            sub = argv[argv.index("--project") + 2:]
            code, out = codes.get(sub[0], (0, "ok"))
            if sub[:2] == ["test", "--list"]:
                code, out = 0, "1 tests matching '^Game.'"
            return types.SimpleNamespace(returncode=code, stdout=out, stderr="")
        project = Project()
        try:
            args = argparse.Namespace(tests=None, bp=None, validate=validate, play=None, cook=False, cook_timeout=60, report=report, verbose=False)
            ctx = argparse.Namespace(project=project.uproject)
            with unittest.mock.patch.object(ue.selftest, "editor_live", return_value={"pid": 1}), \
                    unittest.mock.patch.object(ue.subprocess, "run", side_effect=fake_run), \
                    unittest.mock.patch("builtins.print"):
                return ue.cmd_ci(ctx, args)
        finally:
            project.close()

    def test_failing_step_fails_the_run_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "ci.json")
            self.assertEqual(self.run_ci({"bp": (1, "1 Blueprint ... 1 with problems")}, report), 1)
            data = ue.read_json(report)
            self.assertEqual((data["passed"], data["failed"]), (False, ["blueprints"]))
            self.assertEqual(self.run_ci({"log": (1, "log unreadable")}, report), 0)  # informational only
            self.assertTrue(ue.read_json(report)["passed"])
            self.assertEqual(self.run_ci({"validate": (1, "1 invalid")}, report, validate="/Game"), 1)
            self.assertEqual(ue.read_json(report)["failed"], ["validate"])


class Crashes(unittest.TestCase):
    def test_ensure_reports_are_not_crashes(self):
        project = Project()
        try:
            for name, kind in (("UECC-A_0000", "Crash"), ("UECC-B_0000", "Ensure")):
                folder = os.path.join(project.root, "Saved", "Crashes", name)
                os.makedirs(folder)
                ticks = int((time.time() + (10 if kind == "Ensure" else 0)) * 1e7) + 621355968000000000
                with open(os.path.join(folder, "CrashContext.runtime-xml"), "w") as f:
                    f.write(f"<CrashType>{kind}</CrashType><TimeOfCrash>{ticks}</TimeOfCrash><ErrorMessage>{kind} x</ErrorMessage>")
            self.assertEqual(ue.recent_crash(project.uproject, time.time() - 60)["type"], "Crash")
        finally:
            project.close()

    def test_parse_describe_and_recent(self):
        project = Project()
        try:
            folder = os.path.join(project.root, "Saved", "Crashes", "UECC-Windows-ABC_0000")
            os.makedirs(folder)
            when = 1791800000  # epoch seconds, written as .NET ticks
            with open(os.path.join(folder, "CrashContext.runtime-xml"), "w") as f:
                f.write(f"<FGenericCrashContext><RuntimeProperties><CrashType>Crash</CrashType>"
                        f"<ErrorMessage>Unhandled Exception: EXCEPTION_ACCESS_VIOLATION [File:D:\\x.cpp] [Line: 12] bad</ErrorMessage>"
                        f"<TimeOfCrash>{when * 10000000 + 621355968000000000}</TimeOfCrash>"
                        f"<CallStack>UnrealEditor_Engine\nUnrealEditor_Engine\nUnrealEditor_Game\nUnrealEditor_Core\n</CallStack>"
                        f"</RuntimeProperties></FGenericCrashContext>")
            with open(os.path.join(folder, "Game.log"), "w") as f:
                f.write("[2026.10.12-10.00.00:000][  0]LogTemp: doing the thing\n"
                        "[2026.10.12-10.00.00:000][  0]LogTemp: doing the thing\n"
                        "[2026.10.12-10.00.00:000][  0]LogModuleManager: noise\n"
                        "[2026.10.12-10.00.01:000][  0]LogWindows: Error: appError called\n")
            os.makedirs(os.path.join(project.root, "Source", "Game"))
            report = ue.crash_reports(project.uproject)[0]
            self.assertAlmostEqual(report["time"], when, delta=1)
            self.assertEqual(ue.crash_summary(report), "Unhandled Exception: EXCEPTION_ACCESS_VIOLATION bad")
            text = ue.describe_crash(project.uproject, report)
            self.assertIn("modules (innermost first): Engine <- Game <- Core", text)
            self.assertIn("your modules on the stack: Game", text)
            self.assertEqual(text.count("doing the thing"), 1)
            self.assertNotIn("noise", text)
            self.assertIsNotNone(ue.recent_crash(project.uproject, when - 60))
            self.assertIsNone(ue.recent_crash(project.uproject, when + 60))
        finally:
            project.close()


class Transport(unittest.TestCase):
    def test_choice_by_engine_version_and_override(self):
        project = Project()
        try:
            self.assertEqual(ue.transport(project.uproject), "mcp")  # EngineAssociation 5.8
            with open(project.uproject, "w") as f:
                f.write('{"EngineAssociation": "5.6"}')
            self.assertEqual(ue.transport(project.uproject), "remote")
            ue.write_json(os.path.join(ue.state_dir(project.uproject), "editors", f"{os.getpid()}.json"),
                          {"pid": os.getpid(), "engine": "5.8.3-1+++UE5"})
            self.assertEqual(ue.transport(project.uproject), "mcp")  # the running editor wins
            with unittest.mock.patch.dict(os.environ, {"UE_TRANSPORT": "remote"}):
                self.assertEqual(ue.transport(project.uproject), "remote")
        finally:
            project.close()


class Editors(unittest.TestCase):
    """Several editors on one project: the user's and one `ue open` started (two live pids: ours and our parent's)."""

    def beacon(self, project, pid, **fields):
        ue.write_json(os.path.join(ue.state_dir(project.uproject), "editors", f"{pid}.json"),
                      dict({"pid": pid, "heartbeat": time.time(), "started": 0}, **fields))

    def test_prefers_the_users_editor_then_honours_an_explicit_pid(self):
        project = Project()
        try:
            self.beacon(project, os.getpid(), launched_by_tether=True, headless=True, started=200)
            self.beacon(project, os.getppid(), launched_by_tether=False, started=100)
            self.beacon(project, 999999, launched_by_tether=False, started=300)  # gone
            self.assertEqual(len(ue.live_editors(project.uproject)), 2)
            self.assertEqual(ue.chosen_editor(project.uproject)["pid"], os.getppid())
            with unittest.mock.patch.dict(os.environ, {"UE_EDITOR_PID": str(os.getpid())}):
                self.assertEqual(ue.chosen_editor(project.uproject)["pid"], os.getpid())
            with unittest.mock.patch.dict(os.environ, {"UE_EDITOR_PID": "999999"}):
                with self.assertRaises(ue.CliError):
                    ue.chosen_editor(project.uproject)
            # the editor a session used last stays its default (an editor opened by hand later does not take over)
            self.assertEqual(ue.chosen_editor(project.uproject)["pid"], os.getpid())
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "another"}):  # a fresh session: the one in use
                self.assertEqual(ue.chosen_editor(project.uproject)["pid"], os.getpid())
            path = os.path.join(ue.state_dir(project.uproject), "targets.json")
            ue.write_json(path, {k: dict(v, time=1) for k, v in ue.read_json(path).items()})  # all long ago
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "third"}):  # no one at work: the user's
                self.assertEqual(ue.editor_label(ue.chosen_editor(project.uproject)),
                                 f"pid {os.getppid()}, opened by hand")
        finally:
            project.close()

    def test_tokens_are_per_editor_with_the_old_file_as_fallback(self):
        project = Project()
        try:
            folder = ue.state_dir(project.uproject)
            ue.write_json(os.path.join(folder, "editors", "11.token"), {"pid": 11, "token": "a"})
            ue.write_json(os.path.join(folder, "session.json"), {"pid": 22, "token": "b"})
            self.assertEqual(ue.editor_token(project.uproject, {"pid": 11}), "a")
            self.assertEqual(ue.editor_token(project.uproject, {"pid": 22}), "b")
            self.assertEqual(ue.editor_token(project.uproject, {"pid": 33}), "")  # another editor's token: no
        finally:
            project.close()

    def test_old_single_beacon_still_counts(self):
        project = Project()
        try:
            ue.write_json(os.path.join(ue.state_dir(project.uproject), "editor.json"),
                          {"pid": os.getpid(), "heartbeat": time.time()})
            self.assertEqual(ue.chosen_editor(project.uproject)["pid"], os.getpid())
            self.assertTrue(ue.editor_live(project.uproject))
        finally:
            project.close()


class EditClassifier(unittest.TestCase):
    def test_routes(self):
        cases = [
            ("Game.Build.cs", {"old_string": "a", "new_string": "b"}, "restart"),
            ("Thing.h", {"old_string": "int32 X;", "new_string": "UPROPERTY()\nint32 X;"}, "restart"),
            ("Thing.h", {"old_string": "UPROPERTY() int32 X = 1;", "new_string": "UPROPERTY() int32 X = 2;"}, "restart"),
            ("Thing.h", {"old_string": "", "new_string": "UFUNCTION()\nvoid Go();"}, "live-ufunction"),
            ("Thing.h", {"old_string": "int f() { return 1; }", "new_string": "int f() { return 2; }"}, "maybe-restart"),
            ("Thing.cpp", {"old_string": "return 1;", "new_string": "return 2;"}, "live"),
            ("Thing.h", {"content": "UCLASS()\nclass UThing : public UObject { GENERATED_BODY() };"}, "maybe-restart"),
            ("Thing.cpp", {"content": "int f() { return 3; }"}, "live"),
            ("Coin.cs", {"old_string": "a", "new_string": "b"}, "csharp"),
        ]
        for name, tool_input, route in cases:
            with self.subTest(name=name, tool_input=tool_input):
                self.assertEqual(ue.classify_edit(name, tool_input)[0], route)


class LifecycleHooks(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.cpp = self.project.write("Source/Game/Thing.cpp", "int f() { return 1; }")

    def tearDown(self):
        self.project.close()

    def live(self, **fields):
        ue.write_json(os.path.join(ue.state_dir(self.project.uproject), "editor.json"),
                      dict({"pid": os.getpid(), "heartbeat": time.time(), "map": "/Game/L", "engine": "5.8.3-1+x",
                            "mcp_port": 18000}, **fields))

    def test_session_start(self):
        self.assertIn("editor not running", ue.hook_session_start({"cwd": self.project.root}))
        self.live()
        text = ue.hook_session_start({"cwd": self.project.root})
        self.assertTrue(text.startswith("Unreal project Game: editor ready | map /Game/L | UE 5.8.3-1 | mcp :18000"))
        self.assertEqual(text.splitlines()[1], "Use the `ue` CLI (unreal-tether skill); `ue status` refreshes this.")
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertIsNone(ue.hook_session_start({"cwd": elsewhere}))

    def test_post_edit_only_for_project_source(self):
        payload = {"tool_input": {"file_path": self.cpp, "old_string": "1", "new_string": "2"}}
        self.assertIn("editor not running", ue.hook_post_edit(payload))
        self.live()
        self.assertIn("Live Coding", ue.hook_post_edit(payload))
        readme = self.project.write("README.md", "")
        self.assertIsNone(ue.hook_post_edit({"tool_input": {"file_path": readme}}))
        loose = self.project.write("Tools/x.cpp", "")
        self.assertIsNone(ue.hook_post_edit({"tool_input": {"file_path": loose}}))

    def test_stop_nudges_once_for_recent_unbuilt_code(self):
        old = time.time() - 100
        os.utime(self.cpp, (old, old))
        os.utime(self.project.uproject, (old, old))
        ue.mark_green(self.project.uproject)
        self.live()
        payload = {"cwd": self.project.root, "stop_hook_active": False}
        self.assertIsNone(ue.hook_stop(payload))  # nothing changed since the green build
        future = time.time() + 5
        os.utime(self.cpp, (future, future))
        self.assertIn("`ue build`", ue.hook_stop(payload))
        self.assertIsNone(ue.hook_stop(dict(payload, stop_hook_active=True)))
        ue.write_json(os.path.join(ue.state_dir(self.project.uproject), "editor.json"), {"pid": 0})
        self.assertIsNone(ue.hook_stop(payload))  # editor not running: no nudge

    def test_stop_nudges_once_for_assets_saved_since_validation(self):
        self.live()
        payload = {"cwd": self.project.root, "stop_hook_active": False}
        ue.write_json(os.path.join(ue.state_dir(self.project.uproject), "validated.json"), {"time": time.time() - 100})
        self.project.write("Content/Maps/Old.umap", "x", mtime=time.time() - 200)  # validated already
        self.project.write("Content/Maps/L.umap", "x", mtime=time.time() - 50)
        self.project.write("Content/Notes.txt", "x", mtime=time.time() - 50)
        text = ue.hook_stop(payload)
        self.assertIn("1 asset(s) were saved since the last validation (L.umap)", text)
        self.assertNotIn("ue build", text)
        self.assertIsNone(ue.hook_stop(payload))  # nudged once for this change


class PieDo(unittest.TestCase):
    def test_within_suffix(self):
        match = ue.WITHIN.search("player.Health < 50 within 2.5s")
        self.assertEqual(match.group(1), "2.5")
        self.assertEqual("player.Health < 50 within 2.5s"[: match.start()], "player.Health < 50")
        self.assertIsNone(ue.WITHIN.search("Door_Within.bOpen = true"))

    def test_steps_run_in_order_and_stop_at_the_first_failure(self):
        calls = []

        class Ctx:
            project = "x.uproject"

            def tether(self, tool, **kw):
                calls.append((tool, kw))
                if tool == "pie":
                    return "running"
                return {"ok": "fail" not in kw["step"], "detail": "d"}

        args = argparse.Namespace(script=["at 0,0,0", "expect fail.x = 1", "at 1,1,1"], file=None, reject=None,
                                  warmup=0, keep=False, keep_going=False)
        with unittest.mock.patch.object(ue, "log_end", return_value=0), unittest.mock.patch("builtins.print"):
            self.assertEqual(ue.pie_do(Ctx(), args), 1)
        self.assertEqual([kw["step"] for tool, kw in calls if tool == "pie_step"], ["at 0,0,0", "expect fail.x = 1"])


class Sizes(unittest.TestCase):
    def test_human_bytes_and_groups(self):
        self.assertEqual(ue.human_bytes(512), "512 B")
        self.assertEqual(ue.human_bytes(1536), "1.5 KB")
        self.assertEqual(ue.human_bytes(250 * 1024 ** 2), "250 MB")
        groups = ue.size_groups({"/Game/A/x": 10, "/Game/A/B/y": 5, "/Game/z": 1}, 1)
        self.assertEqual(groups, {"/Game/A": (15, 2), "/Game": (1, 1)})

    def test_folder_packages_counts_every_file_of_a_package(self):
        project = Project()
        try:
            project.write("Content/Props/SM_Rock.uasset", "x" * 100)
            project.write("Content/Props/SM_Rock.ubulk", "x" * 50)
            project.write("Content/Maps/L.umap", "x" * 10)
            project.write("Content/Props/readme.txt", "x" * 999)
            sizes = ue.folder_packages(project.uproject, "/Game")
            self.assertEqual(sizes, {"/Game/Props/SM_Rock": 150, "/Game/Maps/L": 10})
            self.assertEqual(ue.package_bytes(project.uproject, "/Game/Props/SM_Rock"), 150)
        finally:
            project.close()

    def test_memreport_summary(self):
        report = "\n".join([
            'MemReport: Begin command "Mem FromReport"',
            "Process Physical Memory: 4096.00 MB used, 8192.00 MB peak",
            "AssetRegistry memory usage = 256.62 MB",
            'MemReport: End command "Mem FromReport"',
            'MemReport: Begin command "obj list -resourcesizesort"',
            "    Class    Count      NumKB      MaxKB   ResExcKB  ResExcDedSysKB  ResExcDedVidKB     ResExcUnkKB",
            "    SkeletalMesh      2 1024.00 1024.00 1024.00 0.00 0.00 0.00",
            "    StaticMesh      3    10.00    10.00   10.00 0.00 0.00 0.00",
            'MemReport: End command "obj list -resourcesizesort"',
            'MemReport: Begin command "obj list class=SkeletalMesh -resourcesizesort"',
            "    SkeletalMesh /Game/Trees/SKM_Pine.SKM_Pine   1000.00    1000.00  2048.00  0.00 0.00 0.00",
            'MemReport: End command "obj list class=SkeletalMesh -resourcesizesort"',
            'MemReport: Begin command "ListTextures"',
            "2048x2048 (5461 KB, 0), 1024x1024 (1365 KB), PF_DXT5, TEXTUREGROUP_World, /Game/T_Bark.T_Bark, YES, NO, NO, 1, 12, NO",
            "Total size: InMem= 1.33 MB  OnDisk= 5.33 MB  Count=1, CountApplicableToMin=0",
            'MemReport: End command "ListTextures"'])
        lines = ue.memreport_summary(report)
        self.assertEqual(lines[0], "process: 4.0 GB used, 8.0 GB peak (of which asset registry 257 MB)")
        self.assertEqual(lines[1], "by class (object + resource memory): SkeletalMesh x2 2.0 MB, StaticMesh x3 20.0 KB")
        self.assertEqual(lines[2], "biggest SkeletalMeshes: SKM_Pine 3.0 MB")
        self.assertEqual(lines[3], "textures: 1 loaded, 1 MB in memory; biggest T_Bark 1.3 MB (World)")


class EngineDiscovery(unittest.TestCase):
    def test_falls_back_to_the_default_install_folder(self):
        project = Project()
        try:
            with open(project.uproject, "w") as f:
                f.write('{"EngineAssociation": "5.7"}')
            engine = os.path.join(project.root, "Epic Games", "UE_5.7")
            os.makedirs(os.path.join(engine, "Engine", "Binaries"))
            with unittest.mock.patch.dict(os.environ, {"UE_ROOT": ""}), \
                    unittest.mock.patch.object(ue.core, "launcher_manifest", return_value={}), \
                    unittest.mock.patch.object(ue.core, "registered_builds", return_value={}), \
                    unittest.mock.patch.object(ue.core, "installed_engine_dirs", return_value=["/nowhere", engine]):
                self.assertEqual(ue.engine_root(project.uproject), engine)
        finally:
            project.close()


class Validate(unittest.TestCase):
    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_content_package(self):
        root = self.project.root
        self.project.write("Plugins/Feature/Stuff/Feature.uplugin", "{}")
        self.assertEqual(ue.content_package(self.project.uproject, os.path.join(root, "Content", "Maps", "L.umap")),
                         "/Game/Maps/L")
        self.assertEqual(ue.content_package(self.project.uproject,
                                            os.path.join(root, "Plugins", "Feature", "Stuff", "Content", "M.uasset")),
                         "/Feature/M")
        self.assertIsNone(ue.content_package(self.project.uproject, os.path.join(root, "Content", "readme.txt")))
        self.assertIsNone(ue.content_package(self.project.uproject, os.path.join(root, "Source", "A.uasset")))

    def test_package_file_and_stage_copy(self):
        root = self.project.root
        self.project.write("Content/Maps/L.umap", "map")
        self.project.write("Plugins/Group/Feature/Feature.uplugin", "{}")
        self.assertEqual(ue.package_file(self.project.uproject, "/Game/Maps/L"), os.path.join(root, "Content", "Maps", "L.umap"))
        self.assertEqual(ue.package_file(self.project.uproject, "/Game/Gone/X"), os.path.join(root, "Content", "Gone", "X.uasset"))
        self.assertEqual(ue.package_file(self.project.uproject, "/Feature/M"),
                         os.path.join(root, "Plugins", "Group", "Feature", "Content", "M.uasset"))
        self.assertIsNone(ue.package_file(self.project.uproject, "/Engine/BasicShapes/Cube"))
        first = ue.stage_copy(self.project.uproject, b"v1", "BP_X", ".uasset")
        self.assertRegex(first, r"^/Temp/Tether/diff/[0-9a-f]{12}/BP_X[.]BP_X$")
        self.assertEqual(ue.stage_copy(self.project.uproject, b"v1", "BP_X", ".uasset"), first)  # same bytes, same copy
        self.assertNotEqual(ue.stage_copy(self.project.uproject, b"v2", "BP_X", ".uasset"), first)
        tag = first.split("/")[4]
        with open(os.path.join(root, "Saved", "Tether", "diff", tag, "BP_X.uasset"), "rb") as f:
            self.assertEqual(f.read(), b"v1")

    def test_actor_packages_are_never_staged(self):
        # UnrealRacing: a loaded copy of 3MDIC43SSQ2X1VGJQTW71G.uasset took the live NRCityCell_Skyline_0 over
        actor = b"\xc1\x83\x2a\x9e" + b"\x10\x00\x00\x00PersistentLevel\x00\x12\x34\x56\x78" + b"\x06\x00\x00\x00Level\x00"
        with self.assertRaisesRegex(ue.CliError, "World Partition actor package"):
            ue.stage_copy(self.project.uproject, actor, "3MDIC43SSQ2X1VGJQTW71G", ".uasset")
        self.assertFalse(os.path.isdir(os.path.join(self.project.root, "Saved", "Tether", "diff")))
        self.assertRegex(ue.stage_copy(self.project.uproject, actor, "L_Bay", ".umap"), "/L_Bay[.]L_Bay$")  # a map holds it too
        self.assertFalse(ue.is_actor_package(b"...PersistentLevel.NRCityCell_0...", ".uasset"))  # a soft path's sub-path
        ctx = FakeCtx()
        ctx.project, ctx.beacon = self.project.uproject, {}
        args = argparse.Namespace(files=None, asset="/Game/__ExternalActors__/Maps/L_Bay/3M/DI/C43SSQ2X1VGJQTW71G", rev=None,
                                  to=None, as_path=None, limit=20)
        with self.assertRaisesRegex(ue.CliError, "Diff its map instead"):
            ue.cmd_diff(ctx, args)
        live = self.project.write("Content/__ExternalActors__/Maps/L_Bay/3M/X.uasset", "x")
        args = argparse.Namespace(files=[live, live], asset=None, rev=None, to=None, as_path=None, limit=20)
        with self.assertRaisesRegex(ue.CliError, "World Partition actor package"):
            ue.cmd_diff(ctx, args)
        self.assertEqual(ctx.calls, [])  # nothing reached the editor

    def test_cook_refuses_references_to_diff_copies(self):
        self.project.write("Content/__ExternalActors__/Maps/L_Bay/A/B/BUILDER.uasset",
                           "NRCityBuilder_0 ... /Temp/Tether/diff/b5c6e7d7160f/3MDIC43SSQ2X1VGJQTW71G")
        self.project.write("Content/__ExternalActors__/Maps/L_Bay/A/C/FINE.uasset", "PersistentLevel")
        self.project.write("Content/Maps/L_Bay.umap", "map")
        self.project.write("Content/Docs/Note.uasset", "/Temp/Tether/diff/ named in a plain asset is not scanned")
        self.assertEqual(ue.diff_copy_references(self.project.uproject), ["__ExternalActors__/Maps/L_Bay/A/B/BUILDER.uasset"])
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        with self.assertRaisesRegex(ue.CliError, "1 content file.*reference Tether's diff copies.*BUILDER.uasset"):
            ue.cmd_job(ctx, argparse.Namespace(action="cook"))

    @unittest.skipUnless(__import__("shutil").which("git"), "git not installed")
    def test_git_changes(self):
        root = self.project.root
        run = lambda *a: __import__("subprocess").run(["git", "-C", root, *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        self.project.write("Content/A.uasset", "a")
        self.project.write("Content/B.uasset", "b")
        self.project.write("Content/Old Name.uasset", "c")
        run("add", "-A")
        run("commit", "-qm", "base")
        self.project.write("Content/A.uasset", "a2")  # modified
        self.project.write("Content/New.uasset", "n")  # untracked
        os.remove(os.path.join(root, "Content", "B.uasset"))  # deleted
        run("mv", "Content/Old Name.uasset", "Content/Renamed.uasset")  # renamed, path with a space
        changed, deleted = ue.git_changes(self.project.uproject)
        names = lambda paths: sorted(os.path.relpath(p, os.path.realpath(root)).replace(os.sep, "/") for p in paths)
        self.assertEqual(names(changed), ["Content/A.uasset", "Content/New.uasset", "Content/Renamed.uasset"])
        self.assertEqual(names(deleted), ["Content/B.uasset", "Content/Old Name.uasset"])
        run("add", "-A")
        run("commit", "-qm", "work")
        self.assertEqual(ue.git_changes(self.project.uproject), ([], []))
        changed, deleted = ue.git_changes(self.project.uproject, "HEAD~1")
        self.assertEqual(names(changed), ["Content/A.uasset", "Content/New.uasset", "Content/Renamed.uasset"])
        self.assertEqual(names(deleted), ["Content/B.uasset", "Content/Old Name.uasset"])


@unittest.skipUnless(os.name == "nt", "Win32 window enumeration")
class Watchdog(unittest.TestCase):
    """Real top-level windows in this process, off-screen tool windows so nothing shows on the desktop."""

    def setUp(self):
        import ctypes
        from ctypes import wintypes
        self.user32 = user32 = ctypes.windll.user32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.proc = proc_type(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", proc_type), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        wc = WNDCLASSW(lpfnWndProc=self.proc, lpszClassName="UnrealWindow")
        user32.RegisterClassW(ctypes.byref(wc))  # fails harmlessly if a previous test registered it
        self.windows = []

    def window(self, title, enabled=True):
        hwnd = self.user32.CreateWindowExW(0x80 | 0x08000000, "UnrealWindow", title, 0x80000000 | 0x10000000,
                                           -32000, -32000, 10, 10, None, None, None, None)  # TOOLWINDOW|NOACTIVATE, POPUP|VISIBLE
        self.assertTrue(hwnd)
        self.user32.EnableWindow(hwnd, enabled)
        self.windows.append(hwnd)
        return hwnd

    def tearDown(self):
        for hwnd in self.windows:
            self.user32.DestroyWindow(hwnd)

    def test_slate_modal_found_only_while_main_frame_is_disabled(self):
        main = self.window("Game - Unreal Editor")
        self.window("Output Log")
        self.assertEqual(ue.blocking_dialogs(os.getpid()), [])  # floating tab, nothing modal
        self.user32.EnableWindow(main, False)
        self.window("Save Content")
        self.assertEqual(sorted(ue.blocking_dialogs(os.getpid())), ["Output Log", "Save Content"])

    def test_stall_report_names_the_dialog(self):
        project = Project()
        try:
            ue.write_json(os.path.join(ue.state_dir(project.uproject), "editor.json"),
                          {"pid": os.getpid(), "heartbeat": time.time() - 60})
            self.assertEqual(ue.stall_report(project.uproject), "no heartbeat for 60s (game thread busy or hung)")
            self.window("Game - Unreal Editor", enabled=False)
            self.window("Save Content")
            self.assertEqual(ue.stall_report(project.uproject), 'blocked by modal: "Save Content"')
        finally:
            project.close()



if __name__ == "__main__":
    unittest.main()


class SlateSnapshots(unittest.TestCase):
    SNAPSHOT = "\n".join([
        'window "Game - Unreal Editor" [pos=0,0 size=1280,720] [src=SWindow.cpp:1] [ref=w1]',
        '  splitter [pos=4,102 size=799,582] [src=SDockingSplitter.cpp:15] [ref=sp4]',
        '    tab "Viewport 1" [pos=4,102 size=160,25] [src=SLevelEditor.cpp:1346] [ref=t3]',
        '      text "Viewport 1" [pos=31,106 size=61,15] [src=SDockTab.cpp:723] [ref=x6]',
        '    combobox [pos=571,131 size=56,24] [src=SToolBarComboButtonBlock.cpp:359] [ref=co10]',
        '      text "Lit" [pos=597,135 size=14,15] [src=SToolBarComboButtonBlock.cpp:209] [ref=x8]',
        '    text "INVENTORY" [pos=700,150 size=100,20] [src=SInventoryScreen.cpp:40] [ref=x12]',
        '    button "USE" [pos=700,200 size=60,20] [src=SInventoryScreen.cpp:80] [ref=b22]',
        '    button "DROP" [disabled] [pos=760,200 size=60,20] [src=SInventoryScreen.cpp:81] [ref=b23]',
        '    image [pos=0,0 size=0,0] [src=SImage.cpp:1] [ref=i9]',
        '  splitter [pos=808,102 size=468,582] [src=SDockingSplitter.cpp:15] [ref=sp5]',
        '    tab "Outliner" [pos=808,102 size=160,25] [src=SLevelEditor.cpp:1113] [ref=t4]'])

    def test_game_ui_drops_the_editor_viewport_chrome(self):
        nodes = ue.game_ui(ue.parse_snapshot(self.SNAPSHOT))
        self.assertEqual(ue.ui_lines(nodes), ['text "INVENTORY"', 'button "USE"  (b22)', 'button "DROP" [disabled]  (b23)'])

    def test_find_ui_by_label_or_ref_and_refuses_disabled(self):
        nodes = ue.game_ui(ue.parse_snapshot(self.SNAPSHOT))
        self.assertEqual(ue.find_ui(nodes, "use")["ref"], "b22")
        self.assertEqual(ue.find_ui(nodes, "b22")["label"], "USE")
        with self.assertRaises(ue.CliError):
            ue.find_ui(nodes, "DROP")
        with self.assertRaises(ue.CliError):
            ue.find_ui(nodes, "Lit")  # the editor's own control is not game UI

    def test_the_viewport_wins_over_a_stale_client_window_unless_that_client_is_targeted(self):
        stale = ('window "Game Preview [NetMode: Client 1]  (64-bit)" [ref=w9]\n'
                 '  button "Close" [size=10,10] [src=SWindow.cpp:1] [ref=b9]\n')
        nodes = ue.parse_snapshot(stale + self.SNAPSHOT)
        self.assertIn('text "INVENTORY"', ue.ui_lines(ue.game_ui(nodes)))
        self.assertEqual(ue.ui_lines(ue.game_ui(nodes, client=1)), ['button "Close"  (b9)'])

    def test_a_pie_window_is_all_game(self):
        text = 'window "Game Preview [NetMode: Standalone 0]" [ref=w2]\n  button "Play" [size=10,10] [src=SMenu.cpp:1] [ref=b1]'
        self.assertEqual(ue.ui_lines(ue.game_ui(ue.parse_snapshot(text))), ['button "Play"  (b1)'])


class InsightsTraces(unittest.TestCase):
    def test_timer_stats_merge_same_names_and_rank_without_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "game.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("Name,Count,C.Avg,Incl,I.Min,I.Max,I.Avg,I.Med,Excl,E.Min,E.Max,E.Avg,E.Med\n"
                        "SceneRender,100,100,0.5,0,0,0,0,0.4,0,0,0,0\n"
                        "SceneRender,50,50,0.1,0,0,0,0,0.1,0,0,0,0\n"
                        "TSR(sg.AntiAliasingQuality=%d) %dx%d,100,100,0.2,0,0,0,0,0.2,0,0,0,0\n"
                        "WaitForTasks,10,10,0.3,0,0,0,0,0.3,0,0,0,0\n"
                        "Frame,100,100,1.0,0,0,0,0,0.01,0,0,0,0\n")
            stats = ue.read_timer_stats(path)
        self.assertEqual(stats["SceneRender"], (150, 0.6, 0.5))
        self.assertIn("TSR", stats)
        lines, data = ue.trace_scopes({"game": stats}, 100, top=2, last={"game": {"SceneRender": 4.0}})
        self.assertEqual(lines, ["  game: SceneRender 5.00 (+1.00), TSR 2.00 | waiting 3.0"])
        self.assertEqual(data["game"], {"SceneRender": 5.0, "TSR": 2.0})

    def test_gpu_passes_are_taken_out_of_the_cpu_threads(self):
        split = ue.cpu_only({"game": {"Tick": (10, 0.5, 0.4), "Lumen": (5, 0.2, 0.2)}, "gpu": {"Lumen": (5, 0.2, 0.2)}})
        self.assertEqual(split["game"], {"Tick": (10, 0.5, 0.4)})
        self.assertEqual(split["gpu"], {"Lumen": (5, 0.2, 0.2)})


class Packaging(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows executables")
    def test_finds_the_launcher_executable_not_the_binaries_one(self):
        project = Project()
        try:
            root = ue.package_dir(project.uproject, "Win64")
            for rel in ("Windows/Game.exe", "Windows/Game/Binaries/Win64/Game-Win64-Shipping.exe", "Windows/Readme.txt"):
                path = os.path.join(root, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").close()
            self.assertEqual(ue.packaged_executable(project.uproject, "Win64"), os.path.join(root, "Windows", "Game.exe"))
            self.assertIsNone(ue.packaged_executable(project.uproject, "Linux"))
        finally:
            project.close()


class Pngs(unittest.TestCase):
    def test_round_trip_and_diff(self):
        w, h = 6, 4
        pixels = bytes((x * 40) % 256 for x in range(w * h * 3))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            ue.png_write(path, w, h, pixels)
            with open(path, "rb") as f:
                decoded = ue.png_read(f.read())
        self.assertEqual(decoded, (w, h, 3, pixels))
        changed = bytearray(pixels)
        changed[(2 * w + 3) * 3] = (changed[(2 * w + 3) * 3] + 128) % 256  # one pixel at (3, 2)
        share, box, diff = ue.image_diff(decoded, (w, h, 3, bytes(changed)))
        self.assertAlmostEqual(share, 1 / (w * h))
        self.assertEqual(box, [])  # a lone changed pixel is noise, not an area
        self.assertEqual(diff[(2 * w + 3) * 3:(2 * w + 3) * 3 + 3], b"\xff\x00\x00")

    def test_changed_area_ignores_lone_pixels(self):
        w, h = 32, 16
        base = bytes(w * h * 3)
        changed = bytearray(base)
        for y in range(8, 16):  # a solid 8x8 block at (16, 8)
            for x in range(16, 24):
                changed[(y * w + x) * 3] = 200
        changed[(1 * w + 1) * 3] = 200  # and one stray pixel
        share, box, _ = ue.image_diff((w, h, 3, base), (w, h, 3, bytes(changed)))
        self.assertEqual(box, [(16, 8, 23, 15)])
        self.assertAlmostEqual(share, 65 / (w * h))

    def test_png_filters(self):
        import struct
        # A 2x2 RGBA image with one row Sub-filtered and one Paeth-filtered, built by hand.
        rows = [bytes([1, 10, 20, 30, 255, 5, 5, 5, 0]), bytes([4, 1, 1, 1, 0, 2, 2, 2, 0])]
        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", ue.zlib.crc32(kind + body) & 0xFFFFFFFF)
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", ue.zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
        w, h, c, px = ue.png_read(data)
        self.assertEqual((w, h, c), (2, 2, 4))
        self.assertEqual(px[:8], bytes([10, 20, 30, 255, 15, 25, 35, 255]))  # Sub adds the pixel to the left
        self.assertEqual(px[8:], bytes([11, 21, 31, 255, 17, 27, 37, 255]))  # Paeth predicts from above/left


class Overview(unittest.TestCase):
    def test_overview_from_files(self):
        project = Project()
        try:
            with open(project.uproject, "w") as f:
                f.write('{"EngineAssociation": "5.8", "Modules": [{"Name": "Game"}], "Plugins": [{"Name": "X", "Enabled": true}]}')
            project.write("Source/Game/MyMode.h", "class GAME_API AMyMode : public AGameModeBase {};")
            project.write("Source/Game/Hero.h", "class GAME_API AHero : public ABaseHero {};\nclass AbaseX;\n"
                          "class GAME_API ABaseHero : public ACharacter {};")
            project.write("Config/DefaultEngine.ini", "[/Script/EngineSettings.GameMapsSettings]\n"
                          "EditorStartupMap=/Game/Maps/L.L\nGlobalDefaultGameMode=/Script/Game.MyMode\n")
            project.write("Content/Maps/L.umap", "x")
            project.write("Content/__ExternalActors__/Maps/L/0/AB/X.uasset", "x")
            project.write("Content/Input/IA_Jump.uasset", "x")
            lines = ue.project_overview(project.uproject)
            text = "\n".join(lines)
            self.assertIn("modules: Game (3 classes)", text)
            self.assertIn("game mode: AMyMode", text)
            self.assertIn("character: AHero, ABaseHero", text)
            self.assertIn("maps (1): L (WP)", text)
            self.assertIn("input: IA_Jump", text)
            self.assertIn("startup map /Game/Maps/L", text)
        finally:
            project.close()


class Usages(unittest.TestCase):
    def test_code_and_asset_usages(self):
        project = Project()
        try:
            project.write("Source/Game/Door.h", "UFUNCTION(BlueprintCallable)\nvoid OpenDoor();\nvoid OpenDoorFast();\n")
            project.write("Source/Game/Door.cpp", "void ADoor::OpenDoor_Implementation() {}\nIDoor::Execute_OpenDoor(this);\n")
            project.write("Source/Game/Intermediate/Gen.cpp", "OpenDoor();")  # generated code is skipped
            project.write("Content/BP_Door.uasset", "\x00\x01OpenDoor\x00")
            project.write("Content/BP_Wall.uasset", "nothing here")
            hits = ue.code_usages(project.uproject, "OpenDoor")
            self.assertEqual([(os.path.basename(p), n) for p, n, _ in hits],
                             [("Door.cpp", 1), ("Door.cpp", 2), ("Door.h", 2)])
            found, skipped = ue.asset_files_mentioning(project.uproject, "OpenDoor")
            self.assertEqual((sorted(found), skipped), (["/Game/BP_Door"], 0))
        finally:
            project.close()


class TetherCopyNoise(unittest.TestCase):
    def test_compiler_output_of_tether_copies_is_left_out(self):
        lines = [
            stamped("LogBlueprint: Compiling Blueprint '/Temp/Tether/diff/34ed/UDW.UDW'").replace("[398]", "[342]"),
            "[2026.10.13-10.06.44:369][342]LogBlueprint: Error: [AssetLog] D:\\P\\Saved\\Tether\\diff\\34ed\\UDW.uasset: bad",
            "[2026.10.13-10.06.45:254][342]BlueprintLog: Warning: Blueprint failed to compile:  UDW",
            "[2026.10.13-10.06.45:254][343]BlueprintLog: Warning: Blueprint failed to compile:  BP_Real",
            "[2026.10.13-10.06.45:254][342]LogTemp: Warning: unrelated",
        ]
        self.assertEqual(ue.filter_log(lines), ["W BlueprintLog: Blueprint failed to compile:  BP_Real",
                                                "W LogTemp: unrelated"])


class WorldBuilds(unittest.TestCase):
    def test_result_lines(self):
        line = "[2026.10.13-10.11.12:870][  0]LogInit: Display: Success - 0 error(s), 0 warning(s)"
        self.assertEqual(ue.JOB_SUMMARY.search(line).group(1), "Success - 0 error(s), 0 warning(s)")
        line = "[x][  0]LogWorldPartitionHLODsBuilder: Display: #### Built 12 HLOD actors ####"
        self.assertEqual(ue.JOB_SUMMARY.search(line).group(1), "#### Built 12 HLOD actors ####")
        line = "[x][ 1]LogStaticLightingSystem: Warning: WorldSettings.bForceNoPrecomputedLighting is true, Skipping Lighting Build!"
        self.assertIn("Skipping Lighting Build", ue.LIGHTING_DONE.search(line).group(1))
        line = "[x][ 1]LogStaticLightingSystem: Illumination: 12.3 s (1 s encoding lightmaps, 0 s encoding shadowmaps)"
        self.assertTrue(ue.LIGHTING_DONE.search(line).group(1).startswith("Illumination"))


class Audio(unittest.TestCase):
    def test_parse_active_sounds(self):
        lines = [
            "[x][1]LogAudio: Display: Active Sound Count: 2",
            "[x][1]LogAudio: Display: ------------------------",
            "[x][1]LogAudio: Display: Wind_Loop (10000) - AudioComponent_3",
            "[x][1]LogAudio: Display:    Wind_Wave (12.5) (204800) - 0.8",
            "[x][1]LogAudio: Display: Pickup_Cue (0.42) - None",
        ]
        sounds = ue.parse_active_sounds(lines)
        self.assertEqual([s["sound"] for s in sounds], ["Wind_Loop", "Pickup_Cue"])
        self.assertEqual(sounds[0]["waves"], [{"wave": "Wind_Wave", "volume": 0.8}])
        self.assertEqual(sounds[1]["component"], "None")
        self.assertIsNone(ue.parse_active_sounds(["nothing"]))


class Soak(unittest.TestCase):
    def test_memory_trend(self):
        self.assertAlmostEqual(ue.slope_per_minute([(0, 100), (60, 110), (120, 120)]), 10.0)
        self.assertEqual(ue.slope_per_minute([(0, 100), (60, 200)]), 0.0)  # too few samples to call a trend
        self.assertIsNotNone(ue.process_memory_mb(os.getpid()))

    def test_navigation_ensure_from_python_calls_is_left_out(self):
        lines = ["[x][103]LogOutputDevice: Error: === Handled ensure: ===",
                 "[x][103]LogOutputDevice: Error: Ensure condition failed: false || !HasAnyFlags(RF_ClassDefaultObject)"
                 "  [File:D:/build/Engine/Source/Runtime/NavigationSystem/Public/NavigationSystem.h] [Line: 293]",
                 "[x][103]LogOutputDevice: Error: [Callstack] 0x1 UnrealEditor-NavigationSystem.dll!UnknownFunction []",
                 "[x][104]LogTemp: Error: a real one"]
        self.assertEqual(ue.filter_log(lines, level="Error"), ["E LogTemp: a real one"])


class InputMap(unittest.TestCase):
    DATA = {
        "actions": [{"name": "IA_Jump", "path": "/Game/IA_Jump.IA_Jump", "type": "Boolean", "triggers": ["pressed"]},
                    {"name": "IA_Move", "path": "/Game/IA_Move.IA_Move", "type": "Axis2D", "triggers": []},
                    {"name": "IA_Unused", "path": "/Game/IA_Unused.IA_Unused", "type": "Boolean", "triggers": []}],
        "contexts": [{"name": "IMC_Default", "path": "/Game/IMC_Default", "active": True, "priority": 0,
                      "referencers": ["/Game/BP_Player"],
                      "mappings": [{"action": "IA_Jump", "key": "SpaceBar", "modifiers": [], "triggers": []},
                                   {"action": "IA_Move", "key": "W", "modifiers": ["swizzle"], "triggers": []}]},
                     {"name": "IMC_UI", "path": "/Game/IMC_UI", "active": False, "priority": None, "referencers": [],
                      "mappings": [{"action": "IA_Jump", "key": "Enter", "modifiers": [], "triggers": ["hold"]}]}],
        "pie": True, "live": {"/Game/IA_Jump.IA_Jump": ["SpaceBar"]},
    }

    def test_actions_list_keys_by_context_and_live_state(self):
        lines = ue.input_lines(self.DATA, code_refs={"IMC_UI": ["Source/UI.cpp:12"]})
        self.assertEqual(lines[0], "IA_Jump  Boolean, pressed: SpaceBar [IMC_Default], Enter (on hold) [IMC_UI]"
                                   "  | in PIE: SpaceBar")
        self.assertIn("W (swizzle) [IMC_Default]  | in PIE: no active context maps it", lines[1])
        self.assertIn("IA_Unused  Boolean: not in any mapping context", lines[2])
        self.assertIn("IMC_Default  2 mapping(s)  added by: BP_Player  active in PIE (priority 0)", lines[4])
        self.assertIn("IMC_UI  1 mapping(s)  added by: Source/UI.cpp:12  not active in PIE", lines[5])

    def test_match_shows_only_those_actions(self):
        lines = ue.input_lines(dict(self.DATA, pie=False), "move")
        self.assertEqual(lines, ["IA_Move  Axis2D: W (swizzle) [IMC_Default]"])


class PieWatch(unittest.TestCase):
    RESULT = {"targets": ["player.Health", "BP_Enemy", "BP_Enemy.State", "Ghost"],
              "samples": [[0.0, 0.0, 100, None, None, None],
                          [0.5, 0.5, 100, [0, 0, 0], "Idle", None],
                          [1.0, 1.0, 60, [300, 400, 0], "Chase", None],
                          [1.5, 1.5, 35, [300, 400, 0], "Attack", None],
                          [2.0, 2.0, 35, None, None, None]]}

    def test_summaries_by_kind(self):
        lines, summary = ue.watch_summary(self.RESULT)
        self.assertEqual(lines[0], "player.Health  100 -> 35  (min 35 at 1.5s, max 100 at 0.0s, 2 changes)")
        self.assertEqual(lines[1], "BP_Enemy  moved 500 cm: (0, 0, 0) -> (300, 400, 0), top speed 1000 cm/s"
                                   "  [appeared at 0.5s; gone after 1.5s]")
        self.assertTrue(lines[2].startswith("BP_Enemy.State  Idle (0.5s) -> Chase (1.0s) -> Attack (1.5s)"))
        self.assertEqual(lines[3], "Ghost  never found")
        self.assertEqual(summary["player.Health"]["min"], 35)
        self.assertEqual(summary["BP_Enemy"]["travelled"], 500)

    def test_constant_values_say_throughout(self):
        lines, _ = ue.watch_summary({"targets": ["A.b", "A.s"], "samples": [[0, 0, 1.5, "x"], [1, 1, 1.5, "x"]]})
        self.assertEqual(lines, ["A.b  1.5 throughout", "A.s  x throughout"])

    def test_csv_splits_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.csv")
            ue.write_watch_csv(path, self.RESULT)
            with open(path, encoding="utf-8") as f:
                rows = f.read().splitlines()
        self.assertEqual(rows[0], "real_s,game_s,player.Health,BP_Enemy.x,BP_Enemy.y,BP_Enemy.z,BP_Enemy.State,Ghost")
        self.assertEqual(rows[1], "0.0,0.0,100,,,,,")
        self.assertEqual(rows[3], "1.0,1.0,60,300,400,0,Chase,")


class PieRecord(unittest.TestCase):
    MAPPINGS = [{"context": "IMC", "action": "IA_Jump", "type": "boolean", "key": "SpaceBar", "modifiers": []},
                {"context": "IMC", "action": "IA_Use", "type": "boolean", "key": "E", "modifiers": []},
                {"context": "IMC_UI", "action": "IA_Confirm", "type": "boolean", "key": "E", "modifiers": []},
                {"context": "IMC", "action": "IA_Move", "type": "axis2d", "key": "W", "modifiers": ["swizzle:YXZ"]},
                {"context": "IMC", "action": "IA_Move", "type": "axis2d", "key": "A", "modifiers": ["negate:xyz"]}]

    def steps(self, events, **kw):
        lines = ue.record_steps({"mappings": self.MAPPINGS, "active": ["IMC"], "events": events, "seconds": 4}, **kw)
        return [l for l in lines if not l.startswith("#")]

    def test_keys_become_action_steps_with_modifiers(self):
        steps = self.steps([[0.0, "pose", [0, 0, 90], [0.0, 0.0], [0, 0, 150]],
                            [0.5, "down", "W"], [1.0, "down", "A"], [1.5, "up", "W"], [2.0, "up", "A"],
                            [2.2, "down", "SpaceBar"], [2.3, "up", "SpaceBar"], [2.6, "down", "E"], [3.4, "up", "E"],
                            [3.5, "pose", [300, -100, 90], [0.0, 0.0], [300, -100, 150]]])
        self.assertEqual(steps, ["at 0,0,90", "look 1000,0,150", "wait 0.5", "hold IA_Move 0,1", "wait 0.5",
                                 "hold IA_Move -1,1", "wait 0.5", "hold IA_Move -1,0", "wait 0.5", "release IA_Move",
                                 "wait 0.2", "tap IA_Jump", "wait 0.4", "hold IA_Use", "wait 0.8", "release IA_Use",
                                 "wait 0.1", "near 300,-100,90 500"])

    def test_turning_adds_look_steps_and_aims_before_a_press(self):
        steps = self.steps([[0.0, "pose", [0, 0, 90], [0.0, 0.0], [0, 0, 100]],
                            [1.0, "pose", [0, 0, 90], [0.0, 90.0], [0, 0, 100]],
                            [1.5, "pose", [0, 0, 90], [0.0, 95.0], [0, 0, 100]],
                            [1.6, "down", "SpaceBar"], [1.65, "up", "SpaceBar"]])
        self.assertEqual(steps[2:], ["wait 1", "look 0,1000,100", "wait 0.6", "look -87,996,100", "tap IA_Jump",
                                     "near 0,0,90 500"])

    def test_map_header_and_unreleased_holds(self):
        lines = ue.record_steps({"mappings": self.MAPPINGS, "active": ["IMC"], "seconds": 2,
                                 "events": [[0.5, "down", "W"], [1.0, "down", "SpaceBar"]]}, "/Game/Maps/L")
        self.assertEqual(lines[0], "# map: /Game/Maps/L")
        self.assertEqual(lines[-2:], ["release IA_Jump", "release IA_Move"])

    def test_modifiers(self):
        self.assertEqual(ue.playtest._apply_modifiers((1, 0, 0), ["swizzle:YXZ", "negate:y"]), [0, -1, 0])
        self.assertEqual(ue.playtest._apply_modifiers((1, 0, 0), ["scalar:0.5,1,1"]), [0.5, 0, 0])


class ImportPlan(unittest.TestCase):
    def test_folders_keep_their_structure_and_skip_other_files(self):
        project = Project()
        try:
            for rel in ("Art/Rock.fbx", "Art/Rock_D.png", "Art/notes.txt", "Art/Sub/Hit.wav", "Art/.git/x.png", "Lone.obj"):
                project.write(rel)
            art, lone = os.path.join(project.root, "Art"), os.path.join(project.root, "Lone.obj")
            plan = ue.import_plan([art, lone], "/Game/Props/")
            self.assertEqual({k: [os.path.relpath(f, project.root).replace(os.sep, "/") for f in v] for k, v in plan.items()},
                             {"/Game/Props": ["Art/Rock.fbx", "Art/Rock_D.png", "Lone.obj"], "/Game/Props/Sub": ["Art/Sub/Hit.wav"]})
            with self.assertRaises(ue.CliError):
                ue.import_plan([os.path.join(project.root, "missing.fbx")], "/Game")
            with self.assertRaises(ue.CliError):
                ue.import_plan([os.path.join(project.root, "Art", "notes.txt")[:-9] + "Sub2"], "/Game")
        finally:
            project.close()


class UnusedRoots(unittest.TestCase):
    def test_config_and_code_paths_and_cook_folders(self):
        project = Project()
        try:
            project.write("Config/DefaultEngine.ini", """[/Script/EngineSettings.GameMapsSettings]
GameDefaultMap=/Game/Maps/MainMenu.MainMenu
GlobalDefaultGameMode=/Game/Core/BP_GameMode.BP_GameMode_C
""")
            project.write("Config/DefaultGame.ini", """[/Script/UnrealEd.ProjectPackagingSettings]
+DirectoriesToAlwaysCook=(Path="/Game/UI")
+DirectoriesToNeverCook=(Path="/Game/Dev")
[/Script/Engine.AssetManagerSettings]
+PrimaryAssetTypesToScan=(PrimaryAssetType="Map",AssetBaseClass=/Script/Engine.World,Directories=((Path="/Game/Maps")))
""")
            project.write("Source/Game/Hero.cpp",
                          'static ConstructorHelpers::FObjectFinder<USoundBase> Hit(TEXT("/Game/Audio/S_Hit.S_Hit"));')
            roots = ue.unused_roots(project.uproject)
            self.assertEqual(roots, ["/Game/Audio/S_Hit", "/Game/Core/BP_GameMode", "/Game/Maps/MainMenu",
                                     "dir:/Game/Maps", "dir:/Game/UI"])
        finally:
            project.close()


class Lint(unittest.TestCase):
    ROWS = [["/Game/Props/Rock", "StaticMesh", {"Triangles": "20000", "LODs": "1", "CollisionPrims": "0",
                                                 "CollisionComplexity": "CTF_UseSimpleAndComplex", "NaniteEnabled": "False",
                                                 "DefaultCollision": "BlockAll"}],
            ["/Game/Props/SM_Grass", "StaticMesh", {"Triangles": "20000", "LODs": "1", "CollisionPrims": "0",
                                                    "CollisionComplexity": "CTF_UseSimpleAndComplex", "NaniteEnabled": "True",
                                                    "DefaultCollision": "NoCollision"}],
            ["/Game/Tex/T_Rock_N", "Texture2D", {"Dimensions": "1000x1024", "CompressionSettings": "TC_Default",
                                                  "SRGB": "True", "LODGroup": "TEXTUREGROUP_World"}],
            ["/Game/Tex/T_Rock_ORM", "Texture2D", {"Dimensions": "8192x8192", "CompressionSettings": "TC_Masks",
                                                    "SRGB": "True", "LODGroup": "TEXTUREGROUP_World"}],
            ["/Game/UI/T_Icon", "Texture2D", {"Dimensions": "100x60", "LODGroup": "TEXTUREGROUP_UI"}],
            ["/Game/Maps/Lvl_Island", "World", {}],
            ["/Game/Fab/Thing", "StaticMesh", {}],
            ["/Game/Developers/me/junk", "Blueprint", {}],
            ["/Game/BP_Door Old", "Blueprint", {}]]

    def test_rules(self):
        found = {(rule, package) for rule, package, _ in ue.lint_rules(self.ROWS)}
        self.assertEqual(found, {("naming", "/Game/Props/Rock"), ("mesh-collision", "/Game/Props/Rock"),
                                 ("mesh-lods", "/Game/Props/Rock"), ("texture-pow2", "/Game/Tex/T_Rock_N"),
                                 ("texture-normal", "/Game/Tex/T_Rock_N"), ("texture-size", "/Game/Tex/T_Rock_ORM"),
                                 ("texture-mask-srgb", "/Game/Tex/T_Rock_ORM"), ("naming", "/Game/Fab/Thing"),
                                 ("characters", "/Game/BP_Door Old")})

    def test_config_overrides(self):
        config = {"prefixes": {"World": ["Map_"]}, "ignore": ["/Game/Fab/*"], "disable": ["mesh-lods"],
                  "max_texture_size": 8192}
        found = {(rule, package) for rule, package, _ in ue.lint_rules(self.ROWS, config)}
        self.assertIn(("naming", "/Game/Maps/Lvl_Island"), found)
        self.assertNotIn(("naming", "/Game/Fab/Thing"), found)
        self.assertNotIn(("mesh-lods", "/Game/Props/Rock"), found)
        self.assertNotIn(("texture-size", "/Game/Tex/T_Rock_ORM"), found)


class PerfLoad(unittest.TestCase):
    LOG = [
        "[2026.10.13-14.09.59:000][920]LogPython: TetherFrameLog mark 1000.000",
        "[2026.10.13-14.10.00:500][123]LogStreaming: Verbose: CreateAsyncPackages: AddPackage: /Game/A (0x1) /Game/A (0x1) - Start loading package.",
        "[2026.10.13-14.10.00:900][123]LogStreaming: Display: FlushAsyncLoading(12): 1 QueuedPackages, 0 AsyncPackages",
        "[2026.10.13-14.10.00:950][124]LogStreaming: Verbose: GameThread: LoadCompleted: /Game/A (0x1) /Game/A (0x1) - All loading of package is done",
        "[2026.10.13-14.10.01:000][124]LogStreaming: Verbose: CreateAsyncPackages: AddPackage: /Game/B (0x2) /Game/B (0x2) - Start loading package.",
        "[2026.10.13-14.10.01:010][125]LogStreaming: Verbose: GameThread: LoadCompleted: /Game/B (0x2) /Game/B (0x2) - All loading of package is done",
    ]

    def test_log_is_put_on_the_marker_clock(self):
        events, marked = ue.parse_load_log(self.LOG)
        self.assertTrue(marked)
        self.assertEqual([(round(t, 3), k, w) for t, k, w in events],
                         [(1001.5, "start", "/Game/A"), (1001.9, "flush", "1"), (1001.95, "done", "/Game/A"),
                          (1002.0, "start", "/Game/B"), (1002.01, "done", "/Game/B")])
        self.assertEqual(ue.parse_load_log(self.LOG[1:]), ([], False))

    def test_hitches_name_the_loads_inside_them(self):
        events, _ = ue.parse_load_log(self.LOG)
        frames = [[1000.5 + i * 0.01, 0.01, True] for i in range(100)]  # 10 ms frames until 1001.49
        frames += [[1001.96, 0.2, True], [1001.97, 0.01, True], [1002.5, 0.06, True], [999.0, 0.5, True],
                   [1001.98, 0.3, False]]  # before the start and outside PIE: ignored
        lines, data = ue.load_report(frames, events, started=1000.0)
        self.assertEqual(lines[0], "103 frames, median 10.0 ms; 2 hitch(es) over 50 ms:")
        self.assertEqual(lines[1], "  +  1.96s     200 ms  game thread waited for loading (1 flush); "
                                   "1 package(s) finished: A")
        self.assertEqual(lines[2], "  +  2.50s      60 ms  no loading in it")
        self.assertEqual(lines[3:], ["2 package(s) loaded; slowest (request to done):",
                                     "      450 ms  /Game/A", "       10 ms  /Game/B"])
        self.assertEqual(data["hitches"][0]["loaded"], ["/Game/A"])


class BpTrace(unittest.TestCase):
    def test_blueprint_functions_in_order_with_counts(self):
        timers = [{"Id": "1", "Name": "Function/Game/Doors/BP_Door.BP_Door_C.ReceiveBeginPlay", "File": "x.h"},
                  {"Id": "2", "Name": "Function/Game/Doors/BP_Door.BP_Door_C.OnInteract", "File": "x.h"},
                  {"Id": "3", "Name": "Function/Game/Doors/BP_Door.BP_Door_C.ExecuteUbergraph_BP_Door", "File": "x.h"},
                  {"Id": "4", "Name": "Function/Script/Engine.Actor.ReceiveTick", "File": "x.h"},
                  {"Id": "5", "Name": "UWorld::Tick", "File": "World.cpp"},
                  {"Id": "6", "Name": "Function/Game/AI/BP_Cultist.BP_Cultist_C.ReceiveTick", "File": "x.h"}]
        events = [{"TimerId": "5", "StartTime": "10.0"}, {"TimerId": "1", "StartTime": "10.2"},
                  {"TimerId": "3", "StartTime": "10.2"}, {"TimerId": "6", "StartTime": "10.3"},
                  {"TimerId": "6", "StartTime": "10.4"}, {"TimerId": "4", "StartTime": "10.4"},
                  {"TimerId": "2", "StartTime": "11.5"}]
        calls = [(round(t, 2), bp, fn, n) for t, bp, fn, n in ue.bp_calls(timers, events)]
        self.assertEqual(calls, [(0.2, "BP_Door", "ReceiveBeginPlay", 1), (0.3, "BP_Cultist", "ReceiveTick", 2),
                                 (1.5, "BP_Door", "OnInteract", 1)])


class LandscapePicture(unittest.TestCase):
    def test_height_is_brightness_and_layer_is_colour(self):
        grid = {"px": 2, "layers": ["Grass", "Rock"], "heights": [[0.0, 100.0], [None, 50.0]],
                "dominant": [["Grass", "Rock"], [None, None]]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "l.png")
            self.assertEqual(ue.landscape_png(grid, path), (0.0, 100.0))
            with open(path, "rb") as f:
                width, height, _, rgb = ue.png_read(f.read())
        self.assertEqual((width, height), (2, 2))
        pixel = lambda x, y: tuple(rgb[(y * width + x) * 3:(y * width + x) * 3 + 3])  # noqa: E731
        self.assertEqual(pixel(0, 1), (38, 56, 24))     # bottom row is the first grid row (north up): low grass
        self.assertEqual(pixel(1, 1), (140, 130, 120))  # high rock, full brightness
        self.assertEqual(pixel(0, 0), (255, 0, 255))    # no landscape there


class LevelCheck(unittest.TestCase):
    def test_grouped_by_kind_in_a_fixed_order(self):
        data = {"actors": 9, "issues": [{"kind": "stacked", "actor": "B", "detail": "same as A"},
                                         {"kind": "floating", "actor": "Lamp", "detail": "bottom is 40 cm above Floor"},
                                         {"kind": "floating", "actor": "Crate", "detail": "bottom is 9 cm above Floor"}]}
        self.assertEqual(ue.level_lines(data, per_kind=1),
                         ["9 actors checked: 2 floating, 1 stacked", "floating:", "  Lamp  bottom is 40 cm above Floor",
                          "  ... +1 more (--limit N)", "stacked:", "  B  same as A"])
        self.assertEqual(ue.level_lines({"actors": 3, "issues": []}), ["3 actors checked: nothing to report"])


class WatchBaseline(unittest.TestCase):
    BASE = {"targets": ["player.Health", "player", "Enemy.State", "Ghost"],
            "samples": [[0.0, 0, 100, [0, 0, 0], "Idle", None], [1.0, 1, 80, [300, 0, 0], "Idle", None],
                        [2.0, 2, 60, [600, 0, 0], "Chase", None], [3.0, 3, 60, [600, 0, 0], "Attack", None]]}

    def test_identical_runs_match(self):
        self.assertEqual(ue.compare_watch(self.BASE, self.BASE), [])

    def test_small_jitter_is_tolerated(self):
        run = {"targets": self.BASE["targets"],
               "samples": [[0.0, 0, 100, [0, 0, 0], "Idle", None], [1.2, 1, 79, [290, 0, 0], "Idle", None],
                           [2.4, 2, 59, [590, 10, 0], "Chase", None], [3.3, 3, 59, [595, 10, 0], "Attack", None]]}
        self.assertEqual(ue.compare_watch(self.BASE, run), [])

    def test_divergence_is_named(self):
        run = {"targets": self.BASE["targets"],
               "samples": [[0.0, 0, 100, [0, 0, 0], "Idle", None], [1.0, 1, 100, [0, 0, 0], "Idle", None],
                           [5.0, 5, 100, [0, 0, 0], "Chase", None], [6.0, 6, 100, [0, 0, 0], "Attack", [1, 2, 3]]]}
        found = {}
        for spec, message in ue.compare_watch(self.BASE, run):
            found.setdefault(spec, []).append(message)
        self.assertEqual(found["player.Health"], ["ends at 100 (baseline 60)", "min 100 (baseline 60)"])
        self.assertIn("ends 600 cm from where it did", found["player"][0])
        self.assertEqual(found["Enemy.State"], ["reached Chase at 5.0s (baseline 2.0s)",
                                                "reached Attack at 6.0s (baseline 3.0s)"])
        self.assertEqual(found["Ghost"], ["now found, but not in the baseline"])

    def test_different_state_sequence(self):
        run = {"targets": ["Enemy.State"], "samples": [[0, 0, "Idle"], [1, 1, "Flee"]]}
        base = {"targets": ["Enemy.State"], "samples": [[0, 0, "Idle"], [1, 1, "Chase"]]}
        self.assertEqual(ue.compare_watch(base, run), [("Enemy.State", "went Idle -> Flee (baseline Idle -> Chase)")])

    def test_watch_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# map: /Game/L\n# watch: player.Health Enemy.State\n# watch: player\nat 0,0,0\n")
            self.assertEqual(ue.watch_header(path), ["player.Health", "Enemy.State", "player"])
        self.assertTrue(ue.baseline_path("D:/P/Game.uproject", "door").replace("\\", "/").endswith(
            "P/tests/play/baselines/door.json"))


class Budget(unittest.TestCase):
    def test_rows_in_a_fixed_order_with_verdicts(self):
        rows = ue.budget_rows({"memory_mb": 8000, "frame_ms_p95": 16.7, "pie_start_s": 5},
                              {"frame_ms_p95": 21.4, "memory_mb": 6400.0})
        self.assertEqual(rows, [("frame time p95", "21.4 ms", 16.7, "OVER"), ("editor + PIE memory", "6400.0 MB", 8000, "OK"),
                                ("PIE start", "not measured", 5, "n/a")])

    def test_memreport_numbers(self):
        text = ("MemReport: Begin command \"Mem FromReport\"\nProcess Physical Memory: 5120.50 MB used, 6000.00 MB peak\n"
                "MemReport: End command\nTotal size: InMem= 812.31 MB  OnDisk= 900.00 MB\n")
        self.assertEqual(ue.memreport_numbers(text), {"process_mb": 5120.5, "peak_mb": 6000.0, "textures_mb": 812.31})


class TraceOutput(unittest.TestCase):
    def test_blocking_and_passing_hits(self):
        data = {"start": [0, 0, 150], "end": [1000, 0, 150], "channel": "Interact", "sphere": 0,
                "hits": [{"actor": "Trigger", "component": "Box", "distance": 120.0, "blocking": False, "profile": "Trigger",
                          "response": "overlap", "phys_mat": None},
                         {"actor": "Door_2", "component": "Mesh", "distance": 312.5, "blocking": True, "profile": "BlockAll",
                          "response": "block", "phys_mat": "PM_Wood"}]}
        self.assertEqual(ue.trace_lines(data), [
            "line trace on Interact from (0, 0, 150) to (1000, 0, 150):",
            "  passes Trigger (Box) at 120 cm, profile Trigger, overlap on Interact",
            "  BLOCKED by Door_2 (Mesh) at 312.5 cm, profile BlockAll, block on Interact, PM_Wood"])
        self.assertEqual(ue.trace_lines(dict(data, hits=[], sphere=20.0))[0],
                         "sphere 20 cm trace on Interact from (0, 0, 150) to (1000, 0, 150): nothing hit")


class LintFix(unittest.TestCase):
    FOUND = [("naming", "/Game/P/Rock", "StaticMesh: expected SM_"),
             ("naming", "/Game/P/Hero", "SkeletalMesh: expected SK_ or SKM_"),
             ("characters", "/Game/P/My Rock", "spaces or non-ASCII characters"),
             ("texture-normal", "/Game/T/T_Rock_N", "named as a normal map"),
             ("texture-mask-srgb", "/Game/T/T_Rock_ORM", "a mask"),
             ("mesh-collision", "/Game/P/SM_Fence", "no simple collision"),
             ("texture-pow2", "/Game/T/T_Odd", "not a power of two")]

    def test_safe_fixes_only_unless_renames_are_asked_for(self):
        self.assertEqual([(p["package"], p["fix"]) for p in ue.lint_plan(self.FOUND)],
                         [("/Game/T/T_Rock_N", "normalmap"), ("/Game/T/T_Rock_ORM", "linear"), ("/Game/P/SM_Fence", "box")])

    def test_renames_with_their_rule(self):
        plan = ue.lint_plan(self.FOUND, {"naming", "characters"})
        self.assertEqual([p["fix"] for p in plan if p["rule"] in ("naming", "characters")],
                         ["rename:SM_Rock", "rename:SK_Hero", "rename:My_Rock"])


class AssetPattern(unittest.TestCase):
    def test_folder_and_name_glob(self):
        self.assertEqual(ue.asset_pattern("/Game/Props"), ("/Game/Props", ""))
        self.assertEqual(ue.asset_pattern("/Game/Props/"), ("/Game/Props", ""))
        self.assertEqual(ue.asset_pattern("/Game/Props/**"), ("/Game/Props", ""))
        self.assertEqual(ue.asset_pattern("/Game/Tex/*_N"), ("/Game/Tex", "*_N"))
        self.assertEqual(ue.asset_pattern("/Game/**/T_*"), ("/Game", "T_*"))


class JsonEnvelope(unittest.TestCase):
    def run_json(self, project, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ue.main(["--project", project.uproject, *argv, "--json"])
        return code, json.loads(out.getvalue())

    def test_success_carries_lines_and_data(self):
        project = Project()
        try:
            code, doc = self.run_json(project, "overview")
            self.assertEqual(code, 0)
            self.assertEqual((doc["command"], doc["ok"], doc["exit"], doc["error"]), ("overview", True, 0, None))
            self.assertTrue(any("startup map" in line for line in doc["lines"]))
            self.assertEqual(doc["data"]["content"], "0 packages, 0 B")
        finally:
            project.close()

    def test_failure_is_still_one_document(self):
        project = Project()
        try:
            code, doc = self.run_json(project, "props", "NoSuchActor")
            self.assertNotEqual(code, 0)
            self.assertFalse(doc["ok"])
            self.assertEqual(doc["exit"], code)
            self.assertTrue(doc["error"])
        finally:
            project.close()


class Batch(unittest.TestCase):
    def run_batch(self, text, stop=False):
        ran = []

        def fake(ctx, args):
            ran.append(args.match)
            if args.match == "bad":
                raise ue.CliError("nope")
            return 0

        parser = ue.build_parser()
        for action in parser._subparsers._group_actions[0].choices.values():
            if action.get_default("fn") is ue.cmd_actors:
                action.set_defaults(fn=fake)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cmds.txt")
            with open(path, "w") as f:
                f.write(text)
            with unittest.mock.patch.object(ue.cli, "build_parser", return_value=parser), \
                    unittest.mock.patch("builtins.print"):
                code = ue.cmd_batch(None, argparse.Namespace(file=path, stop_on_error=stop))
        return code, ran

    def test_runs_every_line_and_returns_the_worst_code(self):
        self.assertEqual(self.run_batch("actors one\n# skip\nue actors bad\nactors three\n"), (1, ["one", "bad", "three"]))

    def test_stop_on_error(self):
        self.assertEqual(self.run_batch("actors bad\nactors two\n", stop=True), (1, ["bad"]))


class ContactSheet(unittest.TestCase):
    def test_tiles_are_scaled_and_placed(self):
        red = (4, 2, 3, bytes([255, 0, 0] * 8))
        blue = (4, 2, 4, bytes([0, 0, 255, 255] * 8))
        w, h, rgb = ue.contact_sheet([red, blue], columns=2, tile_width=2, gap=1)
        self.assertEqual((w, h), (2 * 2 + 3, 1 + 2))
        px = lambda x, y: rgb[(y * w + x) * 3:(y * w + x) * 3 + 3]
        self.assertEqual(px(1, 1), b"\xff\x00\x00")  # first tile starts after the gap
        self.assertEqual(px(4, 1), b"\x00\x00\xff")  # second tile, RGBA reduced to RGB
        self.assertEqual(px(0, 0), b"\x18\x18\x18")  # background


class LogSummary(unittest.TestCase):
    def test_shape_folds_numbers_paths_and_instances(self):
        a = ue.log_shape("Failed to load /Game/Props/SM_Rock_12.SM_Rock_12 after 3.5 s")
        b = ue.log_shape("Failed to load /Game/Props/SM_Tree.SM_Tree after 12 s")
        self.assertEqual(a, b)
        self.assertEqual(ue.log_shape("Actor BP_Door_C_12 has no mesh"), ue.log_shape("Actor BP_Door_C_7 has no mesh"))

    def test_summary_groups_and_orders_errors_first(self):
        lines = [stamped("LogTemp: Warning: slow frame 41 ms"), stamped("LogTemp: Warning: slow frame 55 ms"),
                 stamped("LogBlueprint: Error: BP_X_C_3 failed"), stamped("LogTemp: Display: fine"),
                 stamped("LogModelContextProtocol: Warning: ours")]
        rows, hidden = ue.summarize_log(lines, ignore=False)
        self.assertEqual(hidden, 0)
        self.assertEqual([(r["level"], r["category"], r["count"]) for r in rows],
                         [("Error", "LogBlueprint", 1), ("Warning", "LogTemp", 2)])
        self.assertEqual(rows[1]["first"], "slow frame 41 ms")

    def test_known_noise_is_hidden_and_counted(self):
        project = Project()
        project.write("Config/Tether/log-ignore.json", json.dumps(
            {"ignore": ["^LogAutomationTest: Condition failed", {"pattern": "slow frame", "why": "editor"}]}))
        lines = [stamped("LogAutomationTest: Error: Condition failed"), stamped("LogTemp: Warning: slow frame 3 ms"),
                 stamped("LogTemp: Warning: real")]
        try:
            ue.load_log_ignore(project.uproject)
            shown = ue.filter_log(lines)
            self.assertEqual(list(shown), ["W LogTemp: real"])
            self.assertEqual(shown.hidden, 2)
            self.assertEqual(len(ue.filter_log(lines, ignore=False)), 3)
            rows, hidden = ue.summarize_log(lines)
            self.assertEqual((len(rows), hidden), (1, 2))
        finally:
            ue.LOG_IGNORE[:] = []

    def test_add_ignore_appends_once(self):
        project = Project()
        ctx = types.SimpleNamespace(project=project.uproject)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ue.add_log_ignore(ctx, "^LogFoo: bar", "noise")
            ue.add_log_ignore(ctx, "^LogFoo: bar")
        self.assertIn("already ignored", out.getvalue())
        self.assertEqual(ue.log_ignore_entries(project.uproject), ["^LogFoo: bar"])
        with self.assertRaises(ue.CliError):
            ue.add_log_ignore(ctx, "([")


class Cvars(unittest.TestCase):
    def test_cvar_line(self):
        self.assertEqual(ue.cvar_line({"name": "r.A", "value": "1", "setby": "Constructor"}), "r.A = 1")
        self.assertEqual(ue.cvar_line({"name": "r.A", "value": "512", "setby": "Scalability", "default": "2048"}),
                         "r.A = 512  (Scalability; default 2048)")
        self.assertEqual(ue.cvar_line({"name": "r.A", "value": "160", "setby": "Ini", "default": "160.0",
                                       "same_as_default": True}), "r.A = 160  (Ini)")

    def test_scalability_levels(self):
        cvars = ue.scalability_cvars("Epic")
        self.assertEqual(cvars["sg.ShadowQuality"], "3")
        self.assertEqual(len(cvars), len(ue.SCALABILITY_GROUPS))
        self.assertEqual(ue.scalability_cvars("0")["sg.TextureQuality"], "0")
        with self.assertRaises(ue.CliError):
            ue.scalability_cvars("ultra")

    def test_scalability_table(self):
        data = {"FrameTime": {"avg": 8.4, "p95": 9.0}, "GameThreadTime": {"avg": 7.5}, "GPUTime": {"avg": 1.2},
                "hitches": 0, "bound": "Game"}
        lines = ue.scalability_rows([("low", data)])
        self.assertIn("level", lines[0])
        self.assertRegex(lines[1], r"low\s+8\.4\s+9\.0\s+7\.5\s+-\s+1\.2\s+0\s+Game")


class Textures(unittest.TestCase):
    def tags(self, **extra):
        return dict({"Dimensions": "2048x2048", "Format": "PF_DXT1", "CompressionSettings": "TC_Default",
                     "SRGB": "True", "LODGroup": "TEXTUREGROUP_World", "NeverStream": "False",
                     "MipGenSettings": "TMGS_FromTextureGroup"}, **extra)

    def test_bytes_include_mips_cubes_and_volumes(self):
        self.assertEqual(ue.texture_bytes(2048, 2048, self.tags()), int(2048 * 2048 / 2 * 4 / 3))
        self.assertEqual(ue.texture_bytes(128, 128, {"Dimensions": "128x128x128", "Format": "B8G8R8A8",
                                                     "MipGenSettings": "TMGS_NoMipmaps"}), 128 ** 3 * 4)
        self.assertEqual(ue.texture_bytes(512, 512, {"Format": "FloatRGBA", "_class": "TextureCube"}),
                         int(512 * 512 * 8 * 4 / 3 * 6))
        self.assertEqual(ue.texture_bytes(4096, 4096, self.tags(MaxTextureSize="1024")),
                         ue.texture_bytes(1024, 1024, self.tags()))

    def test_rows_flags_and_issues(self):
        rows = [["/Game/T/T_Big_N", "Texture2D", self.tags(Dimensions="8192x8192", Format="PF_B8G8R8A8")],
                ["/Game/T/T_Sky", "Texture2D", self.tags(NeverStream="True")],
                ["/Game/T/T_HDR", "TextureCube", self.tags(Format="FloatRGBA", CompressionSettings="TC_HDR")],
                ["/Game/M/SM_X", "StaticMesh", {}]]
        out = ue.texture_rows(rows)
        self.assertEqual([r["package"] for r in out], ["/Game/T/T_Big_N", "/Game/T/T_HDR", "/Game/T/T_Sky"])
        rules = {r["package"]: {i[0] for i in r["issues"]} for r in out}
        self.assertEqual(rules["/Game/T/T_Big_N"], {"texture-size", "texture-normal", "texture-uncompressed"})
        self.assertEqual(rules["/Game/T/T_Sky"], {"texture-never-stream"})
        self.assertEqual(rules["/Game/T/T_HDR"], set())  # uncompressed on purpose
        self.assertIn("never streams", out[2]["flags"])
        self.assertIn("8192x8192 B8G8R8A8 World", ue.texture_line(out[0]))


def rhi(text):
    return stamped("LogRHI: Display: " + text)


class GpuProfile(unittest.TestCase):
    LOG = [rhi("GPU Profile for Frame 1804 - Graphics pipeline 0 - GPU 0"), rhi(""),
           rhi("    - Frame Time                    : 2.68ms"), rhi(""),
           rhi("100.0%  2.40ms + 0.20 Wait   <root>"),
           rhi("   99.5%  2.40ms   Frame 1781"),
           rhi("      60.0%  1.50ms   SceneRender - ViewFamilies"),
           rhi("         58.0%  1.40ms   Scene"),
           rhi("            20.0%  0.50ms   ShadowDepths"),
           rhi("               20.0%  0.50ms   ParallelDraw"),
           rhi("            30.0%  0.70ms   BasePass"),
           rhi("            8.0%  0.20ms   PostProcessing"),
           rhi("               8.0%  0.20ms   TSR UpdateHistory"),
           rhi("      36.0%  0.90ms   RenderGraphExecute - Slate"),
           rhi(""),
           rhi("GPU Profile for Frame 1804 - Async compute pipeline 0 - GPU 0"),
           rhi("100.0%  0.30ms   <root>"), rhi("")]

    def test_parse_tree(self):
        blocks = ue.gpu_profile_blocks(self.LOG)
        self.assertEqual([b["pipeline"] for b in blocks], ["Graphics pipeline 0", "Async compute pipeline 0"])
        nodes = blocks[0]["nodes"]
        self.assertEqual(blocks[0]["frame_ms"], 2.68)
        self.assertEqual((nodes[0]["name"], nodes[0]["wait"], nodes[0]["depth"]), ("<root>", 0.2, 0))
        scene = next(i for i, n in enumerate(nodes) if n["name"] == "Scene")
        self.assertEqual([nodes[c]["name"] for c in nodes[scene]["children"]], ["ShadowDepths", "BasePass", "PostProcessing"])

    def test_summary_kinds_and_passes(self):
        summary = ue.gpu_summary(ue.gpu_profile_blocks(self.LOG + self.LOG))
        self.assertEqual(summary["frames"], 2)
        self.assertEqual(summary["gpu_ms"], 2.4)
        self.assertTrue(summary["scene"])
        self.assertEqual(summary["kinds"]["shadows"], 0.5)
        self.assertEqual(summary["kinds"]["UI"], 0.9)
        self.assertEqual(summary["kinds"]["post processing"], 0.2)
        self.assertEqual(summary["passes"][:2], [("RenderGraphExecute - Slate", 0.9), ("BasePass", 0.7)])
        self.assertNotIn("SceneRender - ViewFamilies", dict(summary["passes"]))


class Beacons(unittest.TestCase):
    def test_dead_and_reused_pids_are_pruned(self):
        project = Project()
        folder = os.path.join(ue.state_dir(project.uproject), "editors")
        now = time.time()
        for pid, heartbeat in ((111, now - 50), (222, now - 500), (333, now - 900), (os.getpid(), now - 700)):
            ue.write_json(os.path.join(folder, f"{pid}.json"), {"pid": pid, "heartbeat": heartbeat})
            ue.write_json(os.path.join(folder, f"{pid}.token"), {"token": "t"})
        names = {os.getpid(): "python.exe"}  # a live pid that is no editor: reused
        alive = {os.getpid(), 222}
        with unittest.mock.patch("tether_cli.core.pid_alive", side_effect=lambda pid: pid in alive),                 unittest.mock.patch("tether_cli.core.process_name", side_effect=lambda pid: names.get(pid, "UnrealEditor.exe")):
            beacons = ue.editor_beacons(project.uproject)
        # 222 runs an editor; 111 is the newest dead one (kept for crash reports); 333 and the reused pid go.
        self.assertEqual([b["pid"] for b in beacons], [111, 222])
        self.assertEqual(sorted(os.listdir(folder)), ["111.json", "111.token", "222.json", "222.token"])
        project.close()

    def test_clean_exit_retires_older_dead_beacons(self):
        project = Project()
        folder = os.path.join(ue.state_dir(project.uproject), "editors")
        ue.write_json(os.path.join(folder, "111.json"), {"pid": 111, "heartbeat": 1000})
        ue.write_json(os.path.join(ue.state_dir(project.uproject), "last-exit.json"), {"pid": 5, "time": 2000})
        with unittest.mock.patch("tether_cli.core.pid_alive", return_value=False):
            self.assertEqual(ue.editor_beacons(project.uproject), [])
            self.assertIsNone(ue.chosen_editor(project.uproject))
        project.close()


class PyErrors(unittest.TestCase):
    def test_printed_output_keeps_its_lines(self):
        text = "RuntimeError: x (line 1)\n<<printed before the error>>\na\nb\n"
        self.assertEqual(ue.compact_error(text), "RuntimeError: x (line 1)\n<<printed before the error>>\na\nb")
        self.assertEqual(ue.compact_error("Some   error\n  spread out"), "Some error spread out")

    def test_pitfalls(self):
        notes = ue.py_pitfalls("mesh.build_from_static_mesh_descriptions([d])\n")
        self.assertTrue(notes and "crashes the editor" in notes[0])
        loop = "for slot in mesh.get_editor_property('static_materials'):\n    slot.set_editor_property('x', m)\n"
        self.assertTrue(any("copies" in n for n in ue.py_pitfalls(loop)))
        self.assertEqual(ue.py_pitfalls("for a in actors():\n    print(a)\n"), [])


class LogSincePie(unittest.TestCase):
    def test_since_last_pie_start(self):
        lines = ["a", stamped("LogPlayLevel: Creating play world package: /Game/UEDPIE_0_L"), "b",
                 stamped("LogPlayLevel: Creating play world package: /Game/UEDPIE_1_L"), "c",
                 stamped("LogPlayLevel: Creating play world package: /Game/UEDPIE_0_L"), "d"]
        self.assertEqual(ue.since_pie(lines)[1:], ["d"])
        self.assertEqual(ue.since_pie(["x", "y"]), ["x", "y"])

    def test_help_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_help(None, argparse.Namespace(topic="log")), 0)
        self.assertIn("--since", out.getvalue())


class HudCrop(unittest.TestCase):
    SNAPSHOT = """window "Sandbox - Unreal Editor" [pos=0,0 size=1280,720] [ref=w1]
  splitter [pos=3,3 size=1274,714] [ref=sp1]
      splitter [pos=7,105 size=795,576] [ref=sp3]
          tab "Viewport 1" [pos=7,105 size=160,25] [ref=t3]
            text "UMG probe widget" [pos=26,181 size=260,37] [ref=x13]"""

    def test_rect_is_bottom_of_the_viewport_panel(self):
        self.assertEqual(ue.hud_rect(self.SNAPSHOT, 795, 519), (7, 162, 795, 519))
        self.assertIsNone(ue.hud_rect(self.SNAPSHOT, 1920, 1080))

    def test_crop(self):
        width, height = 6, 4
        rgb = bytes((x * 10, y * 10, 0) [c] for y in range(height) for x in range(width) for c in range(3))
        out = ue.png_read(ue.png_crop(ue.png_bytes(width, height, rgb), (2, 1, 3, 2)))
        self.assertEqual(out[:3], (3, 2, 3))
        self.assertEqual(out[3][:3], bytes((20, 10, 0)))


class NetSource(unittest.TestCase):
    HEADER = """
UCLASS()
class GAME_API ARobot : public ACharacter
{
	GENERATED_BODY()
	UPROPERTY(ReplicatedUsing = OnRep_Health, BlueprintReadOnly)
	float Health = 100.f;
	UPROPERTY(replicated)
	uint8 bArmed : 1;
	UPROPERTY(EditAnywhere)
	float NotNet;
	UFUNCTION(Server, Reliable, WithValidation)
	void ServerFire(FVector Aim);
	UFUNCTION(NetMulticast, Unreliable)
	void MulticastSpark();
	UFUNCTION(reliable, client)
	void ClientHit(int32 Damage);
};
class OTHER_API AOther : public AActor { UPROPERTY(Replicated) int32 X; };
"""

    def test_header(self):
        rep = ue.replication_in_header(self.HEADER, "ARobot")
        self.assertEqual(rep["properties"], [("Health", "OnRep_Health"), ("bArmed", None)])
        self.assertEqual(rep["rpcs"], [("ServerFire", "server", True, True), ("MulticastSpark", "multicast", False, False),
                                       ("ClientHit", "client", True, False)])
        self.assertIsNone(ue.replication_in_header(self.HEADER, "ANope"))

    def test_conditions(self):
        cpp = ("DOREPLIFETIME_CONDITION(ARobot, Health, COND_OwnerOnly);\nFDoRepLifetimeParams P;\n"
               "P.Condition = COND_SkipOwner;\nDOREPLIFETIME_WITH_PARAMS_FAST(ARobot, bArmed, P);")
        self.assertEqual(ue.replication_conditions(cpp, "ARobot"), {"Health": "OwnerOnly", "bArmed": "SkipOwner"})

    def test_words(self):
        self.assertEqual(ue.net_word("roleautonomousproxy", "role"), "autonomous proxy")
        self.assertEqual(ue.net_word("DORM_Awake", "dorm"), "awake")


class LodNames(unittest.TestCase):
    def test_unity_lod_meshes_in_fbx(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "robot.fbx")
        with open(path, "wb") as f:
            f.write(b"Robot_LOD0\x00\x01Model Robot_LOD1\x00\x01Model Robot_LOD2\x00\x01Model Arm_LOD0\x00\x01Model")
        self.assertEqual(ue.lod_named_meshes(path), {"Robot": [0, 1, 2]})
        self.assertEqual(ue.lod_named_meshes(os.path.join(tmp.name, "t.png")), {})
        tmp.cleanup()


class NoiseForms(unittest.TestCase):
    def test_pattern_with_the_level_matches(self):
        import re as _re
        known = [_re.compile("^LogAutomationTest: Error: Condition failed")]
        self.assertTrue(ue.noise_match(known, "LogAutomationTest", "Error", "Condition failed"))
        self.assertFalse(ue.noise_match(known, "LogAutomationTest", "Warning", "Condition failed"))
        self.assertTrue(ue.noise_match([_re.compile("^LogFoo: bar")], "LogFoo", "Error", "bar baz"))


class WatchShapes(unittest.TestCase):
    def test_position_rotation_and_other_vectors(self):
        self.assertTrue(ue.is_position("player"))
        self.assertTrue(ue.is_position("Door.RelativeLocation"))
        self.assertFalse(ue.is_position("player.CharMoveComp.Velocity"))
        self.assertTrue(ue.is_rotation("player.control"))
        result = {"targets": ["player.GetVelocity()", "player.control"],
                  "samples": [[0.0, 0.0, [0, 0, 0], [0, 10, 0]], [0.5, 0.5, [600, 0, 0], [0, 40, 0]],
                              [1.0, 1.0, [300, 400, 0], [0, 40, 0]]]}
        lines, summary = ue.watch_summary(result)
        self.assertIn("max 600", lines[0])
        self.assertNotIn("moved", lines[0])
        self.assertEqual(summary["player.GetVelocity()"]["last_size"], 500.0)
        self.assertIn("yaw range 30.0", lines[1])

    def test_shot_args_cover_the_shot_parser(self):
        parser = ue.build_parser()
        sub = next(a for a in parser._actions if a.dest == "cmd").choices["shot"]
        wanted = {a.dest for a in sub._actions if a.dest not in ("help", "project", "pid", "port", "timeout", "json",
                                                                 "limit", "time", "bulk", "ignore_lease")}
        self.assertLessEqual(wanted, set(vars(ue.shot_args())))


class LiveCodingErrors(unittest.TestCase):
    def test_link_errors_and_hint(self):
        lines = ['[2026.10.14-05.03.45:001]X.cpp.obj : error LNK2019: unresolved external symbol "__declspec(dllimport) '
                 'public: static class IThing & __cdecl IThing::Get(void)" (__imp_?Get@IThing@@SAAEAV1@XZ) referenced in '
                 'function "void __cdecl Foo(void)" (?Foo@@YAXXZ)',
                 "Game.patch_0.exe : fatal error LNK1120: 1 unresolved externals", "Building patch...",
                 "Y.cpp(12): error C2065: 'Z': undeclared identifier"]
        errors, hints = ue.live_coding_errors(lines + lines[:1])
        self.assertEqual(len(errors), 3)
        self.assertIn("IThing::Get(void)", errors[0])
        self.assertNotIn("__imp_", errors[0])
        self.assertTrue(hints and "Build.cs" in hints[0])


class CppCollisions(unittest.TestCase):
    def test_file_scope_names(self):
        src = ('#include "x.h"\nnamespace { const float CapsuleHalfHeight = 88.f; struct FLocal { int A; };\n'
               'void Helper(int X) { if (X) {} } }\nstatic FString Settings = TEXT("a;b"); // ; {\n'
               'namespace Game { int Shared = 1; }\nvoid APawn::Tick(float D) { static int Counter = 0; }\n')
        self.assertEqual(ue.file_scope_names(src), ["CapsuleHalfHeight", "FLocal", "Helper", "Settings"])

    def test_module_clashes(self):
        project = Project()
        project.write("Source/Game/Game.Build.cs", "")
        project.write("Source/Game/A.cpp", "namespace { float Settings = 1; }\nstatic void OnlyHere() {}\n")
        project.write("Source/Game/Sub/B.cpp", "static int Settings = 2;\n")
        self.assertEqual(ue.cpp_collisions(project.uproject), {"Game": {"Settings": ["A.cpp", os.path.join("Sub", "B.cpp")]}})
        project.close()


class ContentRules(unittest.TestCase):
    def test_missing_refs_and_empty_meshes(self):
        rows = [["/Game/SM_A", "StaticMesh", {"_missing": ["/Game/M_Gone"], "_materials": 0}],
                ["/Game/SM_B", "StaticMesh", {"_materials": 2}], ["/Game/T_C", "Texture2D", {}]]
        found = {(r, p) for r, p, _ in ue.lint_rules(rows)}
        self.assertIn(("missing-ref", "/Game/SM_A"), found)
        self.assertIn(("mesh-no-material", "/Game/SM_A"), found)
        self.assertNotIn(("mesh-no-material", "/Game/SM_B"), found)


class UiWindows(unittest.TestCase):
    SNAP = '''window "Game - Unreal Editor" [pos=0,0 size=1280,720] [ref=w1]
  splitter [pos=7,105 size=795,576] [ref=sp3]
    button "SETTINGS" [pos=300,400 size=200,40] [ref=b1]
window "Message Log" [pos=200,300 size=400,200] [ref=w2]
  text "Asset Check" [pos=210,310 size=580,280] [ref=x1]'''

    def test_parse_two_keys_in_one_bracket(self):
        nodes = ue.parse_snapshot(self.SNAP)
        self.assertEqual((nodes[0]["pos"], nodes[0]["size"]), ("0,0", "1280,720"))

    def test_windows_scaled_to_their_widgets_and_cover(self):
        nodes = ue.parse_snapshot(self.SNAP)
        wins = ue.windows(nodes)
        self.assertEqual(wins[1][2], (200.0, 300.0, 600.0, 300.0))  # 150 %: the text reaches x 790
        button = next(n for n in nodes if n["label"] == "SETTINGS")
        self.assertEqual(ue.covering_window(nodes, button), "Message Log")
        far = dict(button, pos="900,600")
        self.assertIsNone(ue.covering_window(nodes + [far], far))


class Round14(unittest.TestCase):
    def test_bone_tree_folds_deep_branches(self):
        bones = [["root", ""], ["pelvis", "root"], ["spine", "pelvis"], ["hand", "spine"]] + \
            [[f"finger{i}", "hand"] for i in range(10)]
        full = ue.bone_tree_lines(bones, budget=100, sockets=[{"name": "Grip", "bone": "hand"}])
        self.assertIn("      hand  @Grip", full)
        self.assertEqual(len(full), 14)
        folded = ue.bone_tree_lines(bones, budget=5)
        self.assertTrue(any("(+" in line for line in folded))
        self.assertLessEqual(len(folded), 5)

    def test_data_columns(self):
        rows = [{"asset": "/Game/A", "values": {"Health": 100, "Speed": 600.0, "Tags": ["a"]}},
                {"asset": "/Game/B", "values": {"Health": 150, "Speed": 600.0, "Tags": ["a"]}}]
        self.assertEqual(ue.data_columns(rows), (["Health"], ["Speed", "Tags"]))
        self.assertEqual(ue.data_text(600.0), "600")
        self.assertEqual(ue.data_text(["a"]), '["a"]')

    def test_map_picture_places_markers(self):
        data = {"actors": [["Start", "PlayerStart", "marker", 0, 0, 0, 0, 0],
                           ["Wall", "StaticMeshActor", "geometry", 500, 0, 300, 100, 400]], "landscape": False}
        canvas, legend, numbered = ue.map_picture(data, 100, (0, 0), 1000)
        self.assertEqual(legend, [("red", "PlayerStart", 1)])
        self.assertEqual(numbered[0][:2], (1, "Start"))
        i = (50 * 100 + 50) * 3
        self.assertEqual(bytes(canvas.px[i:i + 3]), bytes((230, 60, 60)))  # the marker at the centre
        j = (50 * 100 + 75) * 3
        self.assertNotEqual(bytes(canvas.px[j:j + 3]), bytes((36, 38, 42)))  # the wall east of it

    def test_draw_text(self):
        rgb = bytearray(20 * 12 * 3)
        ue.draw_text(rgb, 20, 0, 0, "1")
        self.assertIn(bytes((255, 255, 255)), bytes(rgb))

    def test_since_duration(self):
        lines = ["[2026.10.14-10.00.00:000][1]LogTemp: old", "[2026.10.14-10.04.30:000][2]LogTemp: new", "tail"]
        import calendar
        now = calendar.timegm((2026, 10, 14, 10, 5, 0))
        self.assertEqual(ue.since_duration(lines, "2m", now=now), lines[1:])
        self.assertEqual(ue.since_duration(lines, "1h", now=now), lines)
        with self.assertRaises(ue.CliError):
            ue.since_duration(lines, "2x")

    def test_log_keeps_event_order(self):
        lines = [stamped("LogTemp: Display: a"), stamped("LogTemp: Display: b"), stamped("LogTemp: Display: a"),
                 stamped("LogTemp: Display: a")]
        self.assertEqual(list(ue.filter_log(lines, "Display", consecutive=True)),
                         ["D LogTemp: a", "D LogTemp: b", "D LogTemp: a  x2"])
        self.assertEqual(list(ue.filter_log(lines, "Display")), ["D LogTemp: a  x3", "D LogTemp: b"])

    def test_pie_start_marker_ignores_travel(self):
        lines = ["x", stamped("LogPlayLevel: PlayLevel: No blueprints needed recompiling"),
                 stamped("LogPlayLevel: Creating play world package: /Game/UEDPIE_0_L"), "leaving for the menu",
                 stamped("LogPlayLevel: Creating play world package: /Game/UEDPIE_0_Menu"), "menu"]
        self.assertEqual(ue.since_pie(lines), lines[1:])  # the travel's world package is not a new session

    def test_hud_rect_with_a_taller_panel(self):
        snap = 'window "Ed" [pos=0,0 size=1920,1080] [ref=w1]\n  splitter [pos=10,100 size=1202,900] [ref=sp]'
        self.assertEqual(ue.hud_rect(snap, 1200, 640), (11.0, 360, 1200, 640))

    def test_game_command(self):
        project = Project()
        args = argparse.Namespace(packaged=False, windowed=False, map="/Game/Maps/Arena", game="/Script/G.Mode",
                                  server_args="-SDHost", client_args="", platform=None)
        with unittest.mock.patch("tether_cli.jobs.engine_root", return_value="E:/UE"):
            server = ue.game_command(types.SimpleNamespace(project=project.uproject), args, "listen", 0, 17777, "s.log")
            client = ue.game_command(types.SimpleNamespace(project=project.uproject), args, "client", 1, 17777, "c.log")
            dedicated = ue.game_command(types.SimpleNamespace(project=project.uproject), args, "dedicated", 0, 7777, "d.log")
        self.assertIn("/Game/Maps/Arena?listen?game=/Script/G.Mode", server)
        self.assertIn("-game", server)
        self.assertIn("-SDHost", server)
        self.assertIn("-DisablePython", server)
        self.assertIn("127.0.0.1:17777", client)
        self.assertIn("-server", dedicated)
        self.assertIn("-nullrhi", client)
        project.close()

    def test_stereo_3d_sound_rule(self):
        rows = [["/Game/S_Stereo", "SoundWave", {"NumChannels": "2", "_attenuated": True}],
                ["/Game/S_Mono", "SoundWave", {"NumChannels": "1", "_attenuated": True}],
                ["/Game/S_Music", "SoundWave", {"NumChannels": "2", "_attenuated": False}]]
        found = [(r, p) for r, p, _ in ue.lint_rules(rows) if r == "sound-stereo-3d"]
        self.assertEqual(found, [("sound-stereo-3d", "/Game/S_Stereo")])

    def test_game_port_follows_the_server(self):
        import socket
        held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        held.bind(("0.0.0.0", 0))
        taken = held.getsockname()[1]
        try:
            self.assertNotEqual(ue.free_udp_port(taken), taken)
        finally:
            held.close()
        self.assertEqual(ue.listening_port(["LogNet: Name:GameNetDriver IpNetDriver listening on port 17778"]), 17778)
        self.assertIsNone(ue.listening_port(["LogNet: nothing"]))

    def test_hud_rect_at_150_percent_dpi(self):
        # Epic's inspector: absolute pixel positions, DPI-independent sizes (ShooterDomination, 3840x2088 at 150 %)
        snap = ('window "SD" [pos=0,0 size=2560,1392] [ref=w1]\n'
                '  splitter [pos=6,153 size=2552,1254] [ref=sp2]\n'
                '    splitter [pos=6,153 size=1606,1254] [ref=sp5]\n'
                '    splitter [pos=2422,153 size=941,1254] [ref=sp6]')
        self.assertEqual(ue.hud_rect(snap, 2409, 1830, 1.5), (6.0, 204.0, 2409, 1830))
        moved = snap.replace("pos=0,0", "pos=100,50").replace("pos=6,153", "pos=106,203")
        self.assertEqual(ue.hud_rect(moved, 2409, 1830, 1.5), (6.0, 204.0, 2409, 1830))

    def test_package_staleness(self):
        project = Project()
        root = os.path.dirname(project.uproject)
        exe_dir = os.path.join(root, "pkg")
        os.makedirs(exe_dir)
        exe = os.path.join(exe_dir, "Game.exe")
        open(exe, "w").close()
        os.makedirs(os.path.join(root, "Content"), exist_ok=True)
        asset = os.path.join(root, "Content", "Map.umap")
        open(asset, "w").close()
        os.utime(exe, (time.time() - 3600, time.time() - 3600))
        self.assertIn("Map.umap", ue.package_staleness(project.uproject, exe))
        os.utime(exe, None)
        self.assertIsNone(ue.package_staleness(project.uproject, exe))
        project.close()

    def test_crop_pixels_drops_alpha(self):
        rgba = bytes([1, 2, 3, 255, 4, 5, 6, 255, 7, 8, 9, 255, 10, 11, 12, 255])  # 2x2
        self.assertEqual(ue.crop_pixels((2, 2, 4, rgba), (1, 0, 1, 2)), (1, 2, 3, bytes([4, 5, 6, 10, 11, 12])))
        self.assertEqual(ue.crop_pixels((2, 1, 1, bytes([7, 9])), (0, 0, 2, 1)), (2, 1, 3, bytes([7, 7, 7, 9, 9, 9])))

    def test_audio_pairs_components_by_sound_and_name(self):
        sounds = [{"sound": "Ambient_Wind_Loop_1", "duration": 100.0, "component": "None", "waves": [{"wave": "w", "volume": 1.28}]},
                  {"sound": "Drone_Hum", "duration": 10000.0, "component": "AudioComponent", "waves": [{"wave": "h", "volume": 0.4}]}]
        comps = [{"component": "AudioComponent", "sound": "Drone_Hum", "owner": f"SDRobot_{i}", "distance": 7064 + i,
                  "volume": 1.0, "attenuation": "SoundAttenuation_0"} for i in range(4)]
        ctx = types.SimpleNamespace(beacon={"pie": True}, data=None, tether=lambda *a, **k: json.dumps(comps))
        out = io.StringIO()
        with unittest.mock.patch("tether_cli.play.active_sounds", return_value=sounds), contextlib.redirect_stdout(out):
            ue.cmd_audio(ctx, argparse.Namespace(match=None, limit=20))
        text = out.getvalue()
        self.assertIn("Ambient_Wind_Loop_1 (100.0 s), volume 1.28\n", text)  # no drone owner or attenuation on the bed
        self.assertIn("Drone_Hum (loop) on SDRobot_0, 7064 cm away", text)
        self.assertEqual(text.count("(component only)"), 3)

    def test_wave_stats_of_a_known_sine(self):
        import math
        import struct
        import wave
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sine.wav")
            amp = int(32767 * 10 ** (-6 / 20))
            with wave.open(path, "wb") as f:
                f.setnchannels(2)
                f.setsampwidth(2)
                f.setframerate(8000)
                silence = b"\0\0\0\0" * 800  # 0.1 s before the tone
                tone = b"".join(struct.pack("<hh", *(2 * [int(amp * math.sin(2 * math.pi * 500 * i / 8000))])) for i in range(8000))
                f.writeframes(silence + tone)
            stats = ue.wave_stats(path)
        self.assertEqual((stats["peak_db"], stats["loud50_db"], stats["channels"]), (-6.0, -9.0, 2))
        self.assertAlmostEqual(stats["bright_hz"], 500, delta=40)  # spectral centroid of a 500 Hz tone
        self.assertLessEqual(stats["attack_ms"], 5)

    def test_audio_log_flags_what_played(self):
        starts = [{"sound": "Shot", "sound_path": "/Game/S/Shot.Shot", "spatialized": True, "attenuated": True,
                   "attenuation": "ATT_Range", "falloff_m": 36.0, "stereo_spread": 200.0, "sound_class": "SFX",
                   "waves": [{"wave": "Shot", "channels": 2}], "owner": "Robot_1", "attached": True},
                  {"sound": "Hum", "sound_path": "/Game/S/Hum.Hum", "spatialized": False, "attenuated": False,
                   "attenuation": None, "sound_class": None, "waves": [{"wave": "Hum", "channels": 1}],
                   "owner": "Robot_2", "attached": True}]
        replies = iter([{"recording": True}, {"starts": starts, "seconds": 1.0, "ended": None}])
        ctx = types.SimpleNamespace(beacon={"pie": True}, data=None, tether=lambda *a, **k: next(replies))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch("time.sleep"):
            ue.audio_log(ctx, argparse.Namespace(log=1, do=None, limit=20))
        text = out.getvalue()
        self.assertIn("! stereo positioned in 3D (stereo spread 200)", text)
        self.assertIn("Hum (mono)  2D, class none, on Robot_2  ! attached to an actor but not attenuated", text)

    def test_audio_log_spread_zero_and_ownerless_3d(self):
        base = {"spatialized": True, "attenuated": True, "attenuation": "ATT", "falloff_m": 80.0, "sound_class": "SFX"}
        starts = [dict(base, sound="Boom", sound_path="/G/Boom.Boom", stereo_spread=0.0, owner="R1", attached=True,
                       waves=[{"wave": "Boom", "channels": 2, "duration": 2.0}]),
                  dict(base, sound="Whiz", sound_path="/G/Whiz.Whiz", stereo_spread=200.0, owner=None, attached=False,
                       location=[100, 200, 50], distance=640, waves=[{"wave": "Whiz", "channels": 1, "duration": 0.5}],
                       cut=True),
                  dict(base, sound="Whiz", sound_path="/G/Whiz.Whiz", stereo_spread=200.0, owner=None, attached=False,
                       location=[300, 200, 50], distance=900, waves=[{"wave": "Whiz", "channels": 1, "duration": 0.5}],
                       virtualized=True)]
        replies = iter([{"recording": True}, {"starts": starts, "seconds": 1.0, "ended": None}])
        ctx = types.SimpleNamespace(beacon={"pie": True}, data=None, tether=lambda *a, **k: next(replies))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch("time.sleep"):
            ue.audio_log(ctx, argparse.Namespace(log=1, do=None, limit=20))
        text = out.getvalue()
        self.assertIn("(stereo, spread 0: folded onto the source)", text)
        self.assertNotIn("! stereo", text)
        self.assertIn("Whiz (mono)  3D, attenuation ATT (80.0 m), class SFX, no owner actor, 640-900 cm away (1 cut, 1 virtualized)", text)
        self.assertNotIn("(2D)", text)
        self.assertIn("1 cut off at once", text)

    def test_py_warns_on_additive_set_editor_properties(self):
        code = "a = unreal.load_asset('/Game/A/AS_Slam')\na.set_editor_properties({'additive_anim_type': t})"
        self.assertTrue(any("recompressed" in n for n in ue.py_pitfalls(code)))
        self.assertFalse(ue.py_pitfalls("m.set_editor_properties({'x': 1})"))


class Fixes0192(unittest.TestCase):
    """The ShooterDomination 0.19.1 field notes."""

    def test_decline_package_restore_keeps_utf16(self):
        project = Project()
        try:
            self.assertFalse(ue.decline_package_restore(project.uproject))  # no file
            path = os.path.join(project.root, "Saved", "Autosaves", "PackageRestoreData.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as f:  # as the editor writes it: UTF-16 LE, no BOM, CRLF and tabs
                f.write('{\r\n\t"RestoreEnabled": true,\r\n\t"Packages": ["/Game/L"]\r\n}'.encode("utf-16-le"))
            self.assertTrue(ue.decline_package_restore(project.uproject))
            with open(path, "rb") as f:
                raw = f.read()
            self.assertEqual(raw[:2], b"{\x00")
            self.assertEqual(json.loads(raw.decode("utf-16-le")), {"RestoreEnabled": False, "Packages": ["/Game/L"]})
            self.assertFalse(ue.decline_package_restore(project.uproject))  # already off
        finally:
            project.close()

    def nav_build(self, status, save=True):
        replies = {"console": "", "py": "False", "nav": status, "save": "saved 1 package"}

        class Ctx(FakeCtx):
            def tether(self, tool, **kwargs):
                self.calls.append((tool, kwargs))
                return replies[tool]
        ctx = Ctx()
        since = {"new": ["/Game/Maps/Spire"], "count": 1, "held": ["/Game/Maps/Spire"]}
        reply = lambda c, code: ["/Game/Maps/Spire"] if "NavigationData" in code else since  # noqa: E731
        args = argparse.Namespace(action="build", what=["nav"], save=save, wait=0, limit=20)
        with unittest.mock.patch.object(ue.play, "py_json", side_effect=reply), \
                unittest.mock.patch.object(ue.play, "rebuild_navigation"), \
                unittest.mock.patch.object(ue.play.time, "sleep"), unittest.mock.patch("builtins.print"):
            try:
                return ue.cmd_world(ctx, args), ctx.calls
            except ue.CliError as exc:
                return exc, ctx.calls

    def test_nav_build_never_saves_a_map_without_navmesh(self):
        result, calls = self.nav_build("/Game/Maps/Spire\n  bounds NavBounds: 200 x 200 x 20 m, NO navmesh inside")
        self.assertIsInstance(result, ue.CliError)
        self.assertIn("not saved", str(result))
        self.assertNotIn("save", [tool for tool, _ in calls])
        result, calls = self.nav_build("/Game/Maps/Spire\n  bounds NavBounds: 200 x 200 x 20 m, navmesh present")
        self.assertEqual(result, 0)
        self.assertIn("save", [tool for tool, _ in calls])

    def test_exec_folds_only_repeats_in_a_row_and_counts_them(self):
        a, b = stamped("LogTemp: Display: objective types: 3"), stamped("LogTemp: Display: district 2")
        shown = ue.filter_log([a, b, a], level="Log", consecutive=True)
        self.assertEqual(len(shown), 3)
        self.assertEqual(shown.folded, 0)
        shown = ue.filter_log([a, a, b], level="Log", consecutive=True)
        self.assertEqual(shown, ["D LogTemp: objective types: 3  x2", "D LogTemp: district 2"])
        self.assertEqual(shown.folded, 1)
        self.assertEqual(ue.filter_log([a, b, a], level="Log").folded, 1)

    def test_property_name_hint_lists_python_names(self):
        error = ("Exception: SDMissionDef: Failed to find property 'in_order' for attribute 'in_order' on "
                 "'SDMissionDef'")
        hint = ue.property_name_hint(FakeCtx(json.dumps(["order", "objectives", "district"])), error)
        self.assertIn("closest to 'in_order': order", hint)
        self.assertIsNone(ue.property_name_hint(FakeCtx("[]"), "NameError: name 'x' is not defined"))

    def test_compile_check_builds_the_game_target_without_marking_green(self):
        project = Project()
        try:
            ctx = types.SimpleNamespace(project=project.uproject)
            self.assertIsNone(ue.compile_check(ctx, 20))  # no game target
            project.write("Source/Game.Target.cs")
            seen = []

            def fake_run(argv, **kw):
                seen.append(argv)
                return types.SimpleNamespace(returncode=0, stdout="Result: Succeeded", stderr="")
            with unittest.mock.patch.object(ue.build, "engine_root", return_value="E"), \
                    unittest.mock.patch.object(ue.build, "ubt_command", return_value=["ubt"]), \
                    unittest.mock.patch.object(ue.build, "live_editors", return_value=[{"pid": 1}]), \
                    unittest.mock.patch.object(ue.subprocess, "run", side_effect=fake_run), \
                    unittest.mock.patch("builtins.print"):
                self.assertEqual(ue.compile_check(ctx, 20), 0)
            self.assertEqual(seen[0][1], "Game")
            self.assertIn("-NoHotReloadFromIDE", seen[0])  # with an editor up: its Live Coding is left alone
            self.assertFalse(os.path.exists(os.path.join(ue.state_dir(project.uproject), "last-build.json")))
        finally:
            project.close()

    def test_restart_keeps_the_editor_open_when_the_check_fails(self):
        project = Project()
        try:
            ctx = types.SimpleNamespace(project=project.uproject, beacon={"pid": 1, "map": "/Game/M"})
            args = argparse.Namespace(check=False, no_check=False, restart=True, save=True, discard=False, limit=20,
                                      ignore_lease=False)
            with unittest.mock.patch.object(ue.editor, "editor_process", return_value=True), \
                    unittest.mock.patch.object(ue.editor, "classify_changes", return_value=(["A.h"], [], ["A.h"])), \
                    unittest.mock.patch.object(ue.editor, "compile_check", return_value=1), \
                    unittest.mock.patch.object(ue.editor, "cmd_close") as close, \
                    unittest.mock.patch("builtins.print"):
                self.assertEqual(ue.cmd_build(ctx, args), 1)
            close.assert_not_called()
        finally:
            project.close()

    def test_pie_run_uses_a_fresh_namespace(self):
        project = Project()
        try:
            project.write("Saved/Logs/Game.log")
            script = project.write("check.py", "print('world' in globals(), tether is not None, __name__)")
            sent = []
            persistent = {"world": "stale", "tether": object(), "unreal": None}

            def tether(tool, **kwargs):
                if tool != "py":
                    return "running"
                sent.append(kwargs["code"])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    exec(kwargs["code"], persistent)
                return out.getvalue()
            ctx = types.SimpleNamespace(project=project.uproject, transport="remote", tether=tether, beacon=None)
            args = argparse.Namespace(script=[script], expect=None, reject=None, seconds=0.1, keep=True)
            out = io.StringIO()
            with unittest.mock.patch.object(ue.play, "flush_editor_log"), contextlib.redirect_stdout(out):
                ue.pie_run(ctx, args)
            self.assertIn("script: False True __pie_run__", out.getvalue())
        finally:
            project.close()


class SharedEditor(unittest.TestCase):
    """0.20: the editor lease, `ue who`, the PIE owner and who wrote a failing file."""

    def setUp(self):
        self.project = Project()
        self.env = unittest.mock.patch.dict(os.environ, {"UE_SESSION": "menu"})
        self.env.start()
        os.environ.pop("UE_IGNORE_LEASE", None)

    def tearDown(self):
        self.env.stop()
        self.project.close()

    def as_session(self, sid):
        return unittest.mock.patch.dict(os.environ, {"UE_SESSION": sid})

    def test_lease_take_renew_refuse_release(self):
        p = self.project.uproject
        lease, how = ue.take_lease(p, "map rebuild + PIE", 15, now=1000)
        self.assertEqual(how, "taken")
        lease, how = ue.take_lease(p, "map rebuild + PIE", 15, now=1100)
        self.assertEqual((how, lease["since"]), ("renewed", 1000))  # renewal keeps since
        with self.as_session("spire"):
            with self.assertRaises(ue.CliError) as caught:
                ue.take_lease(p, "mine", 15, now=1200)
            self.assertEqual(caught.exception.code, ue.LEASED)
            self.assertIn("leased by menu: map rebuild + PIE", str(caught.exception))
            with unittest.mock.patch.object(ue.share.time, "time", return_value=1200):
                with self.assertRaises(ue.CliError):
                    ue.release_lease(p)
        self.assertIsNone(ue.active_lease(p, now=1100 + 15 * 60 + 1))  # expired
        with unittest.mock.patch.object(ue.share.time, "time", return_value=1200):
            self.assertEqual(ue.release_lease(p)[0]["what"], "map rebuild + PIE")
            self.assertIsNone(ue.active_lease(p))

    def test_a_script_inside_a_lease_gives_it_back(self):  # 210: run_city_check.sh's trap dropped its caller's lease
        p = self.project.uproject
        ue.take_lease(p, "pieces round 3", 40, now=1000)
        lease, how = ue.take_lease(p, "city check (run_city_check.sh)", 15, now=1100)
        self.assertEqual((how, lease["outer"]["what"]), ("nested", "pieces round 3"))
        self.assertIn("(inside its lease for pieces round 3", ue.describe_lease(p, lease, now=1100))
        self.assertEqual(ue.take_lease(p, "city check (run_city_check.sh)", 15, now=1150)[1], "renewed")
        with unittest.mock.patch.object(ue.share.time, "time", return_value=1200):
            released, back = ue.release_lease(p)  # the script's EXIT trap
            self.assertEqual((released["what"], back["what"]), ("city check (run_city_check.sh)", "pieces round 3"))
            self.assertEqual(ue.active_lease(p)["what"], "pieces round 3")
        # the inner lease running out gives the outer one back too
        ue.take_lease(p, "level check", 1, now=1300)
        self.assertEqual(ue.active_lease(p, now=1300 + 61)["what"], "pieces round 3")
        with unittest.mock.patch.object(ue.share.time, "time", return_value=1400):
            released, back = ue.release_lease(p)  # level check ran out: the session's own release ends its lease
            self.assertEqual((released["what"], back), ("pieces round 3", None))
            self.assertIsNone(ue.active_lease(p))

    def test_ubt_build_waits_for_another_sessions_restart(self):  # 210
        p = self.project.uproject
        restart = {"session": "skyline", "cmd": "ue build --restart", "pid": 4242, "since": time.time() - 30}
        with unittest.mock.patch.object(ue.editor, "running_commands", return_value=[restart]):
            with self.assertRaises(ue.CliError) as caught:
                ue.check_restarts(p)
            self.assertEqual(caught.exception.code, ue.LEASED)
            self.assertIn("closed for another session's restart (skyline `ue build --restart`", str(caught.exception))
            ue.check_restarts(p, ignore=True)
        with unittest.mock.patch.object(ue.editor, "running_commands", return_value=[dict(restart, session="menu")]):
            ue.check_restarts(p)  # this session's own restart
        with self.as_session("skyline"):
            ue.take_lease(p, "before shots", 15)
        with self.assertRaisesRegex(ue.CliError, r"build \(UBT, no editor open\) refused: the editor is leased by skyline"):
            ue.check_restarts(p)

    def test_saves_refuse_under_another_sessions_lease(self):  # 214
        p = self.project.uproject
        with self.as_session("skyline"):
            ue.take_lease(p, "GPU baseline", 15)
        sent = []
        ctx = ue.Ctx(argparse.Namespace(project=p, timeout=5))
        ctx._project, ctx._transport = p, "mcp"
        ctx._mcp = types.SimpleNamespace(call=lambda toolset, tool, kw: (sent.append(tool) or '"ok"', False))
        transport = sys.modules["tether_cli.transport"]
        with unittest.mock.patch.object(transport, "editor_token", return_value="t"), \
                unittest.mock.patch.object(transport, "chosen_editor", return_value={"pid": 1}), \
                unittest.mock.patch.object(transport, "live_editors", return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            for tool, kwargs in (("save", {}), ("import_files", {"files": "[]"}), ("new_asset", {"save": True})):
                with self.assertRaises(ue.CliError) as caught:
                    ctx.tether(tool, **kwargs)
                self.assertEqual(caught.exception.code, ue.LEASED)
                self.assertIn("(it writes to disk) refused: the editor is leased by skyline", str(caught.exception))
            ctx.tether("set_props", target="A", values="{}")  # edits in memory go ahead, with one note
            ctx.tether("spawn", what="X")
            with unittest.mock.patch.dict(os.environ, {"UE_IGNORE_LEASE": "1"}):
                ctx.tether("save")
        self.assertEqual(sent, ["set_props", "spawn", "save"])
        self.assertEqual(err.getvalue().count("note: the editor is leased by skyline"), 1)

    def test_check_shared(self):
        p = self.project.uproject
        with self.as_session("spire"):
            ue.record_activity(p, ["ue", "pie", "do", "wait 4 game"], now=1000)
        # no lease, another session active 2 min ago: a restart asks for the lease; a PIE start does not
        with self.assertRaises(ue.CliError) as caught:
            ue.check_shared(p, "build --restart", destructive=True, now=1120)
        self.assertIn("spire 2 min ago: ue pie do wait 4 game", str(caught.exception))
        ue.check_shared(p, "pie start", destructive=False, now=1120)
        ue.check_shared(p, "build --restart", destructive=True, now=1000 + 6 * 60)  # long enough ago
        ue.check_shared(p, "build --restart", destructive=True, ignore=True, now=1120)
        ue.take_lease(p, "restart", 15, now=1100)
        ue.check_shared(p, "build --restart", destructive=True, now=1120)  # my lease
        with self.as_session("spire"):
            with self.assertRaises(ue.CliError) as caught:
                ue.check_shared(p, "pie start", destructive=False, now=1120)
            self.assertEqual(caught.exception.code, ue.LEASED)
            with unittest.mock.patch.dict(os.environ, {"UE_IGNORE_LEASE": "1"}):
                ue.check_shared(p, "pie start", destructive=False, now=1120)

    def test_who_lists_sessions_names_and_lease(self):
        p = self.project.uproject
        now = time.time()
        with self.as_session("spire"):
            ue.record_activity(p, ["ue", "world", "build", "nav", "--save"], now=now - 30)
            ue.set_session_name(p, "Spire district")
        ue.record_activity(p, ["ue", "build"], now=now - 200)
        ue.record_activity(p, ["ue", "exec", "x"], now=now - 3600)  # too old
        ctx = types.SimpleNamespace(project=p, beacon=None, data=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ue.cmd_who(ctx, argparse.Namespace(minutes=15, name=None))
        text = out.getvalue()
        self.assertIn("in the last 15 min (2)", text)
        self.assertIn("Spire district (spire): 30s ago, ue world build nav --save", text)
        self.assertIn("menu (you): 3 min ago, ue build", text)
        self.assertIn("lease: none", text)

    def test_pie_owner(self):
        p = self.project.uproject
        ue.record_pie_start(p, now=1000)
        self.assertEqual(ue.pie_owner(p, {"pie": True, "pie_since": 1003}), "started by menu")
        self.assertEqual(ue.pie_owner(p, {"pie": True, "pie_since": 5000}), "started by hand in the editor")
        ue.clear_pie_start(p)  # pie stop
        self.assertEqual(ue.pie_owner(p, {"pie": True, "pie_since": 1003}), "started by hand in the editor")
        self.assertIsNone(ue.pie_owner(p, {"pie": False}))
        self.assertIsNone(ue.pie_owner(p, {"pie": True}))  # an older plugin: no pie_since

    def test_error_owners(self):
        p = self.project.uproject
        path = self.project.write("Source/Game/UI/MainMenu.cpp", "int x")
        with self.as_session("spire"):
            ue.record_edit(p, path, "menu", now=time.time())
            self.assertIn("edited by menu", ue.file_owner(p, "Source/Game/UI/MainMenu.cpp"))
            self.assertIn("(not you)", ue.file_owner(p, "Source/Game/UI/MainMenu.cpp"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ue.print_error_owners(p, ["Source/Game/UI/MainMenu.cpp:220: C2664 cannot convert", "LNK2019 x"])
            self.assertIn("MainMenu.cpp: edited by menu", out.getvalue())
        os.utime(path, (time.time() + 60, time.time() + 60))  # written after the recorded edit: not that session's
        self.assertIn("written", ue.file_owner(p, path))

    def test_nav_lock_reasons_and_retry(self):
        locked = stamped("LogNavigation: Warning: UNavigationSystemV1::Build Navigation NOT building because "
                         "navigation build is locked (flags: 0x20).")
        self.assertIn("AsyncLoadLock", ue.nav_lock([locked]))
        self.assertIsNone(ue.nav_lock([stamped("LogTemp: Display: hi")]))
        self.assertTrue(ue.nav_lock([locked.replace("0x20", "0x2")]).startswith("nothing to build"))
        logs = iter([[locked], [locked], []])
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        with unittest.mock.patch.object(ue.play, "new_log_lines", side_effect=lambda c, s: next(logs)), \
                unittest.mock.patch.object(ue.play.time, "sleep"), unittest.mock.patch("builtins.print") as said:
            ue.rebuild_navigation(ctx, 60)
        self.assertEqual([t for t, _ in ctx.calls], ["console"] * 3)
        self.assertIn("AsyncLoadLock", said.call_args_list[0][0][0])

    def test_nav_path_pairs(self):
        replies = iter(["path: 900 cm over 4 points", "partial path: 360 cm over 3 points"])

        class Ctx(FakeCtx):
            def tether(self, tool, **kwargs):
                self.calls.append((tool, kwargs))
                return next(replies)
        ctx = Ctx()
        args = argparse.Namespace(action="path", points=["0,0,0", "9,0,0", "1,2,3", "Tower"], pie=False, limit=20,
                                  agent=None, m=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ue.cmd_nav(ctx, args), 1)  # one partial path
        self.assertEqual(ctx.calls[0][1]["end"], "900,0,0")
        self.assertEqual(ctx.calls[1][1]["end"], "Tower")
        self.assertEqual(ctx.calls[0][1]["agent"], "")  # Epic's MCP schema requires every parameter
        self.assertIn("100,200,300 -> Tower: partial path", out.getvalue())

    def test_world_probe(self):
        rows = [{"ref": "0,0,0", "point": [0, 0, 0], "inside": [["Block_3", "StaticMeshActor", 40.0]],
                 "near": [["Block_3", "StaticMeshActor", 40.0], ["Crate", "StaticMeshActor", 30.0]], "nav": None,
                 "nav_near": [100, 0, 0, 100.0]},
                {"ref": "Pickup_2", "point": [500, 0, 10], "inside": [], "near": [], "nav": 12.0, "nav_near": None}]
        ctx = types.SimpleNamespace(beacon={}, data=None)
        out = io.StringIO()
        with unittest.mock.patch.object(ue.play, "py_json", return_value=rows), contextlib.redirect_stdout(out):
            code = ue.world_probe(ctx, argparse.Namespace(what=["0,0,0", "Pickup_2"], m=False, pie=False, radius=50))
        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn("(0, 0, 0): INSIDE Block_3 (StaticMeshActor); OFF the navmesh: nearest 100 cm away", text)
        self.assertIn("within 50 cm: Crate (StaticMeshActor, 30 cm)", text)
        self.assertIn("(500, 0, 10) [Pickup_2]: clear; on the navmesh (12 cm)", text)

    def test_metres_and_wait(self):
        self.assertEqual(ue.metres_to_cm("1.5,-2,18"), "150,-200,1800")
        self.assertEqual(ue.metres_to_cm("Tower_2"), "Tower_2")
        with unittest.mock.patch.object(ue.play.time, "sleep") as slept, unittest.mock.patch("builtins.print"):
            self.assertEqual(ue.cmd_wait(types.SimpleNamespace(beacon={}), argparse.Namespace(what="4", clock="real")), 0)
        slept.assert_called_once_with(4)
        with self.assertRaises(ue.CliError):
            ue.cmd_wait(types.SimpleNamespace(beacon={}), argparse.Namespace(what="4", clock="game"))


class TerrainRound(unittest.TestCase):
    """0.21: project skill copies, grids and heightmaps, foliage masks, water, wait compile."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_project_skill_copy_keeps_its_intro(self):
        head = "---\nname: unreal-tether\ndescription: x\n---\n\n# Unreal via `ue`\n\n"
        mine = head + "In this project, ue means python D:/T/cli/ue.py.\n\nOld body.\n"
        path = self.project.write(".claude/skills/unreal-tether/SKILL.md")
        with open(path, "w", encoding="utf-8", newline="") as f:  # CRLF, as a Windows checkout has it
            f.write(mine.replace("\n", "\r\n"))
        source = os.path.join(os.path.dirname(ue.CLI_DIR), "skills", "unreal-tether", "SKILL.md")
        with open(source, encoding="utf-8") as f:
            theirs = f.read()
        p, text = ue.project_skill_update(self.project.uproject)
        self.assertEqual(os.path.normcase(p), os.path.normcase(path))
        self.assertIn("In this project, ue means python D:/T/cli/ue.py.\r\n", text)  # its own intro, its line ends
        self.assertTrue(text.replace("\r\n", "\n").endswith(ue.split_skill(theirs)[2]))
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.assertIsNone(ue.project_skill_update(self.project.uproject))  # current now

    def test_grids_png16_r16_u8_and_flip(self):
        values = [0, 1000, 65535, 32768]
        png = self.project.write("h.png")
        with open(png, "wb") as f:
            f.write(ue.png_gray16_bytes(2, 2, values))
        self.assertEqual(ue.read_grid(png), (2, 2, 65535, values))
        r16 = self.project.write("h.r16")
        import array
        with open(r16, "wb") as f:
            array.array("H", values).tofile(f)
        self.assertEqual(ue.read_grid(r16), (2, 2, 65535, values))
        u8 = self.project.write("m.u8")
        with open(u8, "wb") as f:
            f.write(bytes([0, 255, 255, 0, 0, 0, 255, 255, 255]))
        self.assertEqual(ue.read_grid(u8)[:3], (3, 3, 255))
        self.assertEqual(ue.flip_rows(2, 2, values), [65535, 32768, 0, 1000])
        self.assertEqual(ue.grid_to_bytes(2, 2, 65535, values, max_side=1), (1, 1, bytes([int(round(99303 / 4 / 65535 * 255))])))
        with self.assertRaises(ue.CliError):
            ue.read_grid(self.project.write("bad.r16", "abcdef"))  # 3 samples: not square

    def test_foliage_mask_and_avoid_are_sent(self):
        mask = self.project.write("mask.u8")
        with open(mask, "wb") as f:
            f.write(bytes([255, 0, 0, 255]))
        ctx = FakeCtx("added 3")
        args = argparse.Namespace(action="add", what="/Game/FT_Fern", area=None, density=5, count=None, spacing=None,
                                  slope=None, height=None, scale=None, align=False, seed=0, folder="/Game/Foliage",
                                  mask=mask, mask_area="0,0,100,100", flip_y=True, avoid=["Camp|Path:1500", "Hut"],
                                  distance=None, scope=None)
        with unittest.mock.patch("builtins.print"):
            ue.cmd_foliage(ctx, args)
        sent = ctx.calls[0][1]
        grid = json.loads(sent["mask"])
        import base64
        self.assertEqual((grid["w"], grid["h"], grid["rect"]), (2, 2, [0.0, 0.0, 100.0, 100.0]))
        self.assertEqual(base64.b64decode(grid["data"]), bytes([0, 255, 255, 0]))  # flipped rows
        self.assertEqual(json.loads(sent["avoid"]), [["Camp|Path", 1500.0], ["Hut", 500.0]])
        with self.assertRaises(ue.CliError):
            ue.cmd_foliage(ctx, argparse.Namespace(**dict(vars(args), action="fix", what="leaves", mask=None, avoid=None)))

    def test_water_checks(self):
        data = {"plugin": True, "landscapes": 1,
                "zones": [{"label": "Zone", "extent": [400000, 400000], "resolution": [1024, 1024], "local": False,
                           "local_extent": None}],
                "bodies": [{"label": "Ocean", "class": "WaterBodyOcean", "z": 0, "affects_landscape": True,
                            "material": "/Game/Water/MI_Storm", "lod_material": "/Water/Materials/Water_Material_Ocean_LOD",
                            "waves": True}]}
        out = io.StringIO()
        with unittest.mock.patch.object(ue.content, "py_json", return_value=data), contextlib.redirect_stdout(out):
            code = ue.cmd_water(types.SimpleNamespace(data=None), argparse.Namespace())
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("8000 x 8000 m, water-info 1024 x 1024 (7.81 m/texel)", text)
        self.assertIn("shoreline fade is coarse", text)
        self.assertIn("it edits the landscape", text)
        self.assertIn("distant LOD mesh still uses the stock one", text)
        self.assertIn("3 issue(s)", text)

    def test_landscape_files_need_rendering(self):
        ctx = types.SimpleNamespace(project=self.project.uproject, beacon={"nullrhi": True})
        with self.assertRaises(ue.CliError) as caught:
            ue.landscape_file(ctx, argparse.Namespace(action="export", where="x.png", flip_y=False, mask=None))
        self.assertIn("--render", str(caught.exception))

    def test_wait_compile_until_quiet(self):
        samples = iter([(10, 0, 2), (12, 40, 2), (13, 80, 2), (13.5, 80, 1), (14, 80, 0), (14.5, 80, 0), (15, 80, 0)])
        ctx = types.SimpleNamespace(beacon={"pid": 1})
        out = io.StringIO()
        with unittest.mock.patch.object(ue.play, "editor_process", return_value=True), \
                unittest.mock.patch.object(ue.play, "compile_cpu", side_effect=lambda pid: next(samples)), \
                unittest.mock.patch.object(ue.play, "COMPILE_QUIET", 0), \
                unittest.mock.patch.object(ue.play.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(ue.cmd_wait(ctx, argparse.Namespace(what="compile", clock="real", max=600)), 0)
        self.assertIn("shader workers 80 CPU-s", out.getvalue())

    def test_wait_compile_in_a_rendering_editor(self):  # 211: drawing keeps the editor's own CPU up for good
        clock = iter(range(0, 10000))
        editor = iter(range(0, 100000, 3))  # 3 cores, every second, forever
        ctx = types.SimpleNamespace(beacon={"pid": 1, "headless": True, "nullrhi": False})
        out = io.StringIO()
        with unittest.mock.patch.object(ue.play, "editor_process", return_value=True), \
                unittest.mock.patch.object(ue.play, "compile_cpu", side_effect=lambda pid: (next(editor), 0, 0)), \
                unittest.mock.patch.object(ue.play.time, "time", side_effect=lambda: next(clock)), \
                unittest.mock.patch.object(ue.play.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(ue.cmd_wait(ctx, argparse.Namespace(what="compile", clock="real", max=900)), 0)
        self.assertIn("compiled: shader workers quiet for 60s", out.getvalue())
        self.assertIn("the editor itself stayed at 3.0 cores, which in a rendering editor is drawing frames",
                      out.getvalue())
        self.assertIn("still waiting after", out.getvalue())  # a progress line every 30 s
        ctx.beacon["nullrhi"] = True  # no rendering: the editor's CPU is asset builds, and the wait gives up
        clock, editor = iter(range(0, 10000)), iter(range(0, 100000, 3))
        with unittest.mock.patch.object(ue.play, "editor_process", return_value=True), \
                unittest.mock.patch.object(ue.play, "compile_cpu", side_effect=lambda pid: (next(editor), 0, 0)), \
                unittest.mock.patch.object(ue.play.time, "time", side_effect=lambda: next(clock)), \
                unittest.mock.patch.object(ue.play.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ue.CliError, r"still compiling after 100s: shader workers 0.0 cores \(0 "
                                                     r"running\), the editor 3.0 cores.*--workers-only"):
                ue.cmd_wait(ctx, argparse.Namespace(what="compile", clock="real", max=100))

    def test_compile_overlay_tells_others_errors_apart(self):  # 213
        root = tempfile.mkdtemp()
        shadow = os.path.join(root, "shadow", "Game.uproject")
        builds = []

        def build(c, a, check=False, extra=None, output=None):
            builds.append(extra)
            if output is not None:
                output += [os.path.join(os.path.dirname(shadow), "Source", "Game", "NRRoadNode.cpp")
                           + "(12): error C3861: 'Foo': identifier not found"]
            return 1 if extra is None else 0
        ctx = types.SimpleNamespace(project=os.path.join(root, "Game.uproject"))
        out = io.StringIO()
        with unittest.mock.patch.object(ue.build, "game_target", return_value="Game"), \
                unittest.mock.patch.object(ue.build, "claim_overlay", side_effect=lambda p, o, s: o), \
                unittest.mock.patch.object(ue.build, "shadow_project",
                                           return_value=(shadow, ["Source/Game/Skyline.cpp", "Source/Game/Skyline.h"])), \
                unittest.mock.patch.object(ue.build, "ubt_build", side_effect=build), \
                unittest.mock.patch.object(ue.build, "file_owner", return_value="edited by roads (1a2b) 5 min ago (not you)"), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ue.compile_overlay(ctx, "staged", 30, "me"), 0)
        self.assertIn("Source/Game/NRRoadNode.cpp: edited by roads (1a2b) 5 min ago (not you)", out.getvalue())
        self.assertIn("none of them is yours: checking the overlay's own 2 file(s) alone", out.getvalue())
        self.assertEqual(len(builds), 2)
        self.assertIn("-SingleFileBuildDependents", builds[1])
        self.assertTrue(any(e.endswith("Skyline.cpp") for e in builds[1]))

    def test_drop_group_is_sent(self):
        ctx = FakeCtx("group of 3 actor(s) moved -591 cm")
        with unittest.mock.patch("builtins.print"):
            ue.cmd_drop(ctx, argparse.Namespace(actors=["A", "B"], align=False, group=True, rest="mean"))
        self.assertEqual(ctx.calls[0][1], {"targets": "A,B", "align": False, "group": True, "rest": "mean"})


class IslandHorrorRound(unittest.TestCase):
    """0.22: IslandHorror's visual-pass feedback."""

    def test_live_coding_holders(self):
        out = ("65800\t101708\tC:\\UE_5.8\\Engine\\Binaries\\Win64\\LiveCodingConsole.exe\t"
               "-Group=UE_ShooterDomination_0xe2e0a70c -Hidden -ProjectName=\"ShooterDomination\"\n"
               "70000\t1\tC:\\UE_5.7\\Engine\\Binaries\\Win64\\LiveCodingConsole.exe\t-ProjectName=\"Other\"\n")
        with unittest.mock.patch.object(ue.core.os, "name", "nt"), \
                unittest.mock.patch.object(ue.core.subprocess, "run",
                                           return_value=types.SimpleNamespace(stdout=out, returncode=0)):
            self.assertEqual(ue.live_coding_holders("C:\\UE_5.8"), [(65800, "ShooterDomination", 101708)])

    def test_res_percentage_and_upscaler_line(self):
        self.assertEqual(ue.parse_res("2560x1440"), (2560, 1440))
        with self.assertRaises(ue.CliError):
            ue.parse_res("1440p")
        percentage, clamped = ue.res_percentage((2560, 1440), (799, 525))
        self.assertAlmostEqual(percentage, 100 * (2560 * 1440 / (799 * 525)) ** 0.5, places=3)
        self.assertEqual(clamped, "")
        self.assertEqual(ue.res_percentage((7680, 4320), (200, 100))[0], ue.MAX_SCREEN_PERCENTAGE)
        line = "LogRHI: 92.7%  0.29ms   TemporalSuperResolution(sg.AntiAliasingQuality=3) 2397x1575 -> 799x525"
        noise = "LogRHI: 0.7%  0.00ms   Downsample 50x33 -> 25x17"
        self.assertEqual(ue.UPSCALE.findall(line + "\n" + noise), [("2397", "1575", "799", "525")])
        self.assertEqual(ue.res_cvars(296.0, True), {"r.ScreenPercentage": "296.0"})
        self.assertIn("r.Editor.Viewport.ScreenPercentage", ue.res_cvars(296.0, False))

    def test_hud_crop_check(self):
        dark = ue.png_bytes(32, 20, bytes(32 * 20 * 3))
        lit = ue.png_bytes(32, 20, bytes([200]) * (32 * 20 * 3))
        self.assertEqual(ue.thumb_difference(dark, dark), 0)
        self.assertGreater(ue.thumb_difference(dark, lit), ue.HUD_MISMATCH)

    def test_buoyancy_lines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            issues = ue.buoyancy_lines({"label": "Raft", "radii": [30, 30, 30, 30], "coefficient": 0.1, "max_force": 0,
                                        "overlaps_at_load": False, "mass": 200, "simulates": True})
        text = out.getvalue()
        self.assertEqual(issues, 2)
        self.assertIn("ue set Raft bGenerateOverlapEventsDuringLevelStreaming=true", text)
        self.assertIn("it cannot float", text)  # 0.1 x 113097 cm^3 = 11310 < 200 kg x 980
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.buoyancy_lines({"label": "Buoy", "radii": [100] * 4, "coefficient": 0.1, "max_force": 0,
                                                "overlaps_at_load": True, "mass": 50, "simulates": True}), 0)

    def test_failed_materials_from_the_log(self):
        project = Project()
        try:
            content = os.path.join(project.root, "Content", "Fab", "M_MS_Foliage.uasset")
            project.write("Saved/Logs/Game.log", stamped(f"LogMaterial: Warning: [AssetLog] {content}: Failed to compile "
                                                         "Material for platform PCD3D_SM6, Default Material will be used "
                                                         "in game.") + "\n")
            ctx = types.SimpleNamespace(project=project.uproject, beacon=None, transport="mcp")
            with unittest.mock.patch.object(ue.content, "flush_editor_log"):
                self.assertEqual(ue.failed_materials(ctx), ["/Game/Fab/M_MS_Foliage"])
        finally:
            project.close()

    def test_usage_flags_set_by_the_editor(self):
        project = Project()
        try:
            line = stamped("LogMaterial: Display: Material /Game/IH/MI_Fern.MI_Fern needed to set usage flag "
                           "InstancedStaticMeshes")
            project.write("Saved/Logs/Game.log", line + "\n" + line + "\n")
            ctx = types.SimpleNamespace(project=project.uproject, beacon=None)
            self.assertEqual(ue.usage_flagged_materials(ctx), [("/Game/IH/MI_Fern", "InstancedStaticMeshes")])
        finally:
            project.close()

    def test_property_hint_for_blueprint_variables(self):
        error = "Exception: Actor: Failed to find property 'time_of_day' for attribute 'time_of_day' on 'BP_UDS_C'"
        hint = ue.property_name_hint(FakeCtx(json.dumps({"python": ["hidden"], "blueprint": ["Time of Day", "Cloud Speed"]})),
                                     error)
        self.assertIn("get_editor_property('Time of Day')", hint)

    def test_spawn_in_pie_moves_the_summoned_actor(self):
        replies = iter([{"path": "/Script/Engine.StaticMeshActor", "mesh": "/Engine/BasicShapes/Cube.Cube", "count": 1600},
                        None,
                        {"name": "StaticMeshActor_3", "label": "Buoy", "class": "StaticMeshActor", "at": [10, 20, 30],
                         "landed": "Island"}])
        sent, codes = [], []

        def reply(ctx, code):
            codes.append(code)
            return next(replies)

        class Ctx(FakeCtx):
            beacon = {"pie": True}
            data = None

            def tether(self, tool, **kwargs):
                sent.append((tool, kwargs))
                return ""
        out = io.StringIO()
        with unittest.mock.patch.object(ue.calls, "py_json", side_effect=reply), \
                unittest.mock.patch.object(ue.calls.time, "sleep"), contextlib.redirect_stdout(out):
            ue.cmd_spawn(Ctx(), argparse.Namespace(what="/Engine/BasicShapes/Cube", at="10,20,500", rot=None,
                                                   label="Buoy", ground=True, align=False, pie=True))
        self.assertEqual(sent[0], ("console", {"command": "summon /Script/Engine.StaticMeshActor", "pie": True}))
        self.assertIn("spawned Buoy (StaticMeshActor) in the PIE world at (10, 20, 30) on Island", out.getvalue())
        # 208: the actors already there stay in the editor under one key, never in a reply (thousands overflow it)
        key = re.search(r"_tether_spawns\", \{\}\)\['([^']+)'\]", codes[0]).group(1)
        self.assertTrue(all(f"_tether_spawns.get('{key}'" in code for code in codes[1:]))

    def test_py_json_names_a_reply_cut_at_the_output_cap(self):
        with self.assertRaisesRegex(ue.CliError, r"cut short: 20500 chars, more than ue py returns \(20000\)"):
            ue.py_json(FakeCtx("[" + "x" * 19999 + "\n... truncated 500 chars"), "x")

    def test_wait_grass_until_batches_stop(self):
        done = stamped("LogGrass: Verbose: ASYNC GRASS INSTANCES COMPLETE a b 1 2 (3)")
        logs = iter([[], [done], [done, done], [done, done]] + [[done, done]] * 20)
        clock = iter(range(0, 100))
        ctx = FakeCtx()
        ctx.beacon, ctx.project = {"pie": True}, "Game.uproject"
        out = io.StringIO()
        with unittest.mock.patch.object(ue.play, "new_log_lines", side_effect=lambda c, s: next(logs)), \
                unittest.mock.patch.object(ue.play, "unthrottled", contextlib.nullcontext), \
                unittest.mock.patch.object(ue.play, "log_end", return_value=0), \
                unittest.mock.patch.object(ue.play.time, "time", side_effect=lambda: next(clock)), \
                unittest.mock.patch.object(ue.play.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(ue.cmd_wait(ctx, argparse.Namespace(what="grass", clock="real", max=60)), 0)
        self.assertIn("grass settled: 2 batch(es)", out.getvalue())
        self.assertEqual([c[1]["command"] for c in ctx.calls], ["log LogGrass Verbose", "log LogGrass Log"])


class ShooterDominationRound(unittest.TestCase):
    """0.23: ShooterDomination's 0.20 notes."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_game_key_sends_one_frame_sequenced_step(self):
        ctx = FakeCtx(json.dumps({"ok": True, "detail": "LeftControl+G tap (G is also mapped in Enhanced Input: ...)"}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ue.game_key(ctx, "Ctrl+g"), "LeftControl+G")
            ue.game_key(ctx, "esc")
        self.assertEqual([c[1]["step"] for c in ctx.calls], ["key LeftControl+G tap", "key Escape tap"])
        self.assertIn("note: G is also mapped in Enhanced Input", out.getvalue())

    def test_game_command_without_server(self):
        args = argparse.Namespace(packaged=False, windowed=False, map="/Game/Maps/Title", game=None, server_args="",
                                  client_args="", platform=None, server="none", connect=None, python=True)
        ctx = types.SimpleNamespace(project=self.project.uproject)
        with unittest.mock.patch("tether_cli.jobs.engine_root", return_value="E:/UE"):
            solo = ue.game_command(ctx, args, "client", 0, 17777, "c.log")
            joined = ue.game_command(ctx, argparse.Namespace(**dict(vars(args), connect="10.0.0.2:7777")), "client", 0,
                                     17777, "c.log")
        self.assertIn("/Game/Maps/Title", solo)
        self.assertFalse(any("127.0.0.1" in a for a in solo))
        self.assertNotIn("-DisablePython", solo)
        self.assertIn("10.0.0.2:7777", joined)

    def test_cli_version_and_lease_reason(self):
        parser = ue.build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(["--version"])
        self.assertIn("ue (Tether)", out.getvalue())
        self.assertEqual(parser.parse_args(["lease", "take", "--reason", "map rebuild"]).what, "map rebuild")
        self.assertEqual(parser.parse_args(["pie", "run", "-c", "print(1)"]).code, "print(1)")

    def test_screenshot_paths_from_exec(self):
        shot = self.project.write("Saved/Screenshots/WindowsEditor/ScreenShot00003.png", "png")
        ctx = types.SimpleNamespace(project=self.project.uproject)
        lines = [stamped('LogCore: Display: Tracing Screenshot "ScreenShot00003" taken with size: 1280 x 720'),
                 stamped("LogHighResScreenshot: High resolution screenshot saved as D:/x/HighresScreenshot00001.png")]
        self.assertEqual(ue.written_screenshots(ctx, lines), [shot, "D:/x/HighresScreenshot00001.png"])

    def test_map_open_refuses_unsaved_maps(self):
        ctx = types.SimpleNamespace(project=self.project.uproject, beacon={}, tether=lambda *a, **k: "True")
        args = argparse.Namespace(path="/Game/Maps/Spire", save=False, discard=False)
        with unittest.mock.patch.object(ue.content, "py_json", return_value={"maps": ["/Game/Maps/Title"], "assets": 0}):
            with self.assertRaises(ue.CliError) as caught:
                ue.map_open(ctx, args)
        self.assertIn("--save or --discard", str(caught.exception))
        with self.assertRaises(ue.CliError):
            ue.map_open(types.SimpleNamespace(project=self.project.uproject, beacon={"pie": True}), args)

    def test_others_edits_and_link_owners(self):
        p = self.project.uproject
        header = self.project.write("Source/Game/SDPlayerController.h", "UCLASS()\nclass GAME_API ASDPlayerController : "
                                                                         "public APlayerController {};\n")
        cpp = self.project.write("Source/Game/SDMenu.cpp", "int x;")
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "menu"}):
            ue.record_edit(p, header, "menu", now=time.time())
            ue.record_edit(p, cpp, "menu", now=time.time())
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "robots"}):
            theirs = ue.others_edits(p, [header, cpp], minutes=15)
            self.assertEqual([os.path.basename(t[0]) for t in theirs], ["SDPlayerController.h", "SDMenu.cpp"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ue.print_link_owners(p, ['LNK2001 unresolved external symbol "public: void __cdecl '
                                         'ASDPlayerController::DebugGive(int)"'])
            self.assertIn("ASDPlayerController::DebugGive: declared in SDPlayerController.h (edited by menu", out.getvalue())
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "menu"}):
            self.assertEqual(ue.others_edits(p, [header], minutes=15), [])  # my own

    def test_layout_patch_note_is_per_editor_process(self):
        p = self.project.uproject
        ue.write_json(ue.layout_marker(p), {"pid": 42, "time": 1, "headers": ["SDRobot.h"]})
        self.assertIn("SDRobot.h", ue.layout_patch_note(p, {"pid": 42}))
        self.assertIsNone(ue.layout_patch_note(p, {"pid": 43}))  # restarted since

    def test_protected_property_hint(self):
        error = ("Exception: Objective: Property 'Target' for attribute 'target' on 'SDObjective' is protected and "
                 "cannot be read")
        self.assertIn("ue props <object> --match Target", ue.property_name_hint(FakeCtx(), error))

    def test_reflected_name_clashes(self):
        self.project.write("Source/Game/Game.Build.cs", "")
        self.project.write("Source/Game/SetPiece.h", "USTRUCT(BlueprintType)\nstruct FSDSetPiece {};\n")
        self.project.write("Source/Game/SetPieceActor.h", "UCLASS()\nclass GAME_API ASDSetPiece : public AActor {};\n"
                                                          "UENUM()\nenum class ESDMode : uint8 {};\n")
        clashes = ue.reflected_name_clashes(self.project.uproject)
        self.assertEqual(sorted(clashes), ["SDSetPiece"])
        self.assertEqual(sorted(t for t, _ in clashes["SDSetPiece"]), ["ASDSetPiece", "FSDSetPiece"])

    def test_process_tree_has_this_process(self):
        self.assertIn(os.getpid(), ue.process_tree(os.getpid()))


def ui_node(role, label, ref, pos="0,0", size="100,20", flags=()):
    return {"role": role, "label": label, "ref": ref, "pos": pos, "size": size, "flags": list(flags), "indent": 2}


class ShooterDominationRound2(unittest.TestCase):
    """0.24: ShooterDomination's 0.23 notes."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_find_ui_by_index_and_near(self):
        nodes = [ui_node("text", "DEBUG HUD: DIRECTOR", "t1", pos="10,100"), ui_node("button", "OFF", "b1", pos="300,10"),
                 ui_node("button", "OFF", "b2", pos="300,100"), ui_node("text", "DEBUG HUD: WAVES", "t2", pos="10,200"),
                 ui_node("button", "OFF", "b3", pos="300,200"), ui_node("button", "OFF", "b0", size="0,0")]
        self.assertEqual(ue.find_ui(nodes, "OFF#2")["ref"], "b2")
        self.assertEqual(ue.find_ui(nodes, "OFF", near="debug hud: waves")["ref"], "b3")
        self.assertEqual(ue.find_ui(nodes, "OFF", near="DIRECTOR")["ref"], "b2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ue.find_ui(nodes, "OFF")["ref"], "b1")  # the first visible one, with a note
        self.assertIn("3 widgets match 'OFF'", out.getvalue())
        with self.assertRaises(ue.CliError):
            ue.find_ui(nodes, "OFF#4")
        with self.assertRaises(ue.CliError):
            ue.find_ui(nodes, "OFF", near="nowhere")
        literal = nodes + [ui_node("button", "Slot#2", "s2")]
        self.assertEqual(ue.find_ui(literal, "Slot#2")["ref"], "s2")  # a label that really has a #

    def test_edit_ledger_keeps_every_writer(self):
        p = self.project.uproject
        path = self.project.write("Docs/Notes.md", "x")
        ue.record_edit(p, path, "menu", now=time.time() - 60)
        ue.record_edit(p, path, "depth", now=time.time())
        record = ue.read_json(ue.edits_path(p))[ue.edit_key(p, path)]
        self.assertEqual(record["session"], "depth")
        self.assertEqual(sorted(record["by"]), ["depth", "menu"])

    def test_post_edit_hook_records_any_project_file(self):
        notes = self.project.write("Docs/Notes.md", "x")
        self.assertIsNone(ue.hook_post_edit({"tool_input": {"file_path": notes}, "session_id": "abcdef1234"}))
        record = ue.read_json(ue.edits_path(self.project.uproject))[ue.edit_key(self.project.uproject, notes)]
        self.assertEqual(record["session"], "abcdef12")

    def test_sort_changes_mine_shared_theirs_unknown(self):
        p = self.project.uproject
        now = time.time()
        files = {name: self.project.write(f"Source/Game/{name}.cpp", "x", mtime=now - 30)
                 for name in ("Mine", "Shared", "Theirs", "Saved")}
        ue.record_edit(p, files["Mine"], "me", now=now - 20)
        ue.record_edit(p, files["Shared"], "menu", now=now - 25)
        ue.record_edit(p, files["Shared"], "me", now=now - 20)
        ue.record_edit(p, files["Theirs"], "menu", now=now - 20)
        rows = [(" M", f) for f in files.values()] + [(" D", os.path.join(self.project.root, "Gone.cpp"))]
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "me"}):
            split = ue.sort_changes(p, rows, committed=lambda path: now - 3600)
            self.assertEqual(split["mine"], [files["Mine"]])
            self.assertEqual(split["shared"], [(files["Shared"], ["menu"])])
            self.assertEqual(split["theirs"], [(files["Theirs"], "menu")])
            self.assertEqual(len(split["unknown"]), 2)  # written by something else, and a deletion
            # menu's write came before the last commit: no longer shared
            self.assertEqual(ue.sort_changes(p, rows, committed=lambda path: now - 22)["mine"],
                             [files["Mine"], files["Shared"]])

    def test_bind_keys_and_imc_name_tables(self):
        self.project.write("Source/Game/PC.cpp", "InputComponent->BindKey(FInputChord(EKeys::G, /*Shift*/false, "
                                                 "/*Ctrl*/true, false, false), IE_Pressed, this, &A::Menu);\n"
                                                 "InputComponent->BindKey(EKeys::F5, IE_Pressed, this, &A::Save);\n")
        binds = ue.bind_keys(self.project.uproject)
        self.assertEqual([(c, k, line) for c, k, _, line in binds], [("Ctrl+G", "G", 1), ("F5", "F5", 2)])
        imc = os.path.join(self.project.root, "Content", "Input", "IMC_Default.uasset")
        os.makedirs(os.path.dirname(imc))
        with open(imc, "wb") as f:
            f.write(b"\x00junk" + (2).to_bytes(4, "little") + b"G\x00" + b"more")
        self.assertEqual(ue.imc_keys_offline(self.project.uproject, {"G", "F5"}), {"G": [("IMC_Default", "")]})
        ctx = types.SimpleNamespace(project=self.project.uproject)
        with unittest.mock.patch.object(ue.content, "editor_live", return_value=None):
            conflicts, source = ue.bind_key_conflicts(ctx, binds)
        self.assertEqual([c[0] for c in conflicts], ["Ctrl+G"])
        self.assertIn("IMC_", source)

    def test_log_grep_reaches_verbose(self):
        lines = [stamped("LogSD: Verbose: took the jump pad"), stamped("LogSD: Warning: low ammo")]
        self.assertEqual(len(ue.filter_log(lines, "VeryVerbose", pattern="jump pad")), 1)
        self.assertEqual(len(ue.filter_log(lines, "Log", pattern="jump pad")), 0)

    def test_hud_gone_note(self):
        p = self.project.uproject
        ue.write_json(os.path.join(ue.state_dir(p), "last-hud.json"), {"widgets": 34, "map": "/Game/Spire", "time": 1})
        ctx = FakeCtx("0")
        ctx.project, ctx.beacon = p, {"map": "/Game/Spire", "pie": True}
        self.assertIn("had 34", ue.hud_gone_note(ctx))
        ctx.reply = "12"
        self.assertIsNone(ue.hud_gone_note(ctx))
        ctx.reply, ctx.beacon = "0", {"map": "/Game/Title"}
        self.assertIsNone(ue.hud_gone_note(ctx))  # another map: nothing to compare with

    def test_pie_loop_arguments(self):
        parser = ue.build_parser()
        args = parser.parse_args(["pie", "loop", "stat unit; wait 5", "--seconds", "30", "--every", "10", "memreport"])
        ctx = FakeCtx(json.dumps({"running": False, "ended": "done", "ran": 7, "passes": 6, "every_runs": 3,
                                  "elapsed": 30, "seconds": 30, "errors": []}))
        ctx.project, ctx.beacon = self.project.uproject, {"pie": True}
        self.project.write("Saved/Logs/Game.log", "")
        out = io.StringIO()
        with unittest.mock.patch.object(ue.playtest, "flush_editor_log"), contextlib.redirect_stdout(out):
            self.assertEqual(ue.pie_command(ctx, args), 0)
        tool, kwargs = ctx.calls[0]
        self.assertEqual((tool, kwargs["script"], kwargs["seconds"], kwargs["every"], kwargs["every_script"]),
                         ("pie_loop", "stat unit; wait 5", 30, 10.0, "memreport"))
        self.assertIn("7 command(s), 6 pass(es), the --every commands 3 time(s)", out.getvalue())
        with self.assertRaises(ue.CliError):
            ue.pie_command(ctx, parser.parse_args(["pie", "watch", "player.Health", "--every", "x"]))

    def test_shot_frames_contact_sheet(self):
        tile = ue.png_bytes(8, 4, bytes([200, 40, 40] * 32))
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        args = ue.shot_args(frames=3, interval=0.01, output=os.path.join(self.project.root, "sheet.png"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.shot_frames(ctx, args, False, lambda: tile), 0)
        with open(args.output, "rb") as f:
            width, height, _, _ = ue.png_read(f.read())
        self.assertEqual(len(ctx.data["times"]), 3)
        self.assertGreater(width, height)
        with self.assertRaises(ue.CliError):
            ue.shot_frames(ctx, ue.shot_args(frames=1), False, lambda: tile)

    def test_python_only_plugin_update(self):
        uplugin = '{\n\t"Version": %d,\n\t"VersionName": "0.%d.0",\n\t"Modules": []\n}\n'
        for side, version in (("src", 33), ("dst", 32)):
            self.project.write(f"{side}/Tether.uplugin", uplugin % (version, version))
            self.project.write(f"{side}/Content/Python/tether/toolsets.py", f"V = {version}\n")
            self.project.write(f"{side}/Source/Tether/Tether.cpp", "// same\n")
        src, dst = (os.path.join(self.project.root, d) for d in ("src", "dst"))
        self.assertTrue(ue.python_only_update(src, dst))
        self.project.write("src/Source/Tether/Tether.cpp", "// changed\n")
        self.assertFalse(ue.python_only_update(src, dst))

    def test_build_files_go_with_check(self):
        parser = ue.build_parser()
        self.assertEqual(parser.parse_args(["build", "--check", "--files", "Source/A.cpp", "Source/B.h"]).files,
                         ["Source/A.cpp", "Source/B.h"])
        with self.assertRaises(ue.CliError):
            ue.cmd_build(types.SimpleNamespace(project=self.project.uproject),
                         parser.parse_args(["build", "--files", "Source/A.cpp"]))


def transcript_line(kind, stamp, **block):
    """One Claude Code transcript record holding a tool_use or tool_result block."""
    return json.dumps({"type": "assistant" if kind == "tool_use" else "user", "timestamp": stamp,
                       "message": {"content": [dict(type=kind, **block)]}}) + "\n"


class UnrealRacingRound(unittest.TestCase):
    """0.25: UnrealRacing's notes and ShooterDomination's rainy-night pass."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_stamped_names_do_not_collide(self):
        folder = os.path.join(self.project.root, "shots")
        os.makedirs(folder)
        first = ue.stamped_name(folder, "shot", ".png")
        open(first, "w").close()
        second = ue.stamped_name(folder, "shot", ".png")
        self.assertNotEqual(first, second)
        self.assertRegex(os.path.basename(first), r"^shot-\d{8}-\d{6}-\d{3}")

    def test_log_noise_shapes(self):
        lines = [stamped('LogJson: Warning: Property "Cloud Coverage" type FMulticastDelegateProperty unhandled during '
                         'Json schema generation'),
                 "[2026.10.12-10.13.00:858][  0]LogAutomationTest: Error: Condition failed",
                 stamped("LogAutomationTest: Error: Condition failed"),
                 stamped("LogSD: Warning: real")]
        shown = ue.filter_log(lines, "Warning")
        self.assertEqual(len(shown), 2)  # the frame-0 boot test is gone; one later in the session stays
        self.assertTrue(any("real" in l for l in shown))

    def test_py_pitfalls_round(self):
        kept = ("arr = []\nfor slot in mesh.get_editor_property('static_materials'):\n"
                "    slot.set_editor_property('x', m)\n    arr.append(slot)\n")
        self.assertFalse(any("copies" in n for n in ue.py_pitfalls(kept)))  # the edited copy is written back
        notes = " ".join(ue.py_pitfalls("mpc = unreal.MaterialParameterCollection\nmpc.set_editor_property("
                                        "'scalar_parameters', [p])\nunreal.MaterialProperty.MP_CUSTOM_DATA0"))
        self.assertIn("new IDs", notes)
        self.assertIn("clear coat", notes)

    def test_live_coding_patches_counted_until_ubt(self):
        p = self.project.uproject
        ue.mark_green(p, live=True)
        ue.mark_green(p, live=True)
        self.assertEqual(ue.live_patches(p), 2)
        ue.mark_green(p, "cs_time")
        self.assertEqual(ue.live_patches(p), 2)  # a C# reload is not a UBT build
        ue.mark_green(p)
        self.assertEqual(ue.live_patches(p), 0)

    def test_transcript_attribution(self):
        path = os.path.join(self.project.root, "t.jsonl")
        target = self.project.write("Source/Game/A.cpp", "x")
        with open(path, "w", encoding="utf-8") as f:
            f.write(transcript_line("tool_use", "2026-10-16T08:00:00.000Z", id="w1", name="Write",
                                    input={"file_path": target}))
            f.write(transcript_line("tool_result", "2026-10-16T08:00:01.500Z", tool_use_id="w1", content="ok"))
            f.write(transcript_line("tool_use", "2026-10-16T08:01:00.000Z", id="b1", name="Bash",
                                    input={"command": "python build.py"}))
            f.write(transcript_line("tool_result", "2026-10-16T08:02:00.000Z", tool_use_id="b1", content="done"))
            f.write(transcript_line("tool_use", "2026-10-16T08:03:00.000Z", id="r1", name="Read",
                                    input={"file_path": target}))
            f.write(transcript_line("tool_result", "2026-10-16T08:03:30.000Z", tool_use_id="r1", content="x"))
        writes, spans = ue.transcript_events(path, 0)
        base = ue.stamp_time(ue.STAMP.search('"timestamp":"2026-10-16T08:00:00.000Z"'))
        self.assertEqual(len(writes), 1)
        self.assertEqual(spans, [(base + 60, base + 120)])  # the Read is not a span
        sessions = {"aaaa1111": (writes, spans), "bbbb2222": ([], [(base + 100, base + 130)])}
        self.assertEqual(ue.transcript_owners(target, base + 1.5, sessions), {"aaaa1111"})
        self.assertEqual(ue.transcript_owners(target, base + 110, sessions), {"aaaa1111", "bbbb2222"})
        self.assertEqual(ue.transcript_owners(target, base + 125, sessions), {"bbbb2222"})
        os.utime(target, (base + 1.5, base + 1.5))
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "aaaa1111-full", "UE_SESSION": ""}):
            split = ue.sort_changes(self.project.uproject, [(" M", target)], committed=lambda p: 0.0,
                                    sessions=sessions)
        self.assertEqual(split["mine"], [target])
        self.assertEqual(split["via"], {"transcripts": 1})

    def test_claude_plugin_setting(self):
        home = os.path.join(self.project.root, "home")
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            self.assertIsNone(ue.claude_plugin_enabled(self.project.uproject))
            path = ue.enable_claude_plugin(self.project.uproject)
            self.assertEqual(ue.claude_plugin_enabled(self.project.uproject), path)
            settings = ue.read_json(path)
            ref, market, root = ue.claude_plugin()
            self.assertTrue(settings["enabledPlugins"][ref])
            self.assertEqual(settings["extraKnownMarketplaces"][market]["source"]["path"], root)

    def fake_engine(self):
        engine = os.path.join(self.project.root, "UE")
        blank = os.path.join(engine, "Templates", "TP_Blank")
        files = {"TP_Blank.uproject": json.dumps({"FileVersion": 3, "Modules": [{"Name": "TP_Blank", "Type": "Runtime"}]}),
                 "Config/DefaultEngine.ini": "[/Script/EngineSettings.GameMapsSettings]\nGameDefaultMap=/Engine/M\n",
                 "Config/DefaultGame.ini": "", "Config/TemplateDefs.ini":
                 'SharedContentPacks=(MountName="Vehicles",DetailLevels=("Standard"))\n',
                 "Source/TP_Blank.Target.cs": "public class TP_BlankTarget : TargetRules {}\n",
                 "Source/TP_Blank/TP_Blank.Build.cs": "public class TP_Blank : ModuleRules\n{\n\tpublic TP_Blank("
                                                      "ReadOnlyTargetRules Target) : base(Target)\n\t{\n\t\tPCHUsage = "
                                                      "PCHUsageMode.UseExplicitOrSharedPCHs;\n\t}\n}\n",
                 "Source/TP_Blank/TP_Blank.cpp": 'IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, TP_Blank, '
                                                 '"TP_Blank");\n', "Media/x.png": "png"}
        for rel, text in files.items():
            full = os.path.join(blank, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
        pack = os.path.join(engine, "Templates", "TemplateResources", "Standard", "Vehicles")
        os.makedirs(os.path.join(pack, "Content", "SportsCar"))
        with open(os.path.join(pack, "Content", "SportsCar", "SKM_Car.uasset"), "wb") as f:
            f.write(b"\x00/Game/Vehicles/SportsCar/SKM_Car\x00/Game/Input/IA_Go\x00")
        os.makedirs(os.path.join(pack, "__ExternalActors__", "Maps"))
        open(os.path.join(pack, "__ExternalActors__", "Maps", "A.uasset"), "w").close()
        os.makedirs(os.path.join(engine, "Templates", "TemplateResources", "High", "Input", "Content"))
        return engine

    def test_new_project_from_template(self):
        engine = self.fake_engine()
        folder = os.path.join(self.project.root, "made", "Racer")
        args = ue.build_parser().parse_args(["new", "project", folder, "--cpp"])
        ctx = types.SimpleNamespace(_project=None)
        with unittest.mock.patch.dict(os.environ, {"UE_ROOT": engine}), \
                unittest.mock.patch.object(ue.setup, "cmd_init", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.new_project(ctx, args), 0)
        self.assertIn("-> /Game/Vehicles", out.getvalue())
        descriptor = ue.read_json(os.path.join(folder, "Racer.uproject"))
        self.assertEqual(descriptor["Modules"][0]["Name"], "Racer")
        with open(os.path.join(folder, "Source", "Racer", "Racer.Build.cs"), encoding="utf-8") as f:
            build = f.read()
        self.assertIn("public class Racer : ModuleRules", build)
        self.assertIn("PublicIncludePaths.Add(ModuleDirectory);", build)
        self.assertTrue(os.path.isfile(os.path.join(folder, "Source", "Racer", "RacerGameMode.h")))
        with open(os.path.join(folder, "Config", "DefaultEngine.ini"), encoding="utf-8") as f:
            self.assertIn("GlobalDefaultGameMode=/Script/Racer.RacerGameMode", f.read())
        self.assertFalse(os.path.exists(os.path.join(folder, "Media")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "Content", "Vehicles", "SportsCar", "SKM_Car.uasset")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "Content", "__ExternalActors__", "Vehicles", "Maps", "A.uasset")))
        self.assertEqual(ctx._project, os.path.join(folder, "Racer.uproject"))
        with self.assertRaises(ue.CliError):
            ue.new_project(ctx, ue.build_parser().parse_args(["new", "project", folder]))  # not empty

    def test_template_pack_needs(self):
        engine = self.fake_engine()
        mount, level, files, needs = ue.add_template_pack(self.project.uproject, engine, "vehicles", dry_run=True)
        self.assertEqual((mount, level, files), ("Vehicles", "Standard", 2))
        self.assertEqual(needs, ["Input"])  # a pack it refers to that the project lacks
        with self.assertRaises(ue.CliError):
            ue.add_template_pack(self.project.uproject, engine, "Nope")

    def test_lint_mpc_and_clear_view(self):
        ctx = FakeCtx(json.dumps({"collections": 1, "materials": 2, "fixed": 0, "stale": [
            {"material": "/Game/M_Neon", "parameter": "Glow", "collection": "/Game/MPC_City", "why": "new ID"}]}))
        args = argparse.Namespace(path="/Game", fix=False, dry_run=False, limit=30, strict=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.lint_mpc(ctx, args), 1)
        self.assertIn("/Game/M_Neon: Glow (MPC_City)", out.getvalue())
        pose = {"location": {"x": 0, "y": 0, "z": 500}}
        with unittest.mock.patch.object(ue.images, "py_json", return_value=[560, 0, 500, 560, "Tower"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ue.clear_view(FakeCtx(), pose, "3000,0,500", False)
        self.assertEqual(pose["location"]["x"], 560)
        self.assertIn("in front of Tower", out.getvalue())
        with self.assertRaises(ue.CliError):
            ue.clear_view(FakeCtx(), pose, None, False)


class UnrealRacingRound2(unittest.TestCase):
    """0.26: UnrealRacing's notes 126-141, and older open items."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_shadow_project_overlays_and_stays_incremental(self):
        p = self.project.uproject
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"FileVersion": 3, "AdditionalPluginDirectories": ["../Plugins"]}, f)
        self.project.write("Source/Game/A.cpp", "int a;\n")
        self.project.write("Plugins/Kit/Kit.uplugin", "{}")
        self.project.write("Plugins/Kit/Build/Scripts/Kit.csproj", "<x/>")
        self.project.write("Plugins/Kit/Binaries/Win64/Kit.dll", "bin")
        stage = os.path.join(self.project.root, "stage")
        os.makedirs(os.path.join(stage, "Source", "Game"))
        with open(os.path.join(stage, "Source", "Game", "New.h"), "w") as f:
            f.write("#pragma once\n")
        shadow, overlaid = ue.shadow_project(p, stage, "me")
        root = os.path.dirname(shadow)
        self.assertEqual(overlaid, ["Source/Game/New.h"])
        for rel in ("Source/Game/A.cpp", "Source/Game/New.h", "Plugins/Kit/Build/Scripts/Kit.csproj"):
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(root, "Plugins", "Kit", "Binaries")))
        descriptor = ue.read_json(shadow)
        self.assertTrue(os.path.isabs(descriptor["AdditionalPluginDirectories"][0]))
        before = os.path.getmtime(os.path.join(root, "Source", "Game", "A.cpp"))
        time.sleep(0.05)
        os.remove(os.path.join(stage, "Source", "Game", "New.h"))
        with open(os.path.join(stage, "Source", "Game", "Other.h"), "w") as f:
            f.write("#pragma once\n")
        ue.shadow_project(p, stage, "me")
        self.assertEqual(os.path.getmtime(os.path.join(root, "Source", "Game", "A.cpp")), before)  # untouched
        self.assertFalse(os.path.exists(os.path.join(root, "Source", "Game", "New.h")))  # gone from the overlay
        with self.assertRaises(ue.CliError):
            ue.shadow_project(p, os.path.join(self.project.root, "empty"), "me")

    def test_lease_request_and_grant(self):
        p = self.project.uproject
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "map rebuild", 15)
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "asker"}):
            self.assertEqual(ue.request_lease(p, "build --restart", 10)["session"], "holder")
            with self.assertRaises(ue.CliError):
                ue.grant_lease(p)  # not the holder
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            self.assertIn("waiting for the editor: asker", ue.lease_messages(p)[0])
            self.assertEqual(ue.lease_messages(p), [])  # said once
            lease = ue.grant_lease(p)
        self.assertEqual((lease["session"], lease["what"]), ("asker", "build --restart"))
        self.assertEqual(ue.lease_requests(p), [])
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "asker"}):
            self.assertIn("granted by holder", ue.lease_messages(p)[0])
            self.assertEqual(ue.lease_messages(p), [])  # said once

    def test_an_idle_lease_lapses_while_another_session_waits(self):  # UnrealRacing 2026-10-26: 1.9 h queued at an idle editor
        p, t = self.project.uproject, time.time()
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "rebuild + shots", 40, now=t)
            ue.note_lease_active(p, now=t + 60)  # a command ended a minute in
        self.assertEqual(ue.active_lease(p, now=t + 600)["session"], "holder")  # idle, but nobody waits
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "asker"}):
            ue.request_lease(p, "PIE suites", 10, now=t + 120)
        self.assertEqual(ue.active_lease(p, now=t + 60 + 170)["session"], "holder")  # under LEASE_IDLE
        self.assertIsNone(ue.active_lease(p, now=t + 60 + ue.LEASE_IDLE * 60 + 1))  # lapsed: the asker may take it
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            with unittest.mock.patch.object(ue.share, "holder_pie_running", return_value=True):
                self.assertEqual(ue.active_lease(p, now=t + 900)["session"], "holder")  # its PIE test still runs
            note = ue.lease_messages(p, now=t + 900)
            self.assertIn("lapsed", note[0])
            self.assertIn("asker", note[0])
            self.assertEqual(ue.lease_messages(p, now=t + 901), [])  # said once
            ue.take_lease(p, "unsaved hand edits", 30, now=t + 1000, hold=True)
        self.assertEqual(ue.active_lease(p, now=t + 2500)["session"], "holder")  # --hold never lapses

    def test_a_release_holds_while_another_process_reads_the_lease(self):  # Windows: no delete while a reader has it open
        p = self.project.uproject
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "PIE suites", 15)
            with unittest.mock.patch.object(ue.share.os, "remove", side_effect=PermissionError("in use")), \
                    unittest.mock.patch.object(ue.share.time, "sleep"):
                held, back = ue.release_lease(p)
        self.assertEqual((held["what"], back), ("PIE suites", None))
        self.assertIsNone(ue.active_lease(p))  # released, though the file could not be deleted
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "asker"}):
            self.assertEqual(ue.take_lease(p, "rebuild", 15)[1], "taken")

    def test_find_with_a_path_glob(self):
        ctx = FakeCtx("9 assets under /Game/UDS\n/Game/UDS/Presets/P_Rain  DataAsset\n/Game/UDS/Presets/P_Snow  "
                      "DataAsset\n/Game/UDS/Blueprints/BP_Sky  Blueprint")
        args = argparse.Namespace(query="/Game/UDS/Presets/*", cls=None, under="/Game", limit=30)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ue.cmd_find(ctx, args)
        self.assertEqual(ctx.calls[0][1]["under"], "/Game/UDS/Presets")
        self.assertIn("2 assets matching /Game/UDS/Presets/*", out.getvalue())
        self.assertNotIn("BP_Sky", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            ue.cmd_find(ctx, argparse.Namespace(query="/Game/UDS/Presets", cls=None, under="/Game", limit=30))
        self.assertEqual(ctx.calls[-1][1]["under"], "/Game/UDS/Presets")  # a folder: what is in it

    def test_step_file_shot_and_log_counts(self):
        args = ue.shot_step_args("--at 90000,55500,3800 --rot -89,0,0", False)
        self.assertEqual((args.at, args.rot, args.output), ("90000,55500,3800", "-89,0,0", None))
        self.assertEqual(ue.shot_step_args("out.png --hud", False).output, "out.png")
        with self.assertRaises(ue.CliError):
            ue.shot_step_args("--zoom 3", False)
        lines = [stamped("LogRace: gate 1"), stamped("LogRace: finished A"), stamped("LogRace: finished B")]
        self.assertEqual(ue.log_check("finished count >= 2", lambda: lines)()[0], True)
        self.assertEqual(ue.log_check("finished count == 3", lambda: lines)()[0], False)
        self.assertTrue(ue.log_check("in order: gate 1, finished A, finished B", lambda: lines)()[0])
        ok, detail = ue.log_check("in order: finished B, gate", lambda: lines)()
        self.assertFalse(ok)
        self.assertIn("missing /gate/", detail)

    def test_sticky_target_and_beacon_retry(self):
        path = self.project.write("Saved/Tether/x.json", "{bad")
        self.assertIsNone(ue.read_json_retry(path, tries=2))
        self.assertIsNone(ue.read_json_retry(os.path.join(self.project.root, "missing.json")))
        with open(path, "w") as f:
            f.write('{"pid": 3}')
        self.assertEqual(ue.read_json_retry(path), {"pid": 3})

    def test_stale_code_note(self):
        p = self.project.uproject
        ue.mark_green(p, live=True, pid=111)
        self.assertIn("runs 1 Live Coding patch(es) newer than the last UBT build", ue.stale_code_note(p, {"pid": 111}))
        self.assertIn("without the 1 Live Coding patch(es) made in pid 111", ue.stale_code_note(p, {"pid": 222}))
        ue.mark_green(p)
        self.assertIsNone(ue.stale_code_note(p, {"pid": 222}))

    def test_smoke_maps_and_parsers(self):
        self.project.write("Content/Maps/L_A.umap", "")
        self.project.write("Content/Developers/me/L_Test.umap", "")
        self.project.write("Content/__ExternalActors__/Maps/L_A/x.uasset", "")
        maps, skipped = ue.smoke_maps(self.project.uproject, None)
        self.assertEqual((maps, skipped), (["/Game/Maps/L_A"], 0))
        self.assertEqual(ue.smoke_maps(self.project.uproject, "/Game/A, /Game/B")[0], ["/Game/A", "/Game/B"])
        parser = ue.build_parser()
        self.assertEqual(parser.parse_args(["class", "BP_Sky", "--functions", "--grep", "cloud"]).section, "functions")
        self.assertEqual(parser.parse_args(["build", "--check", "--overlay", "stage"]).overlay, "stage")
        self.assertTrue(parser.parse_args(["build", "--when-closed"]).when_closed)
        self.assertEqual(parser.parse_args(["exec", "stat", "unit", "--shot"]).shot, "")
        self.assertEqual(parser.parse_args(["lease", "grant", "asker"]).to, "asker")

    def test_exec_shot_runs_after_the_command(self):
        order = []
        args = ue.build_parser().parse_args(["exec", "SD.Flash", "--shot", "a.png"])
        with unittest.mock.patch.object(ue.cli, "cmd_exec", side_effect=lambda ctx, a, after=None: (
                order.append("exec"), after())), \
                unittest.mock.patch.object(ue.cli, "cmd_shot", side_effect=lambda ctx, a: order.append(a.output)):
            ue.exec_command(FakeCtx(), args)
        self.assertEqual(order, ["exec", "a.png"])

    def test_py_hints_round(self):
        self.assertTrue(ue.EDITOR_WORLD_API.search("unreal.get_editor_subsystem(unreal.EditorActorSubsystem)"))
        self.assertFalse(ue.EDITOR_WORLD_API.search("tether.world()"))
        ctx = FakeCtx("True")
        hint = ue.property_name_hint(ctx, "AttributeError: 'NRRoadNode' object has no attribute 'rebuild_junction'")
        self.assertIn("call_method('RebuildJunction')", hint)

    def test_collide_picture(self):
        ctx = FakeCtx()
        ctx.project, ctx.beacon = self.project.uproject, {}
        parts = [{"keys": ["NRRoadNode"], "grid": [[1, 0]], "actors": {"NRRoadNode": ["J_1"]}},
                 {"keys": ["Wall", "NRRoadNode"], "grid": [[2, 1]], "actors": {"Wall": ["W"], "NRRoadNode": ["J_2"]}}]
        args = argparse.Namespace(where="0,0,100", m=False, step=100.0, z=None, half=40.0, pie=False,
                                  output=os.path.join(self.project.root, "c.png"))
        with unittest.mock.patch.object(ue.content, "COLLIDE_CELLS", 2), \
                unittest.mock.patch.object(ue.content, "py_json", side_effect=parts), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_collide(ctx, args), 0)
        self.assertTrue(os.path.isfile(args.output))
        self.assertIn("75% blocked", out.getvalue())  # two calls of one row each, merged
        self.assertIn("NRRoadNode: 2 cell(s), e.g. J_1, J_2", out.getvalue())
        self.assertIn("Wall: 1 cell(s)", out.getvalue())
        with self.assertRaisesRegex(ue.CliError, "not JSON: Traceback ... RuntimeError: boom"):
            ue.py_json(FakeCtx("Traceback ...\nRuntimeError: boom"), "x")


class UnrealRacingRound3(unittest.TestCase):
    """0.27: UnrealRacing's notes 142-159."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def beacon(self, pid, **fields):
        ue.write_json(os.path.join(ue.state_dir(self.project.uproject), "editors", f"{pid}.json"),
                      dict({"pid": pid, "heartbeat": time.time(), "started": 0}, **fields))

    def test_second_editor_is_its_owners_only(self):
        p = self.project.uproject
        self.beacon(os.getpid(), launched_by_tether=True, headless=True, started=100)
        self.beacon(os.getppid(), launched_by_tether=True, headless=True, started=200, owner="captures")
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "other"}):  # newest, but another session's
            self.assertEqual(ue.chosen_editor(p)["pid"], os.getpid())
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "captures"}):
            ue.remember_target(p, {"pid": os.getppid()})
            self.assertEqual(ue.chosen_editor(p)["pid"], os.getppid())
            self.assertIn("this session's own", ue.editor_label(ue.chosen_editor(p)))
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
                ue.take_lease(p, "map rebuild", 15)
            ue.check_shared(p, "build --restart", destructive=True)  # its own editor: the lease is the shared one's
            self.assertIsNone(ue.lease_note(p))
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "other"}):
            with self.assertRaises(ue.CliError):
                ue.check_shared(p, "build --restart", destructive=True)
        args = ue.build_parser().parse_args(["open", "--second"])
        self.assertTrue(args.second)

    def test_lease_requests_are_said_once_then_reminded(self):
        p = self.project.uproject
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "menu pass", 60)
        for sid, what in (("a", "captures"), ("b", "material rebuild")):
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": sid}):
                ue.request_lease(p, what, 10)
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            first = ue.lease_messages(p)
            self.assertEqual(len(first), 1)  # both requests on one line
            self.assertIn("a (", first[0])
            self.assertIn("material rebuild", first[0])
            self.assertEqual(ue.lease_messages(p), [])
            self.assertEqual(len(ue.lease_messages(p, now=time.time() + 11 * 60)), 1)  # reminded

    def test_wait_lease(self):
        p = self.project.uproject
        ctx = FakeCtx()
        ctx.project = p
        args = argparse.Namespace(what="lease", max=0, clock="real")
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "menu pass", 60)
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "waiter"}):
            with self.assertRaisesRegex(ue.CliError, "leased by holder"):
                ue.cmd_wait(ctx, args)
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
                ue.release_lease(p)
            self.beacon(os.getpid(), pie=True)
            with self.assertRaisesRegex(ue.CliError, "PIE is running"):
                ue.cmd_wait(ctx, args)
            self.beacon(os.getpid(), pie=False)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ue.cmd_wait(ctx, args), 0)
            self.assertIn("the editor is free", out.getvalue())

    def test_validate_without_git_takes_what_the_editor_saved(self):
        p = self.project.uproject
        self.project.write("Saved/Logs/Game.log")
        old = self.project.write("Content/Maps/L_Old.umap", "", mtime=1000)
        new = self.project.write("Content/Maps/L_Hikari.umap")
        self.project.write("Plugins/Kit/Content/K.uasset")
        found = ue.saved_since(p, 2000)
        self.assertIn(new, found)
        self.assertNotIn(old, found)
        self.assertTrue(any(f.endswith("K.uasset") for f in found))
        ctx = FakeCtx({"checked": 1, "not_validated": 0, "problems": [], "broken_refs": []})
        ctx.project, ctx.beacon = p, {"started": 2000, "map": "/Game/Maps/L_Hikari"}
        args = argparse.Namespace(paths=[], changed=None, map_check=False, limit=20, strict=False)
        with unittest.mock.patch.object(ue.assets, "run_git", return_value=None), \
                unittest.mock.patch.object(ue.assets, "flush_editor_log"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ue.cmd_validate(ctx, args)
        sent = ctx.calls[0][1]["paths"].split(",")
        self.assertIn("/Game/Maps/L_Hikari", sent)
        self.assertNotIn("/Game/Maps/L_Old", sent)
        self.assertIn("saved since the editor started", out.getvalue())

    def test_tethers_own_screenshot_lookups_leave_the_log(self):
        lines = [stamped("LogConsoleManager: Warning: Performance warning: Many (31) failed FindConsoleObject() for 'shot'"),
                 stamped("LogGame: Warning: rival stuck")]
        kept = ue.filter_log(lines, level="Warning")
        self.assertEqual(len(kept), 1)
        self.assertIn("rival stuck", kept[0])

    def test_unlabeled_buttons_take_the_text_under_them(self):
        nodes = ue.parse_snapshot(
            'generic [pos=0,0 size=400,100]\n'
            '  image [pos=0,0 size=400,100]\n'
            '  text "GARAGE" [pos=20,30 size=100,40]\n'
            '  button [ref=b120] [pos=0,0 size=400,100]\n'
            '  button "QUIT" [ref=b121] [pos=0,200 size=400,100]\n')
        ue.label_buttons(nodes)
        node = ue.find_ui(nodes, "GARAGE")
        self.assertEqual(node["ref"], "b120")
        self.assertIn("text under it", node["flags"])
        lines = ue.ui_lines(nodes, rects=True)
        self.assertTrue(any('button "GARAGE" [text under it]  (b120)  @0,0 400x100' in l for l in lines))
        self.assertIsNone(ue.os_window_at((10, 10), None))

    def test_keys_go_through_slate_when_a_menu_has_focus(self):
        focused = ue.parse_snapshot('button "PLAY" [ref=b1] [focused] [pos=0,0 size=10,10]')
        sent = []
        with unittest.mock.patch.object(ue.play, "ui_nodes", return_value=(focused, True)), \
                unittest.mock.patch.object(ue.play, "mcp_tool", side_effect=lambda *a: sent.append(a[3]["key"])), \
                unittest.mock.patch.object(ue.play, "game_key", side_effect=lambda ctx, t: t):
            self.assertIn('through Slate (button "PLAY" has keyboard focus)', ue.press_key(None, "enter"))
            self.assertEqual(sent, ["Enter"])
            self.assertIn("in the game", ue.press_key(None, "Escape"))  # Escape stays the game's
            self.assertIn("in the game", ue.press_key(None, "Enter", "game"))
        with unittest.mock.patch.object(ue.play, "ui_nodes", return_value=([], True)), \
                unittest.mock.patch.object(ue.play, "umg_focus", return_value=None), \
                unittest.mock.patch.object(ue.play, "game_key", side_effect=lambda ctx, t: t):
            self.assertEqual(ue.press_key(None, "Enter"), "pressed Enter in the game")
        sent.clear()
        with unittest.mock.patch.object(ue.play, "ui_nodes", return_value=([], True)), \
                unittest.mock.patch.object(ue.play, "umg_focus", return_value="WBP_Title_C"), \
                unittest.mock.patch.object(ue.play, "mcp_tool", side_effect=lambda *a: sent.append(a[3]["key"])):
            # 0.28: a title screen holding focus itself is not in the snapshot; UMG says it has focus
            self.assertIn("UMG widget WBP_Title_C has keyboard focus", ue.press_key(None, "Enter"))
            self.assertEqual(sent, ["Enter"])
        args = ue.build_parser().parse_args(["ui", "key", "Enter", "--game"])
        self.assertTrue(args.game)

    def test_world_subsystem_hint(self):
        hint = ue.property_name_hint(FakeCtx(), "AttributeError: module 'unreal' has no attribute "
                                                "'SubsystemBlueprintLibrary'")
        self.assertIn("tether.world_subsystem(", hint)

    def test_fab_remove(self):
        p = self.project.uproject
        files = [self.project.write(f"Content/Tokyo/{n}") for n in ("Maps/Demo.umap", "Input/IA_Look.uasset")]
        ue.write_json(os.path.join(ue.state_dir(p), "fab.json"),
                      {"added": {"/Game/Tokyo": {"title": "Tokyo Stylized Environment", "listing": "9eec"}}})
        ue.write_json(os.path.join(ue.state_dir(p), "fab-listings.json"), {})
        item = {"kind": "pack", "path": "/Game/Tokyo", "title": "Tokyo Stylized Environment", "listing": "9eec"}
        ctx = FakeCtx()
        ctx.project, ctx.beacon = p, {"map": "/Game/Maps/L_Hikari"}
        args = argparse.Namespace(rest=["Tokyo"], dry_run=False, force=False, ignore_lease=False)
        refs = {"assets": 2, "outside": [["/Game/Maps/L_Hikari", "/Game/Tokyo/Input/IA_Look"]]}
        with unittest.mock.patch.object(ue.fab, "project_fab", return_value=[item]), \
                unittest.mock.patch.object(ue.fab, "editor_live", return_value=True), \
                unittest.mock.patch.object(ue.fab, "py_json", return_value=refs), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.fab_remove(ctx, args), 1)
        self.assertIn("nothing deleted", out.getvalue())
        self.assertTrue(all(os.path.isfile(f) for f in files))
        real_remove = os.remove

        def locked(path):  # the editor holds the Input Action open
            if path.endswith("IA_Look.uasset"):
                raise PermissionError(path)
            real_remove(path)
        args.force = True
        with unittest.mock.patch.object(ue.fab, "project_fab", return_value=[item]), \
                unittest.mock.patch.object(ue.fab, "editor_live", return_value=True), \
                unittest.mock.patch.object(ue.fab, "py_json", side_effect=[refs, [["/Game/Tokyo", False]]]), \
                unittest.mock.patch.object(ue.fab.os, "remove", side_effect=locked), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.fab_remove(ctx, args), 0)
        self.assertIn("deleted when it closes", out.getvalue())
        self.assertFalse(os.path.exists(files[0]))
        self.assertTrue(os.path.exists(files[1]))
        self.assertNotIn("/Game/Tokyo", ue.read_json(os.path.join(ue.state_dir(p), "fab.json"))["added"])
        done, left = ue.flush_deletes(p)  # the editor has closed
        self.assertEqual((len(done), left), (1, []))
        self.assertFalse(os.path.isdir(os.path.join(self.project.root, "Content", "Tokyo")))
        args = ue.build_parser().parse_args(["fab", "remove", "Tokyo", "--dry-run"])
        self.assertEqual((args.action, args.dry_run), ("remove", True))

    def test_failed_steps_release_held_keys(self):
        calls = []

        class Ctx:
            project = "x.uproject"

            def tether(self, tool, **kw):
                calls.append((tool, kw))
                if tool == "pie":
                    return "running"
                return {"ok": "fail" not in kw["step"], "detail": "d"}

        args = argparse.Namespace(script=["key W down", "key E down", "key E up", "expect fail.x = 1"], file=None,
                                  reject=None, warmup=0, keep=False, keep_going=False)
        with unittest.mock.patch.object(ue, "log_end", return_value=0), \
                unittest.mock.patch.object(ue.play, "slate_focus", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.pie_do(Ctx(), args), 1)
        steps = [kw["step"] for tool, kw in calls if tool == "pie_step"]
        self.assertEqual(steps[-1], "key W up")
        self.assertNotIn("key E up", steps[4:])
        self.assertIn("released W (still down", out.getvalue())

    def test_split_key_steps_reach_a_focused_umg_screen(self):  # 209
        calls, sent = [], []

        class Ctx:
            project = "x.uproject"

            def tether(self, tool, **kw):
                calls.append((tool, kw))
                return "running" if tool == "pie" else {"ok": True, "detail": "d"}

        def run(script, focus):
            calls.clear(), sent.clear()
            args = argparse.Namespace(script=script, file=None, reject=None, warmup=0, keep=False, keep_going=False)
            with unittest.mock.patch.object(ue, "log_end", return_value=0), \
                    unittest.mock.patch.object(ue.play, "slate_focus", return_value=focus), \
                    unittest.mock.patch.object(ue.play, "mcp_tool", side_effect=lambda *a: sent.append(a[3]["key"])), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ue.pie_do(Ctx(), args), 0)
            return [kw["step"] for tool, kw in calls if tool == "pie_step"], out.getvalue()

        steps, out = run(["key E down", "key E up", "key Enter tap"], "UMG widget WBP_Map_C has keyboard focus")
        self.assertEqual((steps, sent), ([], ["E", "Enter"]))  # one whole press each, at down / tap
        self.assertIn("pressed E through Slate (UMG widget WBP_Map_C has keyboard focus)", out)
        self.assertIn("E up: nothing left to send", out)
        steps, _ = run(["key --slate Enter down", "key --slate Enter up"], None)
        self.assertEqual((steps, sent), ([], ["Enter"]))
        steps, _ = run(["key W down", "key W up", "key --game E down", "key --game E up"], "a button has focus")
        self.assertEqual(sent, ["W"])  # with a widget focused, W went to it; --game kept E in the game
        self.assertEqual(steps, ["key E down", "key E up"])
        steps, _ = run(["key W down 0.5", "key W up"], None)
        self.assertEqual((steps, sent), (["key W down 0.5", "key W up"], []))  # nothing focused: the game's input

    def test_watch_csv_names_rotator_parts(self):
        path = os.path.join(self.project.root, "w.csv")
        result = {"targets": ["player.GetActorRotation()", "player.location"], "kinds": ["rotator", None],
                  "samples": [[0.0, 0.0, [0, 90, 0], [1, 2, 3]]]}
        ue.write_watch_csv(path, result)
        with open(path, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        self.assertIn("player.GetActorRotation().yaw", header)
        self.assertIn("player.location.x", header)
        del result["kinds"]  # an older plugin: told apart by the name
        ue.write_watch_csv(path, result)
        with open(path, encoding="utf-8") as f:
            self.assertIn("player.GetActorRotation().pitch", f.readline())

    def test_overlay_folders_are_per_session(self):
        p = self.project.uproject
        with self.assertRaisesRegex(ue.CliError, "no staging folder for this session"):
            ue.claim_overlay(p, None, "handling")  # a check creates nothing
        ue.set_session_name(p, "signals", "handling")
        os.makedirs(os.path.join(ue.state_dir(p), "staging", "signals-handling", "Source"))
        mine = ue.claim_overlay(p, None, "handling")  # `ue who --as signals`: <name>-<id>
        self.assertTrue(mine.endswith(os.path.join("staging", "signals-handling")))
        shared = os.path.join(self.project.root, "staging")
        os.makedirs(shared)
        self.assertEqual(ue.claim_overlay(p, shared, "menus"), shared)
        with self.assertRaisesRegex(ue.CliError, "session menus compiled"):
            ue.claim_overlay(p, os.path.join(self.project.root, "Staging"), "handling")  # same folder on Windows
        self.assertEqual(ue.claim_overlay(p, shared, "handling", now=time.time() + 13 * 3600), shared)  # stale claim
        self.assertEqual(ue.build_parser().parse_args(["build", "--check", "--overlay"]).overlay, "-")

    def test_an_active_lease_lasts_past_the_last_command(self):
        p = self.project.uproject
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(p, "city rebuild", 15)
            later = time.time() + 12 * 60  # 3 min left
            notes = ue.lease_messages(p, now=later)
            self.assertIn("lasts 5 min past your last", notes[0])
            self.assertAlmostEqual(ue.active_lease(p, later)["until"], later + 300, delta=1)
            self.assertEqual(ue.lease_messages(p, now=later + 120), [])  # extended again, said once
            self.assertAlmostEqual(ue.active_lease(p, later + 120)["until"], later + 420, delta=1)
        self.assertIsNone(ue.active_lease(p, later + 900))  # idle: it runs out


class UnrealRacingRound4(unittest.TestCase):
    """0.28: UnrealRacing's notes 160-164."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_gpu_ab_alternates_and_compares(self):
        ctx = FakeCtx()
        ctx.project, ctx.beacon, ran, frames = self.project.uproject, {}, [], iter([6.76, 6.89, 6.74, 6.91])

        def gpu(c, a):
            ms = next(frames)
            c.data = {"gpu_ms": ms, "kinds": {"base pass": ms - 4.0, "post": 1.0},
                      "passes": [("BasePass", ms - 4.0), ("PostProcessing", 1.0)]}
        args = ue.parse_command(ue.build_parser(), ["gpu", "--ab", "mat MI_X UseWindows=false",
                                                    "mat MI_X UseWindows=true", "--restore", "mat MI_X --reset UseWindows"])
        with unittest.mock.patch.object(ue.cli, "run_line", side_effect=lambda c, l: (ran.append(l), (0, ""))[1]), \
                unittest.mock.patch.object(ue.cli, "wait_compile"), unittest.mock.patch.object(ue.cli, "cmd_gpu", side_effect=gpu), \
                unittest.mock.patch.object(ue.play, "py_json", return_value={"new": ["/Game/MI_X"], "count": 1, "held": []}), \
                unittest.mock.patch.object(ue.cli.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.gpu_command(ctx, args), 0)
        self.assertEqual(ran, ["mat MI_X UseWindows=false", "mat MI_X UseWindows=true"] * 2 + ["mat MI_X --reset UseWindows"])
        text = out.getvalue()
        self.assertIn("A 6.75 ms, B 6.90 ms per frame: B is +0.15 ms", text)
        self.assertIn("base pass +0.15 ms", text)
        self.assertIn("+0.15 ms  BasePass", text)
        self.assertIn("1 package(s) left unsaved by the switch: /Game/MI_X", text)
        with self.assertRaisesRegex(ue.CliError, "--restore goes with --ab"):
            ue.gpu_command(ctx, ue.parse_command(ue.build_parser(), ["gpu", "--restore", "x"]))

    def test_dirty_packages_are_compared_in_the_editor(self):
        # UnrealRacing: ~6k unsaved packages made the whole list (444,279 chars) overflow a ue py reply
        class Package:
            def __init__(self, name):
                self.name = name

            def get_name(self):
                return self.name

        dirty = [Package(f"/Game/__ExternalActors__/Maps/L/{i:05d}") for i in range(6000)]
        utils = types.SimpleNamespace(get_dirty_map_packages=lambda: [], get_dirty_content_packages=lambda: list(dirty))
        unreal = types.SimpleNamespace(EditorLoadingAndSavingUtils=utils)
        sent = []

        class Ctx(FakeCtx):
            def tether(self, tool, **kwargs):
                sent.append(kwargs["code"])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    exec(kwargs["code"], {"unreal": unreal})
                return out.getvalue()
        ctx = Ctx()
        mark = ue.dirty_mark(ctx)
        dirty += [Package("/Game/MI_X"), Package("/Temp/Untitled_1"), Package("/Game/Maps/L")]
        since = ue.dirty_since(ctx, mark, ["/Game/Maps/L", "/Game/Other"])
        self.assertEqual(since, {"new": ["/Game/MI_X", "/Game/Maps/L"], "count": 2, "held": ["/Game/Maps/L"]})
        self.assertLess(max(len(code) for code in sent), 2000)
        self.assertIsNone(ue.dirty_since(ctx, mark)["new"])  # a mark is taken once

    def test_run_line_captures_and_refuses_nesting(self):
        code, out = ue.run_line(FakeCtx(), "ue gpu --ab a b")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot run inside another command", out)

    def test_lint_materials(self):
        ctx = FakeCtx(json.dumps({"checked": 12, "materials": [["/Game/M/M_NR_Building", 43, 20, ["Multiply", "Lerp"]]]}))
        args = argparse.Namespace(path="/Game", min_unused=5, limit=20, strict=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.lint_materials(ctx, args), 1)
        self.assertIn("/Game/M/M_NR_Building: 20 of 43 nodes unused (Multiply, Lerp)", out.getvalue())
        self.assertIn("prune", out.getvalue())
        self.assertTrue(ue.build_parser().parse_args(["lint", "--materials"]).materials)

    def test_link_errors_name_the_missing_symbol_and_its_module(self):
        e = ('E LNK2019 unresolved external symbol "__declspec(dllimport) public: static struct FKey const EKeys::Enter" '
             '(__imp_?Enter@EKeys@@2UFKey@@B) referenced in function "protected: virtual class FReply __cdecl '
             'UTetherProbeTitle::NativeOnKeyDown(struct FGeometry const &,struct FKeyEvent const &)"')
        mine = 'error LNK2019: unresolved external symbol "public: void __cdecl AFoo::Bar(int)" referenced in function "x"'
        self.assertEqual(ue.unresolved_symbols([e, mine]), [("EKeys", "Enter", True), ("AFoo", "Bar", False)])
        self.project.write("Source/Game/Foo.h", "class GAME_API AFoo : public AActor { void Bar(int); };")
        with unittest.mock.patch.object(ue.build, "engine_module_of", return_value="InputCore"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ue.print_link_owners(self.project.uproject, [e, mine])
        self.assertIn('EKeys::Enter: from another module\'s DLL: add "InputCore" to the Build.cs dependencies', out.getvalue())
        self.assertIn("AFoo::Bar: declared in Foo.h", out.getvalue())
        self.assertNotIn("NativeOnKeyDown", out.getvalue())


class UnrealRacingRound5(unittest.TestCase):
    """0.29: UnrealRacing's notes 165-179."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_log_keeps_the_indented_reason_line(self):
        lines = [stamped("LogMaterial: Warning: [AssetLog] D:/P/UnrealRacing/Content/NR/M_NR_SignPanel.uasset: "
                         "Failed to compile Material for platform PCD3D_SM6, Default Material will be used in game."),
                 "\t(Node Custom) Custom material NR missing input 1 (WP)",
                 stamped("LogMaterial: Display: next")]
        shown = ue.filter_log(lines)
        self.assertEqual(len(shown), 1)
        self.assertIn("| (Node Custom) Custom material NR missing input 1 (WP)", shown[0])
        ctx = FakeCtx("M_NR_SignPanel (Material)\ncompiled /Game/NR/M_NR_SignPanel: no errors")
        stale = ue.stale_material_failures(ctx, list(shown))
        self.assertEqual(stale, {shown[0]: "M_NR_SignPanel"})
        self.assertEqual(ctx.calls[0][1]["target"], "/Game/NR/M_NR_SignPanel")

    def test_startup_crash_seen_before(self):
        reports = [{"time": 2000.0, "type": "Crash", "error": "Unhandled Exception: EXCEPTION_ACCESS_VIOLATION",
                    "stack": ["ProceduralMeshComponent", "Renderer"], "folder": "b"},
                   {"time": 1000.0, "type": "Crash", "error": "Unhandled Exception: EXCEPTION_ACCESS_VIOLATION",
                    "stack": ["ProceduralMeshComponent", "Renderer"], "folder": "a"}]
        with unittest.mock.patch.object(ue.core, "crash_reports", return_value=reports):
            why = ue.startup_failure(self.project.uproject, 1500)
        self.assertIn("it crashed at startup: Unhandled Exception", why)
        self.assertIn("the same crash happened at " + time.strftime("%H:%M", time.localtime(1000)), why)

    def test_second_editor_boot_reads_its_own_log(self):
        logs = os.path.join(self.project.root, "Saved", "Logs")
        with open(os.path.join(logs, "Game.log"), "w") as f:
            f.write("old\n")
        old = time.time() - 100
        os.utime(os.path.join(logs, "Game.log"), (old, old))
        own = self.project.write("Saved/Logs/Game_2.log", stamped("LogInit: Display: hi") + "\n")
        self.assertEqual(ue.newest_log(self.project.uproject, time.time() - 5), own)
        self.assertEqual(ue.log_file_lines(own), [stamped("LogInit: Display: hi")])

    def test_save_names_the_editor_holding_files(self):
        ctx = FakeCtx()
        ctx.project, ctx.beacon = self.project.uproject, {"pid": 1}
        log = self.project.write("Saved/Logs/Game.log", stamped(
            "LogFileManager: Error: Error moving file 'D:/P/Content/M_NR_LandmarkSign.uasset' to 'Saved/x.tmp'") + "\n")
        with unittest.mock.patch.object(ue.calls, "flush_editor_log"), \
                unittest.mock.patch.object(ue.calls, "log_path", return_value=log), \
                unittest.mock.patch.object(ue.calls, "live_editors", return_value=[{"pid": 1}, {"pid": 7, "owner": "caps"}]):
            note = ue.held_by_other_editor(ctx, 0)
        self.assertIn("M_NR_LandmarkSign.uasset could not be moved aside", note)
        self.assertIn("pid 7 (session caps's --second editor)", note)

    def test_overlay_takes_only_code_folders(self):
        p = self.project.uproject
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"FileVersion": 3}, f)
        self.project.write("Source/Game/A.cpp", "int a;\n")
        stage = os.path.join(self.project.root, "stage")
        for rel in ("Source/Game/New.h", "Source/Game/New.cpp", "checksums.txt", "notes/plan.md"):
            path = os.path.join(stage, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
        shadow, overlaid = ue.shadow_project(p, stage, "me")
        self.assertEqual(overlaid, ["Source/Game/New.cpp", "Source/Game/New.h"])
        self.assertEqual(ue.shadow_project.ignored, ["checksums.txt", "notes/plan.md"])

    def test_actors_exit_1_without_echoing_the_pattern(self):
        ctx = FakeCtx("0 actors matching /^SkidPad$/")
        args = argparse.Namespace(match="^SkidPad$", limit=60, pie=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_actors(ctx, args), 1)
        self.assertNotIn("SkidPad", out.getvalue())
        ctx = FakeCtx("0 assets under /Game matching /NoSuch/")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.cmd_find(ctx, argparse.Namespace(query="NoSuch", cls=None, under="/Game", limit=30)), 1)

    def test_pie_delete_and_spawn_replace(self):
        ctx = FakeCtx()
        ctx.beacon = {"pie": True}
        with unittest.mock.patch.object(ue.calls, "py_json", return_value=["CrashWall"]) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_delete(ctx, argparse.Namespace(actors=["CrashWall", "Ghost"], pie=True)), 0)
        self.assertIn("['CrashWall', 'Ghost']", run.call_args[0][1])
        self.assertIn("not found: Ghost", out.getvalue())
        with self.assertRaisesRegex(ue.CliError, "--replace goes with --pie --label"):
            ue.cmd_spawn(ctx, argparse.Namespace(what="X", pie=True, label=None, replace=True))
        args = ue.build_parser().parse_args(["spawn", "/Game/W", "--pie", "--label", "CrashWall", "--replace"])
        self.assertTrue(args.replace)
        self.assertTrue(ue.build_parser().parse_args(["delete", "A", "--pie"]).pie)

    def test_pie_run_prints_a_json_result_and_waits_for_finish(self):
        calls = []

        class Ctx:
            project, transport, beacon = self.project.uproject, "mcp", {"pie": True}

            def tether(self, tool, **kw):
                calls.append(kw.get("code", ""))
                return {"passes": 3} if "exec(compile" in kw.get("code", "") else "running"
        self.project.write("Saved/Logs/Game.log", "")
        script = self.project.write("checks.py", "import json\nprint(json.dumps({'passes': 3}))\nopen('/d/x.json')")
        args = argparse.Namespace(script=[script], code=None, expect=None, reject=None, seconds=0.1, keep=True,
                                  warmup=0, wait=5, wait_file=None)
        with unittest.mock.patch.object(ue.play, "wait_run_result", return_value={"done": True, "value": {"ok": 1}}), \
                unittest.mock.patch.object(ue.play, "flush_editor_log"), \
                unittest.mock.patch.object(ue.play, "unwrap", return_value=True), \
                unittest.mock.patch.object(Ctx, "mcp", create=True, new=types.SimpleNamespace(call=lambda *a: ("true", False))), \
                unittest.mock.patch.object(ue.play, "resolve_toolset", return_value="x"), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ue.pie_run(Ctx(), args), 0)
        self.assertIn('script: {"passes": 3}', out.getvalue())
        self.assertIn('result: {"ok": 1}', out.getvalue())
        self.assertIn("sys._tether_run_result = None", calls[0])
        self.assertIn("/d/Projects/... in the script means", err.getvalue())

    def test_pie_status_says_stopped(self):
        self.assertTrue(ue.POSIX_DRIVE.search("open('/d/Projects/x')"))
        self.assertFalse(ue.POSIX_DRIVE.search("load('/Game/Maps/L')"))

    def test_steps_from_stdin(self):
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        seen = {}
        args = ue.parse_command(ue.build_parser(), ["pie", "do", "-f", "-", "--keep"])
        with unittest.mock.patch.object(ue.playtest, "cmd_pie", side_effect=lambda c, a: seen.update(file=a.file) or 0), \
                unittest.mock.patch("sys.stdin", io.StringIO("exec NRFreeRoam\nwait 3 game\n")):
            ue.pie_command(ctx, args)
        with open(seen["file"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "exec NRFreeRoam\nwait 3 game\n")

    def test_camera_options_on_gpu_and_perf(self):
        p = ue.build_parser()
        self.assertEqual(p.parse_args(["gpu", "--at", "1,2,3", "--look-at", "Pole_1"]).look_at, "Pole_1")
        self.assertEqual(p.parse_args(["perf", "--camera", "street"]).camera, "street")
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        with ue.camera_at(ctx, argparse.Namespace(at=None, look_at=None, rot=None, actor=None, camera=None)) as pose:
            self.assertIsNone(pose)
        self.assertEqual(ctx.calls, [])
        ue.write_json(os.path.join(ue.state_dir(ctx.project), "shots", "baselines", "street.json"),
                      {"pose": {"location": {"x": 1, "y": 2, "z": 3}, "rotation": {"pitch": -10, "yaw": 90, "roll": 0}}})
        self.assertEqual(ue.baseline_pose(ctx, "street"), ("1,2,3", "-10,90,0"))

    def test_lease_renewal_shows_the_new_expiry(self):
        p = self.project.uproject
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "tokyo"}):
            first, _ = ue.take_lease(p, "final shots", 15, now=1000.0)
            lease, renewed = ue.take_lease(p, "final shots", 15, now=1000.0 + 6 * 60)
        self.assertTrue(renewed)
        text = ue.describe_lease(p, lease, now=1000.0 + 6 * 60)
        until = time.strftime("%H:%M", time.localtime(1000.0 + 21 * 60))
        since = time.strftime("%H:%M", time.localtime(1000.0))
        self.assertIn(f"until {until} (15 min left), held since {since}", text)


class UnrealRacingRound6(unittest.TestCase):
    """0.29.2: UnrealRacing's notes 181-182."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_live_coding_reaches_every_editor(self):
        p = self.project.uproject
        log = self.project.write("Saved/Logs/Other.log", "")
        size = os.path.getsize(log)
        with open(log, "a") as f:
            f.write(stamped("LogLiveCoding: Display: Starting Live Coding compile.") + "\n"
                    + stamped("LogLiveCoding: Display: Live coding succeeded") + "\n")
        quiet = self.project.write("Saved/Logs/Quiet.log", "")
        user = {"pid": 79284, "launched_by_tether": False, "pie": True}
        found = ue.editors_also_patched(p, {79284: (user, log, size), 5: ({"pid": 5}, quiet, 0)}, wait=0.3)
        self.assertEqual(found, [user])
        ue.mark_green(p, live=True, pid=16404, also=[79284])
        ue.mark_green(p, live=True, pid=16404)
        self.assertIn("runs 2 Live Coding patch(es) newer", ue.stale_code_note(p, {"pid": 16404}))
        self.assertIn("plus 1 of the 2 Live Coding patch(es)", ue.stale_code_note(p, {"pid": 79284}))
        self.assertIn(", without the 2", ue.stale_code_note(p, {"pid": 5}))
        ue.mark_green(p)
        self.assertIsNone(ue.stale_code_note(p, {"pid": 79284}))

    def test_looking_does_not_pick_the_sessions_editor(self):
        p = self.project.uproject
        folder = ue.state_dir(p)
        for pid, fields in ((os.getpid(), {"launched_by_tether": True, "headless": True, "started": 200}),
                            (os.getppid(), {"launched_by_tether": False, "started": 100})):
            ue.write_json(os.path.join(folder, "editors", f"{pid}.json"),
                          dict({"pid": pid, "heartbeat": time.time()}, **fields))
        try:
            ue.core.KEEP_TARGET = False
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "fresh"}):
                self.assertEqual(ue.chosen_editor(p)["pid"], os.getppid())  # nobody at work: the user's
            self.assertIsNone(ue.read_json(os.path.join(folder, "targets.json")))
        finally:
            ue.core.KEEP_TARGET = True
        # the lease holder's editor outweighs the others'
        ue.write_json(os.path.join(folder, "targets.json"),
                      {"a": {"pid": os.getppid(), "time": time.time()}, "b": {"pid": os.getppid(), "time": time.time()},
                       "rivals": {"pid": os.getpid(), "time": time.time()}, "old": os.getppid()})
        ue.write_json(os.path.join(folder, "lease.json"), {"session": "rivals", "until": time.time() + 600})
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "fresh"}):
            self.assertEqual(ue.chosen_editor(p)["pid"], os.getpid())
        self.assertEqual(ue.read_json(os.path.join(folder, "targets.json"))["fresh"]["pid"], os.getpid())

    def test_when_closed_job_drops_the_pid(self):
        seen = {}
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        args = ue.build_parser().parse_args(["--pid", "16404", "build", "--when-closed"])
        with unittest.mock.patch.dict(os.environ, {"UE_EDITOR_PID": "16404"}), \
                unittest.mock.patch.object(ue.cli, "live_editors", return_value=[{"pid": 16404}]), \
                unittest.mock.patch.object(ue.cli, "cmd_job",
                                           side_effect=lambda c, a: seen.update(pid=os.environ.get("UE_EDITOR_PID")) or 0), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.build_command(ctx, args), 0)
            self.assertEqual(os.environ.get("UE_EDITOR_PID"), "16404")  # this command's own choice is kept
        self.assertIsNone(seen["pid"])


class Batch030(unittest.TestCase):
    """0.30: NEXT.md rows 32, 9a, 9c, 34, 40, 37, 57, 44, 42, 17."""

    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_shot_parser_takes_view_burst_and_window(self):
        p = ue.build_parser()
        args = p.parse_args(["shot", "--view", "shadercomplexity", "--burst", "5", "--every", "0.2"])
        self.assertEqual((args.view, args.burst, args.every), ("shadercomplexity", 5, 0.2))
        self.assertEqual(p.parse_args(["shot", "--window", "Outliner"]).window, "Outliner")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["shot", "--view", "pretty"])
        self.assertEqual(set(ue.VIEW_MODES), set(p._subparsers._group_actions[0].choices["shot"]._option_string_actions["--view"].choices))

    def test_window_and_tab_regions_from_a_snapshot(self):
        snapshot = "\n".join([
            'window "Sandbox - Unreal Editor" [pos=0,0 size=1280,720] [ref=w1]',
            '    tab "L_TetherProbe" [pos=99,25 size=210,31] [ref=t2]',
            '      splitter [pos=808,102 size=468,582] [ref=sp6]',
            '        tab "Outliner" [pos=808,102 size=160,25] [ref=t5]',
            '          text "Outliner" [pos=835,106 size=45,15] [ref=x23]',
            '        text "4 actors" [pos=822,243 size=47,15] [ref=x25]',
            '        tab "Details" [pos=808,272 size=160,25] [ref=t6]',
            '        tab "World Settings" [pos=970,272 size=160,25] [ref=t7]',
            'window "Content Browser" [pos=1126,289 size=1280,720] [ref=w2]',
        ])
        rows = ue.slate_entries(snapshot)
        window, tab, rect = ue.find_window_or_tab(rows, "outliner")
        self.assertEqual((window[7], tab[7], rect), ("w1", "t5", (808, 102, 468, 170)))
        window, tab, rect = ue.find_window_or_tab(rows, "Details")
        self.assertEqual(rect, (808, 272, 468, 412))  # down to the splitter's bottom
        window, tab, rect = ue.find_window_or_tab(rows, "content browser")
        self.assertEqual((window[7], tab, rect), ("w2", None, None))
        with self.assertRaisesRegex(ue.CliError, 'no editor window or tab "Mission".*tab "Outliner"'):
            ue.find_window_or_tab(rows, "Mission")

    def test_compile_times_by_file_and_module(self):
        lines = [r"time(C:\VC\bin\Hostx64\x64\c1xx.dll)=3.10s < 1 - 2 > BB [D:\G\Source\Game\A.cpp]",
                 r"time(C:\VC\bin\Hostx64\x64\c2.dll)=0.60s < 2 - 3 > BB [D:\G\Source\Game\A.cpp]",
                 r"time(C:\VC\bin\Hostx64\x64\c1xx.dll)=1.00s < 1 - 2 > BB "
                 r"[D:\G\Plugins\P\Intermediate\Build\Win64\x64\UnrealGame\Development\PCore\Module.PCore.cpp]",
                 "[3/9] Compile [x64] A.cpp"]
        times = ue.compile_times(lines)
        self.assertEqual(times[r"D:\G\Source\Game\A.cpp"], [3.10, 0.60])
        self.assertEqual(ue.module_of(r"D:\G\Source\Game\A.cpp"), "Game")
        self.assertEqual(ue.module_of(r"D:\G\Plugins\P\Intermediate\Build\Win64\x64\UnrealGame\Development\PCore\M.cpp"),
                         "PCore")
        timing = self.project.write("Intermediate/A.cpp.timing", "Include Headers:\n\tCount: 2\n"
                                    "\t\tD:\\G\\Source\\Game\\A.h: 0.84s\n\t\t\tC:\\E\\UserWidget.h: 0.79s\n"
                                    "Class Definitions:\n\tCount: 1\n\t\tFoo: 0.5s\n")
        costs = ue.header_costs([timing, timing], self.project.root)
        self.assertEqual(costs["D:\\G\\Source\\Game\\A.h"], [1.68, 2])
        self.assertNotIn("Foo", costs)

    def test_startup_report_phases_and_pauses(self):
        def at(sec, text, frame=0):
            return f"[2026.10.17-12.25.{sec:02d}:000][{frame:>3}]{text}"
        lines = ["LogConfig: Display: Loading ini files took 0.09 seconds",
                 at(0, "LogInit: Running DelayedAutoRegister Phase PreRHIInit"),
                 at(1, "LogRHI: Using Default RHI: D3D12"),
                 at(2, "LogInit: Running DelayedAutoRegister Phase ShaderTypesReady"),
                 at(3, "LogModuleManager: InternalLoadLibrary: 'Slow'"),
                 at(9, "LogSlow: done"),
                 at(10, "LogInit: Display: Engine is initialized. Leaving FEngineLoop::Init()"),
                 at(11, "LogUnrealEdMisc: Total Editor Startup Time, took 13.335"),
                 at(30, "LogTemp: much later", frame=500)]
        report = ue.startup_report(lines)
        self.assertEqual(report["total"], 11)
        self.assertEqual([p[0] for p in report["phases"]][:3], ["config, modules and plugins", "RHI (graphics device)",
                                                                  "shader types"])
        self.assertEqual(report["reported"]["editor"], 13.335)
        self.assertEqual(report["gaps"][0][0], 6)
        self.assertIn("'Slow'", report["gaps"][0][1])
        with self.assertRaisesRegex(ue.CliError, "no time-stamped startup lines"):
            ue.startup_report(["plain"])

    def test_stats_recorded_and_summarised(self):
        p = self.project.uproject
        ue.record_stat(p, "pie start", 0, 2.0, now=1000)
        ue.record_stat(p, "pie start", 4, 9.0, error="timed out after 30 s waiting for /Game/Maps/L_A", now=1001)
        ue.record_stat(p, "pie start", 4, 8.0, error="timed out after 45 s waiting for /Game/Maps/L_B", now=1002)
        ue.record_stat(p, "status", 0, 0.1, now=1003)
        ctx = FakeCtx()
        ctx.project = p
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_stats(ctx, argparse.Namespace(days=None, limit=30)), 0)
        text = out.getvalue()
        self.assertIn("4 ue command(s)", text)
        self.assertRegex(text, r"pie start\s+3\s+2 66%")
        self.assertIn("2x  ue pie start: timed out", text)  # the two time-outs group despite their numbers and paths
        self.assertEqual(ctx.data["status"], {"runs": 1, "failed": 0})

    def test_lint_fixes_meshes_only_when_asked(self):
        found = [("mesh-lods", "/Game/Rocks/SM_Rock", "120000 triangles, one LOD and no Nanite"),
                 ("mesh-collision", "/Game/Rocks/SM_Rock", "no simple collision")]
        self.assertEqual([i["fix"] for i in ue.lint_plan(found)], ["box"])
        self.assertEqual([i["fix"] for i in ue.lint_plan(found, {"mesh-lods"})], ["nanite", "box"])
        self.assertEqual(ue.lint_plan(found, {"mesh-lods"}, "lods")[0]["fix"], "lods:4")

    def test_move_pack_reports_config_and_code_that_name_the_old_path(self):
        self.project.write("Config/DefaultGame.ini", "[/Script/X]\nMenuMap=/Game/CityPack/Maps/L_Demo.L_Demo\n")
        self.project.write("Source/Game/A.cpp", 'auto M = TEXT("/Game/Other/M_A");\n')
        ctx = FakeCtx(json.dumps({"moved": 12, "maps": ["/Game/CityPack/Maps/L_Demo"], "referencers": ["/Game/Maps/L_Main"],
                                  "notes": [], "left": []}))
        ctx.project = self.project.uproject
        args = argparse.Namespace(folder="/Game/CityPack", dest=None, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ue.cmd_move_pack(ctx, args), 0)
        self.assertEqual(ctx.calls[0][1]["dest"], "/Game/ThirdParty/CityPack")
        self.assertIn("moved 12 asset(s) /Game/CityPack -> /Game/ThirdParty/CityPack, 1 map(s)", out.getvalue())
        self.assertIn(os.path.join("Config", "DefaultGame.ini") + ":2", out.getvalue())
        self.assertNotIn("A.cpp", out.getvalue())

    def test_call_rejects_arguments_without_a_value(self):
        class Ctx:
            transport, project, beacon = "mcp", self.project.uproject, {}
        args = argparse.Namespace(target="SlateInspectorToolset.Windows", kv=["list"], args=None, json=False)
        tools = {"Windows": {"name": "SlateInspectorToolset.Windows", "inputSchema": {"properties": {}}}}
        with unittest.mock.patch.object(ue.calls, "resolve_toolset", return_value="SlateInspectorToolset"), \
                unittest.mock.patch.object(ue.calls, "schema", return_value=tools):
            with self.assertRaisesRegex(ue.CliError, "arguments are key=value \\(not 'list'\\)"):
                ue.cmd_call(Ctx(), args)

    def test_level_tab_brought_forward_only_when_hidden(self):
        rows = ue.slate_entries("\n".join([
            'window "Sandbox - Unreal Editor" [pos=0,0 size=1280,720] [ref=w1]',
            '    tab "L_TetherProbe" [pos=102,28 size=210,31] [ref=t2]',
            '    tab "Message Log" [pos=314,28 size=210,31] [ref=t3]']))
        self.assertEqual(ue.level_tab_hidden(rows, "L_TetherProbe")[7], "t2")
        self.assertIsNone(ue.level_tab_hidden(rows + [(12, "tab", "Viewport 1", 4, 102, 160, 25, "t4")], "L_TetherProbe"))


class Round031(unittest.TestCase):
    """0.31: UnrealRacing's notes 184-196, ShooterDomination's 197-200."""

    def setUp(self):
        self.project = Project()
        self.p = self.project.uproject
        self.log = self.project.write("Saved/Logs/Game.log", "")

    def tearDown(self):
        self.project.close()

    def ctx(self, beacon=None, reply=""):
        ctx = FakeCtx(reply)
        ctx.project, ctx.beacon = self.p, beacon or {}
        return ctx

    def running(self, pid, session, cmd, since):
        ue.write_json(os.path.join(ue.running_dir(self.p), f"{pid}.json"),
                      {"session": session, "cmd": cmd, "pid": pid, "since": since})

    # 186, 193: a running command keeps its session's lease and says what the editor is busy with
    def test_a_running_command_keeps_its_sessions_lease(self):
        with unittest.mock.patch("tether_cli.share.process_name", return_value=None):
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
                ue.take_lease(self.p, "city rebuild", 1)
            later = time.time() + 3600
            self.assertIsNone(ue.active_lease(self.p, later))
            self.running(os.getppid(), "holder", "ue py -f Scripts/build_city_layout.py city", time.time() - 420)
            self.running(999999, "holder", "ue py dead", time.time())  # gone: its record goes
            held = ue.active_lease(self.p, later)
            self.assertEqual(held["session"], "holder")
            self.assertIn("busy: `ue py -f Scripts/build_city_layout.py city` for 7 min", ue.describe_lease(self.p, held))
            self.assertFalse(os.path.exists(os.path.join(ue.running_dir(self.p), "999999.json")))
            rows = ue.running_commands(self.p)
            self.assertEqual([r["pid"] for r in rows], [os.getppid()])
            self.assertIn("holder `ue py -f Scripts/build_city_layout.py city` for 7 min",
                          ue.describe_running(self.p, rows[0]))
            ue.stop_running(os.path.join(ue.running_dir(self.p), f"{os.getppid()}.json"))
            self.assertIsNone(ue.active_lease(self.p, later))

    def test_start_and_stop_running(self):
        path = ue.start_running(self.p, ["ue", "py", "-f", "x.py"])
        self.assertEqual(ue.read_json(path)["cmd"], "ue py -f x.py")
        self.assertEqual(ue.running_commands(self.p), [])  # this process's own record is not listed
        ue.stop_running(path)
        self.assertFalse(os.path.exists(path))

    def test_status_says_busy_rather_than_hung(self):
        ctx = self.ctx({"pid": os.getpid(), "heartbeat": time.time() - 90, "map": "/Game/M", "engine": "5.8"})
        ctx.transport = "mcp"
        self.running(os.getppid(), "gen", "ue py -f Scripts/gen.py city", time.time() - 80)
        with unittest.mock.patch("tether_cli.share.process_name", return_value=None), \
                unittest.mock.patch("tether_cli.editor.editor_process", return_value=True), \
                unittest.mock.patch("tether_cli.editor.blocking_dialogs", return_value=[]), \
                unittest.mock.patch("tether_cli.editor.port_open", return_value=True), \
                unittest.mock.patch("tether_cli.editor.live_editors", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ue.cmd_status(ctx, argparse.Namespace())
        self.assertIn("busy: gen `ue py -f Scripts/gen.py city` for 80s", out.getvalue())
        self.assertNotIn("HUNG", out.getvalue())

    def test_keep_lease_counts_from_the_end_of_a_command(self):
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(self.p, "suite", 1)
            self.assertTrue(ue.keep_lease(self.p))
            self.assertAlmostEqual(ue.active_lease(self.p)["until"], time.time() + 300, delta=2)
            self.assertFalse(ue.keep_lease(self.p))  # 5 min left already

    # 190: lease pending / yield
    def lease_args(self, action, **kw):
        return argparse.Namespace(action=action, name=None, what=kw.get("what"), minutes=15, to=None, force=False,
                                  back=kw.get("back", False))

    def test_lease_pending_and_yield(self):
        ctx = self.ctx()
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "races"}):
            ue.take_lease(self.p, "race suite", 30)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ue.cmd_lease(ctx, self.lease_args("pending")), 1)
                self.assertEqual(ue.cmd_lease(ctx, self.lease_args("yield")), 0)
            self.assertIn("nobody is waiting", out.getvalue())
            self.assertEqual(ue.active_lease(self.p)["session"], "races")
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "kinks"}):
            ue.request_lease(self.p, "route kinks", 10)
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "races"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ue.cmd_lease(ctx, self.lease_args("pending")), 0)
                # --back: kinks releases while races waits; races takes it again
                release = lambda s: os.remove(ue.lease_path(self.p))  # noqa: E731
                with unittest.mock.patch("tether_cli.editor.time.sleep", side_effect=release):
                    self.assertEqual(ue.cmd_lease(ctx, self.lease_args("yield", back=True)), 0)
            text = out.getvalue()
            self.assertIn("requested by kinks", text)
            self.assertIn("yielded to kinks: route kinks", text)
            self.assertIn("the editor is free again", text)
            held = ue.active_lease(self.p)
            self.assertEqual((held["session"], held["what"]), ("races", "race suite"))

    # 189
    def test_wait_lease_takes_the_global_timeout(self):
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "holder"}):
            ue.take_lease(self.p, "x", 30)
        args = argparse.Namespace(what="lease", max=None, clock="real", timeout=0.01, timeout_given=True)
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "waiter"}):
            with self.assertRaisesRegex(ue.CliError, r"still waiting after 0.01s.*--max N waits longer"):
                ue.cmd_wait(self.ctx(), args)

    # 195: who started PIE, and the shells still running above that command
    def test_pie_starter_lists_live_shells_above_the_command(self):
        now = time.time()
        ue.write_json(ue.pie_owner_path(self.p), {"session": "ramps", "time": now, "cmd": "ue pie do -f race.txt",
                                                  "pid": 999999, "parents": [[4242, "bash.exe"], [4343, "bash.exe"],
                                                                             [77, "node.exe"], [88, "bash.exe"]]})
        beacon = {"pie": True, "pie_since": now + 1}
        alive = lambda pid: pid in (4242, 88)  # noqa: E731
        with unittest.mock.patch("tether_cli.share.pid_alive", side_effect=alive), \
                unittest.mock.patch("tether_cli.share.process_name", return_value=None), \
                unittest.mock.patch("tether_cli.share.process_ancestry", return_value=[]), \
                unittest.mock.patch("tether_cli.share.command_lines", return_value={4242: "bash ./run_races.sh"}):
            command, shells = ue.pie_starter(self.p, beacon)
            lines = ue.pie_starter_lines(self.p, beacon)
        self.assertEqual(command, "ue pie do -f race.txt")
        self.assertEqual(shells, [(4242, "bash ./run_races.sh")])  # 88 is above node: the agent's, not a loop
        self.assertIn("pid 4242: bash ./run_races.sh", "\n".join(lines))

    def test_py_refuses_scripts_and_editor_world_code_during_pie(self):
        ctx = self.ctx({"pie": True})
        code = argparse.Namespace(file=None, code=["unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)"])
        with self.assertRaisesRegex(ue.CliError, "PIE is running.*--allow-pie"):
            ue.cmd_py(ctx, code)
        script = self.project.write("Scripts/gen.py", "print('x')\n")
        with self.assertRaisesRegex(ue.CliError, "PIE is running"):
            ue.cmd_py(ctx, argparse.Namespace(file=script, code=[]))
        with contextlib.redirect_stdout(io.StringIO()):  # the game world through tether.world() is fine
            self.assertEqual(ue.cmd_py(ctx, argparse.Namespace(file=None, code=["print(tether.world())"])), 0)
            self.assertEqual(ue.cmd_py(ctx, argparse.Namespace(file=script, code=[], allow_pie=True)), 0)

    # 184
    def test_py_fails_when_a_save_failed(self):
        class Saving(FakeCtx):
            def tether(inner, tool, **kwargs):
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(stamped("Message dialog closed, result: Ok, title: Message, text: Unable to save package "
                                    "to D:/G/Content/L_Bay.umap because the file is read-only!\n"))
                    f.write(stamped("LogSavePackage: Cannot remove 'D:/G/Content/M_Light.uasset' as it is read only!\n"))
                    f.write(stamped("LogFileManager: Display: some unrelated file manager line\n"))
                return "saved"
        ctx = Saving()
        ctx.project, ctx.beacon = self.p, {}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ue.cmd_py(ctx, argparse.Namespace(file=None, code=["build()"])), 1)
        text = err.getvalue()
        self.assertIn("save failed: Unable to save package to D:/G/Content/L_Bay.umap because the file is read-only!", text)
        self.assertIn("save failed: LogSavePackage: Cannot remove 'D:/G/Content/M_Light.uasset' as it is read only!", text)
        self.assertNotIn("unrelated", text)
        self.assertIn("ue save --writable", text)

    # 215
    def test_save_failure_dialog_keeps_its_package_list(self):
        lines = [stamped("Message dialog closed, result: Ok, title: Packages Failed To Save, text: The following assets "
                         "failed to save correctly:").rstrip("\n"),
                 "/Temp/Tether/diff/92846921b942/WHYBJASWY127KGT0KVXXSF",
                 stamped("LogContentValidation: Enabled validators:").rstrip("\n")]
        failed = ue.save_failures(lines)
        self.assertEqual(failed, ["The following assets failed to save correctly: "
                                  "/Temp/Tether/diff/92846921b942/WHYBJASWY127KGT0KVXXSF"])
        self.assertEqual(ue.save_hint(self.p, failed)[0], "an actor bound to a Tether diff copy")

    def test_save_hint_names_the_cause(self):
        diff_copy = ["The following packages failed to save: /Temp/Tether/diff/b5c6e7d7160f/3MDIC43SSQ2X1VGJQTW71G"]
        self.assertEqual(ue.save_hint(self.p, diff_copy)[0], "an actor bound to a Tether diff copy")
        self.assertIn("ue map open <map> --discard", ue.save_hint(self.p, diff_copy)[1])
        self.assertEqual(ue.save_hint(self.p, ["LogSavePackage: Error: Failed to save /Game/Maps/L_Bay"]),
                         ("see ue log", ue.SAVE_OTHER))  # its file is writable (or missing): no git-lfs advice
        path = self.project.write("Content/Maps/L_Bay.umap", "map")
        os.chmod(path, 0o444)
        try:
            self.assertEqual(ue.save_hint(self.p, ["LogSavePackage: Error: Failed to save /Game/Maps/L_Bay"]),
                             ("a file is read-only", ue.SAVE_FIX))
        finally:
            os.chmod(path, 0o666)

    def test_save_passes_writable_only_when_asked(self):
        ctx = self.ctx(reply="saved 1 package: /Game/L")
        with contextlib.redirect_stdout(io.StringIO()):
            ue.cmd_save(ctx, argparse.Namespace(paths=[], writable=False))
            ue.cmd_save(ctx, argparse.Namespace(paths=[], writable=True))
        self.assertNotIn("writable", ctx.calls[0][1])
        self.assertTrue(ctx.calls[1][1]["writable"])

    def test_doctor_names_read_only_packages(self):
        path = self.project.write("Content/Maps/L_Bay.umap", "x")
        self.project.write("Content/Maps/L_Ok.umap", "x")
        os.chmod(path, 0o444)
        try:
            self.assertEqual(ue.read_only_packages(self.p), ["Maps/L_Bay.umap"])
        finally:
            os.chmod(path, 0o666)

    # 187
    def test_py_refuses_while_another_session_edits_the_scripts_modules(self):
        script = self.project.write("Scripts/gen.py", "import os\nimport nr_city_plan\nfrom lib.util import x\n")
        plan = self.project.write("Scripts/nr_city_plan.py", "from helpers import y\n")
        helpers = self.project.write("Scripts/helpers.py", "")
        util = self.project.write("Scripts/lib/util.py", "")
        self.assertEqual(ue.script_modules(script), [script, plan, helpers, util])
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "local-gen"}):
            ue.record_edit(self.p, helpers, "local-gen")
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "kinks"}):
            with self.assertRaises(ue.CliError) as caught:
                ue.cmd_py(self.ctx(), argparse.Namespace(file=script, code=["city"]))
            self.assertEqual(caught.exception.code, 5)
            self.assertIn("helpers.py by local-gen", str(caught.exception))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ue.cmd_py(self.ctx(), argparse.Namespace(file=script, code=["city"],
                                                                           include_others=True)), 0)

    def test_edit_key_on_another_drive(self):
        if os.name != "nt":
            self.skipTest("drive letters")
        self.assertEqual(ue.edit_key("D:/Proj/Game.uproject", "C:/Temp/x.py"), "c:/temp/x.py")

    # 188
    def test_overlay_runs_staged_modules_for_one_run(self):
        self.project.write("Scripts/gen.py", "import os, sys, importlib\nsys.path.insert(0, os.path.dirname(__file__))\n"
                                             "import ovl_plan_031\nimportlib.reload(ovl_plan_031)\n"
                                             "RESULT.append((ovl_plan_031.VALUE, sys.argv[1:]))\n")
        self.project.write("Scripts/ovl_plan_031.py", "VALUE = 'shared'\n")
        staged = os.path.join(self.project.root, "Saved", "Tether", "staging", "me", "Scripts", "ovl_plan_031.py")
        os.makedirs(os.path.dirname(staged))
        with open(staged, "w") as f:
            f.write("VALUE = 'staged'\n")
        path, saved_path = os.path.join(self.project.root, "Scripts", "gen.py"), list(sys.path)
        try:
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "me"}), contextlib.redirect_stderr(io.StringIO()) as err:
                code, run = ue.overlay_script(self.ctx(), argparse.Namespace(overlay="-", code=["city"]), path)
            self.assertIn("1 staged .py file(s)", err.getvalue())
            self.assertEqual(run, path)  # the script itself is not staged
            namespace = {"RESULT": []}
            exec(code, namespace)
            exec(code, namespace)  # a second run: the import hook is gone, the staged module was dropped
            self.assertEqual(namespace["RESULT"], [("staged", ["city"]), ("staged", ["city"])])
            self.assertNotIn("ovl_plan_031", sys.modules)
            import ovl_plan_031  # noqa: F401 (the shared copy, as everyone else's stages see it)
            self.assertEqual(sys.modules["ovl_plan_031"].VALUE, "shared")
        finally:
            sys.path[:] = saved_path
            sys.modules.pop("ovl_plan_031", None)

    # 192
    def test_py_hints_at_setters_without_modify(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            ue.cmd_py(self.ctx(), argparse.Namespace(file=None, code=["road.spline.set_location_at_spline_point(0, p, 1)"]))
            ue.cmd_py(self.ctx(), argparse.Namespace(file=None, code=["road.modify(); s.set_location_at_spline_point(0, p, 1)"]))
        self.assertEqual(err.getvalue().count("without marking it modified"), 1)

    # 194
    def test_log_steps_search_on_from_the_previous_match(self):
        lines = [stamped("LogTemp: Free fly on"), stamped("LogTemp: Free fly off"), stamped("LogTemp: Free fly on")]
        cursor = {"line": 0}
        check = lambda text: ue.log_check(text, lambda: lines, cursor)()  # noqa: E731
        self.assertTrue(check("Free fly on")[0])
        ok, detail = check("Free fly off")
        self.assertTrue(ok)
        self.assertIn("(at 10:13:00.858)", detail)
        self.assertTrue(check("Free fly on")[0])
        ok, detail = check("Free fly off")
        self.assertFalse(ok)
        self.assertIn("an earlier one matched", detail)
        self.assertTrue(check("Free fly off anywhere")[0])
        self.assertTrue(check("Free fly on count >= 2")[0])  # a tally is still of the whole run

    # 196
    def test_set_force_and_rerun(self):
        ctx = self.ctx("UDS\nTime of Day: 1300 -> 1300")
        base = dict(target="UDS", assignments=["Time of Day=1300"], pie=False, from_file=None, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()):
            ue.cmd_set(ctx, argparse.Namespace(**base))
            ue.cmd_set(ctx, argparse.Namespace(force=True, **base))
            ue.cmd_rerun(ctx, argparse.Namespace(actors=["UDS", "Sky2"]))
        self.assertNotIn("force", ctx.calls[0][1])
        self.assertTrue(ctx.calls[1][1]["force"])
        self.assertEqual(ctx.calls[2], ("rerun", {"targets": "UDS,Sky2"}))
        self.assertIn("rerun", ue.GATED)
        self.assertIn("tether.rerun(actor)", ue.property_name_hint(
            ctx, "AttributeError: 'Actor' object has no attribute 'rerun_construction_scripts'"))

    # 185
    def test_reflection_changes_tell_declarations_from_bodies(self):
        header = self.project.write("Source/Game/RoadNetwork.h", (
            "#pragma once\nUCLASS()\nclass URoadNetworkSubsystem : public UWorldSubsystem\n{\n\tGENERATED_BODY()\npublic:\n"
            "\tUFUNCTION(BlueprintCallable,\n\t\tCategory = \"Roads\")  // rebuilds\n\tvoid Rebuild();\n"
            "\tint32 Count() const { return 3; }\n};\n"))
        read = lambda: open(header, encoding="utf-8").read()  # noqa: E731
        self.assertEqual(ue.reflection_decls(read()),
                         ["UCLASS() class URoadNetworkSubsystem : public UWorldSubsystem",
                          'UFUNCTION(BlueprintCallable, Category = "Roads") void Rebuild()'])
        self.assertIsNone(ue.reflection_changes(self.p, [header]))  # no record yet
        ue.mark_green(self.p)
        self.assertEqual(list(ue.read_json(ue.reflection_path(self.p))), ["Source/Game/RoadNetwork.h"])
        self.project.write("Source/Game/RoadNetwork.h", read().replace("return 3;", "return 4; /* x */"))
        self.assertEqual(ue.reflection_changes(self.p, [header]), [])  # a body and a comment: Live Coding is fine
        self.project.write("Source/Game/RoadNetwork.h", read().replace(
            "\tint32 Count()", "\tUFUNCTION(BlueprintCallable)\n\tbool SnapKinks(float Tolerance);\n\tint32 Count()"))
        changes = ue.reflection_changes(self.p, [header])
        self.assertEqual(changes, [("RoadNetwork.h", ["UFUNCTION(BlueprintCallable) bool SnapKinks(float Tolerance)"], [])])
        self.assertEqual(ue.describe_reflection(changes),
                         ["RoadNetwork.h: + UFUNCTION(BlueprintCallable) bool SnapKinks(float Tolerance)"])

    def test_pie_start_refuses_after_a_forced_reflection_patch(self):
        ue.write_json(ue.layout_marker(self.p), {"pid": 11, "pids": [11, 22], "headers": ["RoadNetwork.h"],
                                                 "reflection": ["RoadNetwork.h: + UFUNCTION() void Rebuild()"]})
        self.assertIn("ue pie start --patched", ue.reflection_patch_refusal(self.p, {"pid": 22}))
        self.assertIsNone(ue.reflection_patch_refusal(self.p, {"pid": 33}))
        ue.write_json(ue.layout_marker(self.p), {"pid": 11, "headers": ["RoadNetwork.h"]})
        self.assertIsNone(ue.reflection_patch_refusal(self.p, {"pid": 11}))  # a note only (layout_patch_note)
        self.assertIsNotNone(ue.layout_patch_note(self.p, {"pid": 11}))

    # 197
    def test_errors_on_unchanged_lines_are_told_apart(self):
        git = lambda *a: subprocess.run(["git", "-C", self.project.root, "-c", "user.name=t", "-c",  # noqa: E731
                                         "user.email=t@t", *a], capture_output=True, check=True)
        git("init", "-q")
        self.project.write("Source/Game/A.cpp", "".join(f"line {i}\n" for i in range(1, 11)))
        git("add", "-A")
        git("commit", "-q", "-m", "x")
        self.project.write("Source/Game/A.cpp", "".join(f"line {i}{' changed' if i == 5 else ''}\n" for i in range(1, 11)))
        self.project.write("Source/Game/New.cpp", "int x;\n")
        errors = ["Source\\Game\\A.cpp:5: C2065 'mine': undeclared identifier",
                  "Source\\Game\\A.cpp:8: C2065 'FTimerManager': undeclared identifier",
                  "Source\\Game\\New.cpp:1: C2065 'y': undeclared identifier"]
        self.assertEqual(ue.untouched_errors(self.p, errors), [errors[1]])

    # 198
    def test_redefinition_names_the_other_file(self):
        self.project.write("Source/Game/SDSetPiece.cpp", "namespace\n{\n\tconstexpr float ApproachSeconds = 4.f;\n}\n")
        self.project.write("Source/Game/SDExtractionObjective.cpp", "namespace {\nconstexpr float ApproachSeconds = 2.f;\n}\n")
        self.project.write("Source/Game/Other.cpp", "float Wait = ApproachSeconds;  // uses, not definitions\n"
                                                    "float Twice() { return ApproachSeconds; }\nvoid F() { G(ApproachSeconds); }\n")
        hints = ue.redefinition_hints(self.p, [
            "Source\\Game\\SDExtractionObjective.cpp:2: C2374 'ApproachSeconds': redefinition; multiple initialization"])
        self.assertEqual(len(hints), 1)
        self.assertIn("'ApproachSeconds' is defined in SDExtractionObjective.cpp, SDSetPiece.cpp", hints[0])
        self.assertIn("ue lint --cpp", hints[0])

    # 200
    def test_pie_help_lists_the_steps(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            ue.build_parser().parse_args(["pie", "-h"])
        for step in ("log <regex> [within N] [anywhere]", "until ...", "exec <console command>", "example:"):
            self.assertIn(step, out.getvalue())


class Round032(unittest.TestCase):
    """0.32: UnrealRacing's notes 201-206."""

    def setUp(self):
        self.project = Project()
        self.p = self.project.uproject
        ue.core._AGENT.clear()

    def tearDown(self):
        ue.core._AGENT.clear()
        self.project.close()

    # 201
    def test_log_step_suffixes_in_either_order(self):
        for text in ("Standings within 5 anywhere", "Standings anywhere within 5"):
            self.assertEqual(ue.step_suffixes("log", text), ("Standings anywhere", 5.0))
        self.assertEqual(ue.step_suffixes("log", "Standings within 2"), ("Standings", 2.0))
        self.assertEqual(ue.step_suffixes("until", "player.Speed > 3 within 4"), ("player.Speed > 3", 4.0))

    # 202
    def transcript(self, path, command, stamp):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": stamp, "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": command}}]}}) + "\n")

    def test_the_sub_agent_running_a_command_is_found_from_its_transcript(self):
        base = os.path.join(self.project.root, "claude", "projects")
        sid = "11112222-aaaa-bbbb-cccc-000000000000"
        folder = os.path.join(base, "D--Projects-Game", sid)
        main = os.path.join(base, "D--Projects-Game", sid + ".jsonl")
        studio = os.path.join(folder, "subagents", "agent-a55cee026ce86ba58.jsonl")
        signs = os.path.join(folder, "subagents", "agent-b77dee026ce86ba59.jsonl")
        self.transcript(main, "ue status", "2026-10-20T10:00:00.000Z")
        self.transcript(studio, "cd D:/G && ue open --second --headless --render --map L_NR_InteriorStudio",
                        "2026-10-20T10:01:00.000Z")
        self.transcript(signs, "ue shot --at 0,0,100 -o signs.png", "2026-10-20T10:02:00.000Z")
        env = {"CLAUDE_CODE_SESSION_ID": sid}
        with unittest.mock.patch.dict(os.environ, env), unittest.mock.patch.dict(os.environ, {}, clear=False) as e:
            e.pop("UE_SESSION", None)
            e.pop("UE_AGENT", None)
            self.assertEqual(ue.find_agent(["open", "--second", "--headless", "--render", "--map", "L_NR_InteriorStudio"],
                                           base=base), "a55cee02")
            self.assertEqual(ue.find_agent(["shot", "--at", "0,0,100", "-o", "signs.png"], base=base), "b77dee02")
            self.assertIsNone(ue.find_agent(["status"], base=base))  # the main thread's own call
            self.assertIsNone(ue.find_agent(["log"], base=base))  # nobody's: the session itself
            os.utime(studio, (time.time() - 3600, time.time() - 3600))
            os.utime(signs, (time.time() - 3600, time.time() - 3600))
            self.assertIsNone(ue.find_agent(["shot", "--at", "0,0,100", "-o", "signs.png"], base=base))  # idle agents
        with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "x", "UE_AGENT": "studio"}):
            self.assertEqual(ue.find_agent([], base=base), "studio")

    def test_an_agents_second_editor_is_the_default_only_for_that_agent(self):
        def beacon(pid, **fields):
            ue.write_json(os.path.join(ue.state_dir(self.p), "editors", f"{pid}.json"),
                          dict({"pid": pid, "heartbeat": time.time(), "started": time.time() - 100}, **fields))
        beacon(os.getpid(), launched_by_tether=True)
        beacon(os.getppid(), launched_by_tether=True, owner="city/studio", started=time.time())
        with unittest.mock.patch("tether_cli.core.editor_process", return_value=True):
            for agent, want in (("studio", os.getppid()), ("signs", os.getpid()), ("", os.getpid())):
                ue.core._AGENT.clear()
                with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "city", "UE_AGENT": agent}):
                    self.assertEqual(ue.chosen_editor(self.p)["pid"], want, agent)
            ue.core._AGENT.clear()
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "city", "UE_AGENT": "signs"}):
                self.assertIn("agent studio of this session's own (--second)",
                              ue.editor_label({"pid": 1, "owner": "city/studio"}))
                self.assertTrue(ue.same_session("city/studio"))
                ue.set_session_name(self.p, "signs")
            ue.core._AGENT.clear()
            with unittest.mock.patch.dict(os.environ, {"UE_SESSION": "city", "UE_AGENT": "studio"}):
                ue.set_session_name(self.p, "studio")
        self.assertEqual(ue.session_names(self.p), {"city/signs": "signs", "city/studio": "studio"})

    # 203
    def test_mesh_builds_pending_and_done(self):
        lines = [stamped("LogStaticMesh: Display: Building static mesh SM_Road_1 (Required Memory Estimate: 6 MB)..."),
                 stamped("LogStaticMesh: Display: Building static mesh SM_Road_2 (Required Memory Estimate: 6 MB)..."),
                 stamped("LogStaticMesh: Built static mesh [0.25s] /Game/NR/SM_Road_1.SM_Road_1")]
        self.assertEqual(ue.mesh_builds(lines), (["SM_Road_2"], ["SM_Road_1"]))

    def test_shots_render_a_few_frames_after_meshes_were_rebuilt(self):
        log = self.project.write("Saved/Logs/Game.log", stamped("LogInit: boot\n"))
        ctx = FakeCtx()
        ctx.project, ctx.beacon = self.p, {}
        warmed = []
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ue.settle_mesh_builds(ctx, lambda: warmed.append(1)), [])
            with open(log, "a", encoding="utf-8") as f:
                f.write(stamped("LogStaticMesh: Display: Building static mesh SM_Deck (x)...\n"))
                f.write(stamped("LogStaticMesh: Built static mesh [1.0s] /Game/NR/SM_Deck.SM_Deck\n"))
            self.assertEqual(ue.settle_mesh_builds(ctx, lambda: warmed.append(1)), ["SM_Deck"])
            self.assertEqual(ue.settle_mesh_builds(ctx, lambda: warmed.append(1)), [])  # once per rebuild
        self.assertEqual(warmed, [1])
        self.assertIn("1 static mesh(es) were rebuilt since the last shot (SM_Deck)", err.getvalue())

    # 204
    def test_log_skips_the_flush_while_the_editor_is_busy(self):
        self.project.write("Saved/Logs/Game.log", stamped("LogTemp: Warning: careful\n"))
        ctx = FakeCtx()
        ctx.project, ctx.beacon, ctx.transport = self.p, {"pid": 0, "heartbeat": time.time() - 90, "mcp_port": 1}, "mcp"
        with unittest.mock.patch("tether_cli.calls.editor_process", return_value=True), \
                unittest.mock.patch("tether_cli.calls.port_open", return_value=True):
            self.assertTrue(ue.editor_busy(ctx))
            ue.flush_editor_log(ctx, when_busy=False)
            self.assertEqual(ctx.calls, [])
            ue.flush_editor_log(ctx)  # after a call of ours: flushed anyway
            self.assertEqual(ctx.calls, [("console", {"command": "FLUSHLOG"})])

    def test_short_calls_restores_the_timeout(self):
        ctx = FakeCtx()
        ctx._mcp, ctx.args = types.SimpleNamespace(timeout=120.0), types.SimpleNamespace(timeout=120.0)
        with ue.short_calls(ctx, 10):
            self.assertEqual((ctx._mcp.timeout, ctx.args.timeout), (10, 10))
        self.assertEqual((ctx._mcp.timeout, ctx.args.timeout), (120.0, 120.0))


class Round033(unittest.TestCase):
    """0.33: UnrealRacing's note 207 (a map reloaded broken in an editor carrying many Live Coding patches)."""

    GOOD = {"classes": {"StaticMeshActor": [361, 0, 0, 0], "NRRoad": [40, 0, 0, 0], "WorldSettings": [1, 1, 0, 0]},
            "samples": {"StaticMeshActor": {"RoadTile_0_0": [[100, 200, 0], "/Game/NR/SM_Tile_0_0"]},
                        "NRRoad": {"NRRoad_1": [[5000, 0, 10], None]}}, "wp": False}
    BROKEN = {"classes": {"StaticMeshActor": [361, 361, 361, 0], "NRRoad": [40, 40, 0, 40], "WorldSettings": [1, 1, 0, 0]},
              "samples": {"StaticMeshActor": {"RoadTile_0_0": [[0, 0, 0], None]}, "NRRoad": {"NRRoad_1": [[0, 0, 0], None]}},
              "wp": False}

    def setUp(self):
        self.project = Project()
        self.p = self.project.uproject

    def tearDown(self):
        self.project.close()

    def test_a_reload_like_207_is_flagged_against_the_last_good_load(self):
        problems = ue.map_load_problems(self.GOOD, self.BROKEN)
        text = "\n".join(problems)
        self.assertIn("361 of 361 StaticMeshActor at the origin (was 0)", text)
        self.assertIn("361 of 361 StaticMeshActor with no mesh (was 0)", text)
        self.assertIn("40 of 40 NRRoad with a zero-length spline (was 0)", text)
        self.assertIn("RoadTile_0_0 moved to the origin (was 100,200,0)", text)
        self.assertEqual(ue.map_load_problems(self.GOOD, self.GOOD), [])

    def test_without_an_earlier_load_only_plain_breakage_counts(self):
        text = "\n".join(ue.map_load_problems(None, self.BROKEN))
        self.assertIn("361 of 361 StaticMeshActor have no mesh", text)
        self.assertIn("all 40 NRRoad have a zero-length spline", text)
        self.assertEqual(ue.map_load_problems(None, self.GOOD), [])

    def test_fewer_actors_count_only_when_the_map_was_not_saved_since(self):
        fewer = json.loads(json.dumps(self.GOOD))
        fewer["classes"]["StaticMeshActor"][0] = 200
        self.assertEqual(ue.map_load_problems(self.GOOD, fewer), ["200 StaticMeshActor (was 361)"])
        self.assertEqual(ue.map_load_problems(self.GOOD, fewer, saved_since=True), [])
        self.assertEqual(ue.map_load_problems(self.GOOD, dict(fewer, wp=True)), [])

    def _ctx(self, reply, pid=4242):
        ctx = FakeCtx(json.dumps(reply))
        ctx.project, ctx.beacon = self.p, {"pid": pid, "map": "L_HikariBay"}
        return ctx

    def test_check_keeps_good_loads_and_marks_a_broken_one_for_save(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ue.check_map_load(self._ctx(self.GOOD), "/Game/Maps/L_HikariBay"), 0)
            ue.write_json(os.path.join(ue.state_dir(self.p), "last-build.json"),
                          {"live_patches": 7, "live_pids": {"4242": 7}})
            self.assertEqual(ue.check_map_load(self._ctx(self.BROKEN), "/Game/Maps/L_HikariBay"), 1)
        self.assertIn("WARNING: /Game/Maps/L_HikariBay looks broken after loading", out.getvalue())
        self.assertIn("this editor carries 7 Live Coding patch(es)", out.getvalue())
        beacon = {"pid": 4242, "map": "L_HikariBay"}
        self.assertIn("NRRoad", " ".join(ue.suspect_map(self.p, beacon)["problems"]))
        self.assertIsNone(ue.suspect_map(self.p, {"pid": 4242, "map": "L_Other"}))
        self.assertIn("carries 7 Live Coding patches", ue.reload_risk_note(self.p, beacon))
        # ue save refuses the map, by path or when it has unsaved changes; --anyway and other packages pass
        ctx = self._ctx(["/Game/Maps/L_HikariBay"])
        with self.assertRaises(ue.CliError):
            ue.cmd_save(ctx, argparse.Namespace(paths=["/Game/Maps/L_HikariBay"], writable=False, anyway=False))
        with self.assertRaises(ue.CliError):
            ue.cmd_save(ctx, argparse.Namespace(paths=[], writable=False, anyway=False))
        with contextlib.redirect_stdout(io.StringIO()):
            ue.cmd_save(self._ctx("saved 1 package(s)"), argparse.Namespace(paths=["/Game/NR/M_Road"], writable=False,
                                                                           anyway=False))
            ue.cmd_save(self._ctx("saved"), argparse.Namespace(paths=["/Game/Maps/L_HikariBay"], writable=False,
                                                              anyway=True))
        # saved on purpose: the mark and the old baseline go, so the next load is the new baseline
        self.assertIsNone(ue.suspect_map(self.p, beacon))
        self.assertFalse(os.path.exists(ue.map_baseline_path(self.p, "/Game/Maps/L_HikariBay")))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.check_map_load(self._ctx(self.GOOD), "/Game/Maps/L_HikariBay"), 0)
            self.assertEqual(ue.check_map_load(self._ctx(self.BROKEN), "/Game/Maps/L_HikariBay"), 1)
        # a good load afterwards clears the mark, and the baseline stays the good one
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ue.check_map_load(self._ctx(self.GOOD), "/Game/Maps/L_HikariBay"), 0)
        self.assertIsNone(ue.suspect_map(self.p, beacon))

    # the selftest's UI checks on a machine whose own editor layout has floating windows
    def test_a_headless_editor_closing_leaves_the_users_layout_as_it_was(self):
        layout = self.project.write("LocalAppData/UnrealEngine/Editor/EditorLayout.json", '{"mine": 1}')

        class Quitting(FakeCtx):
            def tether(self, tool, **kwargs):
                if "quit_editor" in kwargs.get("code", ""):  # the editor saves its layout as it exits
                    with open(layout, "w") as f:
                        f.write('{"headless": 1}')
                return "0"
        for headless, want in ((True, '{"mine": 1}'), (False, '{"headless": 1}')):
            with open(layout, "w") as f:
                f.write('{"mine": 1}')
            ctx = Quitting()
            ctx.project, ctx.beacon = self.p, {"pid": 4242, "launched_by_tether": True, "headless": headless}
            alive = iter([True, True, False, False, False])
            args = argparse.Namespace(force=False, save=False, discard=False, ignore_lease=False)
            with unittest.mock.patch("tether_cli.editor.pid_alive", side_effect=lambda pid: next(alive)), \
                    unittest.mock.patch("tether_cli.editor.check_shared"), \
                    unittest.mock.patch("tether_cli.editor.user_layout_file", return_value=layout), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ue.close_editor(ctx, args, None), 0)
            with open(layout) as f:
                self.assertEqual(f.read(), want, headless)

    def test_ui_brings_the_level_tab_forward_when_the_message_log_hides_it(self):
        hidden = "\n".join(['window "Sandbox - Unreal Editor" [pos=0,0 size=0,0] [ref=w1]',
                            '  tab "L_TetherProbe" [pos=0,0 size=0,0] [ref=t2]',
                            '  tab "Message Log" [pos=0,0 size=0,0] [ref=t3]'])
        shown = hidden + '\n    tab "Viewport 1" [pos=0,0 size=0,0] [src=SLevelEditor.cpp:1] [ref=t4]'
        calls = []

        def tool(ctx, toolset, name, params):
            calls.append((name, params.get("ref")))
            return shown if any(c[0] == "Click" for c in calls) else hidden
        ctx = FakeCtx()
        ctx.project, ctx.beacon, ctx.transport = self.p, {"pid": 1, "map": "/Game/TetherFixtures/L_TetherProbe"}, "mcp"
        with unittest.mock.patch("tether_cli.play.mcp_tool", side_effect=tool), \
                unittest.mock.patch("tether_cli.play.catalog", return_value={ue.play.UI_TOOLSET: {}}), \
                unittest.mock.patch("tether_cli.play.pie_net", return_value=""), \
                unittest.mock.patch("time.sleep"), contextlib.redirect_stderr(io.StringIO()) as err:
            ue.play.ui_nodes(ctx)
        self.assertEqual(calls, [("Snapshot", ""), ("Click", "t2"), ("Snapshot", "")])
        self.assertIn("brought the level editor's tab to the front", err.getvalue())
        self.assertTrue(ue.play.unlaid_out(ctx.ui_all))
        self.assertFalse(ue.play.unlaid_out(ue.play.parse_snapshot('window "W" [pos=0,0 size=1280,720] [ref=w1]')))

    def test_text_less_buttons_take_their_rows_text_without_geometry(self):
        rows = []
        for i, name in enumerate(("FREE ROAM", "GARAGE", "QUIT")):
            rows += [f'      generic [pos=0,0 size=0,0] [ref=g{i}]', f'        image [pos=0,0 size=0,0] [ref=i{i}]',
                     f'        text "{name}" [pos=0,0 size=0,0] [ref=x{i}]', f'        button [pos=0,0 size=0,0] [ref=b{i}]']
        nodes = ue.play.label_buttons(ue.play.parse_snapshot("\n".join(['    generic [pos=0,0 size=0,0] [ref=g9]'] + rows)))
        self.assertEqual([n["label"] for n in nodes if n["role"] == "button"], ["FREE ROAM", "GARAGE", "QUIT"])
        # as the snapshot really gives a C++-built menu: each row flattened to a text and then its button
        flat = ['    combobox [pos=0,0 size=0,0] [ref=co1]', '      button [pos=0,0 size=0,0] [ref=b1]',
                '        image [pos=0,0 size=0,0] [ref=i1]', '    checkbox [pos=0,0 size=0,0] [ref=cb1]',
                '    button [pos=0,0 size=0,0] [ref=b9]']
        for name in ("GARAGE", "FREE ROAM", "QUIT"):
            flat += [f'    text "{name}" [pos=0,0 size=0,0] [ref=x{name[0]}]', f'    button [pos=0,0 size=0,0] [ref=b{name[0]}]']
        nodes = ue.play.label_buttons(ue.play.parse_snapshot("\n".join(['  generic [pos=0,0 size=0,0] [ref=g9]'] + flat)))
        self.assertEqual([n["label"] for n in nodes if n["role"] == "button"], ["", "", "GARAGE", "FREE ROAM", "QUIT"])


class Perforce(unittest.TestCase):
    def setUp(self):
        self.project = Project()

    def tearDown(self):
        self.project.close()

    def test_p4_options_come_from_the_editors_settings(self):
        self.assertEqual(ue.p4_options(self.project.uproject), [])
        self.project.write("Saved/Config/WindowsEditor/SourceControlSettings.ini",
                           "[SourceControl.SourceControlSettings]\nProvider=Perforce\n\n"
                           "[PerforceSourceControl.PerforceSourceControlSettings]\nPort=ssl:p4:1666\nUserName=kim\n"
                           "Workspace=kim_game\n")
        self.assertEqual(ue.p4_options(self.project.uproject), ["-p", "ssl:p4:1666", "-u", "kim", "-c", "kim_game"])

    def run_p4(self, lines, returncode=0, check=True):
        done = subprocess.CompletedProcess([], returncode, "\n".join(json.dumps(l) for l in lines).encode(), b"")
        with unittest.mock.patch.object(ue.core, "p4_exe", return_value="p4"), \
                unittest.mock.patch("subprocess.run", return_value=done) as run:
            result = ue.run_p4(self.project.uproject, "fstat", "x", check=check)
        self.assertEqual(run.call_args.args[0][:4], ["p4", "-ztag", "-Mj", "fstat"])
        return result

    def test_run_p4_records_and_messages(self):
        record = {"depotFile": "//depot/A.uasset", "headRev": "2"}
        warning = {"data": "x - no such file(s).\n", "severity": 2}
        self.assertEqual(self.run_p4([record]), [record])
        self.assertEqual(self.run_p4([record, warning], check=False), [record])  # a warning only matters with check
        with self.assertRaises(ue.CliError) as caught:
            self.run_p4([warning])
        self.assertEqual(str(caught.exception), "p4 fstat: x - no such file(s).")
        self.assertIsNone(self.run_p4([{"data": "Connect to server failed", "severity": 3}], 1, check=False))

    def test_diff_picks_perforce_by_revision_or_workspace(self):
        uproject = self.project.uproject
        self.assertTrue(ue.uses_p4(uproject, "#have", None))
        self.assertTrue(ue.uses_p4(uproject, "@1234", None))
        self.assertFalse(ue.uses_p4(uproject, "HEAD~1", None))
        with unittest.mock.patch.object(ue.assets, "run_git", return_value=None), \
                unittest.mock.patch.object(ue.assets, "p4_workspace", return_value=True):
            self.assertTrue(ue.uses_p4(uproject, None, None))
        with unittest.mock.patch.object(ue.assets, "run_git", return_value=None), \
                unittest.mock.patch.object(ue.assets, "p4_options", return_value=["-p", "p4:1666"]), \
                unittest.mock.patch.object(ue.assets, "p4_exe", return_value=None):
            with self.assertRaises(ue.CliError):  # the editor uses Perforce: say p4 is missing, not "not a git repo"
                ue.uses_p4(uproject, None, None)

    def test_p4v_temp_file_names_are_staged_as_package_names(self):
        self.assertRegex(ue.stage_copy(self.project.uproject, b"v1", "BP_X#3", ".uasset"), r"/BP_X[.]BP_X$")

    def test_locks_without_an_editor_reads_p4(self):
        content = os.path.join(self.project.root, "Content")
        records = [{"clientFile": os.path.join(content, "A.uasset"), "otherOpen0": "bob@bob_ws", "otherOpen": "1",
                    "haveRev": "1", "headRev": "1"},
                   {"clientFile": os.path.join(content, "B.uasset"), "action": "edit", "haveRev": "1", "headRev": "1"},
                   {"clientFile": os.path.join(content, "C.uasset"), "haveRev": "1", "headRev": "3"},
                   {"clientFile": os.path.join(content, "D.uasset"), "haveRev": "2", "headRev": "2"}]
        with unittest.mock.patch.object(ue.assets, "run_p4", return_value=records) as run:
            data = ue.p4_status(self.project.uproject, [])
        self.assertEqual(run.call_args.args[2:], (os.path.join(content, "..."),))
        self.assertEqual(data["files"], 4)
        self.assertEqual([(r["package"], r["other"], r["mine"], r["behind"]) for r in data["rows"]],
                         [("/Game/A", "bob", False, False), ("/Game/B", "", True, False), ("/Game/C", "", False, True)])
        ctx = FakeCtx()
        ctx.project = self.project.uproject
        args = argparse.Namespace(paths=[], limit=30)
        out = io.StringIO()
        with unittest.mock.patch.object(ue.assets, "editor_live", return_value=False), \
                unittest.mock.patch.object(ue.assets, "p4_workspace", return_value=True), \
                unittest.mock.patch.object(ue.assets, "run_p4", return_value=records), contextlib.redirect_stdout(out):
            self.assertEqual(ue.cmd_locks(ctx, args), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "Perforce: 4 file(s) under /Game; 1 checked out by others, 1 by you, 1 behind the depot",
            "checked out by bob (1): /Game/A", "checked out by you (1): /Game/B",
            "behind the depot, sync before editing (1): /Game/C"])

    def test_bulk_rides_on_the_token(self):
        ctx = ue.Ctx(argparse.Namespace(bulk=True))
        ctx._project = self.project.uproject
        with unittest.mock.patch.object(sys.modules["tether_cli.transport"], "editor_token", return_value="secret"):
            self.assertEqual(ctx.token({}), "secret+bulk")
            plain = ue.Ctx(argparse.Namespace())
            plain._project = self.project.uproject
            self.assertEqual(plain.token({}), "secret")
