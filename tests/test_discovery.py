"""Endpoint selection and redaction with mock process records; never read real memory."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery import discover_runtime as discovery


def private_record(pid=101, url="http://127.0.0.1:1234/api/v1/mcp"):
    return {
        "pid": pid,
        "ppid": 100,
        "image": "PRIVATE_INSTALL_PATH",
        "command_line": "PRIVATE_COMMAND_LINE",
        "environment": {
            "OPENCODE_STUDIO_MCP_URL": url,
            "OPENCODE_CONFIG_CONTENT": "PRIVATE_CONFIG_CONTENT",
            "OPENCODE_CONFIG": "PRIVATE_CONFIG_PATH",
            "OPENCODE_SERVER_PASSWORD": "PRIVATE_PASSWORD",
        },
    }


class DiscoveryTests(unittest.TestCase):
    def test_summary_contains_only_identifiers_and_sanitized_endpoint(self):
        record = private_record(url="http://user:password@127.0.0.1:1234/api/v1/mcp?token=private#private")
        summary = discovery.safe_summary(record)
        self.assertEqual(summary, {"pid": 101, "ppid": 100,
                                   "studio_url": "http://127.0.0.1:1234/api/v1/mcp"})
        self.assertNotIn("PRIVATE_", json.dumps(summary))

    def test_summary_omits_external_and_malformed_urls(self):
        for url in ("https://example.invalid/PRIVATE_PATH", "http://127.0.0.1:PRIVATE_PORT/api/v1/mcp"):
            with self.subTest(url=url):
                summary = discovery.safe_summary(private_record(url=url))
                self.assertNotIn("PRIVATE_", json.dumps(summary))
                self.assertIn("omitted", summary["studio_url"])

    def test_multiple_instances_require_explicit_pid(self):
        inventory = [{"pid": 101, "name": "opencode.exe"}, {"pid": 102, "name": "opencode.exe"}]
        with patch.object(discovery, "processes", return_value=inventory), \
                patch.object(discovery, "read_shadowbot_opencode", side_effect=lambda pid: private_record(pid)):
            with self.assertRaisesRegex(RuntimeError, "Multiple ShadowBot"):
                discovery.discover_studio_endpoint()

    def test_explicit_pid_does_not_enumerate_other_candidates(self):
        with patch.object(discovery, "processes") as inventory, \
                patch.object(discovery, "read_shadowbot_opencode", return_value=private_record(102)) as reader:
            endpoint = discovery.discover_studio_endpoint(102)
        inventory.assert_not_called()
        reader.assert_called_once_with(102)
        self.assertEqual(endpoint["pid"], 102)
        self.assertEqual(endpoint["headers"], {})

    def test_discovery_accepts_literal_ipv6_loopback(self):
        with patch.object(discovery, "read_shadowbot_opencode", return_value=private_record(
                url="http://[::1]:1234/api/v1/mcp")):
            endpoint = discovery.discover_studio_endpoint(101)
        self.assertEqual(endpoint["url"], "http://[::1]:1234/api/v1/mcp")

    def test_discovery_rejects_endpoint_variants_without_echoing_private_parts(self):
        urls = (
            "http://example.invalid:1234/api/v1/mcp",
            "http://localhost:1234/api/v1/mcp",
            "https://127.0.0.1:1234/api/v1/mcp",
            "http://127.0.0.1/api/v1/mcp",
            "http://127.0.0.1:1234/PRIVATE_PATH",
            "http://PRIVATE_USER:PRIVATE_PASSWORD@127.0.0.1:1234/api/v1/mcp",
            "http://127.0.0.1:1234/api/v1/mcp?token=PRIVATE_TOKEN",
            "http://127.0.0.1:1234/api/v1/mcp#PRIVATE_FRAGMENT",
            "http://127.0.0.1:PRIVATE_PORT/api/v1/mcp",
            "http://[PRIVATE_INVALID_IPV6]:1234/api/v1/mcp",
        )
        for url in urls:
            with self.subTest(url=url), \
                    patch.object(discovery, "read_shadowbot_opencode", return_value=private_record(url=url)), \
                    self.assertRaises(RuntimeError) as raised:
                discovery.discover_studio_endpoint(101)
            self.assertNotIn("PRIVATE_", str(raised.exception))

    def test_unrelated_processes_are_not_read(self):
        inventory = [{"pid": 100, "name": "ShadowBot.Shell.exe"},
                     {"pid": 200, "name": "unrelated.exe"}]
        with patch.object(discovery, "processes", return_value=inventory), \
                patch.object(discovery, "read_shadowbot_opencode") as reader, \
                self.assertRaisesRegex(RuntimeError, "No verified"):
            discovery.discover_studio_endpoint()
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
