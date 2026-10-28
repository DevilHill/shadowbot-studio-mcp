# Working on shadowbot-studio-mcp

- This repository contains a local MCP bridge, not a business workflow project.
- Use 64-bit Python 3.11+; run `python -m unittest discover -s tests -v` from the root.
- Keep all tests offline with synthetic fixtures. Do not run real workflows or mutate Studio projects as part of routine checks.
- Keep stdout exclusively for JSON-RPC messages.
- Preserve the explicit tool allowlist, application guards, source hashes, backups and verification. Unknown write outcomes must not trigger automatic retries or rollback.
- Do not overwrite or silently save a user's unsaved Studio edits. Generated VisualFlow Python is not an editing interface.
- Never commit local `.state/`, credentials, chat exports, business source, vendor binaries, decompiled code or extracted tool inventories.
- Discover tool schemas from the active Studio endpoint at runtime. Do not introduce a bundled vendor schema dump.
- Update README.md and README.zh-CN.md together when setup or behavior changes.
