"""Registry of MCP clients that `klyk install <client>` can auto-configure.

Design goal: onboarding to any client should be as easy as adding an MCP to
Claude. Clients that share a known shape need only one CLIENTS entry; a
genuinely distinct format gets one focused adapter behind the same API. Every
client reuses the stdio command with its own reserved identity tag.

Three on-disk shapes are handled:
  - "json"  : a JSON file with a top-level `mcpServers` map (Claude, Cursor,
              Windsurf, Continue, Cline, Gemini/Antigravity). Merged in place so
              the client's other settings are preserved.
  - "toml"  : a TOML file with `[mcp_servers.<name>]` tables (OpenAI Codex CLI).
              Append absent entries or migrate this interpreter's standard
              launch with small, fully parsed edits. Customized commands need
              manual editing; other settings and comments are preserved.
  - "opencode": OpenCode's global JSON/JSONC config, using `mcp.klyk` in V1
              or `mcp.servers.klyk` in V2. The installed CLI selects the schema;
              a span editor preserves comments and unrelated settings.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import jsonc

# Key under which klyk registers in every client's config.
SERVER_KEY = "klyk"

# The canonical stdio command; each client adds its own reserved identity tag.
# command is THIS interpreter (sys.executable), not a bare "python3", so the
# config points at the exact Python klyk is installed in — works whether klyk
# was installed via pip (global), pipx, uv tool, or a dedicated venv. A bare
# "python3" would break for any isolated install (the client would launch the
# wrong interpreter, which can't import klyk).
LAUNCH_ENTRY = {
    "command": sys.executable or "python3",
    "args": ["-P", "-m", "klyk.mcp_server"],
    "env": {"PYTHONPATH": ""},
}
# Claude Code's config additionally tags the transport type; kept for parity
# with existing installs so the idempotency check stays stable.
_CLAUDE_ENTRY = {"type": "stdio", **LAUNCH_ENTRY}

# OpenCode follows its own native local-MCP shape: the executable and arguments
# are one array under `command`, and environment uses its native spelling.
_OPENCODE_ENTRY = {
    "type": "local",
    "command": [LAUNCH_ENTRY["command"], *LAUNCH_ENTRY["args"]],
    "environment": {"PYTHONPATH": ""},
}


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
    fmt: str          # "json" | "toml" | "opencode"
    note: str = ""    # shown after a successful install
    entry: dict = field(default_factory=lambda: dict(LAUNCH_ENTRY))
    # Optional natural-language context file this client feeds to its model
    # (e.g. Gemini CLI reads ~/.gemini/GEMINI.md). When set, install can OPT-IN
    # to add a small klyk usage note there so the agent discovers the
    # `klyk-call` shell fallback if the client's own MCP never surfaces klyk.
    context_file: "Path | None" = None

    def __post_init__(self):
        """Give each launch its own environment and reserved canonical client tag."""
        entry = dict(self.entry)
        env_key = "environment" if self.fmt == "opencode" else "env"
        environment = entry.get(env_key, {})
        if isinstance(environment, dict):
            entry[env_key] = {**environment, "KLYK_CLIENT": self.key}
        object.__setattr__(self, "entry", entry)


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
    "opencode": Client(
        "opencode", "OpenCode CLI",
        _h(".config", "opencode", "opencode.json"), "opencode",
        "OpenCode reloads the config automatically. Run `opencode mcp list` "
        "to confirm klyk is connected.", dict(_OPENCODE_ENTRY),
    ),
    "gemini": Client(
        "gemini", "Gemini CLI", _h(".gemini", "settings.json"), "json",
        "Restart the Gemini CLI, then run /mcp to confirm klyk is listed.",
        context_file=_h(".gemini", "GEMINI.md"),
    ),
    "antigravity": Client(
        "antigravity", "Antigravity CLI (agy)",
        _h(".gemini", "config", "mcp_config.json"), "json",
        "Configuration saved; this does not verify a running Antigravity connection. "
        "Start a new agy session, then use /mcp to confirm klyk is connected. "
        "Workspace .agents/mcp_config.json can also affect the loaded servers.",
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


def current_process_client() -> str | None:
    """Identify this launch without reading configs or trusting process-name substrings."""
    # Grouping metadata for trusted local launches, not an authentication boundary.
    if "KLYK_CLIENT" in os.environ:
        key = os.environ["KLYK_CLIENT"]
        return key if key in CLIENTS else None
    try:
        from .launcher import process_identity
        import pwd

        parent = os.getppid()
        identity = process_identity(parent, include_command=True)
        if (not isinstance(identity, dict) or identity.get("pid") != parent
                or identity.get("uid") != os.getuid() or parent != os.getppid()):
            return "other"
        executable = identity.get("executable")
        argv = identity.get("argv")
        if (not isinstance(executable, str) or not Path(executable).is_absolute()
                or not isinstance(argv, list) or not argv or not isinstance(argv[0], str)):
            return "other"
        # Known native launchers only: shared node/python/shell parents stay Other.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        launchers = {
            "codex": (
                Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
                Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"),
            ),
            "claude": (home / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")),
            "opencode": (home / ".opencode/bin/opencode", Path("/opt/homebrew/bin/opencode"), Path("/usr/local/bin/opencode")),
        }
        matches = set()
        for key, paths in launchers.items():
            for path in paths:
                resolved = str(path.resolve())
                target = Path(resolved)
                native_target = target.name == key
                if key == "claude" and target.parent == (home / ".local/share/claude/versions").resolve():
                    native_target = re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", target.name) is not None
                if not native_target:
                    continue
                if executable == resolved and argv[0] in (str(path), resolved, key):
                    matches.add(key)
        if len(matches) == 1:
            return matches.pop()
    except (OSError, ValueError, KeyError, RuntimeError, ImportError):
        pass
    return "other"


def is_present(client: Client) -> bool:
    """Heuristic: is this client installed on this Mac? True when its config file
    exists or its config directory does — both are created on the client's first
    run, so this reliably distinguishes installed clients from the full catalog."""
    if client.key == "antigravity" and (client.path.parent.parent / "antigravity-cli").is_dir():
        return True
    if client.fmt == "opencode":
        return (
            shutil.which("opencode") is not None
            or _h(".opencode", "bin", "opencode").is_file()
            or any(path.exists() or path.is_symlink() for path in _opencode_paths(client))
        )
    return client.path.exists() or client.path.parent.exists()


def opencode_executable() -> str | None:
    """Resolve OpenCode without starting a service or changing its configuration."""
    found = shutil.which("opencode")
    if found:
        return found
    fallback = _h(".opencode", "bin", "opencode")
    return str(fallback) if fallback.is_file() else None


def opencode_major_version() -> int | None:
    """Read the CLI's version-only output; unknown versions never imply V1."""
    executable = opencode_executable()
    if executable is None:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=3,
                                env={**os.environ, "KLYK_UPDATE_CHECK": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"(?:opencode\s+)?v?(\d+)\.\d+(?:\.\d+)?(?:[-+][\w.]+)?",
                         result.stdout.strip())
    return int(match[1]) if result.returncode == 0 and match else None


