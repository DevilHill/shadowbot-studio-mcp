"""Bounded Streamable HTTP MCP client, restricted to literal loopback URLs."""
import http.client
import json
from urllib.parse import urlsplit

MAX_BYTES = 8 * 1024 * 1024


class TransportError(RuntimeError):
    pass


class McpHttpClient:
    def __init__(self, endpoint, headers=None, timeout=30):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
                or parsed.username or parsed.password or parsed.fragment):
            raise TransportError("Studio endpoint must be an http literal-loopback URL.")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.headers = dict(headers or {})
        forbidden = {"host", "content-length", "transfer-encoding", "connection"}
        if any(k.lower() in forbidden for k in self.headers):
            raise TransportError("Unsupported endpoint authentication header.")
        self.timeout = timeout
        self.session = None
        self.protocol = "2025-03-26"
        self.counter = 0

    def _post(self, message):
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise TransportError("Request exceeds 8 MiB.")
        headers = {**self.headers, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("POST", self.path, body=payload, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise TransportError("Studio rejected authentication. Reopen its AI panel and reconnect; credentials were not guessed.")
            if response.status == 404:
                raise TransportError("Studio endpoint/session expired. Reconnect before retrying; a write is never retried automatically.")
            if response.status not in (200, 202, 204):
                raise TransportError(f"Studio HTTP status {response.status}; no automatic retry.")
            session = response.getheader("Mcp-Session-Id")
            if session:
                if len(session) > 1024 or any(not 33 <= ord(c) <= 126 for c in session):
                    raise TransportError("Invalid MCP session header.")
                self.session = session
            if "id" not in message:
                return None
            content_type = response.getheader("Content-Type", "").lower()
            if "text/event-stream" in content_type:
                reply = self._read_sse(response, message["id"])
            else:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise TransportError("Studio response exceeds 8 MiB.")
                reply = json.loads(raw)
            if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                    or type(reply.get("id")) is not type(message["id"])
                    or reply.get("id") != message["id"]):
                raise TransportError("Studio returned an unmatched JSON-RPC response.")
            if "error" in reply:
                error = reply["error"]
                # The upstream error message is intentionally not logged: it may contain arguments.
                code = error.get("code") if isinstance(error, dict) else "unknown"
                raise TransportError(f"Studio JSON-RPC error {code}; inspect tool inputs or reconnect.")
            if "result" not in reply:
                raise TransportError("Studio returned no result.")
            return reply["result"]
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, TransportError):
                raise
            raise TransportError("Studio transport failed or timed out. Re-read the target before retrying any write.") from None
        finally:
            connection.close()

    @staticmethod
    def _read_sse(response, request_id):
        data, total = [], 0
        while True:
            line = response.readline(MAX_BYTES + 1)
            total += len(line)
            if total > MAX_BYTES:
                raise TransportError("Studio SSE response exceeds 8 MiB.")
            if not line:
                raise TransportError("Studio SSE stream ended before its response; no automatic retry.")
            text = line.decode("utf-8").rstrip("\r\n")
            if text.startswith("data:"):
                data.append(text[5:].removeprefix(" "))
            elif not text and data:
                item = json.loads("\n".join(data))
                data = []
                if (isinstance(item, dict) and type(item.get("id")) is type(request_id)
                        and item.get("id") == request_id and ("result" in item or "error" in item)):
                    return item

    def request(self, method, params=None):
        self.counter += 1
        return self._post({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}})

    def connect(self):
        result = self.request("initialize", {
            "protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "shadowbot-studio-mcp", "version": "1.2.0"}})
        if not isinstance(result, dict) or result.get("protocolVersion") not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            raise TransportError("Unsupported Studio MCP protocol version.")
        self.protocol = result["protocolVersion"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self):
        items, cursor, seen = [], None, set()
        for _ in range(20):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise TransportError("Studio returned an invalid tool inventory.")
            if any(not isinstance(t, dict) or not isinstance(t.get("name"), str) or not isinstance(t.get("inputSchema"), dict) for t in result["tools"]):
                raise TransportError("Studio returned an invalid tool definition.")
            items.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return items
            if not isinstance(cursor, str):
                raise TransportError("Studio returned an invalid pagination cursor.")
            if cursor in seen:
                break
            seen.add(cursor)
        raise TransportError("Studio tool pagination did not terminate.")

    def call(self, name, arguments):
        data = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(data, dict):
            raise TransportError("Studio returned an invalid tool result.")
        return data
