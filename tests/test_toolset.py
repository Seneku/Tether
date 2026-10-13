"""Unit tests for the pure parts of the in-editor toolset (Plugins/Tether), with `unreal` stubbed out."""
import os
import sys
import types
import unittest


def _stub_editor_modules():
    unreal = types.ModuleType("unreal")
    unreal.uclass = lambda *a, **k: (lambda cls: cls)
    unreal.ToolsetDefinition = object
    unreal.log = unreal.log_warning = lambda *a: None
    registry = types.ModuleType("toolset_registry")
    registry.tool_call = lambda fn: fn
    registration = types.ModuleType("toolset_registry.registration")
    registration.Registration = lambda classes: types.SimpleNamespace(classes=classes)
    registry.registration = registration
    sys.modules.update({"unreal": unreal, "toolset_registry": registry,
                        "toolset_registry.registration": registration})


_stub_editor_modules()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Plugins", "Tether", "Content", "Python"))
from tether import toolsets  # noqa: E402

VECTOR = {"title": "Vector", "type": "object", "properties": {"x": {}, "y": {}, "z": {}}}
COLOR = {"title": "LinearColor", "type": "object", "properties": {"r": {}, "g": {}, "b": {}, "a": {}}}
OBJECT = {"title": "Class@/Script/Engine.Actor", "type": "object", "properties": {"refPath": {}}}