def _opencode_native_v2(client: Client) -> bool:
    """Select the installed major, falling back only to an explicit config shape."""
    major = opencode_major_version()
    if major is not None:
        if major not in (1, 2):
            raise ConfigFormatError("This OpenCode major version is not supported; no config was changed")
        return major == 2
    for path in (client.path.parent / "opencode.json", client.path.parent / "opencode.jsonc"):
        if path.exists() or path.is_symlink():
            present, mcp = jsonc.top_level_property(jsonc.read_exact(path), path, "mcp")
            if present and isinstance(mcp, dict) and "servers" in mcp:
                servers = mcp["servers"]
                if isinstance(servers, dict) and not isinstance(servers.get("type"), str):
                    return True
    if opencode_executable() is not None:
        raise ConfigFormatError("Could not determine the installed OpenCode version; no config was changed")
    return False


def _opencode_paths(client: Client, *, native_v2: bool | None = None) -> tuple[Path, ...]:
    """Return OpenCode's global config files in its actual load order."""
    base = client.path.parent
    if native_v2 is None:
        native_v2 = _opencode_native_v2(client)
    modern = (base / "opencode.json", base / "opencode.jsonc")
    return modern if native_v2 else (base / "config.json", *modern)


def config_path(client: Client) -> Path:
    """Return the config file klyk will edit, honoring OpenCode precedence."""
    if client.fmt != "opencode":
        return client.path
    for path in reversed(_opencode_paths(client, native_v2=opencode_major_version() == 2)):
        if path.exists() or path.is_symlink():
            return path
    return client.path


