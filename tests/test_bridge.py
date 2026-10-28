"""Editing invariants against an in-memory Studio double; no real app is touched."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import ShadowBotBridge

ORIGINAL = "def main(args):\n    return 'old'\n"
REPLACEMENT = "def main(args):\n    return 'new'\n"
APP_ID = "test-app-123"


def digest(source):
    return hashlib.sha256(source.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def mcp(data, error=False):
    # Studio returns JSON inside MCP text content, not our bridge's structuredContent.
    return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": error}


class FakeStudio:
    def __init__(self):
        self.source = ORIGINAL
        self.parameters = {"success": True, "flow_id": "perform", "total": 1, "items": [
            {"name": "result", "direction": "Out", "type": "any", "kind": "Text", "value": ""}]}
        self.calls = []
        self.write_mode = "success"
        self.before_write = None
        self.change_after_first_read = None
        self.change_on_first_parameter_read = None
        self.source_reads = 0
        self.parameter_reads = 0
        self.caller_blocks = {}
        self.reset_callers_on_write = False
        self.restore_mode = "success"
        self.before_caller_read = None
        self.caller_reads = 0

    def connect(self):
        return {"protocolVersion": "2025-03-26", "serverInfo": {"name": "fake-studio", "version": "test"}}

    def list_tools(self):
        names = ["app.get_info", "codeflow.read", "codeflow.write", "codeflow.edit",
                 "flow.list_parameters", "app.list_flows", "flow.blocks.list", "flow.fill_blocks",
                 "workflow.run", "execution.run", "app.run"]
        return [{"name": name, "inputSchema": {"type": "object"}} for name in names]

    def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "app.get_info":
            return mcp({"success": True, "app": {"app_id": APP_ID, "name": "Local test"}})
        if name == "app.list_flows":
            flows = [{"flow_id": "perform", "flow_kind": "code"}]
            flows += [{"flow_id": flow_id, "flow_kind": "visual"} for flow_id in self.caller_blocks]
            return mcp({"success": True, "total": len(flows), "flows": flows})
        if name == "flow.blocks.list":
            self.caller_reads += 1
            if self.before_caller_read:
                self.before_caller_read(self.caller_reads)
            blocks = self.caller_blocks[arguments["flow_id"]]
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 50)
            end = min(len(blocks), offset + limit)
            return mcp({"success": True, "flow_id": arguments["flow_id"], "total": len(blocks),
                        "blocks": copy.deepcopy(blocks[offset:end]),
                        "next_offset": end if end < len(blocks) else None})
        if name == "flow.fill_blocks":
            if self.restore_mode == "rejected":
                return mcp({"success": True, "fill_errors": [{"code": "field_invalid"}]})
            if self.restore_mode != "unchanged":
                for fill in arguments["fills"]:
                    block = next(b for b in self.caller_blocks[arguments["flow_id"]] if b["block_id"] == fill["block_id"])
                    for key, value in fill.get("inputs", {}).items():
                        # Real forms may unwrap explicit text to its literal value.
                        block["fields"][key]["value"] = value["text"] if isinstance(value, dict) and set(value) == {"text"} else value
                    if "outputs" in fill:
                        block.setdefault("outputs", {}).update(copy.deepcopy(fill["outputs"]))
            return mcp({"success": True, "fill_errors": []})
        if name == "codeflow.read":
            self.source_reads += 1
            lines = self.source.splitlines(keepends=True)
            offset, limit = arguments.get("offset", 1), arguments.get("limit", 2000)
            selected = lines[offset - 1:offset - 1 + limit]
            next_offset = offset + len(selected)
            data = {"success": True, "flow_id": "perform", "content": "".join(selected),
                    "offset": offset, "limit": limit, "total_lines": len(lines),
                    "next_offset": next_offset if next_offset <= len(lines) else None}
            if self.source_reads == 1 and self.change_after_first_read is not None:
                self.source = self.change_after_first_read
            return mcp(data)
        if name == "flow.list_parameters":
            self.parameter_reads += 1
            if self.parameter_reads == 1 and self.change_on_first_parameter_read is not None:
                self.source = self.change_on_first_parameter_read
            return mcp(copy.deepcopy(self.parameters))
        if name in ("codeflow.write", "codeflow.edit"):
            if self.before_write:
                self.before_write()
            if self.reset_callers_on_write:
                for blocks in self.caller_blocks.values():
                    for block in blocks:
                        if block.get("target_flow", {}).get("flow_id") == "perform":
                            for key, field in block["fields"].items():
                                if key.startswith("arguments.") and "default" in field:
                                    field["value"] = field.pop("default")
            if self.write_mode == "update_failed":
                return mcp({"success": False, "code": "update_failed", "stage": "flow_definition",
                            "message": "The method or operation is not implemented."}, error=True)
            if self.write_mode == "update_failed_nested":
                return mcp({"success": False, "message": "The method or operation is not implemented.",
                            "diagnostics": [{"code": "update_failed", "field": "flow_definition",
                                             "message": "The method or operation is not implemented."}]}, error=True)
            if self.write_mode != "unchanged":
                if name == "codeflow.write":
                    self.source = arguments["content"]
                else:
                    matches = self.source.count(arguments["old_string"])
                    if not matches or (matches > 1 and not arguments.get("replace_all")):
                        return mcp({"success": False, "code": "exact_match_failed",
                                    "message": "Requested source no longer matches."}, error=True)
                    self.source = self.source.replace(arguments["old_string"], arguments["new_string"],
                                                      -1 if arguments.get("replace_all") else 1)
            if self.write_mode == "drop_parameters":
                self.parameters["items"] = []
                self.parameters["total"] = 0
            if self.write_mode == "missing_parameters_after_write":
                self.parameters.pop("items")
            if self.write_mode == "malformed_write_result":
                return {"content": [{"type": "text", "text": "Response was not JSON."}]}
            return mcp({"success": True, "flow_id": "perform"})
        raise AssertionError(f"Unexpected upstream tool: {name}")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup_dir = Path(self.temp.name) / "backups"
        self.studio = FakeStudio()
        self.connections = []

        def client_factory(url, headers=None):
            self.connections.append((url, headers))
            return self.studio

        self.bridge = ShadowBotBridge(
            resolver=lambda: {"url": "http://127.0.0.1:1/mcp", "headers": {}, "source": "test-only"},
            client_factory=client_factory, backup_dir=self.backup_dir)

    def write(self, **overrides):
        arguments = {"flow_id": "perform", "content": REPLACEMENT,
                     "expected_app_id": APP_ID, "expected_source_sha256": digest(ORIGINAL)}
        arguments.update(overrides)
        return self.bridge.call_tool("codeflow_write", arguments)

    def writes(self):
        return [entry for entry in self.studio.calls if entry[0] in ("codeflow.write", "codeflow.edit")]

    def backups(self):
        return list(self.backup_dir.glob("*.json"))

    def add_caller(self, value="non-default", default="default", flow_id="caller", block_id="call-1"):
        if not any(p["name"] == "input_value" for p in self.studio.parameters["items"]):
            self.studio.parameters["items"].insert(0, {
                "name": "input_value", "direction": "In", "type": "str", "kind": "Text", "value": default})
            self.studio.parameters["total"] += 1
        block = {"block_id": block_id, "block_index": len(self.studio.caller_blocks.get(flow_id, [])),
                 "prototype_name": "process.run", "fields": {"process": {"value": "perform"},
                     "arguments.input_value": {"value": copy.deepcopy(value), "default": copy.deepcopy(default)}},
                 "outputs": {"process_result": "saved_result"}, "form_state": "ready",
                 "target_flow": {"flow_id": "perform", "flow_kind": "code"}}
        self.studio.caller_blocks.setdefault(flow_id, []).append(block)
        return block

    def test_missing_or_wrong_app_id_prevents_write(self):
        for expected in (None, "", "different-app"):
            with self.subTest(expected=expected):
                response = self.write(expected_app_id=expected)
                self.assertTrue(response["isError"])
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.backups(), [])
                self.assertEqual(self.studio.source, ORIGINAL)

    def test_stale_source_hash_prevents_write_and_backup(self):
        response = self.write(expected_source_sha256="0" * 64)
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.studio.source, ORIGINAL)

    def test_backup_precedes_write_and_success_requires_readback(self):
        original_parameters = copy.deepcopy(self.studio.parameters)
        observed_backups = []

        def inspect_before_write():
            files = self.backups()
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["source"], ORIGINAL)
            self.assertEqual(saved["parameters"], original_parameters)
            self.assertEqual(saved["app_id"], APP_ID)
            self.assertEqual(saved["flow_id"], "perform")
            self.assertEqual(saved["source_sha256"], digest(ORIGINAL))
            observed_backups.append(files[0])

        self.studio.before_write = inspect_before_write
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertEqual(len(observed_backups), 1)
        self.assertEqual(len(self.writes()), 1)
        metadata = response["structuredContent"]["_bridge"]
        self.assertTrue(metadata["verified"])
        self.assertTrue(metadata["parameters_preserved"])
        self.assertEqual(metadata["source_sha256"], digest(REPLACEMENT))
        names = [name for name, _ in self.studio.calls]
        write_index = names.index("codeflow.edit")
        self.assertIn("codeflow.read", names[write_index + 1:])
        self.assertIn("flow.list_parameters", names[write_index + 1:])
        self.assertEqual(self.writes()[0], ("codeflow.edit", {
            "flow_id": "perform", "old_string": ORIGINAL,
            "new_string": REPLACEMENT, "replace_all": False}))

    def test_concurrent_edit_after_hash_check_is_preserved(self):
        concurrent_source = "def main(args):\n    return 'edited in Studio'\n"
        self.studio.change_on_first_parameter_read = concurrent_source
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "exact_match_failed")
        self.assertFalse(response["structuredContent"]["_bridge"]["verified"])
        self.assertEqual(self.studio.source, concurrent_source)
        self.assertEqual(self.writes(), [("codeflow.edit", {
            "flow_id": "perform", "old_string": ORIGINAL,
            "new_string": REPLACEMENT, "replace_all": False})])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text(encoding="utf-8"))["source"], ORIGINAL)

    def test_matching_source_is_unchanged_without_write_or_backup(self):
        response = self.write(content=ORIGINAL)
        self.assertFalse(response["isError"])
        self.assertTrue(response["structuredContent"]["_bridge"]["unchanged"])
        self.assertTrue(response["structuredContent"]["_bridge"]["verified"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])

    def test_empty_source_cannot_be_written_without_exact_match_guard(self):
        self.studio.source = ""
        response = self.write(expected_source_sha256=digest(""))
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.studio.source, "")

    def test_upstream_update_failed_stays_error_without_retry(self):
        self.studio.write_mode = "update_failed"
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "update_failed")
        self.assertFalse(response["structuredContent"]["_bridge"]["verified"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.studio.source, ORIGINAL)
        metadata = response["structuredContent"]["_bridge"]
        diagnostic = metadata["diagnostic"]
        self.assertEqual(diagnostic["category"], "studio_internal_codeflow_update_failed")
        self.assertFalse(diagnostic["checks"]["observed_source_changed"])
        self.assertTrue(diagnostic["checks"]["parameters_preserved"])
        self.assertIn("Do not blindly retry", metadata["next_step"])
        self.assertIn("does not identify", diagnostic["cause_limit"])

    def test_upstream_success_without_source_change_is_error(self):
        self.studio.write_mode = "unchanged"
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertFalse(response["structuredContent"]["_bridge"]["matches_requested_source"])
        self.assertEqual(len(self.writes()), 1)

    def test_parameter_loss_is_error_even_when_source_matches(self):
        self.studio.write_mode = "drop_parameters"
        response = self.write()
        self.assertTrue(response["isError"])
        metadata = response["structuredContent"]["_bridge"]
        self.assertTrue(metadata["matches_requested_source"])
        self.assertFalse(metadata["parameters_preserved"])
        self.assertFalse(metadata["verified"])
        self.assertEqual(len(self.writes()), 1)

    def test_missing_parameter_items_before_write_rejects_without_mutation(self):
        self.studio.parameters.pop("items")
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "bridge_precondition_failed")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.studio.source, ORIGINAL)

    def test_missing_parameter_items_after_write_cannot_be_verified(self):
        self.studio.write_mode = "missing_parameters_after_write"
        response = self.write()
        self.assertTrue(response["isError"])
        metadata = response["structuredContent"]["_bridge"]
        self.assertFalse(metadata["verified"])
        self.assertIn("verification_error", metadata)
        self.assertEqual(self.studio.source, REPLACEMENT)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.backups()), 1)
        saved = json.loads(Path(metadata["backup_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["parameters"]["items"][0]["name"], "result")
        self.assertEqual(saved["parameters"]["total"], 1)

    def test_malformed_write_reply_preserves_backup_and_marks_unknown_outcome(self):
        self.studio.write_mode = "malformed_write_result"
        response = self.write()
        self.assertTrue(response["isError"])
        data = response["structuredContent"]
        self.assertEqual(data["code"], "write_outcome_unknown")
        self.assertFalse(data["_bridge"]["verified"])
        self.assertEqual(self.studio.source, REPLACEMENT)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.backups()), 1)
        saved = json.loads(Path(data["_bridge"]["backup_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["source"], ORIGINAL)
        self.assertEqual(saved["parameters"]["total"], 1)

    def test_unknown_tools_are_not_forwarded(self):
        for name in ("arbitrary_tool", "workflow_run", "execution_run", "app_run", "codeflow.write"):
            with self.subTest(name=name):
                response = self.bridge.call_tool(name, {})
                self.assertTrue(response["isError"])
        self.assertEqual(self.studio.calls, [])
        self.assertEqual(self.connections, [])

    def test_workflow_execution_is_not_advertised(self):
        self.bridge.call_tool("bridge_status", {})
        names = {tool["name"] for tool in self.bridge.list_tools()}
        self.assertIn("codeflow_write", names)
        self.assertIn("codeflow_read", names)
        self.assertFalse({"workflow_run", "execution_run", "app_run"} & names)
        self.assertFalse(any(name.startswith(("workflow_", "execution_")) for name in names))

    def test_tool_schemas_are_discovered_without_mutating_upstream_catalog(self):
        catalog = self.studio.list_tools()
        write = next(tool for tool in catalog if tool["name"] == "codeflow.write")
        write.update(description="Synthetic runtime schema", inputSchema={
            "type": "object", "properties": {"content": {"type": "string", "maxLength": 321}},
            "required": ["content"], "additionalProperties": False})
        original = copy.deepcopy(catalog)
        self.studio.list_tools = lambda: catalog
        definitions = {tool["name"]: tool for tool in self.bridge.list_tools()}
        schema = definitions["codeflow_write"]["inputSchema"]
        self.assertEqual(schema["properties"]["content"]["maxLength"], 321)
        self.assertEqual(set(schema["required"]), {"content", "expected_app_id", "expected_source_sha256"})
        self.assertIn("Synthetic runtime schema", definitions["codeflow_write"]["description"])
        self.assertEqual(catalog, original)
        self.assertNotIn("workflow_run", definitions)
        self.assertNotIn("app_save", definitions)  # Not advertised by this fake session.
        self.assertEqual(self.studio.calls, [])
        self.assertFalse(self.bridge.take_tool_list_changed())

    def test_offline_status_recovers_and_signals_catalog_change_once(self):
        working_resolver = self.bridge.resolver

        def offline():
            raise OSError("PRIVATE_DISCOVERY_DETAIL")

        self.bridge.resolver = offline
        definitions = self.bridge.list_tools()
        self.assertEqual([tool["name"] for tool in definitions], ["bridge_status"])
        response = self.bridge.call_tool("bridge_status", {})
        self.assertTrue(response["isError"])
        self.assertNotIn("PRIVATE_DISCOVERY_DETAIL", json.dumps(response))
        self.assertFalse(self.bridge.take_tool_list_changed())
        self.bridge.resolver = working_resolver
        self.assertFalse(self.bridge.call_tool("bridge_status", {"reconnect": True})["isError"])
        self.assertTrue(self.bridge.take_tool_list_changed())
        self.assertFalse(self.bridge.take_tool_list_changed())
        self.assertIn("codeflow_read", {tool["name"] for tool in self.bridge.list_tools()})
        self.bridge.resolver = offline
        self.assertTrue(self.bridge.call_tool("bridge_status", {"reconnect": True})["isError"])
        self.assertTrue(self.bridge.take_tool_list_changed())
        self.assertEqual([tool["name"] for tool in self.bridge.list_tools()], ["bridge_status"])

    def test_malformed_runtime_schema_keeps_only_status_tool(self):
        catalog = self.studio.list_tools()
        catalog[0]["inputSchema"] = {"type": "object", "required": "not-a-list"}
        self.studio.list_tools = lambda: catalog
        self.assertEqual([tool["name"] for tool in self.bridge.list_tools()], ["bridge_status"])
        self.assertEqual(self.bridge.available, set())
        self.assertTrue(self.bridge.call_tool("codeflow_write", {})["isError"])
        self.assertEqual(self.writes(), [])

    def test_missing_advertised_write_tool_cannot_be_called_directly(self):
        catalog = [tool for tool in self.studio.list_tools() if tool["name"] != "codeflow.write"]
        self.studio.list_tools = lambda: catalog
        self.assertTrue(self.write()["isError"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])

    def test_reconnect_detects_schema_change_without_persisting_it(self):
        catalog = self.studio.list_tools()
        self.studio.list_tools = lambda: catalog
        self.bridge.list_tools()
        catalog[0]["inputSchema"]["properties"] = {"fresh": {"type": "boolean"}}
        self.assertFalse(self.bridge.call_tool("bridge_status", {"reconnect": True})["isError"])
        self.assertTrue(self.bridge.take_tool_list_changed())
        self.assertFalse(self.bridge.take_tool_list_changed())
        self.assertFalse(self.backup_dir.exists())

    def test_read_page_and_hash_come_from_same_snapshot(self):
        self.studio.change_after_first_read = REPLACEMENT
        response = self.bridge.call_tool("codeflow_read", {"flow_id": "perform", "offset": 2, "limit": 1})
        self.assertFalse(response["isError"])
        data = response["structuredContent"]
        self.assertEqual(data["content"].rstrip("\r\n"), "    return 'old'")
        self.assertEqual(data["_bridge"]["source_sha256"], digest(ORIGINAL))
        self.assertEqual(self.studio.source_reads, 1)

    def test_exact_edit_also_checks_result_and_keeps_parameters(self):
        response = self.bridge.call_tool("codeflow_edit", {
            "flow_id": "perform", "old_string": "'old'", "new_string": "'new'",
            "expected_app_id": APP_ID, "expected_source_sha256": digest(ORIGINAL)})
        self.assertFalse(response["isError"])
        self.assertTrue(response["structuredContent"]["_bridge"]["verified"])
        self.assertEqual(self.studio.source, REPLACEMENT)
        self.assertEqual(self.writes(), [("codeflow.edit", {
            "flow_id": "perform", "old_string": ORIGINAL,
            "new_string": REPLACEMENT, "replace_all": False})])

    def test_successful_code_write_restores_reset_caller_and_backs_up_original(self):
        caller = self.add_caller(value={"expression": "17"}, default={"expression": "3"})
        original_caller = copy.deepcopy(caller)
        self.studio.reset_callers_on_write = True
        response = self.write()
        self.assertFalse(response["isError"])
        metadata = response["structuredContent"]["_bridge"]
        self.assertTrue(metadata["caller_changes_detected"])
        self.assertTrue(metadata["callers_restored"])
        self.assertTrue(metadata["callers_preserved"])
        self.assertTrue(metadata["verified"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], {"expression": "17"})
        saved = json.loads(self.backups()[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["callers"][0]["form"], original_caller)
        fills = [args for name, args in self.studio.calls if name == "flow.fill_blocks"]
        self.assertEqual(fills, [{"flow_id": "caller", "fills": [{"block_id": "call-1",
            "inputs": {"arguments.input_value": {"expression": "17"}}}]}])
        self.assertEqual(len(self.writes()), 1)

    def test_unchanged_callers_are_not_filled(self):
        self.add_caller()
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertTrue(response["structuredContent"]["_bridge"]["callers_preserved"])
        self.assertFalse(response["structuredContent"]["_bridge"]["caller_changes_detected"])
        self.assertFalse(any(name == "flow.fill_blocks" for name, _ in self.studio.calls))

    def test_failed_code_write_can_restore_callers_but_remains_upstream_error(self):
        caller = self.add_caller()
        self.studio.reset_callers_on_write = True
        self.studio.write_mode = "update_failed"
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "update_failed")
        self.assertTrue(data["_bridge"]["callers_restored"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], "non-default")
        self.assertEqual(self.studio.source, ORIGINAL)
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(data["_bridge"]["diagnostic"]["checks"]["callers_restored"])
        self.assertFalse(data["_bridge"]["diagnostic"]["checks"]["matches_requested_source"])

    def test_caller_restoration_rejection_or_false_success_never_verifies(self):
        for mode in ("rejected", "unchanged"):
            with self.subTest(mode=mode):
                self.studio.source = ORIGINAL
                self.studio.caller_blocks.clear()
                self.add_caller()
                self.studio.reset_callers_on_write = True
                self.studio.restore_mode = mode
                response = self.write()
                metadata = response["structuredContent"]["_bridge"]
                self.assertTrue(response["isError"])
                self.assertFalse(metadata["verified"])
                self.assertFalse(metadata["callers_preserved"])
                self.assertIn("caller_restore_error", metadata)

    def test_unsupported_caller_semantics_rejects_before_backup_and_write(self):
        self.add_caller(value={"concat": ["a", {"variable": "b"}]})
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "bridge_precondition_failed")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backups(), [])

    def test_missing_caller_argument_rejects_before_code_write(self):
        caller = self.add_caller()
        del caller["fields"]["arguments.input_value"]
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])

    def test_empty_literal_and_variable_semantics_survive_restoration(self):
        self.add_caller(value="", flow_id="caller-empty")
        self.add_caller(value={"variable": "task_value"}, flow_id="caller-variable")
        self.studio.reset_callers_on_write = True
        response = self.write()
        self.assertFalse(response["isError"])
        fills = [args for name, args in self.studio.calls if name == "flow.fill_blocks"]
        self.assertEqual(fills[0]["fills"][0]["inputs"]["arguments.input_value"], {"text": ""})
        self.assertEqual(fills[1]["fills"][0]["inputs"]["arguments.input_value"], {"variable": "task_value"})

    def test_caller_change_during_verification_is_not_overwritten(self):
        caller = self.add_caller()
        self.studio.reset_callers_on_write = True
        def change_on_second_post_write_read(count):
            if count == 4:
                caller["fields"]["arguments.input_value"]["value"] = "user-edited"
        self.studio.before_caller_read = change_on_second_post_write_read
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertFalse(response["structuredContent"]["_bridge"]["verified"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], "user-edited")
        self.assertFalse(any(name == "flow.fill_blocks" for name, _ in self.studio.calls))

    def test_caller_blocks_after_first_page_are_protected(self):
        self.studio.caller_blocks["caller"] = [
            {"block_id": f"comment-{i}", "block_index": i, "prototype_name": "programing.comment"}
            for i in range(50)]
        caller = self.add_caller()
        self.studio.reset_callers_on_write = True
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], "non-default")
        self.assertTrue(any(name == "flow.blocks.list" and args["offset"] == 50 for name, args in self.studio.calls))

    def test_unrelated_incomplete_target_does_not_block_code_write(self):
        self.studio.caller_blocks["unrelated"] = [{"block_id": "broken-call", "block_index": 0,
            "prototype_name": "process.run", "fields": {"process": {"value": "different_flow"}},
            "form_state": "incomplete"}]
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertEqual(response["structuredContent"]["_bridge"]["caller_count"], 0)

    def test_unresolved_dynamic_target_rejects_before_code_write(self):
        self.studio.caller_blocks["dynamic"] = [{"block_id": "dynamic-call", "block_index": 0,
            "prototype_name": "process.run", "fields": {"process": {"value": {"variable": "target"}}},
            "form_state": "incomplete"}]
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])

    def test_restore_only_changed_fields_keeps_inherited_arguments_implicit(self):
        caller = self.add_caller()
        self.studio.parameters["items"].append({"name": "inherited", "direction": "In", "type": "str",
                                               "kind": "Text", "value": "default-only"})
        self.studio.parameters["total"] += 1
        caller["fields"]["arguments.inherited"] = {"value": "default-only"}
        self.studio.reset_callers_on_write = True
        response = self.write()
        self.assertFalse(response["isError"])
        fill = next(args["fills"][0] for name, args in self.studio.calls if name == "flow.fill_blocks")
        self.assertEqual(fill, {"block_id": "call-1", "inputs": {"arguments.input_value": "non-default"}})

    def test_changed_output_capture_is_not_overwritten(self):
        caller = self.add_caller()
        self.studio.before_write = lambda: caller["outputs"].update(process_result="user_result")
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(caller["outputs"], {"process_result": "user_result"})
        self.assertFalse(any(name == "flow.fill_blocks" for name, _ in self.studio.calls))
        self.assertIn("output captures changed", response["structuredContent"]["_bridge"]["caller_restore_error"])

    def test_unexpected_nondefault_argument_change_is_not_overwritten(self):
        caller = self.add_caller()
        self.studio.before_write = lambda: caller["fields"]["arguments.input_value"].update(value="user-edited")
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], "user-edited")
        self.assertFalse(any(name == "flow.fill_blocks" for name, _ in self.studio.calls))

    def test_caller_change_before_mutation_prevents_code_write(self):
        caller = self.add_caller()
        def change_before_write(count):
            if count == 2:
                caller["fields"]["arguments.input_value"]["value"] = "user-edited"
        self.studio.before_caller_read = change_before_write
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.studio.source, ORIGINAL)

    def test_caller_without_output_capture_is_supported(self):
        caller = self.add_caller()
        del caller["outputs"]
        self.studio.reset_callers_on_write = True
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertTrue(response["structuredContent"]["_bridge"]["callers_restored"])
        self.assertNotIn("outputs", caller)

    def test_parameterless_tool_caller_without_outputs_is_supported(self):
        self.studio.parameters["items"] = []
        self.studio.parameters["total"] = 0
        self.studio.caller_blocks["caller"] = [{"block_id": "tool-call", "block_index": 0,
            "prototype_name": "process.run", "fields": {"process": {"value": "perform"}},
            "form_state": "ready", "target_flow": {"flow_id": "perform", "flow_kind": "code"}}]
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertEqual(response["structuredContent"]["_bridge"]["caller_count"], 1)

    def test_invalid_explicit_outputs_reject_before_write(self):
        caller = self.add_caller()
        caller["outputs"] = None
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(self.writes(), [])

    def test_verification_error_includes_safe_diagnostic_summary(self):
        self.add_caller()
        def break_post_write_snapshot(count):
            if count == 3:
                self.studio.caller_blocks["caller"][0]["block_index"] = 5
        self.studio.before_caller_read = break_post_write_snapshot
        response = self.write()
        self.assertTrue(response["isError"])
        error = response["structuredContent"]["_bridge"]["verification_error"]
        self.assertIn("inconsistent caller block snapshot", error)
        self.assertNotIn("non-default", error)

    def test_nested_update_failure_gets_diagnostic_without_changing_native_response(self):
        self.studio.write_mode = "update_failed_nested"
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertFalse(data["success"])
        self.assertEqual(data["message"], "The method or operation is not implemented.")
        self.assertEqual(data["diagnostics"], [{"code": "update_failed", "field": "flow_definition",
            "message": "The method or operation is not implemented."}])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertIn("internal CodeFlow update failure", data["_bridge"]["diagnostic"]["message"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.studio.source, ORIGINAL)

    def test_update_failure_unknown_checks_remain_unknown_in_diagnostic(self):
        self.studio.write_mode = "update_failed_nested"
        self.studio.before_write = lambda: self.studio.parameters.pop("items")
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertIn("verification_error", data["_bridge"])
        checks = data["_bridge"]["diagnostic"]["checks"]
        self.assertIsNone(checks["parameters_preserved"])
        self.assertIsNone(checks["callers_preserved"])

    def test_successful_write_has_no_internal_failure_diagnostic(self):
        response = self.write()
        self.assertFalse(response["isError"])
        self.assertNotIn("diagnostic", response["structuredContent"]["_bridge"])


if __name__ == "__main__":
    unittest.main()
