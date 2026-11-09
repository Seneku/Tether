"""UE 5.9 compatibility: MCP meta tools, stale sessions, image blocks, Epic's renamed tools, the Fab store guard."""
import json
import os
import sys
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import tether_cli  # noqa: E402,F401  (the package re-exports functions named like its modules: take the modules)
assets, core, hooks, transport = (sys.modules[f"tether_cli.{m}"] for m in ("assets", "core", "hooks", "transport"))


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status, self.reason, self._body = status, {404: "Not Found", 400: "Bad Request"}.get(status, "OK"), body
        self._headers = headers or {}

    def read(self):
        return self._body.encode()

    def getheader(self, name, default=None):
        return self._headers.get(name, default)


class FakeConnection:
    """http.client.HTTPConnection stand-in answering from a queue of FakeResponses."""
    queue = []
    sent = []

    def __init__(self, *a, **k):
        pass

    def request(self, method, path, body, headers):
        FakeConnection.sent.append((json.loads(body), dict(headers)))

    def getresponse(self):
        return FakeConnection.queue.pop(0)

    def close(self):
        pass


def ok(result, session="s2"):
    return FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}),
                        {"Content-Type": "application/json", "Mcp-Session-Id": session})


class StaleSessions(unittest.TestCase):
    def setUp(self):
        FakeConnection.queue, FakeConnection.sent = [], []
        patcher = unittest.mock.patch("http.client.HTTPConnection", FakeConnection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bare_404_reconnects_and_retries(self):
        """5.9 answers an unknown session id with a 404 and no body (5.8 sent a JSON-RPC error)."""
        mcp = transport.Mcp(1, 5)
        mcp.session = "stale"
        FakeConnection.queue = [FakeResponse(404, ""), ok({}), FakeResponse(202, ""),
                                ok({"content": [{"type": "text", "text": "hello"}]})]
        result = mcp.request("tools/call", {"name": "call_tool"})
        self.assertEqual(result["content"][0]["text"], "hello")
        self.assertEqual([m["method"] for m, _ in FakeConnection.sent],
                         ["tools/call", "initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(mcp.session, "s2")

    def test_other_empty_errors_raise(self):
        mcp = transport.Mcp(1, 5)
        FakeConnection.queue = [FakeResponse(400, "")]
        with self.assertRaises(core.CliError) as cm:
            mcp.request("tools/call")
        self.assertIn("HTTP 400", str(cm.exception))

    def test_58_json_error_still_reconnects(self):
        mcp = transport.Mcp(1, 5)
        mcp.session = "stale"
        error = FakeResponse(404, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "message": "Unknown session id abc, client should reinitialize"}}), {"Content-Type": "application/json"})
        FakeConnection.queue = [error, ok({}), FakeResponse(202, ""), ok({"ok": 1})]
        self.assertEqual(mcp.request("tools/list"), {"ok": 1})


class ImageBlocks(unittest.TestCase):
    def test_urn_replaced_by_image_struct(self):
        text = json.dumps({"returnValue": {"image": "urn:toolresult:X.Y:1:/returnValue/image", "width": 4}})
        out = json.loads(transport.inline_images(text, [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]))
        self.assertEqual(out["returnValue"]["image"], {"data": "QUJD", "mimeType": "image/png"})
        self.assertEqual(out["returnValue"]["width"], 4)

    def test_slate_screenshot_shape(self):
        """SlateInspector.Screenshot's whole returnValue is the image: readers expect returnValue.data."""
        text = json.dumps({"returnValue": "urn:toolresult:SlateInspectorToolset.Screenshot:abc:/returnValue"})
        out = json.loads(transport.inline_images(text, [{"data": "UE5HCg==", "mimeType": "image/png"}]))
        self.assertEqual(out["returnValue"]["data"], "UE5HCg==")

    def test_call_inlines(self):
        mcp = transport.Mcp(1, 5)
        result = {"content": [{"type": "text", "text": json.dumps({"returnValue": "urn:toolresult:a:/returnValue"})},
                              {"type": "image", "data": "Zm9v", "mimeType": "image/png"}]}
        with unittest.mock.patch.object(mcp, "request", return_value=result):
            text, failed = mcp.call("SlateInspectorToolset.SlateInspectorToolset", "Screenshot", {"ref": "w_1"})
        self.assertFalse(failed)
        self.assertEqual(core.unwrap(text)["data"], "Zm9v")

    def test_text_without_urn_untouched(self):
        self.assertEqual(transport.inline_images("not json", [{"data": "x"}]), "not json")


