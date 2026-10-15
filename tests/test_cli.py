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
            self.assertEqual(ue.editor_label(ue.chosen_editor(project.uproject)), f"pid {os.getppid()}, opened by hand")
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
                                                                 "limit", "time")}
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
        dirty = iter([[], ["/Game/Maps/Spire"], ["/Game/Maps/Spire"]])
        args = argparse.Namespace(action="build", what=["nav"], save=save, wait=0, limit=20)
        with unittest.mock.patch.object(ue.play, "py_json", side_effect=lambda c, code: next(dirty)), \
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
        lease, renewed = ue.take_lease(p, "map rebuild + PIE", 15, now=1000)
        self.assertFalse(renewed)
        self.assertEqual(ue.take_lease(p, "map rebuild + PIE", 15, now=1100)[0]["since"], 1000)  # renewal keeps since
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
            self.assertEqual(ue.release_lease(p)["what"], "map rebuild + PIE")
            self.assertIsNone(ue.active_lease(p))

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
        replies = iter([{"path": "/Script/Engine.StaticMeshActor", "mesh": "/Engine/BasicShapes/Cube.Cube", "before": []},
                        None,
                        {"name": "StaticMeshActor_3", "label": "Buoy", "class": "StaticMeshActor", "at": [10, 20, 30],
                         "landed": "Island"}])
        sent = []

        class Ctx(FakeCtx):
            beacon = {"pie": True}
            data = None

            def tether(self, tool, **kwargs):
                sent.append((tool, kwargs))
                return ""
        out = io.StringIO()
        with unittest.mock.patch.object(ue.calls, "py_json", side_effect=lambda ctx, code: next(replies)), \
                unittest.mock.patch.object(ue.calls.time, "sleep"), contextlib.redirect_stdout(out):
            ue.cmd_spawn(Ctx(), argparse.Namespace(what="/Engine/BasicShapes/Cube", at="10,20,500", rot=None,
                                                   label="Buoy", ground=True, align=False, pie=True))
        self.assertEqual(sent[0], ("console", {"command": "summon /Script/Engine.StaticMeshActor", "pie": True}))
        self.assertIn("spawned Buoy (StaticMeshActor) in the PIE world at (10, 20, 30) on Island", out.getvalue())

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
