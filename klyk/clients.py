"""Registry of MCP clients that `klyk install <client>` can auto-configure.

Design goal: onboarding to any client should be as easy as adding an MCP to
Claude. Supporting a new agent CLI = ONE entry in CLIENTS — the same stdio
launch entry is reused everywhere; only the config file location and on-disk
shape differ. This keeps the surface uniform and trivially extensible as new
clients appear, without touching the installer logic.

Two on-disk shapes are handled:
  - "json"  : a JSON file with a top-level `mcpServers` map (Claude, Cursor,
              Windsurf, Continue, Cline, Gemini/Antigravity). Merged in place so
              the client's other settings are preserved.
  - "toml"  : a TOML file with `[mcp_servers.<name>]` tables (OpenAI Codex CLI).
              stdlib can read TOML but not write it, so we append the table when
              it's absent and fall back to a printed snippet when a differing
              entry already exists (never clobber hand-edited TOML).
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Key under which klyk registers in every client's config.
SERVER_KEY = "klyk"

# The canonical stdio launch entry — identical for every MCP client.
# command is THIS interpreter (sys.executable), not a bare "python3", so the
# config points at the exact Python klyk is installed in — works whether klyk
# was installed via pip (global), pipx, uvx, or a dedicated venv. A bare
# "python3" would break for any isolated install (the client would launch the
# wrong interpreter, which can't import klyk).
LAUNCH_ENTRY = {
    "command": sys.executable or "python3",
    "args": ["-m", "klyk.mcp_server"],
    "env": {},
}
# Claude Code's config additionally tags the transport type; kept for parity
# with existing installs so the idempotency check stays stable.
_CLAUDE_ENTRY = {"type": "stdio", **LAUNCH_ENTRY}


class ManualEditRequired(Exception):
    """Raised when a config can't be safely auto-edited; carries a paste snippet."""

    def __init__(self, message: str, snippet: str):
        super().__init__(message)
        self.snippet = snippet


@dataclass(frozen=True)
class Client:
    key: str          # identifier used as `klyk install <key>`
    label: str        # human-readable name
    path: Path        # config file location
    fmt: str          # "json" | "toml"
    note: str = ""    # shown after a successful install
    entry: dict = field(default_factory=lambda: dict(LAUNCH_ENTRY))
    # Optional natural-language context file this client feeds to its model
    # (e.g. Gemini CLI reads ~/.gemini/GEMINI.md). When set, install can OPT-IN
    # to add a small klyk usage note there so the agent discovers the
    # `klyk-call` shell fallback if the client's own MCP never surfaces klyk.
    context_file: "Path | None" = None


def _h(*parts) -> Path:
    return Path.home().joinpath(*parts)