def config_files(client: Client) -> tuple[Path, ...]:
    """Return every existing file that may contain this client's klyk entry."""
    if client.fmt == "opencode":
        return tuple(
            path for path in _opencode_paths(client, native_v2=opencode_major_version() == 2)
            if path.exists() or path.is_symlink()
        )
    return (client.path,) if client.path.exists() else ()


# --- optional context-file guide (opt-in) --------------------------------
# A small, precedence-framed note for a client's natural-language context file
# so the agent discovers the `klyk-call` shell fallback when its own MCP plumbing
# never surfaces klyk. Written ONLY on explicit user opt-in, inside HTML-comment
# markers so it merges into an existing file without clobbering, replaces cleanly
# on re-run, and is removable on uninstall.
_CTX_START = "<!-- klyk:start -->"
_CTX_END = "<!-- klyk:end -->"

_GUIDE = """## Computer use via klyk
Use native klyk MCP tools when loaded. If missing in agy, run `klyk install antigravity`, then start a new session and check /mcp. Use `klyk-call` only when native MCP is unavailable:
- `klyk-call --list` (all tools + params) · `klyk-call --schema <tool>` (one tool) · `klyk-call --tool <name> --args '<json>'` (call it)
- e.g. `klyk-call --tool inspect --app "Finder"`. Screenshots return a `saved_path` to open.
- Each shell invocation starts a separate server; it does not share a native MCP session. `--batch` keeps one server for its input lines. A standalone take_control does not carry over to the next invocation. Never reclaim a live driver's control without user authorization. Emergency stop: Cmd+Shift+Esc."""


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
    text = jsonc.read_exact(p)
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
    original = jsonc.read_snapshot(p, missing_ok=True)
    existing = original.text
    bounds = _ctx_bounds(existing)
    if bounds:
        if existing[bounds[0]:bounds[1]].strip() == block.strip():
            return "unchanged"
        new_text = existing[:bounds[0]] + block + existing[bounds[1]:]
        jsonc.atomic_write(p, new_text, expected=original)
        return "updated"
    p.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    jsonc.atomic_write(p, existing + sep + block + "\n", expected=original)
    return "added"


def remove_context_block(client: Client) -> bool:
    """Strip the klyk block from the context file, leaving other content intact.
    Returns True if a block was removed."""
    p = client.context_file
    if not p or not p.exists():
        return False
    original = jsonc.read_snapshot(p)
    text = original.text
    bounds = _ctx_bounds(text)
    if not bounds:
        return False
    remaining = (text[:bounds[0]] + text[bounds[1]:]).strip("\n")
    jsonc.atomic_write(p, remaining + "\n" if remaining else "", expected=original)
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


ConfigFormatError = jsonc.ConfigFormatError


def legacy_antigravity_entry(client: Client):
    """Read an old agy installation for migration without altering its config."""
    if client.key != "antigravity":
        return None
    legacy = replace(client, path=client.path.parent.parent / "antigravity-cli" / "mcp_config.json")
    return current_entry(legacy)


def legacy_opencode_entry(client: Client):
    """Find stored owned entries from either schema, including V1-only config.json."""
    if client.fmt != "opencode":
        return None
    existing = None
    for path in _opencode_paths(client, native_v2=False):
        if path.exists() or path.is_symlink():
            servers = _opencode_servers(jsonc.read_exact(path), path, native_v2=True)
            if SERVER_KEY in servers:
                existing = servers[SERVER_KEY]
    return existing


