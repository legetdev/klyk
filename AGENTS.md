# Repository Instructions

Orientation for contributors (human or AI) working on the klyk codebase.

- **What klyk is, how to install and run it:** [`README.md`](./README.md).
- **How the internals are shaped and why** (AX bridge, OCR, pixel sampling, SkyLight input path, session model): [`ARCHITECTURE.md`](./ARCHITECTURE.md).
- **The source of truth for what each tool does** — its behavior contract, parameters, and the failure modes it guards against — is the tool's `description` field in `klyk/mcp_server.py`. When you add a tool or change its behavior, update that description; it is what the agent actually reads at runtime.
- **Security policy, trust model, and how to report a vulnerability:** [`SECURITY.md`](./SECURITY.md).
