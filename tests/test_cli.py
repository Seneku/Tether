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


import unittest.mock  # noqa: E402  (used above; kept at module level for Python 3.11)

if __name__ == "__main__":
    unittest.main()
