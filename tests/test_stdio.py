"""Exercise the actual stdio loop in child processes; no ShadowBot is required."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

SERVER_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = r'''
from stdio_server import main
class FakeBridge:
    changed = False
    def list_tools(self):
        return [{"name": "echo", "description": "Test echo", "inputSchema": {"type": "object"}}]
    def call_tool(self, name, arguments):
        if name == "fail":
            raise RuntimeError("CREDENTIAL_MUST_NOT_LEAK")
        if name == "bridge_status":
            self.changed = True
        return {"content": [{"type": "text", "text": arguments.get("text", "")}],
                "structuredContent": {"name": name, "arguments": arguments}, "isError": False}
    def take_tool_list_changed(self):
        changed, self.changed = self.changed, False
        return changed
raise SystemExit(main(FakeBridge()))
'''


def request(method, request_id=1, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


class StdioTests(unittest.TestCase):
    def run_server(self, messages):
        wire = b"".join(
            (item if isinstance(item, bytes) else json.dumps(item, ensure_ascii=False).encode("utf-8"))
            + b"\n" for item in messages
        )
        process = subprocess.run(
            [sys.executable, "-u", "-c", LAUNCHER], input=wire,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=SERVER_DIR, timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        self.assertNotIn(b"CREDENTIAL_MUST_NOT_LEAK", process.stdout + process.stderr)
        return [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]

    def test_initialize_and_tool_roundtrip(self):
        replies = self.run_server([
            request("initialize", protocolVersion="2025-06-18", capabilities={},
                    clientInfo={"name": "test", "version": "1"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request("tools/list", 2),
            request("tools/call", "call", name="echo", arguments={"text": "影刀\n第二行"}),
        ])
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[0]["result"]["capabilities"], {"tools": {"listChanged": True}})
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "1.2.0")
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "echo")
        self.assertEqual(replies[2]["id"], "call")
        self.assertEqual(replies[2]["result"]["content"][0]["text"], "影刀\n第二行")
        self.assertEqual(replies[2]["result"]["structuredContent"]["name"], "echo")

    def test_versions_empty_lists_and_ping(self):
        versions = ["2025-03-26", "2024-11-05", "unsupported"]
        messages = [request("initialize", i, protocolVersion=v) for i, v in enumerate(versions)]
        messages += [request(name, 10 + i) for i, name in enumerate(
            ["resources/list", "resources/templates/list", "prompts/list", "ping"])]
        replies = self.run_server(messages)
        self.assertEqual([r["result"]["protocolVersion"] for r in replies[:3]],
                         ["2025-03-26", "2024-11-05", "2025-06-18"])
        self.assertEqual([r["result"] for r in replies[3:]],
                         [{"resources": []}, {"resourceTemplates": []}, {"prompts": []}, {}])

    def test_bad_messages_do_not_break_subsequent_requests(self):
        replies = self.run_server([
            b"{not-json}", b"\xff", [], {"jsonrpc": "1.0", "id": 9, "method": "ping"},
            request("unknown", 3), request("tools/call", 4, name="echo", arguments=[]),
            request("initialize", 5), request("ping", 6),
        ])
        self.assertEqual([r["error"]["code"] for r in replies[:-1]],
                         [-32700, -32700, -32600, -32600, -32601, -32602, -32602])
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 6, "result": {}})

    def test_notifications_have_no_response_and_tool_errors_are_results(self):
        replies = self.run_server([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}},
            request("tools/call", 8, name="fail", arguments={}), request("ping", 9),
        ])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["id"], 8)
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertNotIn("error", replies[0])
        self.assertEqual(replies[1]["id"], 9)

    def test_oversized_line_is_drained_and_server_recovers(self):
        replies = self.run_server([b"x" * (8 * 1024 * 1024 + 100), request("ping", 2)])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertEqual(replies[1]["result"], {})

    def test_import_does_not_start_or_require_a_bridge(self):
        process = subprocess.run(
            [sys.executable, "-c", "import stdio_server"], cwd=SERVER_DIR,
            input=b"invalid", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        self.assertEqual(process.stderr, b"")

    def test_catalog_notification_follows_result_and_is_sent_once(self):
        replies = self.run_server([
            request("initialize", protocolVersion="2025-06-18"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request("tools/call", 2, name="bridge_status", arguments={"reconnect": True}),
            request("ping", 3),
        ])
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[1]["id"], 2)
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.assertEqual(replies[3]["id"], 3)

    def test_import_and_offline_discovery_need_no_vendor_contract_files(self):
        launcher = r'''
from bridge import ShadowBotBridge
from stdio_server import main
def offline():
    raise OSError("NOT_FOR_PUBLIC_OUTPUT")
raise SystemExit(main(ShadowBotBridge(resolver=offline)))
'''
        wire = "\n".join(json.dumps(message) for message in [
            request("initialize", protocolVersion="2025-06-18"),
            request("tools/list", 2), request("tools/call", 3, name="bridge_status", arguments={}),
        ]) + "\n"
        process = subprocess.run([sys.executable, "-u", "-c", launcher], cwd=SERVER_DIR,
                                 input=wire.encode("utf-8"), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], ["bridge_status"])
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertNotIn(b"NOT_FOR_PUBLIC_OUTPUT", process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