def snippet(client: Client) -> str:
    """The exact config block a user would paste for this client."""
    if client.fmt == "toml":
        args = ", ".join(json.dumps(a, ensure_ascii=False) for a in client.entry["args"])
        block = (
            f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {json.dumps(client.entry['command'], ensure_ascii=False)}\n"
            f"args = [{args}]\n"
        )
        if client.entry.get("env"):
            block += f"\n[mcp_servers.{SERVER_KEY}.env]\n"
            block += "".join(f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(value, ensure_ascii=False)}\n"
                             for key, value in client.entry["env"].items())
        return block
    if client.fmt == "opencode":
        servers = {SERVER_KEY: client.entry}
        mcp = {"servers": servers} if _opencode_native_v2(client) else servers
        return json.dumps({"mcp": mcp}, indent=2)
    return json.dumps({"mcpServers": {SERVER_KEY: client.entry}}, indent=2)


# --- read helpers --------------------------------------------------------
def _unambiguous_jsonc_value(node, path: Path):
    """Retain custom owned fields only when every nested property is unambiguous."""
    if node.kind == "object":
        result = {}
        for prop in node.properties:
            if prop.key in result:
                raise ConfigFormatError(f"{path}: duplicate fields in the klyk entry; remove the duplicate before retrying")
            result[prop.key] = _unambiguous_jsonc_value(prop.value, path)
        return result
    if node.kind == "array":
        return [_unambiguous_jsonc_value(item, path) for item in node.items]
    return node.value


def _opencode_servers(text: str, path: Path, *, native_v2: bool) -> dict:
    """Read one unambiguous MCP container without flattening duplicate keys."""
    root = jsonc.parse_object(text, path)
    matches = jsonc._properties(root, "mcp")
    if len(matches) > 1:
        raise ConfigFormatError(f"{path}: duplicate top-level 'mcp' keys")
    if not matches:
        return {}
    container = matches[0].value
    if container.kind != "object":
        raise ConfigFormatError(f"{path}: 'mcp' must be an object")
    owned = jsonc._properties(container, SERVER_KEY)
    legacy_entry = _unambiguous_jsonc_value(owned[-1].value, path) if owned else None
    if native_v2:
        matches = jsonc._properties(container, "servers")
        if len(matches) > 1:
            raise ConfigFormatError(f"{path}: duplicate 'mcp.servers' keys")
        if not matches or (matches[0].value.kind == "object"
                           and isinstance(matches[0].value.to_python().get("type"), str)):
            # A V1 server can itself be called `servers`; its type field
            # distinguishes it from a native server map.
            result = container.to_python()
            if owned:
                result[SERVER_KEY] = legacy_entry
            return result
        native = matches[0].value
        if native.kind != "object":
            raise ConfigFormatError(f"{path}: 'mcp.servers' must be an object")
        legacy = {prop.key: prop.value.to_python() for prop in container.properties
                  if prop.key not in ("servers", "timeout")}
        if owned:
            legacy[SERVER_KEY] = legacy_entry
        owned = jsonc._properties(native, SERVER_KEY)
        values = native.to_python()
        if owned:
            values[SERVER_KEY] = _unambiguous_jsonc_value(owned[-1].value, path)
        # V2 accepts mixed MCP entries; each native server wins its own name.
        return {**legacy, **values}
    # Duplicate owned entries are repairable; the span editor removes them.
    result = container.to_python()
    if owned:
        result[SERVER_KEY] = legacy_entry
    return result