class Describe(unittest.TestCase):
    def ctx(self, engine, response):
        mcp = types.SimpleNamespace(request=unittest.mock.Mock(return_value=response))
        return types.SimpleNamespace(project="x.uproject", beacon={"engine": engine}, mcp=mcp)

    def test_59_uses_describe_toolsets_with_schemas(self):
        body = {"toolsets": {"A.B": {"tools": [{"name": "A.B.Read", "inputSchema": {}}]}}, "errors": {}}
        ctx = self.ctx("5.9.0-60351404", {"content": [{"type": "text", "text": json.dumps(body)}]})
        self.assertEqual([t["name"] for t in transport.describe(ctx, "A.B")], ["A.B.Read"])
        name, args = ctx.mcp.request.call_args[0][1]["name"], ctx.mcp.request.call_args[0][1]["arguments"]
        self.assertEqual(name, "describe_toolsets")
        self.assertEqual(args, {"toolset_names": ["A.B"], "include_schemas": True})

    def test_59_unknown_toolset_raises(self):
        body = {"toolsets": {}, "errors": {"Nope.X": "Unknown toolset"}}
        ctx = self.ctx("5.9.0", {"content": [{"type": "text", "text": json.dumps(body)}]})
        with self.assertRaises(core.CliError):
            transport.describe(ctx, "Nope.X")

    def test_58_uses_describe_toolset(self):
        ctx = self.ctx("5.8.4-1", {"content": [{"type": "text", "text": json.dumps({"tools": [{"name": "A.B.c"}]})}]})
        self.assertEqual(len(transport.describe(ctx, "A.B")), 1)
        self.assertEqual(ctx.mcp.request.call_args[0][1]["name"], "describe_toolset")


class EpicRenames(unittest.TestCase):
    def test_camel(self):
        self.assertEqual(assets.camel("type_name", "Float"), "typeName")
        self.assertEqual(assets.camel("instance_editable", True), "bInstanceEditable")
        self.assertEqual(assets.camel("blueprint", {"refPath": "x"}), "blueprint")

    def test_add_variable(self):
        toolset, tool, args, _ = assets.epic59("blueprint", "add_variable", {
            "blueprint": {"refPath": "/Game/B.B"}, "name": "Health", "type_name": "real", "container_type": "ARRAY"})
        self.assertEqual((toolset, tool), ("BlueprintToolsets.BlueprintToolset", "AddVariable"))
        self.assertEqual(args, {"blueprint": {"refPath": "/Game/B.B"}, "name": "Health", "typeName": "real",
                                "options": {"containerType": "Array"}})

    def test_list_variables_names(self):
        _, tool, _, after = assets.epic59("blueprint", "list_variables", {"blueprint": {"refPath": "/Game/B.B"}})
        self.assertEqual(tool, "ListVariables")
        self.assertEqual(after([{"name": "Health", "bWritable": True}]), ["Health"])

    def test_graph_dsl(self):
        toolset, tool, args, after = assets.epic59("blueprint", "read_graph_dsl", {"graph": {"refPath": "/Game/B.B:EventGraph"}})
        self.assertEqual((toolset, tool, args), ("BlueprintToolsets.BlueprintDslToolset", "Read",
                                                 {"assetPath": "/Game/B.B", "scope": "EventGraph"}))
        self.assertEqual(after({"dsl": "(event x)", "revision": "r1"}), "(event x)")
        _, tool, args, _ = assets.epic59("blueprint", "write_graph_dsl", {"graph": {"refPath": "/Game/B.B:F"}, "code": "c"})
        self.assertEqual((tool, args), ("Write", {"assetPath": "/Game/B.B", "scope": "F", "dsl": "c"}))

    def test_data_table(self):
        toolset, tool, args, _ = assets.epic59("data_table", "get_rows", {"data_table": {"refPath": "/Game/T.T"},
                                                                         "row_names": ["a"]})
        self.assertEqual((toolset, tool, args), ("EditorToolset.DataTableToolset", "GetRows",
                                                 {"dataTable": {"refPath": "/Game/T.T"}, "rowNames": ["a"]}))


class FabStore(unittest.TestCase):
    def test_store_tools_refused(self):
        for tool in ("InstallAsset", "PurchaseListing", "AddToLibrary", "ClaimFreeAsset", "DownloadAsset"):
            self.assertTrue(transport.fab_store_call("FabToolset.FabToolset", tool), tool)
        self.assertFalse(transport.fab_store_call("FabToolset.FabToolset", "SearchCatalog"))
        self.assertFalse(transport.fab_store_call("EditorToolset.AssetToolset", "ImportFile"))

    def test_mcp_call_refuses(self):
        with self.assertRaises(core.CliError) as cm:
            transport.Mcp(1, 5).call("Fab.FabLibraryToolset", "InstallAsset", {})
        self.assertIn("never runs Fab tools", str(cm.exception))

    def test_hook_denies(self):
        payload = {"tool_name": "mcp__unreal-mcp__call_tool",
                   "tool_input": {"toolset_name": "FabToolset.FabToolset", "tool_name": "PurchaseListing"}}
        self.assertIn("never runs Fab tools", hooks.hook_pre_mcp(payload))
        payload["tool_input"]["tool_name"] = "SearchCatalog"
        with unittest.mock.patch.object(hooks, "hook_fab_input", return_value=None), \
                unittest.mock.patch.object(hooks, "hook_start_pie", return_value=None):
            self.assertIsNone(hooks.hook_pre_mcp(payload))


class EngineVersion(unittest.TestCase):
    def test_beacon_then_association(self):
        self.assertEqual(transport.engine_version("x", {"engine": "5.9.0-60351404"}), (5, 9))
        self.assertIsNone(transport.engine_version(os.devnull, {}))


if __name__ == "__main__":
    unittest.main()
