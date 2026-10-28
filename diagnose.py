"""Report the active Studio connection using read-only MCP tools."""
import argparse
import json
from pathlib import Path

from bridge import ShadowBotBridge, payload


def checked(bridge, name, args):
    response = bridge.call_tool(name, args)
    data = payload(response)
    if response.get("isError") or data.get("success") is False:
        # Upstream failures can contain source or arguments. Do not print them.
        raise RuntimeError(f"{name} failed. Open the intended Studio project and AI panel, then reconnect.")
    return data


def diagnose():
    bridge = ShadowBotBridge()
    status = checked(bridge, "bridge_status", {})
    flows = checked(bridge, "app_list_flows", {})
    return {
        "connected": True,
        "endpoint": status["endpoint"],
        "app_id": status["app"]["app"]["app_id"],
        "flow_count": flows.get("total"),
        "implementation_version": status.get("implementation_version"),
        "implementation_sha256": status.get("implementation_sha256"),
        "code_write_backend": status.get("code_write_backend"),
        "exposed_tool_count": len(bridge.list_tools()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optionally save the local diagnostic report.")
    args = parser.parse_args()
    try:
        report = diagnose()
    except Exception as exc:
        report = {"connected": False, "error": "Unable to inspect Studio. Open the intended project and AI panel, then reconnect.",
                  "error_type": type(exc).__name__}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("connected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