def _json_object(text: str, path: Path) -> dict:
    """Reject malformed roots, duplicate keys and non-finite JSON without echoing values."""
    def unique(pairs):
        """Keep ambiguous JSON from silently dropping a user's configuration."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigFormatError(f"{path}: duplicate JSON keys; remove the duplicate before retrying")
            result[key] = value
        return result

    def finite_float(value):
        """Exclude exponent overflow as well as literal NaN and Infinity."""
        number = float(value)
        if not math.isfinite(number):
            raise ConfigFormatError(f"{path}: non-finite JSON numbers are not supported")
        return number

    def reject_constant(value):
        """Reject permissive JSON constants without printing their source value."""
        raise ConfigFormatError(f"{path}: non-JSON constants are not supported")

    try:
        data = json.loads(text.strip() or "{}", object_pairs_hook=unique,
                          parse_float=finite_float, parse_constant=reject_constant)
    except RecursionError:
        raise ConfigFormatError(f"{path}: configuration is nested too deeply") from None
    pending = [(data, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > jsonc._MAX_CONFIG_DEPTH:
            raise ConfigFormatError(f"{path}: configuration is nested too deeply")
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        if visited + len(pending) + len(children) > jsonc._MAX_CONFIG_TOKENS:
            raise ConfigFormatError(f"{path}: configuration contains too many JSON values")
        pending.extend((child, depth + 1) for child in children)
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ConfigFormatError(f"{path}: expected an object with an 'mcpServers' object")
    return data


def _refreshed_entry(client: Client, existing) -> dict:
    """Refresh only owned launch fields, retaining env, disabled state and restrictions."""
    if existing is not None and not isinstance(existing, dict):
        raise ConfigFormatError(f"{client.path}: klyk entry must be an object")
    result = {**client.entry, **(existing or {}),
            "command": client.entry["command"],
            **({"args": client.entry["args"]} if client.fmt != "opencode" else {})}
    if "type" in client.entry:
        result["type"] = client.entry["type"]
    env_key = "environment" if client.fmt == "opencode" else "env"
    custom = result.get(env_key, {})
    if (not isinstance(custom, dict)
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in custom.items())):
        raise ConfigFormatError(f"{client.path}: {env_key} must be an object containing string variables")
    result[env_key] = {**client.entry.get(env_key, {}), **custom,
                       "KLYK_CLIENT": client.key}
    return result


def _toml_entry(data: dict, path: Path):
    """Require real TOML tables before appending a managed server table."""
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigFormatError(f"{path}: 'mcp_servers' must be a table")
    entry = servers.get(SERVER_KEY)
    if entry is not None and not isinstance(entry, dict):
        raise ConfigFormatError(f"{path}: the klyk entry must be a table")
    return entry


def current_entry(client: Client):
    """Return klyk's existing entry in this client's config, or None."""
    if client.fmt == "opencode":
        native_v2 = _opencode_native_v2(client)
        existing = None
        for path in _opencode_paths(client, native_v2=native_v2):
            if not path.exists() and not path.is_symlink():
                continue
            servers = _opencode_servers(jsonc.read_exact(path), path, native_v2=native_v2)
            if SERVER_KEY in servers:
                existing = servers[SERVER_KEY]
        return existing
    if not client.path.exists():
        return None
    if client.fmt == "toml":
        data = tomllib.loads(jsonc.read_exact(client.path))
        return _toml_entry(data, client.path)
    data = _json_object(jsonc.read_exact(client.path), client.path)
    return (data.get("mcpServers") or {}).get(SERVER_KEY)


# --- write / remove ------------------------------------------------------
def write_entry(client: Client) -> str:
    """Add/refresh klyk in this client's config. Returns a status word:
    "added" | "updated" | "unchanged". Raises ManualEditRequired when a TOML
    launch is customized or cannot be edited while preserving other settings."""
    if client.key == "antigravity":
        return _write_antigravity(client)
    if client.fmt == "toml":
        return _write_toml(client)
    if client.fmt == "opencode":
        return _write_opencode(client)
    return _write_json(client)


def _write_antigravity(client: Client) -> str:
    """Configure current agy atomically, preserving custom fields and legacy files."""
    original = jsonc.read_snapshot(client.path, missing_ok=True)
    data = _json_object(original.text, client.path)
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(SERVER_KEY)
    legacy_path = client.path.parent.parent / "antigravity-cli" / "mcp_config.json"
    legacy = None if existing is not None else jsonc.read_snapshot(legacy_path, missing_ok=True)
    source = existing
    if legacy is not None:
        source = _json_object(legacy.text, legacy_path).get("mcpServers", {}).get(SERVER_KEY)
    if source is not None and not isinstance(source, dict):
        raise ConfigFormatError(f"{client.path}: klyk entry must be an object")
    entry = _refreshed_entry(client, source)
    if existing == entry:
        return "unchanged"
    servers[SERVER_KEY] = entry
    if legacy is not None and jsonc.read_snapshot(legacy_path, missing_ok=True) != legacy:
        raise ConfigFormatError(f"{legacy_path}: configuration changed outside klyk; retry")
    jsonc.atomic_write(client.path, json.dumps(data, indent=2) + "\n", expected=original)
    return "updated" if existing is not None else "added"


def _write_opencode(client: Client) -> str:
    """Add or refresh klyk in OpenCode's effective global JSON/JSONC config."""
    native_v2 = _opencode_native_v2(client)
    paths = _opencode_paths(client, native_v2=native_v2)
    path = next((p for p in reversed(paths) if p.exists() or p.is_symlink()), client.path)
    original = jsonc.read_snapshot(path, missing_ok=True)
    text = original.text
    existing = None
    legacy = None
    sources = {}
    for source_path in paths:
        source = original if source_path == path else jsonc.read_snapshot(source_path, missing_ok=True)
        sources[source_path] = source
        source_text = source.text
        servers = _opencode_servers(source_text, source_path, native_v2=native_v2)
        native_servers = servers.get("servers") if not native_v2 else None
        if (isinstance(native_servers, dict)
                and not isinstance(native_servers.get("type"), str)
                and SERVER_KEY in native_servers):
            raise ConfigFormatError(f"{source_path}: a native V2 klyk entry exists; use OpenCode V2 or migrate that entry before configuring V1")
        if SERVER_KEY in servers:
            existing = servers[SERVER_KEY]
        if native_v2:
            old_servers = _opencode_servers(source_text, source_path, native_v2=False)
            if SERVER_KEY in old_servers:
                legacy = old_servers[SERVER_KEY]
    if native_v2 and existing is None and legacy is None:
        legacy_path = client.path.parent / "config.json"
        source = jsonc.read_snapshot(legacy_path, missing_ok=True)
        sources[legacy_path] = source
        legacy = _opencode_servers(source.text, legacy_path, native_v2=False).get(SERVER_KEY)
    entry = _refreshed_entry(client, existing if existing is not None else legacy)
    if native_v2:
        # V2 renamed the disable flag. Other process options remain untouched.
        if "enabled" in entry:
            if not isinstance(entry["enabled"], bool):
                raise ConfigFormatError(f"{path}: legacy enabled flag must be boolean")
            entry.setdefault("disabled", not entry.pop("enabled"))
        timeout = entry.get("timeout")
        if isinstance(timeout, (int, float)):
            if (isinstance(timeout, bool) or timeout <= 0
                    or (isinstance(timeout, float) and not math.isfinite(timeout))):
                raise ConfigFormatError(f"{path}: legacy timeout must be positive milliseconds")
            # OpenCode's documented V1 migration retains this budget for
            # catalog and execution. Native V2 accepts integer milliseconds.
            budget = math.ceil(timeout)
            entry["timeout"] = {"catalog": budget, "execution": budget}
    updated = jsonc.set_mcp_entry(text, path, SERVER_KEY, entry, native_v2=native_v2)
    if native_v2:
        # Remove the selected file's inert legacy sibling after migration, so
        # one document does not offer two conflicting klyk launch paths.
        updated, _ = jsonc.remove_mcp_entry(updated, path, SERVER_KEY)
    if updated == text:
        return "unchanged"
    for source_path, source in sources.items():
        if jsonc.read_snapshot(source_path, missing_ok=True) != source:
            raise ConfigFormatError(f"{source_path}: configuration changed outside klyk; retry")
    jsonc.atomic_write(path, updated, expected=original)
    return "updated" if existing is not None or legacy is not None else "added"


