"""Unit tests for the ue CLI (cli/tether_cli): the parts that need no editor (parsing, filtering, routing, hooks)."""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import tether_cli as ue  # noqa: E402


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
                                      "under=C:/Program Files/Git/Engine", "C:/Program Files/Git/Gameplay"])
        self.assertEqual(out, ["props", "/Game/Maps/L.L", "graph=/Game/BP.BP:G", "D:/Projects/Game/Source",
                               "C:/Program Files/Git/usr/bin", "/Game", "under=/Engine", "C:/Program Files/Git/Gameplay"])


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
            ue.cmd_init(self.ctx, argparse.Namespace(**dict({"unrealsharp": False, "dry_run": False}, **kw)))
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
        self.assertEqual(self.plugins(), dict.fromkeys(ue.REQUIRED_PLUGINS, True))
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
            ue.write_json(os.path.join(ue.state_dir(project.uproject), "editor.json"), {"engine": "5.8.3-1+++UE5"})
            self.assertEqual(ue.transport(project.uproject), "mcp")  # the running editor wins
            with unittest.mock.patch.dict(os.environ, {"UE_TRANSPORT": "remote"}):
                self.assertEqual(ue.transport(project.uproject), "remote")
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
