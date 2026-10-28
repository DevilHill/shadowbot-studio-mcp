"""Client-independent editing tools backed by the running ShadowBot Studio MCP.

Registered projects can use an explicit atomic file + clean-app reload backend.
"""
import copy
import datetime
import hashlib
import json
import math
import os
import pathlib
import threading
import uuid
from urllib.parse import urlsplit

from transport import McpHttpClient, TransportError, MAX_BYTES
from file_backend import FileSourceBackend, BackendError

ROOT = pathlib.Path(__file__).resolve().parent
BRIDGE_VERSION = "1.2.0"
IMPLEMENTATION_SHA256 = hashlib.sha256(b"".join(
    (ROOT / filename).read_bytes() for filename in ("bridge.py", "file_backend.py", "transport.py"))).hexdigest()
READ_TOOLS = {
    "app.get_info", "app.list_flows", "codeflow.read", "diagnostics.snapshot",
    "flow.blocks.list", "flow.list_parameters", "flow.list_variables",
    "selector.details", "selector.list",
    "wiki.list", "wiki.read", "wiki.search", "app.resource.list",
    "app.global_variables.list", "app.databook.read",
}
WRITE_TOOLS = {
    "app.create_flow", "app.copy_flow", "app.rename_flow", "app.delete_flow",
    "app.save", "app.update_info", "codeflow.write", "codeflow.edit",
    "flow.edit_blocks", "flow.fill_blocks", "flow.toggle_blocks", "flow.write_parameters",
    "app.global_variables.write", "app.resource.add", "app.resource.rename", "app.resource.remove",
}
CODE_WRITES = {"codeflow.write", "codeflow.edit"}


class BridgeError(RuntimeError):
    pass


def normalized_source(source):
    return source.replace("\r\n", "\n").replace("\r", "\n")


def source_hash(source):
    return hashlib.sha256(normalized_source(source).encode("utf-8")).hexdigest()


def result(data, error=False):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data, "isError": error}


def payload(response):
    if isinstance(response.get("structuredContent"), dict):
        return response["structuredContent"]
    for item in response.get("content", []):
        if item.get("type") == "text":
            try:
                data = json.loads(item["text"])
                if isinstance(data, dict):
                    return data
            except (ValueError, KeyError):
                continue
    raise BridgeError("Studio returned no structured tool result.")


def failed(response, data):
    return bool(response.get("isError") or data.get("success") is False or data.get("state") == "rejected")