def _write_json(client: Client) -> str:
    """Refresh a strict JSON launch entry without losing customized settings."""
    original = jsonc.read_snapshot(client.path, missing_ok=True)
    data = _json_object(original.text, client.path)
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(SERVER_KEY)
    entry = _refreshed_entry(client, existing)
    if existing == entry:
        return "unchanged"
    servers[SERVER_KEY] = entry
    jsonc.atomic_write(client.path, json.dumps(data, indent=2) + "\n", expected=original)
    return "updated" if existing is not None else "added"


def _checked_toml_edit(text: str, expected: dict, pattern: str, replacement) -> str | None:
    """Accept a small edit only if native parsing proves the full expected result."""
    for count, match in enumerate(re.finditer(pattern, text, re.MULTILINE)):
        # Fake headers in multiline strings are harmless candidates, but a
        # pathological file must not trigger unbounded repeated full parses.
        if count >= 64:
            break
        candidate = text[:match.start()] + replacement(match) + text[match.end():]
        try:
            if tomllib.loads(candidate) == expected:
                return candidate
        except (tomllib.TOMLDecodeError, RecursionError):
            continue
    return None


def _toml_env_edit(text: str, expected: dict, environment: dict, key: str,
                   value: str, newline: str) -> str | None:
    """Edit one managed environment value only when full native parsing agrees."""
    key_text = json.dumps(key, ensure_ascii=False)
    value_text = json.dumps(value, ensure_ascii=False)
    if key in environment:
        # Reserved tags may be corrected; preserve their key, spacing and comment.
        key_pattern = rf"(?:{re.escape(key)}|{re.escape(key_text)}|'{re.escape(key)}')"
        string_value = r'''(?:"(?:[^"\\\r\n]|\\[^\r\n])*"|'[^'\r\n]*')'''
        return _checked_toml_edit(text, expected,
            rf"(?<![\w-])({key_pattern}[ \t]*=[ \t]*){string_value}",
            lambda match: match[1] + value_text)
    section = rf"^[ \t]*\[mcp_servers\.{re.escape(SERVER_KEY)}"
    section_end = r"\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)"

    def add_line(match, name):
        """Insert after the real header, retaining its existing comment and newline."""
        return match[0] + ("" if match[0].endswith("\n") else newline) + f'{name} = {value_text}{newline}'

    edits = (
        (section + r"\.env" + section_end, lambda match: add_line(match, key_text)),
        (r'''^[ \t]*(?:env|"env"|'env')[ \t]*=[ \t]*\{''',
         lambda match: match[0] + f'{key_text} = {value_text}' + (", " if environment else "")),
        (section + section_end, lambda match: add_line(match, f'env.{key_text}')),
    )
    for pattern, replacement in edits:
        candidate = _checked_toml_edit(text, expected, pattern, replacement)
        if candidate is not None:
            return candidate
    return None