class Numbers(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(toolsets._numbers("1,2.5,-3"), [1, 2.5, -3])
        self.assertEqual(toolsets._numbers("(0, 0, 1e2)"), [0, 0, 100.0])
        self.assertEqual(toolsets._numbers("7"), [7])
        self.assertIsNone(toolsets._numbers("Movable"))
        self.assertIsNone(toolsets._numbers("1,,2"))


class Parse(unittest.TestCase):
    def test_struct_tuple_fills_fields_in_schema_order_and_keeps_the_rest(self):
        old = {"r": 1, "g": 1, "b": 1, "a": 1}
        self.assertEqual(toolsets._parse("1,0.5,0.2", COLOR, old), {"r": 1, "g": 0.5, "b": 0.2, "a": 1})
        self.assertEqual(toolsets._parse("0,0,200", VECTOR, None), {"x": 0, "y": 0, "z": 200})
        with self.assertRaises(ValueError):
            toolsets._parse("1,2,3,4", VECTOR, None)

    def test_struct_rejects_non_numeric_text_but_accepts_json(self):
        with self.assertRaises(ValueError):
            toolsets._parse("nonsense", COLOR, None)
        self.assertEqual(toolsets._parse('{"x": 1, "y": 2, "z": 3}', VECTOR, None), {"x": 1, "y": 2, "z": 3})

    def test_booleans(self):
        entry = {"type": "boolean"}
        self.assertIs(toolsets._parse("On", entry, None), True)
        self.assertIs(toolsets._parse("false", entry, None), False)
        with self.assertRaises(ValueError):
            toolsets._parse("maybe", entry, None)

    def test_numbers_must_be_numbers(self):
        self.assertEqual(toolsets._parse("2.5", {"type": "number"}, None), 2.5)
        self.assertEqual(toolsets._parse("3", {"type": "integer"}, None), 3)
        for bad in ("abc", "true", "[1]"):
            with self.assertRaises(ValueError):
                toolsets._parse(bad, {"type": "number"}, None)

    def test_enums_by_bare_or_qualified_name_case_insensitive(self):
        entry = {"type": "string", "enum": ["Static", "Stationary", "Movable"]}
        self.assertEqual(toolsets._parse("movable", {"enum": entry["enum"]}, None), "Movable")
        self.assertEqual(toolsets._parse("EComponentMobility::Static", {"enum": entry["enum"]}, None), "Static")
        with self.assertRaises(ValueError):
            toolsets._parse("Wobbly", {"enum": entry["enum"]}, None)

    def test_strings_are_kept_verbatim(self):
        self.assertEqual(toolsets._parse(" 123 ", {"type": "string"}, None), " 123 ")

    def test_object_references(self):
        self.assertEqual(toolsets._parse("/Game/Props/SM_Rock.SM_Rock", OBJECT, None),
                         {"refPath": "/Game/Props/SM_Rock.SM_Rock"})


class Deltas(unittest.TestCase):
    def test_add_numbers_and_structs(self):
        self.assertEqual(toolsets._add(5, 2.5, "x"), 7.5)
        self.assertEqual(toolsets._add({"x": 1, "y": 2, "z": 3}, {"x": 0, "y": 0, "z": 10}, "loc"), {"x": 1, "y": 2, "z": 13})
        with self.assertRaises(ValueError):
            toolsets._add(True, "x", "flag") if False else toolsets._add("a", 1, "name")


class ReadBack(unittest.TestCase):
    def test_took(self):
        self.assertTrue(toolsets._took(0.5, 0.502))  # 8-bit colour quantisation
        self.assertFalse(toolsets._took(100050, 100000))  # a move that did not happen
        self.assertFalse(toolsets._took(-5, 0))  # clamped
        self.assertTrue(toolsets._took({"pitch": -30, "yaw": 270, "roll": 0}, {"pitch": -30, "yaw": -90, "roll": 0}))
        self.assertFalse(toolsets._took(True, False))
        self.assertTrue(toolsets._took("Movable", "MOVABLE"))  # names and paths are trusted
        self.assertTrue(toolsets._took({"r": 1, "g": 0.5}, {"r": 1, "g": 0.502, "b": 0, "a": 1}))


class Compact(unittest.TestCase):
    def test_numeric_structs_as_tuples_and_tiny_values_as_zero(self):
        self.assertEqual(toolsets._compact({"pitch": -45, "yaw": 30, "roll": 4.4e-15}), "(-45, 30, 0)")
        self.assertEqual(toolsets._compact({"refPath": "/Temp/L.L:PersistentLevel.Light"}), "Light")
        self.assertEqual(toolsets._compact(6.1234567), "6.12346")
        self.assertEqual(toolsets._compact({"x": 0, "y": 0, "z": 100050}), "(0, 0, 100050)")


if __name__ == "__main__":
    unittest.main()


class FakeSourceControl:
    """unreal.SourceControl stand-in: per-file states, check-outs make files writable."""

    def __init__(self, provider, states=None):
        self.provider, self.states, self.calls = provider, states or {}, []

    def is_enabled(self):
        return self.provider != "None"

    def is_available(self):
        return True

    def current_provider(self):
        return self.provider

    def query_file_state(self, path, silent):
        return types.SimpleNamespace(**dict({"is_source_controlled": True, "is_current": True, "can_check_out": True,
                                             "is_checked_out": False, "is_checked_out_other": False,
                                             "checked_out_other": "", "is_deleted": False},
                                            **self.states.get(os.path.basename(path), {})))

    def check_out_file(self, path, silent):
        self.calls.append(("checkout", os.path.basename(path)))
        os.chmod(path, 0o666)
        return True

    def mark_files_for_add(self, files, silent):
        self.calls.append(("add", [os.path.basename(f) for f in files]))
        return True

    def mark_file_for_delete(self, path, silent):
        self.calls.append(("delete", os.path.basename(path)))
        return True

    def last_error_msg(self):
        return "error"


class RevisionControl(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = lambda name: os.path.join(self.tmp.name, name)
        for name in ("A.uasset", "B.uasset"):
            with open(self.path(name), "w") as f:
                f.write("x")
            os.chmod(self.path(name), 0o444)  # what a Perforce workspace syncs

    def tearDown(self):
        for name in os.listdir(self.tmp.name):
            os.chmod(self.path(name), 0o666)
        self.tmp.cleanup()

    def test_perforce_checks_out_refuses_files_locked_by_others_and_adds_new_ones(self):
        sc = FakeSourceControl("Perforce", {"B.uasset": {"is_checked_out_other": True, "checked_out_other": "kim"}})
        vcs = toolsets._Vcs(sc)
        problems, new, notes = vcs.prepare([self.path("A.uasset"), self.path("B.uasset"), self.path("New.uasset")])
        self.assertEqual(problems, ["B.uasset is checked out by kim"])
        self.assertEqual(notes, ["checked out A.uasset"])
        self.assertEqual(new, [self.path("New.uasset")])
        self.assertTrue(os.access(self.path("A.uasset"), os.W_OK))
        with open(self.path("New.uasset"), "w") as f:
            f.write("x")
        self.assertEqual(vcs.added(new), ["marked for add: New.uasset"])
        os.chmod(self.path("A.uasset"), 0o666)
        os.remove(self.path("A.uasset"))
        self.assertEqual(vcs.deleted([self.path("A.uasset")]), ["marked for delete: A.uasset"])

    def test_git_needs_no_checkouts_and_leaves_new_files_alone(self):
        sc = FakeSourceControl("Git")
        vcs = toolsets._Vcs(sc)
        os.chmod(self.path("A.uasset"), 0o666)
        self.assertEqual(vcs.prepare([self.path("A.uasset")]), ([], [], []))
        self.assertEqual(vcs.added([self.path("A.uasset")]), [])
        self.assertEqual(sc.calls, [])

    def test_without_a_provider_read_only_files_are_refused_up_front(self):
        vcs = toolsets._Vcs(FakeSourceControl("None"))
        problems, _, _ = vcs.prepare([self.path("A.uasset")])
        self.assertEqual(len(problems), 1)
        self.assertIn("A.uasset is read-only (not checked out?", problems[0])


class LegacyNames(unittest.TestCase):
    def test_cpp_and_python_names_match(self):
        self.assertEqual(toolsets._legacy_key("bCastShadows"), toolsets._legacy_key("cast_shadows"))
        self.assertEqual(toolsets._legacy_key("LightColor"), toolsets._legacy_key("light_color"))
        self.assertEqual(toolsets._legacy_key("bounds"), "bounds")  # a lowercase b is part of the name
