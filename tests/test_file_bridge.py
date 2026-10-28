"""Real temporary source files plus an in-memory Studio; never touches a live app."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import ShadowBotBridge
from file_backend import BackendError, FileSourceBackend
from transport import TransportError
from test_bridge import APP_ID, ORIGINAL, REPLACEMENT, FakeStudio, digest, mcp


class FileStudio(FakeStudio):
    """Reload reads the actual fixture file, while native source writes fail loudly."""

    def __init__(self, source_path):
        super().__init__()
        self.source_path = source_path
        self.has_unsaved_changes = False
        self.omit_saved_state = False
        self.reload_mode = "success"
        self.on_reload = None
        self.on_save = None
        self.save_mode = "success"
        self.expose_reload = True
        self.expose_save = True

    def list_tools(self):
        tools = super().list_tools()
        if self.expose_reload:
            tools.append({"name": "app.reload", "inputSchema": {"type": "object"}})
        if self.expose_save:
            tools.append({"name": "app.save", "inputSchema": {"type": "object"}})
        return tools

    def call(self, name, arguments):
        if name == "app.get_info":
            self.calls.append((name, copy.deepcopy(arguments)))
            app = {"app_id": APP_ID, "name": "Temporary file backend test"}
            if not self.omit_saved_state:
                app["has_unsaved_changes"] = self.has_unsaved_changes
            return mcp({"success": True, "app": app})
        if name == "app.reload":
            self.calls.append((name, copy.deepcopy(arguments)))
            if self.reload_mode == "timeout":
                raise TransportError("Simulated reload timeout")
            if self.reload_mode == "rejected":
                return mcp({"success": False, "message": "Simulated reload rejection"}, error=True)
            if self.reload_mode != "unchanged":
                self.source = self.source_path.read_text(encoding="utf-8-sig")
            # Observed Studio behavior: reload marks the app dirty even without edits.
            self.has_unsaved_changes = True
            if self.on_reload:
                self.on_reload()
            return mcp({"success": True})
        if name == "app.save":
            self.calls.append((name, copy.deepcopy(arguments)))
            if self.save_mode == "timeout":
                raise TransportError("Simulated save timeout")
            if self.save_mode == "rejected":
                return mcp({"success": False, "message": "Simulated save rejection"}, error=True)
            self.source_path.write_text(self.source, encoding="utf-8", newline="")
            self.has_unsaved_changes = False
            if self.on_save:
                self.on_save()
            return mcp({"success": True})
        if name in {"codeflow.edit", "codeflow.write"}:
            self.calls.append((name, copy.deepcopy(arguments)))
            raise AssertionError("File backend must never call a native source write")
        return super().call(name, arguments)


class FileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".dev").mkdir()
        self.source_path = self.project / "perform.py"
        self.source_path.write_text(ORIGINAL, encoding="utf-8", newline="")
        self.metadata_path = self.project / ".dev" / "perform.flow.json"
        self.metadata_path.write_text(json.dumps({"kind": "Code", "parameters": []}), encoding="utf-8")
        self.manifest_path = self.project / "package.json"
        self.manifest_path.write_text(json.dumps({
            "uuid": APP_ID, "flows": [{"filename": "perform", "kind": "Code"}]}), encoding="utf-8")
        self.original_metadata = self.metadata_path.read_bytes()
        self.original_manifest = self.manifest_path.read_bytes()
        self.config = self.root / "code_backends.json"
        self.config.write_text(json.dumps({"version": 1, "projects": {APP_ID: str(self.project)}}), encoding="utf-8")
        self.backend = FileSourceBackend(self.config)
        self.studio = FileStudio(self.source_path)
        self.backup_dir = self.root / "backups"
        self.bridge = ShadowBotBridge(
            resolver=lambda: {"url": "http://127.0.0.1:1/mcp", "headers": {}, "source": "test-only"},
            client_factory=lambda url, headers=None: self.studio,
            backup_dir=self.backup_dir, source_backend=self.backend)

    def write(self, **overrides):
        arguments = {"flow_id": "perform", "content": REPLACEMENT,
                     "expected_app_id": APP_ID, "expected_source_sha256": digest(ORIGINAL)}
        arguments.update(overrides)
        return self.bridge.call_tool("codeflow_write", arguments)

    def add_caller(self, value="custom", default="default"):
        self.studio.parameters["items"].insert(0, {
            "name": "input_value", "direction": "In", "type": "any", "kind": "Text", "value": default})
        self.studio.parameters["total"] += 1
        block = {"block_id": "caller-block", "block_index": 0, "prototype_name": "process.run",
                 "fields": {"process": {"value": "perform"},
                            "arguments.input_value": {"value": copy.deepcopy(value), "default": default}},
                 "outputs": {"process_result": "saved_result"}, "form_state": "ready",
                 "target_flow": {"flow_id": "perform", "flow_kind": "code"}}
        self.studio.caller_blocks["caller"] = [block]
        return block

    def names(self):
        return [name for name, unused in self.studio.calls]

    def assert_no_native_write(self):
        self.assertFalse(set(self.names()) & {"codeflow.write", "codeflow.edit", "flow.fill_blocks"})

    def assert_untouched(self):
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), ORIGINAL)
        self.assertEqual(self.metadata_path.read_bytes(), self.original_metadata)
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)
        self.assertNotIn("app.reload", self.names())
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def prepare_hook(self, after_prepare=None, after_commit=None):
        """Hook real operations only at the boundary where another editor can intervene."""
        prepare = self.backend.prepare

        def wrapped(*arguments):
            prepared = prepare(*arguments)
            if after_prepare:
                after_prepare()
            if after_commit:
                commit = prepared.commit

                def wrapped_commit():
                    receipt = commit()
                    after_commit()
                    return receipt

                prepared.commit = wrapped_commit
            return prepared

        return patch.object(self.backend, "prepare", side_effect=wrapped)

    def test_registered_write_updates_file_reloads_and_preserves_interface(self):
        self.add_caller()
        parameters = copy.deepcopy(self.studio.parameters)
        callers = copy.deepcopy(self.studio.caller_blocks)
        response = self.write()
        data = response["structuredContent"]
        self.assertFalse(response["isError"], data)
        self.assertTrue(data["success"])
        metadata = data["_bridge"]
        for key in ("verified", "disk_committed", "reload_attempted", "reload_succeeded",
                    "matches_requested_source", "parameters_preserved", "callers_preserved", "disk_source_matches"):
            self.assertTrue(metadata[key], key)
        self.assertEqual(metadata["backend"], "file_reload")
        self.assertFalse(metadata["callers_restored"])
        self.assertEqual(self.names().count("app.reload"), 1)
        self.assertEqual(self.names().count("app.save"), 1)
        self.assertFalse(self.studio.has_unsaved_changes)
        between = self.names()[self.names().index("app.reload") + 1:self.names().index("app.save")]
        self.assertIn("codeflow.read", between)
        self.assertIn("flow.list_parameters", between)
        self.assertIn("flow.blocks.list", between)
        self.assert_no_native_write()
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assertEqual(self.studio.source, REPLACEMENT)
        self.assertEqual(self.studio.parameters, parameters)
        self.assertEqual(self.studio.caller_blocks, callers)
        self.assertEqual(self.metadata_path.read_bytes(), self.original_metadata)
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)
        backup = json.loads(Path(metadata["backup_path"]).read_text(encoding="utf-8"))
        self.assertEqual(backup["source"], ORIGINAL)
        self.assertEqual(backup["parameters"], parameters)
        self.assertEqual(backup["callers"][0]["arguments"]["arguments.input_value"], "custom")

    def test_success_receipt_reports_completed_disk_write(self):
        response = self.write()
        self.assertFalse(response["isError"], response)
        receipt = response["structuredContent"]["_bridge"]["file_receipt"]
        self.assertTrue(receipt["commit_attempted"])
        self.assertTrue(receipt["file_written"])
        self.assertTrue(receipt["disk_verified"])

    def test_exact_edit_uses_file_backend(self):
        response = self.bridge.call_tool("codeflow_edit", {
            "flow_id": "perform", "old_string": "'old'", "new_string": "'new'",
            "expected_app_id": APP_ID, "expected_source_sha256": digest(ORIGINAL)})
        self.assertFalse(response["isError"], response)
        self.assertEqual(self.studio.source, REPLACEMENT)
        self.assertEqual(self.names().count("app.reload"), 1)
        self.assert_no_native_write()

    def test_status_identifies_version_backend_and_registered_project(self):
        response = self.bridge.call_tool("bridge_status", {})
        data = response["structuredContent"]
        self.assertFalse(response["isError"])
        self.assertEqual(data["implementation_version"], "1.2.0")
        self.assertRegex(data["implementation_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(data["code_write_backend"], "file_reload")
        self.assertEqual(Path(data["registered_project"]), self.project)
        self.config.write_text(json.dumps({"version": 1, "projects": {}}), encoding="utf-8")
        data = self.bridge.call_tool("bridge_status", {})["structuredContent"]
        self.assertEqual(data["code_write_backend"], "studio_native")
        self.assertIsNone(data["registered_project"])
        self.assert_untouched()

    def test_dirty_application_rejected_without_write(self):
        self.studio.has_unsaved_changes = True
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertIn("app_save", response["structuredContent"]["message"])
        self.assert_untouched()
        self.assertFalse(list(self.backup_dir.glob("*.json")))

    def test_missing_saved_state_is_not_assumed_clean(self):
        self.studio.omit_saved_state = True
        response = self.write()
        self.assertTrue(response["isError"])
        self.assert_untouched()

    def test_missing_reload_tool_rejects_before_disk_write(self):
        self.studio.expose_reload = False
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertIn("app.reload", response["structuredContent"]["message"])
        self.assert_untouched()

    def test_missing_save_tool_rejects_before_disk_write(self):
        self.studio.expose_save = False
        response = self.write()
        self.assertTrue(response["isError"])
        self.assertIn("app.save", response["structuredContent"]["message"])
        self.assert_untouched()

    def test_reload_rejection_keeps_committed_file_and_reports_incomplete_sync(self):
        self.studio.reload_mode = "rejected"
        self.assert_reload_incomplete()

    def test_reload_timeout_keeps_committed_file_and_does_not_retry(self):
        self.studio.reload_mode = "timeout"
        self.assert_reload_incomplete()

    def assert_reload_incomplete(self):
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        metadata = data["_bridge"]
        self.assertTrue(metadata["disk_committed"])
        self.assertTrue(metadata["reload_attempted"])
        self.assertFalse(metadata["reload_succeeded"])
        self.assertFalse(metadata["verified"])
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assertEqual(self.studio.source, ORIGINAL)
        self.assertEqual(self.names().count("app.reload"), 1)
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_reload_false_success_requires_actual_source_readback(self):
        self.studio.reload_mode = "unchanged"
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertTrue(data["_bridge"]["reload_succeeded"])
        self.assertFalse(data["_bridge"]["matches_requested_source"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_reload_parameter_loss_does_not_report_verified(self):
        self.studio.on_reload = lambda: self.studio.parameters.update(items=[], total=0)
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertFalse(data["_bridge"]["parameters_preserved"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_reload_caller_change_is_reported_without_replaying_values(self):
        caller = self.add_caller()
        self.studio.on_reload = lambda: caller["fields"]["arguments.input_value"].update(value="different")
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertFalse(data["_bridge"]["callers_preserved"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertEqual(caller["fields"]["arguments.input_value"]["value"], "different")
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_source_change_during_preparation_prevents_file_commit(self):
        concurrent = "def main(args):\n    return 'concurrent'\n"
        with self.prepare_hook(after_prepare=lambda: setattr(self.studio, "source", concurrent)):
            response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "file_write_precondition_failed")
        self.assertEqual(self.studio.source, concurrent)
        self.assert_untouched()

    def test_parameter_change_during_preparation_prevents_file_commit(self):
        with self.prepare_hook(after_prepare=lambda: self.studio.parameters["items"][0].update(value="concurrent")):
            response = self.write()
        self.assertTrue(response["isError"])
        self.assertFalse(response["structuredContent"]["_bridge"]["disk_committed"])
        self.assert_untouched()

    def test_caller_change_during_preparation_prevents_file_commit(self):
        caller = self.add_caller()
        with self.prepare_hook(after_prepare=lambda: caller["fields"]["arguments.input_value"].update(value="concurrent")):
            response = self.write()
        self.assertTrue(response["isError"])
        self.assertFalse(response["structuredContent"]["_bridge"]["disk_committed"])
        self.assert_untouched()

    def test_dirty_application_after_commit_prevents_reload(self):
        with self.prepare_hook(after_commit=lambda: setattr(self.studio, "has_unsaved_changes", True)):
            response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertTrue(data["_bridge"]["disk_committed"])
        self.assertFalse(data["_bridge"]["reload_attempted"])
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assertEqual(self.studio.source, ORIGINAL)
        self.assertNotIn("app.reload", self.names())
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_unexpected_memory_source_after_commit_prevents_reload(self):
        concurrent = "def main(args):\n    return 'concurrent'\n"
        with self.prepare_hook(after_commit=lambda: setattr(self.studio, "source", concurrent)):
            response = self.write()
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["code"], "file_sync_incomplete")
        self.assertEqual(self.studio.source, concurrent)
        self.assertNotIn("app.reload", self.names())
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_commit_then_verification_failure_reports_disk_as_committed(self):
        prepare = self.backend.prepare

        def prepare_with_failed_readback(*arguments):
            prepared = prepare(*arguments)
            prepared.verify = lambda: (_ for _ in ()).throw(BackendError("Simulated readback failure after atomic replacement"))
            return prepared

        with patch.object(self.backend, "prepare", side_effect=prepare_with_failed_readback):
            response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertTrue(data["_bridge"]["disk_committed"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertNotIn("app.reload", self.names())
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_replace_effective_then_oserror_is_unknown_and_never_reloaded(self):
        atomic_replace = os.replace

        def replace_then_error(source, destination):
            atomic_replace(source, destination)
            raise OSError("Simulated error after the filesystem operation took effect")

        with patch("file_backend.os.replace", side_effect=replace_then_error):
            response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_write_outcome_unknown")
        metadata = data["_bridge"]
        self.assertIsNone(metadata["disk_committed"])
        self.assertTrue(metadata["file_receipt"]["commit_attempted"])
        self.assertFalse(metadata["verified"])
        self.assertTrue(Path(metadata["backup_path"]).is_file())
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assertEqual(self.studio.source, ORIGINAL)
        self.assertNotIn("app.reload", self.names())
        self.assertNotIn("app.save", self.names())
        self.assert_no_native_write()

    def test_save_timeout_reports_incomplete_sync_without_retry(self):
        self.studio.save_mode = "timeout"
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertTrue(data["_bridge"]["disk_committed"])
        self.assertTrue(data["_bridge"]["reload_succeeded"])
        self.assertTrue(data["_bridge"]["save_attempted"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertEqual(self.names().count("app.save"), 1)
        self.assertEqual(self.names().count("app.reload"), 1)
        self.assert_no_native_write()

    def test_post_save_source_change_is_not_verified(self):
        self.studio.on_save = lambda: setattr(self.studio, "source", "def main(args):\n    return 'changed'\n")
        response = self.write()
        data = response["structuredContent"]
        self.assertTrue(response["isError"])
        self.assertEqual(data["code"], "file_sync_incomplete")
        self.assertTrue(data["_bridge"]["saved"])
        self.assertFalse(data["_bridge"]["matches_requested_source"])
        self.assertFalse(data["_bridge"]["verified"])
        self.assertEqual(self.names().count("app.save"), 1)
        self.assert_no_native_write()

    def test_save_may_refresh_manifest_after_verified_reload(self):
        def update_manifest():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["modified_at"] = "refreshed-by-studio"
            self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.studio.on_save = update_manifest
        response = self.write()
        self.assertFalse(response["isError"], response)
        self.assertTrue(response["structuredContent"]["_bridge"]["verified"])
        self.assertNotEqual(self.manifest_path.read_bytes(), self.original_manifest)
        self.assertEqual(self.source_path.read_text(encoding="utf-8"), REPLACEMENT)
        self.assert_no_native_write()

    def test_complex_caller_values_are_compared_and_never_replayed(self):
        complex_value = {"object": {"items": {"array": ["a", {"variable": "row"}]}}}
        caller = self.add_caller(value=complex_value)
        original = copy.deepcopy(caller)
        response = self.write()
        self.assertFalse(response["isError"], response)
        self.assertTrue(response["structuredContent"]["_bridge"]["callers_preserved"])
        self.assertEqual(caller, original)
        self.assert_no_native_write()

    def test_invalid_python_rejects_before_write_or_reload(self):
        response = self.write(content="def main(args)\n")
        self.assertTrue(response["isError"])
        self.assertIn("valid Python", response["structuredContent"]["message"])
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