def _write_toml(client: Client) -> str:
    """Migrate known generated launches while preserving all unrelated TOML text."""
    original = jsonc.read_snapshot(client.path, missing_ok=True)
    data = tomllib.loads(original.text)
    existing = _toml_entry(data, client.path)
    if existing is not None:
        legacy_args = ["-m", "klyk.mcp_server"]
        if (existing.get("command") != LAUNCH_ENTRY["command"]
                or client.entry["command"] != LAUNCH_ENTRY["command"]
                or client.entry["args"] != LAUNCH_ENTRY["args"]
                or existing.get("args") not in (legacy_args, LAUNCH_ENTRY["args"])):
            raise ManualEditRequired(
                f"{client.path} already has a customized [mcp_servers.{SERVER_KEY}] launch; "
                "edit it by hand to preserve your command and arguments.", snippet(client),
            )
    entry = _refreshed_entry(client, existing)
    expected = {**data, "mcp_servers": {**data.get("mcp_servers", {}), SERVER_KEY: entry}}
    updated = original.text
    newline = "\r\n" if "\r\n" in updated else "\n"
    if existing is not None:
        if existing == entry:
            return "unchanged"
        migrated = dict(existing)
        if existing["args"] == legacy_args:
            migrated["args"] = entry["args"]
            intermediate = {**data, "mcp_servers": {**data["mcp_servers"],
                            SERVER_KEY: migrated}}
            # Insert only -P; even comments inside multiline arrays survive.
            updated = _checked_toml_edit(updated, intermediate,
                r'''^[ \t]*(?:args|"args"|'args')[ \t]*=[ \t]*\[''',
                lambda match: match[0] + '"-P", ')
        env = existing.get("env")
        if updated is not None and env is not None:
            migrated["env"] = dict(env)
            for key, value in entry["env"].items():
                if migrated["env"].get(key) == value:
                    continue
                before = dict(migrated["env"])
                migrated["env"][key] = value
                intermediate = {**data, "mcp_servers": {**data["mcp_servers"], SERVER_KEY: migrated}}
                updated = _toml_env_edit(updated, intermediate, before, key, value, newline)
                if updated is None:
                    break
        block = (f'[mcp_servers.{SERVER_KEY}.env]{newline}'
                 + ''.join(f'{json.dumps(key, ensure_ascii=False)} = {json.dumps(value, ensure_ascii=False)}{newline}'
                           for key, value in entry["env"].items())) if env is None else None
    else:
        block = snippet(client).replace("\n", newline)
    if updated is not None and block is not None:
        sep = "" if updated.endswith(newline * 2) or not updated else (newline if updated.endswith(newline) else newline * 2)
        updated = _checked_toml_edit(updated, expected, r"\Z", lambda match: sep + block)
    if updated is not None:
        # Never report a migration that omitted an owned field or touched other data.
        updated = _checked_toml_edit(updated, expected, r"\Z", lambda match: "")
    if updated is None:
        raise ManualEditRequired(
            f"{client.path}: the klyk TOML entry could not be refreshed without changing other settings; "
            "edit it by hand.", snippet(client),
        )
    jsonc.atomic_write(client.path, updated, expected=original)
    return "updated" if existing is not None else "added"