# Order here is the order shown by `--list`.
CLIENTS: dict[str, Client] = {
    "claude": Client(
        "claude", "Claude Code", _h(".claude.json"), "json",
        "Restart Claude Code to load klyk.", dict(_CLAUDE_ENTRY),
    ),
    "cursor": Client(
        "cursor", "Cursor", _h(".cursor", "mcp.json"), "json",
        "Restart Cursor to load klyk.",
    ),
    "windsurf": Client(
        "windsurf", "Windsurf", _h(".codeium", "windsurf", "mcp_config.json"), "json",
        "Restart Windsurf to load klyk.",
    ),
    "continue": Client(
        "continue", "Continue", _h(".continue", "config.json"), "json",
        "Reload your IDE to load klyk.",
    ),
    "cline": Client(
        "cline", "Cline (VS Code)",
        _h("Library", "Application Support", "Code", "User", "globalStorage",
           "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
        "json", "Reload the VS Code window to load klyk.",
    ),
    "codex": Client(
        "codex", "OpenAI Codex CLI", _h(".codex", "config.toml"), "toml",
        "Restart the Codex CLI to load klyk.",
    ),
    "gemini": Client(
        "gemini", "Gemini CLI", _h(".gemini", "settings.json"), "json",
        "Restart the Gemini CLI, then run /mcp to confirm klyk is listed.",
        context_file=_h(".gemini", "GEMINI.md"),
    ),
    "antigravity": Client(
        "antigravity", "Antigravity CLI (agy)",
        _h(".gemini", "antigravity-cli", "mcp_config.json"), "json",
        "Antigravity's native MCP can be unreliable — if tools don't appear, "
        "drive klyk with `klyk-call` instead (see README).",
        context_file=_h(".gemini", "GEMINI.md"),
    ),
    "grok": Client(
        "grok", "Grok CLI (xAI)", _h(".grok", "config.toml"), "toml",
        "Restart Grok (or run `grok mcp doctor`) to load klyk.",
    ),
}

# Alternate names a user might type for a client. The standard Gemini CLI and
# Antigravity share the ~/.gemini/ folder but read different config files, so
# they are distinct entries; `agy` is the common shell alias for Antigravity.
ALIASES = {"agy": "antigravity"}


def get(key: str) -> Client | None:
    k = key.lower()
    return CLIENTS.get(ALIASES.get(k, k))


def is_present(client: Client) -> bool:
    """Heuristic: is this client installed on this Mac? True when its config file
    exists or its config directory does — both are created on the client's first
    run, so this reliably distinguishes installed clients from the full catalog."""
    return client.path.exists() or client.path.parent.exists()


# --- optional context-file guide (opt-in) --------------------------------
# A small, precedence-framed note for a client's natural-language context file
# so the agent discovers the `klyk-call` shell fallback when its own MCP plumbing
# never surfaces klyk. Written ONLY on explicit user opt-in, inside HTML-comment
# markers so it merges into an existing file without clobbering, replaces cleanly
# on re-run, and is removable on uninstall.
_CTX_START = "<!-- klyk:start -->"
_CTX_END = "<!-- klyk:end -->"

_GUIDE = """## Computer use via klyk
Control this Mac with klyk. If the klyk MCP tools are loaded, use them; otherwise drive the same session from the shell with `klyk-call`:
- `klyk-call --list` (all tools + params) · `klyk-call --schema <tool>` (one tool) · `klyk-call --tool <name> --args '<json>'` (call it)
- e.g. `klyk-call --tool inspect --app "Finder"`. Screenshots return a `saved_path` to open. Emergency stop: Cmd+Shift+Esc."""


def context_block() -> str:
    """The exact marked block written to (or pasted into) a context file."""
    return f"{_CTX_START}\n{_GUIDE.strip()}\n{_CTX_END}"


def _ctx_bounds(text: str):
    """Return (start, end) indices of the marked block in text, or None."""
    if _CTX_START in text and _CTX_END in text:
        start = text.index(_CTX_START)
        end = text.index(_CTX_END) + len(_CTX_END)
        if end > start:
            return start, end
    return None


def context_block_present(client: Client) -> bool:
    """True only when the CURRENT block is already in the client's context file."""
    p = client.context_file
    if not p or not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    bounds = _ctx_bounds(text)
    return bool(bounds) and text[bounds[0]:bounds[1]].strip() == context_block().strip()


def write_context_block(client: Client) -> str:
    """Merge the klyk block into the context file. Replaces an existing klyk
    block in place; otherwise appends; creates the file if absent. Never touches
    content outside the markers. Returns "added" | "updated" | "unchanged"."""
    p = client.context_file
    if p is None:
        raise ValueError("client has no context file")
    block = context_block()
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    bounds = _ctx_bounds(existing)
    if bounds:
        if existing[bounds[0]:bounds[1]].strip() == block.strip():
            return "unchanged"
        new_text = existing[:bounds[0]] + block + existing[bounds[1]:]
        p.write_text(new_text, encoding="utf-8")
        return "updated"
    p.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    p.write_text(existing + sep + block + "\n", encoding="utf-8")
    return "added"


def remove_context_block(client: Client) -> bool:
    """Strip the klyk block from the context file, leaving other content intact.
    Returns True if a block was removed."""
    p = client.context_file
    if not p or not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    bounds = _ctx_bounds(text)
    if not bounds:
        return False
    remaining = (text[:bounds[0]] + text[bounds[1]:]).strip("\n")
    p.write_text(remaining + "\n" if remaining else "", encoding="utf-8")
    return True


def list_text() -> str:
    """A formatted list of supported clients for `--list`."""
    # Reverse the alias map so each client can show the alternate names it accepts.
    alias_of: dict[str, list[str]] = {}
    for alt, target in ALIASES.items():
        alias_of.setdefault(target, []).append(alt)
    width = max(len(c.key) for c in CLIENTS.values()) + 2
    lines = ["Supported clients (klyk install <client>):", ""]
    for c in CLIENTS.values():
        alias = f"  (alias: {', '.join(alias_of[c.key])})" if c.key in alias_of else ""
        lines.append(f"  {c.key:<{width}} {c.label}  →  {c.path}{alias}")
    return "\n".join(lines)


def snippet(client: Client) -> str:
    """The exact config block a user would paste for this client."""
    if client.fmt == "toml":
        args = ", ".join(json.dumps(a) for a in client.entry["args"])
        return (
            f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {json.dumps(client.entry['command'])}\n"
            f"args = [{args}]\n"
        )
    return json.dumps({"mcpServers": {SERVER_KEY: client.entry}}, indent=2)


# --- read helpers --------------------------------------------------------
def current_entry(client: Client):
    """Return klyk's existing entry in this client's config, or None."""
    if not client.path.exists():
        return None
    if client.fmt == "toml":
        with open(client.path, "rb") as f:
            data = tomllib.load(f)
        return (data.get("mcp_servers") or {}).get(SERVER_KEY)
    with open(client.path, encoding="utf-8") as f:
        data = json.load(f)
    return (data.get("mcpServers") or {}).get(SERVER_KEY)


# --- write / remove ------------------------------------------------------
def write_entry(client: Client) -> str:
    """Add/refresh klyk in this client's config. Returns a status word:
    "added" | "updated" | "unchanged". Raises ManualEditRequired when a TOML
    file already has a differing entry (we won't risk clobbering it)."""
    if client.fmt == "toml":
        return _write_toml(client)
    return _write_json(client)


def _write_json(client: Client) -> str:
    client.path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if client.path.exists():
        with open(client.path, encoding="utf-8") as f:
            text = f.read().strip()
        data = json.loads(text) if text else {}
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(SERVER_KEY)
    if existing == client.entry:
        return "unchanged"
    servers[SERVER_KEY] = client.entry
    with open(client.path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return "updated" if existing is not None else "added"


def _write_toml(client: Client) -> str:
    existing = current_entry(client)
    want_args = client.entry["args"]
    if existing is not None:
        if existing.get("command") == client.entry["command"] and existing.get("args") == want_args:
            return "unchanged"
        raise ManualEditRequired(
            f"{client.path} already has a different [mcp_servers.{SERVER_KEY}] entry; "
            "edit it by hand to avoid clobbering your TOML.",
            snippet(client),
        )
    client.path.parent.mkdir(parents=True, exist_ok=True)
    block = snippet(client)
    if client.path.exists():
        prev = client.path.read_text(encoding="utf-8")
        sep = "" if prev.endswith("\n\n") else ("\n" if prev.endswith("\n") else "\n\n")
        client.path.write_text(prev + sep + block, encoding="utf-8")
    else:
        client.path.write_text(block, encoding="utf-8")
    return "added"


def remove_entry(client: Client) -> bool:
    """Remove klyk from this client's config. Returns True if removed."""
    if not client.path.exists():
        return False
    if client.fmt == "toml":
        # stdlib can't rewrite TOML; tell the caller to do it by hand.
        if current_entry(client) is None:
            return False
        raise ManualEditRequired(
            f"Remove the [mcp_servers.{SERVER_KEY}] table from {client.path} by hand "
            "(stdlib can't safely rewrite TOML).",
            "",
        )
    with open(client.path, encoding="utf-8") as f:
        data = json.load(f)
    servers = data.get("mcpServers") or {}
    if SERVER_KEY not in servers:
        return False
    del servers[SERVER_KEY]
    with open(client.path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return True
