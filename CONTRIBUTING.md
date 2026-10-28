# Contributing

Use Windows x64 and 64-bit Python 3.11 or later. The server has no third-party Python dependencies. Read [README.md](README.md) for local client setup.

## Development

1. Fork and clone the repository, then create a branch for your change.
2. Keep changes focused and preserve the existing stdio interface.
3. Run `py -3 -m unittest discover -s tests -v` from the repository root.
4. Describe the changed behavior and validation in your pull request.

Tests must use synthetic fixtures and temporary directories. Add regression tests for behavior changes, especially writes, transport handling and discovery. Never run real business workflows to validate this bridge. Live editing checks require a disposable project and explicit authorization from its owner.

Preserve application identity checks, source hashes, backups and uncertain-outcome handling. Do not add automatic retries to writes or automatic saves of unrelated pending changes. Never treat VisualFlow's generated Python as its normal editing interface.

Do not commit `.state/`, real project source or metadata, process dumps, chat exports, user configuration, credentials, vendor binaries, decompiled vendor code or extracted Studio tool catalogs. Tool schemas are obtained at runtime. Keep logs out of stdout; stdout is reserved for MCP messages.

## Bug reports

Use [Issues](https://github.com/DevilHill/shadowbot-studio-mcp/issues) for ordinary bugs. Provide the bridge version, Python version, ShadowBot version and a minimal synthetic reproduction. Describe whether the failure occurred before or after a write. Redact application identifiers and local paths where they are not needed.

For a security issue, avoid posting credentials or a working exploit against a real project. Open a minimal issue requesting a private contact route without sensitive details.

Contributions are provided under the repository's [Apache-2.0 license](LICENSE). Include source and license information for any third-party code you propose adding.
