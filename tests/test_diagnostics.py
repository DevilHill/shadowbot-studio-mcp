"""Health checks use only read-only tools and keep upstream failure bodies private."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnose


class ReadOnlyBridge:
    def __init__(self, *, failure=False):
        self.calls = []
        self.failure = failure

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failure:
            data = {"success": False, "message": "PRIVATE_UPSTREAM_CONTENT"}
            return {"structuredContent": data, "isError": True}
        if name == "bridge_status":
            data = {
                "connected": True,
                "endpoint": {"url": "http://127.0.0.1:1234/api/v1/mcp"},
                "app": {"app": {"app_id": "fixture-app", "has_unsaved_changes": True}},
                "implementation_version": "fixture-version",
                "implementation_sha256": "fixture-digest",
                "code_write_backend": "studio_native",
            }
        elif name == "app_list_flows":
            data = {"success": True, "total": 2, "flows": []}
        else:
            raise AssertionError(f"Diagnostic attempted a forbidden tool: {name}")
        return {"structuredContent": data, "isError": False}

    def list_tools(self):
        return [{"name": "bridge_status"}, {"name": "app_list_flows"}]


class DiagnosticsTests(unittest.TestCase):
    def test_unsaved_application_is_only_inspected(self):
        bridge = ReadOnlyBridge()
        with patch.object(diagnose, "ShadowBotBridge", return_value=bridge):
            report = diagnose.diagnose()
        self.assertEqual(bridge.calls, [("bridge_status", {}), ("app_list_flows", {})])
        self.assertTrue(report["connected"])
        self.assertEqual(report["flow_count"], 2)
        self.assertEqual(report["exposed_tool_count"], 2)

    def test_upstream_error_body_is_not_in_cli_output(self):
        bridge = ReadOnlyBridge(failure=True)
        stdout = io.StringIO()
        with patch.object(diagnose, "ShadowBotBridge", return_value=bridge), \
                patch.object(sys, "argv", ["diagnose.py"]), contextlib.redirect_stdout(stdout):
            exit_code = diagnose.main()
        self.assertEqual(exit_code, 1)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["connected"])
        self.assertNotIn("PRIVATE_UPSTREAM_CONTENT", stdout.getvalue())
        self.assertEqual(bridge.calls, [("bridge_status", {})])

    def test_unexpected_exception_content_is_not_in_cli_output(self):
        stdout = io.StringIO()
        with patch.object(diagnose, "diagnose", side_effect=RuntimeError("PRIVATE_EXCEPTION_CONTENT")), \
                patch.object(sys, "argv", ["diagnose.py"]), contextlib.redirect_stdout(stdout):
            exit_code = diagnose.main()
        self.assertEqual(exit_code, 1)
        self.assertNotIn("PRIVATE_EXCEPTION_CONTENT", stdout.getvalue())

    def test_removed_write_test_flag_cannot_start_a_bridge(self):
        with patch.object(diagnose, "ShadowBotBridge") as constructor, \
                patch.object(sys, "argv", ["diagnose.py", "--write-test"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            diagnose.main()
        self.assertEqual(raised.exception.code, 2)
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
