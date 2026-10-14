"""Unit tests for ue fab (cli/tether_cli/fab.py, fabfiles.py) and the Fab input guard: no editor, no network."""
import contextlib
import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import unittest.mock
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import tether_cli as ue  # noqa: E402


def fstring(text):
    data = text.encode("utf-8") + b"\0"
    return struct.pack("<i", len(data)) + data


def binary_manifest(fields):
    """A BuildPatch binary manifest: header (magic, header size, sizes, SHA, stored-as, version) + zlib body."""
    body = b"\x01" * 37 + struct.pack("<i", len(fields)) + b"".join(fstring(k) for k in fields) + \
        b"".join(fstring(v) for v in fields.values()) + b"\x00" * 8
    packed = zlib.compress(body)
    header = struct.pack("<IIII", 0x44BEC00C, 41, len(body), len(packed)) + b"\0" * 20 + b"\x01" + struct.pack("<i", 21)
    return header + packed


class World:
    """A vault with two packs (one JSON manifest, one binary, one listed in the launcher's database) and two
    projects: this one, and another holding a Megascans asset."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.vault = os.path.join(self.root, "Vault")
        self.pack("TokyoV1", "Tokyo Stylized Environment ( Tokyo City Japan )", "UE_5.2,UE_5.7", "Tokyo", json_form=True)
        self.pack("SkyV9", "Ultra Dynamic Sky", "UE_5.5,UE_5.8", "UltraDynamicSky")
        os.makedirs(os.path.join(self.vault, "FabLibrary"))
        db = sqlite3.connect(os.path.join(self.vault, "FabLibrary", "listings_v1.db"))
        db.execute("CREATE TABLE local_listing (uid TEXT, title TEXT)")
        db.execute("CREATE TABLE download_meta (listing_uid TEXT, path TEXT, cache_size INTEGER)")
        db.execute("INSERT INTO local_listing VALUES ('84fda27a-c79f-49c9-8458-82401fb37cfb', 'Ultra Dynamic Sky')")
        db.execute("INSERT INTO download_meta VALUES ('84fda27a-c79f-49c9-8458-82401fb37cfb', ?, 1234)",
                   (os.path.join(self.vault, "SkyV9"),))
        db.commit()
        db.close()
        self.here = self.project("Here")
        self.other = self.project("Other")
        mega = "Content/Fab/Megascans/Surfaces/Sand_Debris_phlvsop0/Medium/phlvsop0_tier_2"
        self.write(self.other, f"{mega}/Materials/MI_phlvsop0.uasset", "mi")
        self.write(self.other, "Content/Fab/Materials/M_MS_Srf.uasset", "master")
        self.write(self.other, "Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini",
                   '[/Script/Fab.FabLocalAssets]\nPathsListingID=(("/Game/Fab/Megascans/Surfaces/Sand_Debris_phlvsop0", '
                   '"85a40a20-104c-4523-af91-6d86eceff943"))\n')
        self.patches = [unittest.mock.patch.dict(os.environ, {"TETHER_FAB_VAULT": self.vault}),
                        unittest.mock.patch("tether_cli.fab.engine_minor", return_value="5.8"),
                        unittest.mock.patch("tether_cli.fab.editor_live", return_value=None),
                        unittest.mock.patch("tether_cli.fab.nearby_projects", return_value=[self.other])]
        for p in self.patches:
            p.start()

    def pack(self, folder, title, apps, content, json_form=False):
        base = os.path.join(self.vault, folder)
        os.makedirs(os.path.join(base, "data", "Content", content, "Maps"))
        with open(os.path.join(base, "data", "Content", content, "Maps", "L_Demo.umap"), "w") as f:
            f.write(title)
        fields = {"Vault.TitleText": title, "CompatibleApps": apps, "CatalogItemId": "abc"}
        with open(os.path.join(base, "manifest"), "wb") as f:
            f.write(json.dumps({"AppNameString": folder, "CustomFields": fields}).encode() if json_form
                    else binary_manifest(fields))

    def project(self, name):
        folder = os.path.join(self.root, "Projects", name)
        os.makedirs(os.path.join(folder, "Content"))
        path = os.path.join(folder, f"{name}.uproject")
        with open(path, "w") as f:
            f.write('{"EngineAssociation": "5.8"}')
        return path

    def write(self, project, rel, text):
        path = os.path.join(os.path.dirname(project), *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def ue(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = ue.main(["--project", self.here, "fab", *argv])
        return code, out.getvalue()

    def close(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class FabFiles(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_vault_packs_from_both_manifest_forms(self):
        packs = {p["app"]: p for p in ue.vault_packs()}
        self.assertEqual(packs["TokyoV1"]["engines"], ["5.2", "5.7"])
        self.assertEqual(packs["TokyoV1"]["folders"], ["Tokyo"])
        sky = next(p for p in packs.values() if p["title"] == "Ultra Dynamic Sky")
        self.assertEqual(sky["engines"], ["5.5", "5.8"])
        self.assertEqual(sky["listing"], "84fda27a-c79f-49c9-8458-82401fb37cfb")
        self.assertEqual(sky["bytes"], 1234)

    def test_engine_fit(self):
        self.assertEqual(ue.engine_fit(["5.5", "5.8"], "5.8"), "ok")
        self.assertEqual(ue.engine_fit(["5.2", "5.7"], "5.8"), "older")
        self.assertEqual(ue.engine_fit(["6.0"], "5.8"), "newer")
        self.assertEqual(ue.engine_fit(["5.4", "5.10"], "5.8"), "ok")
        self.assertEqual(ue.engine_fit([], "5.8"), "unknown")

    def test_project_fab_and_origin(self):
        items = ue.project_fab(self.w.other)
        mega = next(i for i in items if i["kind"] == "megascan")
        self.assertEqual((mega["title"], mega["type"], mega["id"], mega["tiers"]), ("Sand Debris", "Surfaces", "phlvsop0", ["Medium"]))
        self.assertEqual(mega["listing"], "85a40a20-104c-4523-af91-6d86eceff943")
        self.assertTrue(any(i["kind"] == "library" for i in items))
        origin = ue.fab_origin(self.w.other, "/Game/Fab/Megascans/Surfaces/Sand_Debris_phlvsop0/Medium/x/MI", items)
        self.assertEqual(origin["id"], "phlvsop0")
        self.assertEqual(ue.fab_origin(self.w.other, "/Game/Fab/Materials/M_MS_Srf", items)["kind"], "library")
        self.assertIsNone(ue.fab_origin(self.w.other, "/Game/Maps/L", items))

    def test_add_from_the_vault_then_refuse_to_overwrite(self):
        code, out = self.w.ue("add", "Tokyo", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("1 new file(s)", out)
        self.assertIn("load in 5.8", out)
        target = os.path.join(os.path.dirname(self.w.here), "Content", "Tokyo", "Maps", "L_Demo.umap")
        self.assertFalse(os.path.exists(target))
        code, out = self.w.ue("add", "Tokyo")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(target))
        self.assertIn("Tokyo Stylized Environment", ue.project_fab(self.w.here)[0]["title"])
        code, out = self.w.ue("add", "Tokyo")
        self.assertIn("1 already here", out)
        with open(target, "w") as f:
            f.write("changed")
        code, out = self.w.ue("add", "Tokyo")
        self.assertEqual(code, 1)
        self.assertIn("nothing copied", out)
        with open(target) as f:
            self.assertEqual(f.read(), "changed")

    def test_add_megascans_from_another_project_brings_the_shared_materials(self):
        code, out = self.w.ue("add", "Sand Debris", "--from", os.path.dirname(self.w.other))
        self.assertEqual(code, 0, out)
        content = os.path.join(os.path.dirname(self.w.here), "Content")
        self.assertTrue(os.path.isfile(os.path.join(content, "Fab", "Materials", "M_MS_Srf.uasset")))
        self.assertTrue(any(i["kind"] == "megascan" for i in ue.project_fab(self.w.here)))
        self.assertFalse(os.path.exists(os.path.join(ue.state_dir(self.w.here), "fab-staging")))

    def test_library_lists_vault_and_other_projects(self):
        code, out = self.w.ue("library")
        self.assertEqual(code, 0, out)
        self.assertIn('ue fab add "Tokyo Stylized Environment"', out)  # the seller's keyword list is dropped
        self.assertIn("Other", out)
        self.assertIn("does not read your account", out)
        code, out = self.w.ue("library", "sand")
        self.assertIn("Sand Debris [phlvsop0]", out)

    def test_open_gives_urls_and_never_acts(self):
        code, out = self.w.ue("open", "Ultra Dynamic")
        self.assertIn("https://www.fab.com/listings/84fda27a-c79f-49c9-8458-82401fb37cfb", out)
        code, out = self.w.ue("open", "rusty barrel")
        self.assertIn("https://www.fab.com/search?q=rusty%20barrel", out)
        code, out = self.w.ue("open", "--library")
        self.assertIn("https://www.fab.com/library", out)

    def test_settings_offline_write_the_ini(self):
        code, out = self.w.ue("settings", "quality=high", "format=fbx")
        self.assertEqual(code, 0, out)
        self.assertIn("quality High, model format FBX", out)
        ini = ue.read_settings_ini(ue.settings_ini(self.w.here))
        self.assertEqual((ini["PreferredQualityTier"], ini["PreferredDefaultFormat"]), ("High", "FBX"))
        code, out = self.w.ue("settings", "quality=ultra")
        self.assertEqual(code, 1)
        code, out = self.w.ue("settings", "customAuthToken=x")
        self.assertEqual(code, 1)


class FabGuard(unittest.TestCase):
    SNAPSHOT = 'window "Ed" [pos=0,0 size=100,100] [ref=w1]\n  tab "Fab" [pos=1,1 size=10,10] [ref=t3]'

    def test_slate_input_refused_while_a_fab_tab_is_open(self):
        mcp = ue.Mcp(1, 1)
        sent = []

        def request(method, params=None, retry=True):
            sent.append(params["arguments"]["tool_name"])
            text = self.SNAPSHOT if params["arguments"]["tool_name"] == "Snapshot" else "ok"
            return {"content": [{"type": "text", "text": text}]}
        mcp.request = request
        with self.assertRaises(ue.CliError) as err:
            mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Click", {"ref": "b1"})
        self.assertIn("Fab", str(err.exception))
        self.assertEqual(sent, ["Snapshot"])
        mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Snapshot", {"ref": ""})  # reading is fine
        self.SNAPSHOT = 'window "Ed" [pos=0,0 size=100,100] [ref=w1]\n  tab "Fabric Tools" [pos=1,1 size=10,10]'
        mcp.call("SlateInspectorToolset.SlateInspectorToolset", "PressKey", {"key": "Enter"})
        self.assertEqual(sent[-1], "PressKey")

    def test_hook_ignores_other_tools(self):
        self.assertIsNone(ue.hook_fab_input({"tool_input": {"toolset_name": "EditorToolset", "tool_name": "Click"}}))
        self.assertIsNone(ue.hook_fab_input({"tool_input": {"toolset_name": "SlateInspectorToolset", "tool_name": "Snapshot"}}))


if __name__ == "__main__":
    unittest.main()
