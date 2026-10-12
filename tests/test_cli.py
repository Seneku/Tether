"""Unit tests for cli/ue.py: the parts that need no editor (parsing, filtering, routing, hooks)."""
import argparse
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import ue  # noqa: E402


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
        args = argparse.Namespace(target="Light", assignments=["intensity=2", "location+=0,0,10", "name=a=b"], pie=False)
        with unittest.mock.patch("builtins.print"):
            self.assertEqual(ue.cmd_set(ctx, args), 0)
        tool, kwargs = ctx.calls[0]
        self.assertEqual(tool, "set_props")
        self.assertEqual(json.loads(kwargs["values"]), {"intensity": "2", "location+": "0,0,10", "name": "a=b"})

    def test_set_rejects_missing_equals(self):
        args = argparse.Namespace(target="Light", assignments=["intensity"], pie=False)
        with self.assertRaises(ue.CliError):
            ue.cmd_set(FakeCtx(), args)

    def test_set_error_drops_epics_object_path(self):
        class Failing(FakeCtx):
            def tether(self, tool, **kwargs):
                raise ue.CliError("SetObjectProperties on '/Temp/L.L:PersistentLevel.A.C' (LightComponent): the "
                                  "following properties could not be set: mobility invalid value (see ue log) (rolled back)")
        args = argparse.Namespace(target="A.C", assignments=["mobility=Wobbly"], pie=False)
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
        self.ctx = argparse.Namespace(project=self.project.uproject)

    def tearDown(self):
        self.project.close()

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
        self.assertEqual(len(text.splitlines()), 2)
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


import unittest.mock  # noqa: E402  (used above; kept at module level for Python 3.11)

if __name__ == "__main__":
    unittest.main()
