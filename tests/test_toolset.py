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


class Compact(unittest.TestCase):
    def test_numeric_structs_as_tuples_and_tiny_values_as_zero(self):
        self.assertEqual(toolsets._compact({"pitch": -45, "yaw": 30, "roll": 4.4e-15}), "(-45, 30, 0)")
        self.assertEqual(toolsets._compact({"refPath": "/Temp/L.L:PersistentLevel.Light"}), "Light")
        self.assertEqual(toolsets._compact(6.123456), "6.123")


if __name__ == "__main__":
    unittest.main()
