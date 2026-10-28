"""Line-delimited UTF-8 MCP transport for the local ShadowBot bridge."""

import json
import math
import sys

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LINE_BYTES = 8 * 1024 * 1024
SERVER_INFO = {"name": "shadowbot-studio-mcp", "version": "1.2.0"}


class RpcError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _tool_error():
    return {"content": [{"type": "text", "text": "Tool execution failed."}],
            "isError": True}


def _dispatch(method, params, bridge):
    if not isinstance(params, dict):
        raise RpcError(-32602, "Parameters must be an object.")
    if method == "initialize":
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise RpcError(-32602, "protocolVersion must be a string.")
        return {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS
            else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        try:
            tool_list = bridge.list_tools()
            if not isinstance(tool_list, list):
                raise TypeError()
            return {"tools": tool_list}
        except Exception:
            raise RpcError(-32603, "Tool listing failed.") from None
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise RpcError(-32602, "Expected a tool name and an arguments object.")
        try:
            result = bridge.call_tool(name, arguments)
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                return _tool_error()
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return result
        except Exception:
            return _tool_error()
    empty_lists = {"resources/list": "resources",
                   "resources/templates/list": "resourceTemplates",
                   "prompts/list": "prompts"}
    if method in empty_lists:
        return {empty_lists[method]: []}
    raise RpcError(-32601, "Method not found.")


def _handle(request, bridge):
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid request.")
    request_id = request.get("id")
    valid_id = request_id is None or (
        isinstance(request_id, (str, int, float)) and not isinstance(request_id, bool))
    if isinstance(request_id, float) and not math.isfinite(request_id):
        valid_id = False
    if (request.get("jsonrpc") != "2.0" or not valid_id
            or not isinstance(request.get("method"), str)):
        return _error(None, -32600, "Invalid request.")
    if "id" not in request:
        return None
    try:
        result = _dispatch(request["method"], request.get("params", {}), bridge)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as error:
        return _error(request_id, error.code, error.message)
    except Exception:
        return _error(request_id, -32603, "Internal error.")


def _reject_constant(_value):
    raise ValueError("Invalid JSON constant")


def _write(response, output):
    try:
        encoded = json.dumps(response, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        encoded = json.dumps(_error(response.get("id"), -32603, "Internal error."))
    output.write(encoded.encode("utf-8") + b"\n")
    output.flush()


def main(bridge=None):
    """Run until stdin closes; a bridge instance can be supplied by a test launcher."""
    if bridge is None:
        try:
            from bridge import ShadowBotBridge

            bridge = ShadowBotBridge()
        except Exception:
            print("Unable to initialize the ShadowBot bridge.", file=sys.stderr)
            return 1
    source, output = sys.stdin.buffer, sys.stdout.buffer
    initialized = False
    while True:
        line = source.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_LINE_BYTES:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_LINE_BYTES + 1)
            response = _error(None, -32600, "Request exceeds the 8 MiB line limit.")
        else:
            try:
                request = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                response = _error(None, -32700, "Parse error.")
            else:
                response = _handle(request, bridge)
                if isinstance(request, dict) and request.get("jsonrpc") == "2.0":
                    if request.get("method") == "initialize":
                        initialized = False
                    elif request.get("method") == "notifications/initialized" and "id" not in request:
                        initialized = True
        if response is not None:
            try:
                _write(response, output)
            except (BrokenPipeError, OSError):
                return 0
        if initialized and hasattr(bridge, "take_tool_list_changed"):
            try:
                if bridge.take_tool_list_changed():
                    _write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, output)
            except (BrokenPipeError, OSError):
                return 0
            except Exception:
                print("Unable to report a tool catalog change.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
