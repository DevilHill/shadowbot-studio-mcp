# shadowbot-studio-mcp

[简体中文](README.zh-CN.md) · [Apache-2.0](LICENSE)

A local MCP server that lets AI agents inspect and edit ShadowBot (影刀) Studio projects on Windows. It connects a standard **stdio MCP client** to Studio's existing local HTTP MCP endpoint, with application checks, source backups, stale-write detection and readback verification.

Version **1.2.0**. This is an independent community integration. You must install and sign in to ShadowBot separately. The repository contains our bridge and synthetic tests; Studio tool definitions are discovered at runtime and are not distributed here.

## Getting the code

```powershell
git clone https://github.com/DevilHill/shadowbot-studio-mcp.git
cd shadowbot-studio-mcp
```

No submodules, package installation or build step is needed. The server uses only the Python standard library.

## Prerequisites

| Component | Requirement |
| --- | --- |
| OS | Windows x64; run the bridge as the same Windows user as Studio |
| [Python](https://www.python.org/downloads/windows/) | 64-bit Python 3.11 or later; CI covers 3.11–3.13 |
| [ShadowBot](https://www.yingdao.com/) | Installed separately; target project and its AI panel open |
| MCP client | A local client able to start a stdio server, such as Codex; it needs access to local processes, loopback networking and the project files |
| [Git](https://git-scm.com/downloads) | Needed for the clone command; not needed to run an existing checkout |

Check the Python selected by the Windows launcher:

```powershell
py -3 -c "import sys, struct; print(sys.executable, sys.version); assert sys.version_info >= (3, 11) and struct.calcsize('P') == 8"
```

The integration was developed against **ShadowBot 6.3.34**. Dynamic schemas avoid a bundled version-specific catalog, but do not guarantee compatibility with other Studio versions. Updates can change response formats or process discovery.

## Getting started

### Connect an MCP client

1. Open the intended ShadowBot project and its AI panel. This starts the bundled OpenCode process used to discover Studio's MCP endpoint; you do not need to send a model request.
2. Configure your client with the **absolute** paths to Python and `stdio_server.py`. [examples/mcp.example.json](examples/mcp.example.json) uses the common `mcpServers` shape. Adapt its wrapper to your client's settings.
3. Start or refresh the MCP connection. Ask the agent to call `bridge_status`, then `app_get_info` and `app_list_flows` to confirm the active project.

For **Codex**, run these commands from the repository directory after installing its CLI. First inspect existing entries and choose an unused server name:

```powershell
codex mcp list
$bridgePython = (py -3 -c "import sys; print(sys.executable)").Trim()
$bridgeScript = (Resolve-Path .\stdio_server.py).Path
codex mcp add shadowbot_studio --env PYTHONUTF8=1 -- $bridgePython $bridgeScript
codex mcp get shadowbot_studio
```

Refresh the desktop app's MCP connection or restart it to load the entry. See [OpenAI's MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Existing users of an older local bridge should use a separate server name when evaluating this checkout.

The same Python command works with other local stdio MCP clients. This checkout does not provide a remotely hosted MCP endpoint; the process must run on the Windows machine containing Studio.

### Run and inspect locally

The server entry point is:

```powershell
py -3 .\stdio_server.py
```

It waits for newline-delimited JSON-RPC on stdin. It has no browser page or startup banner. Normally your MCP client launches it for you. For a human-readable, **read-only** connection check:

```powershell
py -3 .\diagnose.py
```

Success returns JSON containing `connected: true`, the active application ID and version. No workflows are copied, saved or run by this diagnostic command.

When Studio is unavailable, the server still starts and lists `bridge_status`. After opening Studio, call `bridge_status` with `{"reconnect": true}`. The server sends `notifications/tools/list_changed` after its discovered inventory changes. If a client does not refresh on that notification, restart its MCP connection. There is no background polling.

### Editing tools and safeguards

Tools are an explicit allowlist intersected with the active Studio tool inventory. Names such as `codeflow.read` become `codeflow_read`. Available categories include:

- Application information, flows, diagnostics, elements, variables, resources and wiki reads.
- Flow creation, copying, renaming, deletion and saving.
- CodeFlow source edits, VisualFlow block edits, parameters, globals and resources.

Workflow execution tools are not exposed. Editing a workflow can still change what it does when someone later runs it.

Every write requires `expected_app_id` from the current application. For code edits, pass the recent `codeflow_read` result's `_bridge.source_sha256` as the write argument `expected_source_sha256`. The bridge backs up source, parameters and VisualFlow caller bindings, checks for stale content, and verifies the result. Other edits do not have the same source backup guarantees. On an uncertain write result, inspect Studio and the backup before taking another action; writes are not retried automatically.

Both code-write backends require existing, non-empty source of at most 2,000 lines because Studio does not provide an atomic snapshot across source pages. For an empty flow, add an initial `main(args)` function in Studio first. Larger source can be read in pages, but writes are refused. Requests and responses are bounded to 8 MiB.

Start with a disposable project. Confirm unsaved changes before editing. The native API may leave changes pending in Studio; `app_save` is a separate write operation.

### Optional file backend for CodeFlow edits

Some Studio builds reject native code updates with `update_failed` / `flow_definition`. An **opt-in** backend can atomically replace an existing CodeFlow source file, reload a saved application, verify its source and interfaces, then save it. Projects not registered here use the native API.

To opt in, create `.state/code_backends.json` beside `bridge.py` using [the example](examples/code_backends.example.json). Replace both placeholders with your own exact application ID and absolute `xbot_robot` directory. Do not use the directory containing business inputs or outputs.

The backend requires a matching `package.json` entry and `.dev/<flow_id>.flow.json` identifying an existing CodeFlow. It rejects generated VisualFlow Python, missing metadata, mismatched disk/editor content, symlinks, junctions and hard-linked files. It does not create or rewrite flow metadata.

Studio must report `has_unsaved_changes: false` before replacement and reload. Decide whether to save existing edits, save them explicitly if appropriate, then read the source again. No automatic rollback is attempted after an uncertain result. Inspect disk and editor state before saving or reloading again.

Keep **one writer per application**, including manual Studio edits. Multiple agents may use the same checkout, but its file-backend lock only coordinates file writes sharing the same `.state` directory. It is not a general Studio transaction lock.

### Connection troubleshooting

| Symptom | Action |
| --- | --- |
| Only `bridge_status` appears | Open the project and AI panel, reconnect, then refresh the tool list |
| Multiple Studio instances | Set `SHADOWBOT_OPENCODE_PID` in the MCP client's server environment to the intended bundled `opencode.exe` PID |
| Auto-discovery is unavailable | Set `SHADOWBOT_MCP_URL` to an **observed** literal-loopback HTTP endpoint; do not guess a port |
| Studio restarted / session expired | Call `bridge_status` with `reconnect: true`; re-read the target before a new edit |
| Native code update fails | Check the returned diagnostic and consider the optional file backend on a disposable project |
| Unsaved application rejected | Resolve pending edits in Studio and read the source again |

Discovery performs bounded, read-only process-parameter access to a verified ShadowBot child process, extracting only `OPENCODE_STUDIO_MCP_URL`. It does not read OpenCode configuration files, inject code or forward OpenCode credentials. Explicit URLs also remain restricted to HTTP on `127.0.0.1` or `::1`; proxies and redirects are not used.

Backups and the project registry live under the ignored `.state/` directory. Backups may contain complete source code and secrets from your own project. Keep that directory private and review diagnostic output before posting it. The agent client receives requested tool results, so choose a client appropriate for your project data.

## Build, test and development

Run the offline suite from the repository root:

```powershell
py -3 -m unittest discover -s tests -v
```

Tests use temporary files, a fake Studio and local HTTP fixtures. They do not require ShadowBot or operate a real workflow. GitHub Actions runs the same suite on Windows with Python 3.11, 3.12 and 3.13. CI passing does not establish live compatibility with a new Studio release.

| File | Purpose |
| --- | --- |
| `stdio_server.py` | JSON-RPC/MCP entry point and tool-list notifications |
| `bridge.py` | Tool allowlist, dynamic schemas and guarded editing |
| `transport.py` | Bounded loopback HTTP/SSE transport |
| `file_backend.py` | Explicit project registration and atomic source replacement |
| `discovery/discover_runtime.py` | Windows process endpoint discovery |
| `diagnose.py` | Read-only connection report |

There is no separate build or hosted deployment. Keep the checkout in a stable, writable directory and register its entry point in your client.

## Contributing and license

Report reproducible bugs in [GitHub Issues](https://github.com/DevilHill/shadowbot-studio-mcp/issues). Include versions, the tool name and a synthetic example; omit real workflow source, credentials, `.state/`, chat databases and full diagnostic dumps. See [CONTRIBUTING.md](CONTRIBUTING.md) for development expectations.

This bridge is licensed under [Apache License 2.0](LICENSE); see [NOTICE](NOTICE). That license covers this repository's code, not the separately installed ShadowBot software or user workflows. This project is not affiliated with or endorsed by ShadowBot.
