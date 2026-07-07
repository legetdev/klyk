"""Klyk — OS-level macOS app testing via MCP.

Portfolio project. Showcases product thinking and shipped tooling.
Bug reports won't be actively triaged. Well-scoped PRs are welcome —
but expect a slow review cadence.

Primary interface: the MCP server (`python -m klyk.mcp_server`).

Module-level access for Python library users:
    from klyk import computer, capture, matcher, ocr, session, launcher

A higher-level Python library API may follow if there's demand.
"""

__version__ = "0.3.0"