def remove_entry(client: Client) -> bool:
    """Remove klyk from this client's config. Returns True if removed."""
    if client.fmt == "opencode":
        return _remove_opencode(client)
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
    original = jsonc.read_snapshot(client.path)
    data = _json_object(original.text, client.path)
    servers = data.get("mcpServers") or {}
    if SERVER_KEY not in servers:
        return False
    del servers[SERVER_KEY]
    jsonc.atomic_write(client.path, json.dumps(data, indent=2) + "\n", expected=original)
    return True


def _remove_opencode(client: Client) -> bool:
    """Remove klyk from every OpenCode global config with rollback on failure."""
    changes: list[tuple[Path, jsonc.FileSnapshot, str]] = []
    # Remove both generations from every historically managed global file.
    # V2 ignores config.json, but leaving an old owned entry there could make
    # it reappear if the user later returns to V1.
    for path in _opencode_paths(client, native_v2=False):
        if not path.exists() and not path.is_symlink():
            continue
        original = jsonc.read_snapshot(path)
        updated, changed = jsonc.remove_mcp_entry(original.text, path, SERVER_KEY)
        mcp = _opencode_servers(updated, path, native_v2=False)
        if (isinstance(mcp.get("servers"), dict)
                and not isinstance(mcp["servers"].get("type"), str)):
            updated, native_changed = jsonc.remove_mcp_entry(updated, path, SERVER_KEY, native_v2=True)
            changed = changed or native_changed
        if changed:
            changes.append((path, original, updated))
    if not changes:
        return False

    written: list[tuple[Path, jsonc.FileSnapshot, str]] = []
    try:
        for path, original, updated in changes:
            jsonc.atomic_write(path, updated, expected=original)
            written.append((path, original, updated))
    except Exception:
        for path, original, updated in reversed(written):
            try:
                saved = jsonc.read_snapshot(path)
                if saved.text != updated or saved.target != original.target:
                    continue  # Preserve an external edit made during uninstall.
                jsonc.atomic_write(path, original.text, expected=saved)
            except Exception:
                pass
        raise
    return True