class ShadowBotBridge:
    def __init__(self, resolver=None, client_factory=McpHttpClient, backup_dir=None, source_backend=None):
        self.resolver = resolver or self._discover
        self.client_factory = client_factory
        self.backup_dir = pathlib.Path(backup_dir or ROOT / ".state" / "backups")
        self.client = None
        self.endpoint = None
        self.available = set()
        self.schemas = {}
        self.descriptions = {}
        self._published_tools_digest = None
        self.lock = threading.RLock()
        self.source_backend = source_backend if source_backend is not None else FileSourceBackend()

    @staticmethod
    def _discover():
        explicit = os.environ.get("SHADOWBOT_MCP_URL")
        if explicit:
            return {"url": explicit, "headers": {}, "source": "SHADOWBOT_MCP_URL"}
        from discovery.discover_runtime import discover_studio_endpoint
        selected_pid = os.environ.get("SHADOWBOT_OPENCODE_PID")
        try:
            return discover_studio_endpoint(int(selected_pid) if selected_pid else None)
        except (RuntimeError, OSError, ValueError):
            raise BridgeError("Cannot discover one ShadowBot Studio instance. Open the project and its AI panel, then use bridge_status(reconnect=true). For multiple instances configure SHADOWBOT_OPENCODE_PID, or set an explicitly observed SHADOWBOT_MCP_URL.") from None

    def _connect(self):
        if self.client:
            return
        connection = self.resolver()
        client = self.client_factory(connection["url"], headers=connection.get("headers", {}))
        info = client.connect()
        tools = client.list_tools()
        schemas, descriptions = {}, {}
        if not isinstance(tools, list):
            raise BridgeError("Studio returned an invalid tool catalog.")
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise BridgeError("Studio returned an invalid tool catalog.")
            name, schema = tool["name"], tool.get("inputSchema")
            if (name in schemas or not isinstance(schema, dict) or schema.get("type") != "object"
                    or not isinstance(schema.get("properties", {}), dict)
                    or not isinstance(schema.get("required", []), list)
                    or any(not isinstance(field, str) for field in schema.get("required", []))):
                raise BridgeError("Studio returned an unsupported tool schema.")
            schemas[name] = copy.deepcopy(schema)
            description = tool.get("description")
            descriptions[name] = description if isinstance(description, str) else "Studio tool: " + name + "."
        if not {"app.get_info", "codeflow.read"}.issubset(schemas):
            raise BridgeError("The discovered server does not advertise the expected Studio editing tools.")
        self.available = set(schemas)
        self.schemas, self.descriptions = schemas, descriptions
        self.client = client
        parsed = urlsplit(connection["url"])
        self.endpoint = {"url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "source": connection.get("source", "explicit"),
                         "pid": connection.get("pid"), "server": info.get("serverInfo"),
                         "protocol": info.get("protocolVersion")}

    def _disconnect(self):
        self.client = None
        self.endpoint = None
        self.available = set()
        self.schemas, self.descriptions = {}, {}

    def _tool_definitions(self):
        # Only this bridge-owned status schema is available without a live catalog.
        definitions = [{"name": "bridge_status", "description": "Inspect the local ShadowBot connection and active app. Set reconnect=true after restarting ShadowBot. Does not edit or run workflows.",
                        "inputSchema": {"type": "object", "properties": {"reconnect": {"type": "boolean"}}, "additionalProperties": False},
                        "annotations": {"readOnlyHint": True, "openWorldHint": False}}]
        for name in sorted(READ_TOOLS | WRITE_TOOLS):
            if name not in self.schemas:
                continue
            schema = copy.deepcopy(self.schemas[name])
            description = self.descriptions[name]
            if name in WRITE_TOOLS:
                schema.setdefault("properties", {})["expected_app_id"] = {
                    "type": "string", "minLength": 1,
                    "description": "app_id from app_get_info; prevents targeting a different active application."}
                if "expected_app_id" not in schema.setdefault("required", []):
                    schema["required"].append("expected_app_id")
                description += " Bridge: provide expected_app_id from app_get_info. This writes the live Studio app."
            if name in CODE_WRITES:
                schema["properties"]["expected_source_sha256"] = {
                    "type": "string", "pattern": "^[a-f0-9]{64}$",
                    "description": "_bridge.source_sha256 from the latest codeflow_read; checked against the complete source before writing."}
                if "expected_source_sha256" not in schema["required"]:
                    schema["required"].append("expected_source_sha256")
                description += " Backs up source, parameters and VisualFlow caller bindings, rejects stale source, and reads back to verify. Registered projects use atomic source-file replacement and require a saved application; confirm the user's intent before saving pending edits. Other projects use the native API with caller-reset protection. Never blindly retry an unknown outcome or replace flows."
            if name == "codeflow.read":
                description += " Returns _bridge.app_id and a SHA-256 of the complete source (LF-normalized), even when the visible result is paginated."
            definitions.append({"name": name.replace(".", "_"), "description": description,
                                "inputSchema": schema, "annotations": {
                                    "readOnlyHint": name in READ_TOOLS,
                                    "destructiveHint": name in WRITE_TOOLS,
                                    "openWorldHint": False}})
        return definitions

    @staticmethod
    def _tools_digest(definitions):
        return hashlib.sha256(json.dumps(definitions, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()

    def list_tools(self):
        with self.lock:
            try:
                self._connect()
            except Exception:
                # Discovery failures must not prevent clients from loading bridge_status.
                self._disconnect()
            definitions = self._tool_definitions()
            self._published_tools_digest = self._tools_digest(definitions)
            return definitions

    def take_tool_list_changed(self):
        """Report observed catalog changes; no background discovery or tool calls."""
        with self.lock:
            current = self._tools_digest(self._tool_definitions())
            changed = self._published_tools_digest is not None and current != self._published_tools_digest
            if changed:
                self._published_tools_digest = current
            return changed

    def _call(self, name, args):
        if name not in self.available:
            raise BridgeError(f"This ShadowBot session does not expose {name}.")
        response = self.client.call(name, args)
        return response, payload(response)

    def _app(self):
        response, info = self._call("app.get_info", {})
        if failed(response, info) or not info.get("app", {}).get("app_id"):
            raise BridgeError("Open the intended application in ShadowBot Studio first.")
        return info

    def _check_app(self, expected):
        info = self._app()
        if info["app"]["app_id"] != expected:
            raise BridgeError("Active application changed. Read app_get_info and review the target before editing.")
        return info

    def _source(self, flow_id, for_write=False):
        chunks, offset, total_bytes = [], 1, 0
        for _ in range(100):
            response, data = self._call("codeflow.read", {"flow_id": flow_id, "offset": offset, "limit": 2000})
            if failed(response, data) or not isinstance(data.get("content"), str):
                raise BridgeError("Cannot obtain the complete source for a verified edit.")
            chunk = data["content"]
            total_bytes += len(chunk.encode("utf-8"))
            if total_bytes > MAX_BYTES:
                raise BridgeError("Complete source exceeds the 8 MiB verification limit.")
            chunks.append(chunk)
            next_offset = data.get("next_offset")
            if for_write and next_offset is not None:
                raise BridgeError("Verified live editing is limited to 2000 source lines because Studio does not provide an atomic multi-page snapshot. No write was attempted.")
            if next_offset is None:
                # Studio pages represent line ranges. Insert one separator only when a page omitted it.
                source = ""
                for item in chunks:
                    if source and not source.endswith(("\r", "\n")) and item:
                        source += "\n"
                    source += item
                return source
            if type(next_offset) is not int or next_offset <= offset:
                raise BridgeError("Studio returned invalid source pagination.")
            offset = next_offset
        raise BridgeError("Studio source pagination did not terminate.")

    def _backup(self, app_id, flow_id, source, parameters, callers):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"{stamp}-{uuid.uuid4().hex}.json"
        record = {"app_id": app_id, "flow_id": flow_id, "source": source,
                  "source_sha256": source_hash(source), "parameters": parameters,
                  "callers": callers, "created_utc": stamp,
                  "note": "Pre-write backup. Source is never restored automatically; caller bindings reset by Studio may be restored and verified."}
        with target.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        return str(target)

    def _parameters(self, flow_id):
        response, data = self._call("flow.list_parameters", {"flow_id": flow_id})
        items = data.get("items")
        if (failed(response, data) or data.get("flow_id") != flow_id or not isinstance(items, list)
                or type(data.get("total")) is not int or data["total"] != len(items)
                or any(not isinstance(p, dict) or not {"name", "direction", "type", "kind"}.issubset(p) for p in items)):
            raise BridgeError("Studio did not return a complete parameter snapshot for this flow_id. Use the exact ID from app_list_flows.")
        return data

    @staticmethod
    def _fillable_value(value):
        """Only replay semantic values whose meaning is documented by fill_blocks."""
        if isinstance(value, str):
            return {"text": ""} if value == "" else value
        if type(value) in (bool, int) or (type(value) is float and math.isfinite(value)):
            return value
        if isinstance(value, dict) and len(value) == 1:
            kind, content = next(iter(value.items()))
            if kind in {"text", "variable", "expression", "selector"} and isinstance(content, str):
                if content or kind == "text":
                    return copy.deepcopy(value)
        raise BridgeError("A caller argument cannot be replayed without changing its semantics. Caller protection currently supports scalar literals and text/variable/expression/selector values; array/object/json/concat/group values require manual review.")

    def _callers(self, target_id, parameters, replay=True):
        """Snapshot effective arguments and output captures, never UI default projections alone."""
        response, data = self._call("app.list_flows", {})
        flows = data.get("flows")
        if (failed(response, data) or not isinstance(flows, list)
                or type(data.get("total")) is not int or data["total"] != len(flows)):
            raise BridgeError("Cannot obtain a complete flow inventory for caller protection.")
        expected_arguments = {"arguments." + p["name"] for p in parameters["items"]
                              if p["direction"] in {"In", "InOut"}}
        target_names = {target_id}
        target_names.update(f["flow_name"] for f in flows if isinstance(f, dict)
                            and f.get("flow_id") == target_id and isinstance(f.get("flow_name"), str))
        callers, seen_flows = [], set()
        for flow in flows:
            caller_id = flow.get("flow_id") if isinstance(flow, dict) else None
            if (not isinstance(caller_id, str) or not caller_id or caller_id in seen_flows
                    or flow.get("flow_kind") not in {"code", "visual"}):
                raise BridgeError("Studio returned an ambiguous flow inventory; caller protection cannot proceed.")
            seen_flows.add(caller_id)
            if flow["flow_kind"] != "visual":
                continue
            offset, total, seen_blocks = 0, None, set()
            for _ in range(2000):
                response, page = self._call("flow.blocks.list", {
                    "flow_id": caller_id, "detail": "detail", "offset": offset, "limit": 50})
                blocks, page_total = page.get("blocks"), page.get("total")
                if (failed(response, page) or page.get("flow_id") != caller_id or not isinstance(blocks, list)
                        or type(page_total) is not int or page_total < 0
                        or (total is not None and total != page_total)):
                    raise BridgeError("Cannot obtain a complete caller block snapshot.")
                total = page_total
                for index, block in enumerate(blocks, offset):
                    block_id = block.get("block_id") if isinstance(block, dict) else None
                    if (not isinstance(block_id, str) or not block_id or block_id in seen_blocks
                            or block.get("block_index") != index):
                        raise BridgeError("Studio returned an inconsistent caller block snapshot.")
                    seen_blocks.add(block_id)
                    if block.get("prototype_name") != "process.run":
                        continue
                    target = block.get("target_flow")
                    fields = block.get("fields")
                    if not isinstance(target, dict) or not isinstance(target.get("flow_id"), str):
                        # An unrelated unfinished form must not block every code edit.
                        process = fields.get("process", {}).get("value") if isinstance(fields, dict) else None
                        if isinstance(process, dict) and set(process) == {"text"}:
                            process = process["text"]
                        if isinstance(process, str) and process and process not in target_names:
                            continue
                        raise BridgeError("A process.run target cannot be resolved; caller protection cannot proceed.")
                    if target["flow_id"] != target_id:
                        continue
                    # Studio omits outputs when the block captures no child result.
                    outputs = block.get("outputs", {})
                    if (block.get("form_state") != "ready" or not isinstance(fields, dict)
                            or not isinstance(outputs, dict)
                            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in outputs.items())):
                        raise BridgeError("A caller has an incomplete form or unsupported output capture; no code write was attempted.")
                    arguments = {k: v for k, v in fields.items() if k.startswith("arguments.")}
                    if set(arguments) != expected_arguments or any(not isinstance(v, dict) or "value" not in v for v in arguments.values()):
                        raise BridgeError("A caller argument form does not match the CodeFlow interface.")
                    values = {k: copy.deepcopy(v["value"]) for k, v in arguments.items()}
                    fills = {k: self._fillable_value(v) for k, v in values.items()} if replay else {}
                    callers.append({"flow_id": caller_id, "block_id": block_id,
                                    "target_flow_id": target_id, "arguments": values,
                                    "outputs": copy.deepcopy(outputs), "fill_inputs": fills,
                                    "form": copy.deepcopy(block)})
                next_offset = page.get("next_offset")
                if next_offset is None:
                    if offset + len(blocks) != total:
                        raise BridgeError("Studio omitted caller blocks from its snapshot.")
                    break
                if type(next_offset) is not int or next_offset != offset + len(blocks) or next_offset <= offset:
                    raise BridgeError("Studio returned invalid caller pagination.")
                offset = next_offset
            else:
                raise BridgeError("Caller block pagination did not terminate.")
        return callers

    @staticmethod
    def _caller_bindings(callers):
        return {(c["flow_id"], c["block_id"]): {
            "target_flow_id": c["target_flow_id"], "arguments": c["arguments"], "outputs": c["outputs"]}
                for c in callers}

    def _protect_callers(self, flow_id, parameters, before, expected_app, allow_restore):
        current = self._callers(flow_id, parameters)
        original_bindings, current_bindings = self._caller_bindings(before), self._caller_bindings(current)
        info = {"caller_count": len(before), "callers_preserved": original_bindings == current_bindings,
                "callers_restored": False, "caller_changes_detected": original_bindings != current_bindings}
        if info["callers_preserved"] or not allow_restore:
            return info
        if set(original_bindings) != set(current_bindings):
            info["caller_restore_error"] = "Caller targets or blocks changed; automatic restoration was not attempted."
            return info
        changed = [c for c in before if original_bindings[(c["flow_id"], c["block_id"])] !=
                   current_bindings[(c["flow_id"], c["block_id"])]]
        for caller in changed:
            observed = current_bindings[(caller["flow_id"], caller["block_id"])]
            if caller["outputs"] != observed["outputs"]:
                info["caller_restore_error"] = "Caller output captures changed; this is not the known argument-reset defect, so automatic restoration was not attempted."
                return info
            for key, original in caller["arguments"].items():
                if original != observed["arguments"].get(key):
                    field = caller["form"]["fields"][key]
                    if "default" not in field or field["default"] != observed["arguments"].get(key):
                        info["caller_restore_error"] = "A caller argument changed to an unexpected value, not its default; automatic restoration was not attempted."
                        return info
        # Do not overwrite another edit made after the first verification read.
        self._check_app(expected_app)
        if self._caller_bindings(self._callers(flow_id, parameters)) != current_bindings:
            info["caller_restore_error"] = "Caller bindings changed during verification; automatic restoration was not attempted."
            return info
        for caller in changed:
            self._check_app(expected_app)
            observed = current_bindings[(caller["flow_id"], caller["block_id"])]
            fill = {"block_id": caller["block_id"]}
            inputs = {k: v for k, v in caller["fill_inputs"].items()
                      if caller["arguments"][k] != observed["arguments"].get(k)}
            # Restore only changed fields, preserving inherited/default arguments as inherited.
            if inputs:
                fill["inputs"] = inputs
            response, restored = self._call("flow.fill_blocks", {
                "flow_id": caller["flow_id"], "fills": [fill]})
            if failed(response, restored) or restored.get("fill_errors"):
                info["caller_restore_error"] = "Studio rejected caller restoration; inspect the backup before further editing."
                return info
        self._check_app(expected_app)
        info["callers_preserved"] = self._caller_bindings(self._callers(flow_id, parameters)) == original_bindings
        info["callers_restored"] = info["callers_preserved"]
        if not info["callers_preserved"]:
            info["caller_restore_error"] = "Caller restoration did not survive readback."
        return info

    def _require_saved_app(self, expected_app):
        info = self._check_app(expected_app)
        if info["app"].get("has_unsaved_changes") is not False:
            raise BridgeError("The file_reload backend requires a saved application. Call app_save to preserve pending edits, then read the source again; no reload was attempted.")
        return info

    def _file_code_write(self, flow_id, before, desired, expected_app, params, callers):
        """Write only an allowlisted CodeFlow file, then reload a clean Studio app."""
        if not {"app.reload", "app.save"}.issubset(self.available):
            raise BridgeError("Studio must expose app.reload and app.save for verified file synchronization. Nothing was written.")
        self._require_saved_app(expected_app)
        metadata = {"backend": "file_reload", "bridge_version": BRIDGE_VERSION,
                    "app_id": expected_app, "verified": False,
                    "source_sha256_before": source_hash(before), "disk_committed": False,
                    "reload_attempted": False, "reload_succeeded": False,
                    "save_attempted": False, "saved": False,
                    "caller_count": len(callers), "callers_restored": False}
        stage = "prepare source file"
        prepared = None
        try:
            prepared = self.source_backend.prepare(expected_app, flow_id, before, desired)
            metadata["file_receipt"] = prepared.receipt
            metadata["backup_path"] = self._backup(expected_app, flow_id, before, params, callers)
            # Recheck memory immediately before the atomic file replacement.
            stage = "pre-commit application and source verification"
            self._require_saved_app(expected_app)
            if source_hash(self._source(flow_id, for_write=True)) != source_hash(before):
                raise BridgeError("Studio source changed during preparation; nothing was written.")
            if self._parameters(flow_id)["items"] != params["items"]:
                raise BridgeError("Flow parameters changed during preparation; nothing was written.")
            if self._caller_bindings(self._callers(flow_id, params, replay=False)) != self._caller_bindings(callers):
                raise BridgeError("Caller bindings changed during preparation; nothing was written.")
            self._require_saved_app(expected_app)
            stage = "atomic source file replacement"
            prepared.commit()
            metadata["disk_committed"] = True
            # Never save stale memory over the newly committed file. If the user
            # started editing, keep their in-memory work and report pending sync.
            stage = "pre-reload clean application verification"
            self._require_saved_app(expected_app)
            current = self._source(flow_id)
            if source_hash(current) not in {source_hash(before), source_hash(desired)}:
                raise BridgeError("Studio source changed after file commit; reload was not attempted.")
            if self._parameters(flow_id)["items"] != params["items"] or self._caller_bindings(
                    self._callers(flow_id, params, replay=False)) != self._caller_bindings(callers):
                raise BridgeError("Flow interface or callers changed after file commit; reload was not attempted.")
            self._require_saved_app(expected_app)
            stage = "reload saved application"
            metadata["reload_attempted"] = True
            response, data = self._call("app.reload", {})
            metadata["reload_succeeded"] = not failed(response, data) and data.get("success") is True
            if not metadata["reload_succeeded"]:
                raise BridgeError("Studio did not confirm reloading the committed file.")
            stage = "verify synchronized source, parameters and callers"
            self._check_app(expected_app)
            after = self._source(flow_id)
            metadata.update({"source_sha256": source_hash(after),
                             "matches_requested_source": normalized_source(after) == normalized_source(desired),
                             "observed_source_changed": source_hash(after) != source_hash(before),
                             "parameters_preserved": self._parameters(flow_id)["items"] == params["items"],
                             "callers_preserved": self._caller_bindings(self._callers(flow_id, params, replay=False)) == self._caller_bindings(callers),
                             "disk_source_matches": prepared.verify()})
            metadata["caller_changes_detected"] = not metadata["callers_preserved"]
            metadata["file_receipt"] = prepared.receipt
            if not all(metadata[k] for k in (
                    "matches_requested_source", "parameters_preserved", "callers_preserved", "disk_source_matches")):
                raise BridgeError("File/Studio readback or preserved interface checks did not match.")
            # Studio 6.3.34 marks even an unchanged reloaded app dirty. Save only
            # AFTER proving memory contains the new source and preserved bindings.
            # Saving before reload would overwrite the file with stale memory.
            stage = "save verified reloaded application"
            self._check_app(expected_app)
            metadata["save_attempted"] = True
            response, saved = self._call("app.save", {})
            metadata["saved"] = not failed(response, saved) and saved.get("success") is True
            if not metadata["saved"]:
                raise BridgeError("Studio did not confirm saving the synchronized application.")
            stage = "final persisted verification"
            self._require_saved_app(expected_app)
            after = self._source(flow_id)
            metadata.update({"source_sha256": source_hash(after),
                             "matches_requested_source": normalized_source(after) == normalized_source(desired),
                             "parameters_preserved": self._parameters(flow_id)["items"] == params["items"],
                             "callers_preserved": self._caller_bindings(self._callers(flow_id, params, replay=False)) == self._caller_bindings(callers),
                             "disk_source_matches": hashlib.sha256(prepared.source_path.read_bytes()).hexdigest() == prepared.desired_sha256})
            metadata["caller_changes_detected"] = not metadata["callers_preserved"]
            self._require_saved_app(expected_app)
            metadata["verified"] = all(metadata[k] for k in (
                "matches_requested_source", "parameters_preserved", "callers_preserved", "disk_source_matches"))
            if not metadata["verified"]:
                raise BridgeError("Saved file/Studio source or preserved interface checks did not match.")
            return result({"success": True, "message": "Source file updated and reloaded in Studio; source, parameters and callers verified.",
                           "_bridge": metadata})
        except (BackendError, BridgeError, TransportError, OSError) as exc:
            # A failed filesystem operation can have an unknown commit outcome;
            # use the receipt state rather than assuming a raised call did nothing.
            if prepared is not None:
                metadata["disk_committed"] = metadata["disk_committed"] or bool(getattr(prepared, "committed", False))
                metadata["file_receipt"] = prepared.receipt
                if metadata["file_receipt"].get("commit_attempted") and not metadata["disk_committed"]:
                    metadata["disk_committed"] = None
            metadata["verified"] = False
            summary = str(exc) if isinstance(exc, (BackendError, BridgeError)) else "Local file or Studio transport operation failed."
            metadata["failure_stage"] = stage
            metadata["next_step"] = ("Inspect the backup, disk file and live source before further actions. Do not save stale Studio memory or automatically retry/reload."
                                     if metadata["disk_committed"] is not False else "Resolve the reported precondition, read the source again, then request a new edit.")
            code = ("file_write_outcome_unknown" if metadata["disk_committed"] is None else
                    "file_sync_incomplete" if metadata["disk_committed"] else "file_write_precondition_failed")
            return result({"success": False,
                           "code": code,
                           "message": summary, "_bridge": metadata}, True)
        finally:
            if prepared is not None:
                prepared.close()

    def _code_write(self, name, args, expected_app, expected_hash):
        flow_id = args.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id:
            raise BridgeError("flow_id is required.")
        before = self._source(flow_id, for_write=True)
        if source_hash(before) != expected_hash:
            raise BridgeError("Source changed since codeflow_read. Read it again and merge your edit; nothing was written.")
        if not before:
            raise BridgeError("Empty source cannot be guarded by Studio's exact-match editing API. Add a main(args) stub in Studio before editing through the bridge.")
        if name == "codeflow.write":
            desired = args.get("content")
            if not isinstance(desired, str):
                raise BridgeError("content must be a string.")
        else:
            old, new = args.get("old_string"), args.get("new_string")
            if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
                raise BridgeError("Provide nonempty old_string and a different new_string.")
            count = before.count(old)
            if not count or (count > 1 and not args.get("replace_all", False)):
                raise BridgeError("Exact edit is absent or ambiguous. Read the source and include more context.")
            desired = before.replace(old, new) if args.get("replace_all", False) else before.replace(old, new, 1)
        if desired == before:
            return result({"success": True, "message": "Source already matches; no write was sent.",
                           "_bridge": {"app_id": expected_app, "verified": True, "unchanged": True,
                                       "source_sha256": source_hash(before)}})
        params = self._parameters(flow_id)
        file_backend = self.source_backend.project_for(expected_app) is not None
        callers = self._callers(flow_id, params, replay=not file_backend)
        if file_backend:
            return self._file_code_write(flow_id, before, desired, expected_app, params, callers)
        if callers and "flow.fill_blocks" not in self.available:
            raise BridgeError("Studio does not expose caller restoration; no code write was attempted.")
        backup = self._backup(expected_app, flow_id, before, params, callers)
        self._check_app(expected_app)
        if self._caller_bindings(self._callers(flow_id, params)) != self._caller_bindings(callers):
            raise BridgeError("Caller bindings changed before the code write. Read them again; no code write was attempted.")
        try:
            # Full-source matching also catches changes between the preflight and the actual call.
            # Studio does not expose an app-bound transaction; pre/post app checks remain necessary.
            response, data = self._call("codeflow.edit", {"flow_id": flow_id,
                "old_string": before, "new_string": desired, "replace_all": False})
        except (TransportError, BridgeError) as exc:
            self._disconnect()
            return result({"success": False, "code": "write_outcome_unknown", "message": str(exc),
                           "_bridge": {"backup_path": backup, "app_id": expected_app,
                                       "source_sha256_before": source_hash(before), "verified": False,
                                       "next_step": "Read the source before deciding whether to retry. The request may have reached Studio."}}, True)
        metadata = {"backup_path": backup, "app_id": expected_app,
                    "verified": False, "source_sha256_before": source_hash(before)}
        verification_stage = "active application"
        try:
            self._check_app(expected_app)
            verification_stage = "CodeFlow source"
            after = self._source(flow_id)
            metadata.update({"source_sha256": source_hash(after),
                             "observed_source_changed": source_hash(after) != source_hash(before),
                             "matches_requested_source": normalized_source(after) == normalized_source(desired)})
            verification_stage = "CodeFlow parameters"
            after_params = self._parameters(flow_id)
            metadata["parameters_preserved"] = after_params["items"] == params["items"]
            verification_stage = "caller verification/restoration"
            metadata.update(self._protect_callers(flow_id, params, callers, expected_app,
                allow_restore=metadata["parameters_preserved"] and (
                    metadata["matches_requested_source"] or normalized_source(after) == normalized_source(before))))
            if metadata["callers_restored"]:
                # A caller fill is another mutation; verify the CodeFlow still matches afterwards.
                verification_stage = "CodeFlow after caller restoration"
                after = self._source(flow_id)
                metadata["source_sha256"] = source_hash(after)
                metadata["matches_requested_source"] = normalized_source(after) == normalized_source(desired)
                metadata["parameters_preserved"] = self._parameters(flow_id)["items"] == params["items"]
            metadata["verified"] = (metadata["matches_requested_source"] and metadata["parameters_preserved"]
                                    and metadata["callers_preserved"] and not failed(response, data))
        except (BridgeError, TransportError) as exc:
            # BridgeError summaries contain no source/argument values. Do not echo a
            # transport exception because a malformed remote error code may contain data.
            summary = str(exc) if isinstance(exc, BridgeError) else "Studio request failed or returned an invalid transport response."
            metadata["verification_error"] = (
                "Could not verify " + verification_stage + " after the attempt: " + summary
                + " Read the active app/source before any retry.")
        is_error = failed(response, data) or not metadata["verified"]
        if is_error:
            metadata["next_step"] = "Inspect the result and backup. Do not automatically retry, reload, rename, delete, or bypass Studio by editing disk files."
        diagnostics = data.get("diagnostics")
        reported_failures = [data] + ([d for d in diagnostics if isinstance(d, dict)]
                                     if isinstance(diagnostics, list) else [])
        if any(d.get("code") == "update_failed" for d in reported_failures):
            # Keep the native response intact. This classification is based on its
            # reported failure, not a guess about a particular Studio implementation.
            metadata["verified"] = False
            is_error = True
            metadata["diagnostic"] = {
                "category": "studio_internal_codeflow_update_failed",
                "message": "Studio reported an internal CodeFlow update failure. The write is not verified as successful, even if some readback checks passed.",
                "cause_limit": "This tool error alone does not identify the failing internal method or establish that reconnecting or retrying will fix it.",
                "checks": {key: metadata.get(key) for key in (
                    "observed_source_changed", "matches_requested_source", "parameters_preserved",
                    "caller_changes_detected", "callers_preserved", "callers_restored")},
            }
            metadata["next_step"] = (
                "Inspect the current source, parameters, caller bindings and pre-write backup. "
                "Do not blindly retry the same write or replace the flow. "
                "No source-write retry, replacement-flow fallback or direct filesystem overwrite was attempted.")
        data = {**data, "_bridge": metadata}
        return result(data, is_error)

    def call_tool(self, name, arguments):
        with self.lock:
            try:
                if not isinstance(arguments, dict):
                    raise BridgeError("Tool arguments must be an object.")
                args = copy.deepcopy(arguments)
                if name == "bridge_status":
                    if args.get("reconnect"):
                        self._disconnect()
                    self._connect()
                    info = self._app()
                    project = self.source_backend.project_for(info["app"]["app_id"])
                    backend = "file_reload" if project is not None else "studio_native"
                    return result({"connected": True, "endpoint": self.endpoint, "app": info,
                                   "implementation_version": BRIDGE_VERSION,
                                   "implementation_sha256": IMPLEMENTATION_SHA256,
                                   "code_write_backend": backend,
                                   "registered_project": str(project) if project is not None else None,
                                   "available_editing_tools": sorted((READ_TOOLS | WRITE_TOOLS) & self.available),
                                   "limitations": ["Registered CodeFlows require a saved application before atomic source replacement and reload; confirm the user's intent before saving pending edits. Other projects use Studio's native code update API.",
                                                   "App guards are pre/post checks; keep the intended application active during an edit.",
                                                   "CodeFlow writes verify source, parameters and VisualFlow caller bindings. The file backend does not rebuild parameters or restore callers.",
                                                   "Studio exposes no atomic transaction with the local filesystem; keep the app stable during editing. Incomplete synchronization remains an error.",
                                                   "Other editing tools return Studio's result."]})
                mapping = {t.replace(".", "_"): t for t in READ_TOOLS | WRITE_TOOLS}
                native = mapping.get(name)
                if native is None:
                    raise BridgeError("Unknown or unsupported editing tool.")
                self._connect()
                if native not in self.available:
                    raise BridgeError("This Studio session does not advertise the requested editing tool.")
                expected_app = args.pop("expected_app_id", None)
                expected_hash = args.pop("expected_source_sha256", None)
                if native in WRITE_TOOLS:
                    if not isinstance(expected_app, str) or not expected_app:
                        raise BridgeError("expected_app_id from app_get_info is required for writes.")
                    self._check_app(expected_app)
                if native in CODE_WRITES:
                    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                        raise BridgeError("expected_source_sha256 from codeflow_read is required.")
                    return self._code_write(native, args, expected_app, expected_hash)
                if native == "codeflow.read":
                    info = self._app()
                    offset, limit = args.get("offset", 1), args.get("limit", 2000)
                    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 2000:
                        raise BridgeError("offset must be >=1 and limit must be 1..2000.")
                    complete = self._source(args["flow_id"])
                    self._check_app(info["app"]["app_id"])
                    lines = complete.splitlines(keepends=True)
                    end = offset - 1 + limit
                    data = {"success": True, "content": "".join(lines[offset - 1:end]),
                            "total_lines": len(lines), "next_offset": end + 1 if end < len(lines) else None,
                            "_bridge": {"app_id": info["app"]["app_id"], "source_sha256": source_hash(complete),
                                        "implementation_version": BRIDGE_VERSION,
                                        "hash_scope": "complete source, LF-normalized",
                                        "verified_write_supported": len(lines) <= 2000}}
                    return result(data)
                response, data = self._call(native, args)
                return result(data, failed(response, data))
            except TransportError as exc:
                self._disconnect()
                return result({"success": False, "message": str(exc), "code": "studio_transport_error",
                               "write_outcome": "If a write was attempted its outcome may be unknown. Read before retrying."}, True)
            except BridgeError as exc:
                return result({"success": False, "message": str(exc), "code": "bridge_precondition_failed"}, True)
            except BackendError as exc:
                return result({"success": False, "message": str(exc), "code": "file_backend_precondition_failed"}, True)
            except Exception:
                # Do not expose environment values, request data or credential-bearing exceptions.
                return result({"success": False, "code": "bridge_internal_error",
                               "message": "Local bridge failed. Run the diagnostic command; no automatic retry was performed."}, True)
