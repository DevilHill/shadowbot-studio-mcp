"""Local HTTP replay tests for the real MCP transport; never contact ShadowBot."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transport import McpHttpClient, TransportError


def rpc(message, result):
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


@contextmanager
def replay(responder):
    """responder returns (status, headers, bytes-or-JSON-body) for each request."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"message": message, "headers": dict(self.headers), "path": self.path})
            status, headers, body = responder(message, self.headers)
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp?project=local", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


JSON_HEADERS = {"Content-Type": "application/json"}


class TransportTests(unittest.TestCase):
    def test_initialize_session_notification_and_json_tool_result(self):
        result = {"content": [{"type": "text", "text": "影刀：只读回放"}],
                  "structuredContent": {"ok": True}, "isError": False}

        def responder(message, _headers):
            if message["method"] == "initialize":
                return 200, {**JSON_HEADERS, "Mcp-Session-Id": "session-local-123"}, rpc(message, {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "replay", "version": "1"}})
            if message["method"] == "notifications/initialized":
                return 202, {}, b""
            return 200, JSON_HEADERS, rpc(message, result)

        with replay(responder) as (url, requests):
            client = McpHttpClient(url, headers={"Authorization": "TEST_ONLY"})
            self.assertEqual(client.connect()["protocolVersion"], "2025-06-18")
            self.assertEqual(client.call("echo", {"value": "中文"}), result)
            self.assertEqual(len(requests), 3)
            self.assertNotIn("id", requests[1]["message"])
            self.assertEqual(requests[2]["message"]["params"],
                             {"name": "echo", "arguments": {"value": "中文"}})
            for entry in requests[1:]:
                headers = {key.lower(): value for key, value in entry["headers"].items()}
                self.assertEqual(headers["mcp-session-id"], "session-local-123")
                self.assertEqual(headers["mcp-protocol-version"], "2025-06-18")
                self.assertEqual(headers["authorization"], "TEST_ONLY")
            self.assertEqual(requests[0]["path"], "/mcp?project=local")

    def test_tool_list_follows_pagination(self):
        first = {"name": "first", "inputSchema": {"type": "object"}}
        second = {"name": "second", "inputSchema": {"type": "object"}}

        def responder(message, _headers):
            cursor = message["params"].get("cursor")
            result = ({"tools": [first], "nextCursor": "page-2"}
                      if cursor is None else {"tools": [second]})
            return 200, JSON_HEADERS, rpc(message, result)

        with replay(responder) as (url, requests):
            self.assertEqual(McpHttpClient(url).list_tools(), [first, second])
            self.assertEqual([r["message"]["params"] for r in requests], [{}, {"cursor": "page-2"}])

    def test_repeated_pagination_cursor_is_rejected(self):
        with replay(lambda message, _: (200, JSON_HEADERS, rpc(message, {
                "tools": [], "nextCursor": "same"}))) as (url, requests):
            with self.assertRaises(TransportError):
                McpHttpClient(url).list_tools()
            self.assertEqual(len(requests), 2)

    def test_sse_skips_notification_and_unrelated_response(self):
        expected = {"content": [{"type": "text", "text": "SSE 中文"}], "isError": False}

        def responder(message, _headers):
            events = [
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
                {"jsonrpc": "2.0", "id": 99999, "result": {"wrong": True}},
                rpc(message, expected),
            ]
            body = ": heartbeat\r\n\r\n" + "".join(
                "event: message\r\ndata: " + json.dumps(item, ensure_ascii=False) + "\r\n\r\n"
                for item in events)
            return 200, {"Content-Type": "text/event-stream; charset=utf-8"}, body.encode("utf-8")

        with replay(responder) as (url, requests):
            self.assertEqual(McpHttpClient(url).call("read", {}), expected)
            self.assertEqual(len(requests), 1)

    def test_write_timeout_is_not_retried(self):
        def responder(message, _headers):
            time.sleep(0.2)
            return 200, JSON_HEADERS, rpc(message, {"content": []})

        with replay(responder) as (url, requests):
            with self.assertRaises(TransportError):
                McpHttpClient(url, timeout=0.04).call("write", {"change": "once"})
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"]["method"], "tools/call")

    def test_http_errors_are_not_retried_or_disclosed(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                with replay(lambda *_: (status, JSON_HEADERS, {"error": "SECRET_BODY"})) as (url, requests):
                    with self.assertRaises(TransportError) as raised:
                        McpHttpClient(url).call("write", {})
                    self.assertEqual(len(requests), 1)
                    self.assertNotIn("SECRET_BODY", str(raised.exception))

    def test_redirect_is_refused_without_contacting_destination(self):
        with replay(lambda message, _: (200, JSON_HEADERS, rpc(message, {}))) as (destination, received):
            with replay(lambda *_: (307, {"Location": destination}, b"")) as (url, requests):
                with self.assertRaises(TransportError):
                    McpHttpClient(url).call("write", {})
                self.assertEqual(len(requests), 1)
                self.assertEqual(received, [])

    def test_non_loopback_and_ambiguous_urls_are_rejected(self):
        for endpoint in ("http://localhost/mcp", "http://example.com/mcp", "http://192.168.1.1/mcp",
                         "https://127.0.0.1/mcp", "http://127.0.0.1@evil.test/mcp",
                         "http://user:pass@127.0.0.1/mcp", "http://127.0.0.1/mcp#fragment"):
            with self.subTest(endpoint=endpoint), self.assertRaises(TransportError):
                McpHttpClient(endpoint)

    def test_wrong_response_ids_fail_without_retry(self):
        for response_id in (999, "1", None):
            with self.subTest(response_id=response_id):
                with replay(lambda *_: (200, JSON_HEADERS, {
                        "jsonrpc": "2.0", "id": response_id, "result": {}})) as (url, requests):
                    with self.assertRaises(TransportError):
                        McpHttpClient(url).call("write", {})
                    self.assertEqual(len(requests), 1)

    def test_sse_ending_with_only_wrong_id_is_rejected(self):
        body = b'data: {"jsonrpc":"2.0","id":987,"result":{}}\n\n'
        with replay(lambda *_: (200, {"Content-Type": "text/event-stream"}, body)) as (url, requests):
            with self.assertRaises(TransportError):
                McpHttpClient(url).call("write", {})
            self.assertEqual(len(requests), 1)

    def test_boolean_response_id_is_not_integer_request_id(self):
        with replay(lambda *_: (200, JSON_HEADERS, {
                "jsonrpc": "2.0", "id": True, "result": {}})) as (url, _):
            with self.assertRaises(TransportError):
                McpHttpClient(url).call("read", {})

    def test_response_must_declare_jsonrpc_2(self):
        with replay(lambda message, _: (200, JSON_HEADERS, {
                "jsonrpc": "1.0", "id": message["id"], "result": {}})) as (url, _):
            with self.assertRaises(TransportError):
                McpHttpClient(url).call("read", {})

    def test_invalid_result_shapes_report_transport_error(self):
        for operation in ("connect", "list_tools"):
            with self.subTest(operation=operation):
                with replay(lambda message, _: (200, JSON_HEADERS, rpc(message, []))) as (url, _):
                    with self.assertRaises(TransportError):
                        getattr(McpHttpClient(url), operation)()


if __name__ == "__main__":
    unittest.main()
