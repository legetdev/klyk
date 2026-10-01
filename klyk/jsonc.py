"""Small span-preserving JSONC editor used for OpenCode configuration.

It supports comments, trailing commas, CRLF, duplicate owned keys, atomic
writes, and byte-preserving edits outside the requested object property.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MAX_CONFIG_TOKENS = 100_000
_MAX_CONFIG_DEPTH = 128


class ConfigFormatError(ValueError):
    """Raised when a JSONC document cannot be parsed or safely edited."""


@dataclass(frozen=True)
class FileSnapshot:
    """Exact text and file identity used to refuse stale configuration edits."""

    text: str
    target: Path
    identity: tuple | None


def _file_identity(info) -> tuple:
    """Include replacement, content and permission changes in an edit snapshot."""
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _resolved_target(path: Path) -> Path:
    """Honor intentional dotfile symlinks, while refusing broken final links."""
    if path.is_symlink() and not path.exists():
        raise ConfigFormatError(f"{path}: broken symlink")
    return path.resolve()


def read_snapshot(path: Path, *, missing_ok: bool = False) -> FileSnapshot:
    """Read a bounded owned regular file without blocking on FIFOs or devices."""
    target = _resolved_target(path)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing_ok:
            return FileSnapshot("", target, None)
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigFormatError(f"{path}: expected a regular file owned by this user")
        if info.st_size > _MAX_CONFIG_BYTES:
            raise ConfigFormatError(f"{path}: configuration exceeds the 8 MiB limit")
        data = bytearray()
        while len(data) <= _MAX_CONFIG_BYTES:
            chunk = os.read(fd, min(65536, _MAX_CONFIG_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_CONFIG_BYTES:
            raise ConfigFormatError(f"{path}: configuration exceeds the 8 MiB limit")
        identity = _file_identity(info)
        if (_file_identity(os.fstat(fd)) != identity
                or _resolved_target(path) != target
                or _file_identity(target.stat(follow_symlinks=False)) != identity):
            raise ConfigFormatError(f"{path}: configuration changed while it was being read; retry")
        return FileSnapshot(data.decode("utf-8"), target, identity)
    finally:
        os.close(fd)


def _reject_non_json_constant(value: str) -> None:
    """Reject Python's permissive NaN/Infinity extensions to strict JSON."""
    raise ValueError(f"non-JSON constant {value}")


@dataclass(frozen=True)
class _Token:
    """One significant JSONC token with its exact source span."""

    kind: str
    start: int
    end: int
    value: object = None


@dataclass
class _Property:
    """One object property, including the comma that follows it when present."""

    key: str
    key_token: _Token
    value: "_Node"
    comma: "_Token | None" = None


@dataclass
class _Node:
    """A parsed JSONC value retaining source spans for surgical edits."""

    kind: str
    start: int
    end: int
    value: object = None
    properties: list[_Property] = field(default_factory=list)
    items: list["_Node"] = field(default_factory=list)

    def to_python(self):
        """Convert the syntax node to its ordinary Python value."""
        if self.kind == "object":
            return {prop.key: prop.value.to_python() for prop in self.properties}
        if self.kind == "array":
            return [item.to_python() for item in self.items]
        return self.value


def _tokens(text: str) -> list[_Token]:
    """Tokenize JSON with comments and trailing commas without losing offsets."""
    result: list[_Token] = []
    i = 0
    depth = 0
    punctuation = {"{", "}", "[", "]", ":", ","}
    while i < len(text):
        if len(result) >= _MAX_CONFIG_TOKENS:
            raise ConfigFormatError("configuration contains too many JSONC tokens")
        ch = text[i]
        if ch.isspace() or (ch == "\ufeff" and i == 0):
            i += 1
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i + 2)
            i = len(text) if newline < 0 else newline
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ConfigFormatError("unterminated block comment")
            i = end + 2
            continue
        if ch in punctuation:
            if ch in ("{", "["):
                depth += 1
                if depth > _MAX_CONFIG_DEPTH:
                    raise ConfigFormatError("configuration is nested too deeply")
            elif ch in ("}", "]"):
                depth -= 1
            result.append(_Token(ch, i, i + 1, ch))
            i += 1
            continue
        if ch == '"':
            start = i
            i += 1
            escaped = False
            while i < len(text):
                current = text[i]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    i += 1
                    break
                i += 1
            else:
                raise ConfigFormatError("unterminated string")
            raw = text[start:i]
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigFormatError(f"invalid string at character {start}: {exc.msg}") from exc
            result.append(_Token("string", start, i, value))
            continue
        start = i
        while i < len(text):
            if text[i].isspace() or text[i] in punctuation:
                break
            if text.startswith("//", i) or text.startswith("/*", i):
                break
            i += 1
        raw = text[start:i]
        try:
            value = json.loads(raw, parse_constant=_reject_non_json_constant)
        except ValueError as exc:
            # Malformed config tokens can themselves be credentials.
            raise ConfigFormatError(f"invalid value at character {start}") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigFormatError(f"non-finite number at character {start}")
        result.append(_Token("literal", start, i, value))
    return result


class _Parser:
    """Parse JSONC into a small span-preserving syntax tree."""

    def __init__(self, text: str):
        self._text = text
        self._tokens = _tokens(text)
        self._index = 0

    def parse(self) -> _Node:
        """Parse one complete JSONC document."""
        if not self._tokens:
            return _Node("object", 0, 0, properties=[])
        node = self._parse_value()
        if self._index != len(self._tokens):
            token = self._tokens[self._index]
            raise ConfigFormatError(f"unexpected token at character {token.start}")
        return node

    def _peek(self) -> _Token | None:
        """Return the next token without consuming it."""
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self, kind: str | None = None) -> _Token:
        """Consume the next token, optionally requiring a specific kind."""
        token = self._peek()
        if token is None:
            raise ConfigFormatError("unexpected end of file")
        if kind is not None and token.kind != kind:
            raise ConfigFormatError(
                f"expected {kind!r} at character {token.start}, got {token.kind!r}"
            )
        self._index += 1
        return token

    def _parse_value(self) -> _Node:
        """Parse one object, array, string, or primitive value."""
        token = self._peek()
        if token is None:
            raise ConfigFormatError("expected a value at end of file")
        if token.kind == "{":
            return self._parse_object()
        if token.kind == "[":
            return self._parse_array()
        if token.kind in ("string", "literal"):
            token = self._take()
            return _Node("value", token.start, token.end, value=token.value)
        raise ConfigFormatError(f"expected a value at character {token.start}")

    def _parse_object(self) -> _Node:
        """Parse an object, accepting JSONC's optional trailing comma."""
        opening = self._take("{")
        properties: list[_Property] = []
        if self._peek() and self._peek().kind == "}":
            closing = self._take("}")
            return _Node("object", opening.start, closing.end, properties=properties)
        while True:
            key = self._take("string")
            self._take(":")
            value = self._parse_value()
            prop = _Property(str(key.value), key, value)
            properties.append(prop)
            token = self._peek()
            if token and token.kind == ",":
                prop.comma = self._take(",")
                token = self._peek()
                if token and token.kind == "}":
                    closing = self._take("}")
                    break
                continue
            if token and token.kind == "}":
                closing = self._take("}")
                break
            where = token.start if token else len(self._text)
            raise ConfigFormatError(f"expected ',' or '}}' at character {where}")
        return _Node("object", opening.start, closing.end, properties=properties)

    def _parse_array(self) -> _Node:
        """Parse an array, accepting JSONC's optional trailing comma."""
        opening = self._take("[")
        items: list[_Node] = []
        if self._peek() and self._peek().kind == "]":
            closing = self._take("]")
            return _Node("array", opening.start, closing.end, items=items)
        while True:
            items.append(self._parse_value())
            token = self._peek()
            if token and token.kind == ",":
                self._take(",")
                token = self._peek()
                if token and token.kind == "]":
                    closing = self._take("]")
                    break
                continue
            if token and token.kind == "]":
                closing = self._take("]")
                break
            where = token.start if token else len(self._text)
            raise ConfigFormatError(f"expected ',' or ']' at character {where}")
        return _Node("array", opening.start, closing.end, items=items)


def parse_object(text: str, path: Path) -> _Node:
    """Parse a config and require an object root before any edit."""
    try:
        root = _Parser(text).parse()
    except (ConfigFormatError, RecursionError) as exc:
        if isinstance(exc, RecursionError):
            raise ConfigFormatError(f"{path}: configuration is nested too deeply") from None
        raise ConfigFormatError(f"{path}: {exc}") from exc
    if root.kind != "object":
        raise ConfigFormatError(f"{path}: the root value must be an object")
    return root


def top_level_property(text: str, path: Path, key: str) -> tuple[bool, object]:
    """Read one unique top-level property without discarding duplicate keys."""
    root = parse_object(text, path)
    matches = _properties(root, key)
    if len(matches) > 1:
        raise ConfigFormatError(
            f"{path}: duplicate top-level {key!r} keys; remove the duplicate before retrying"
        )
    if not matches:
        return False, None
    return True, matches[0].value.to_python()


def _properties(node: _Node, key: str) -> list[_Property]:
    """Return every property named key from an object node."""
    if node.kind != "object":
        return []
    return [prop for prop in node.properties if prop.key == key]


def _line_indent(text: str, position: int) -> str:
    """Return leading whitespace on the source line containing position."""
    start = max(text.rfind("\n", 0, position), text.rfind("\r", 0, position)) + 1
    prefix = text[start:position]
    return prefix if not prefix.strip() else ""


def _indent_unit(root: _Node, text: str) -> str:
    """Infer tabs or the existing root indentation, defaulting to two spaces."""
    for prop in root.properties:
        indent = _line_indent(text, prop.key_token.start)
        if indent:
            return "\t" if "\t" in indent else indent
    return "  "


def _format_value(value: object, base_indent: str, newline: str) -> str:
    """Serialize a value while aligning continuation lines to its property."""
    lines = json.dumps(value, indent=2, ensure_ascii=False).splitlines()
    return lines[0] + "".join(newline + base_indent + line for line in lines[1:])


def _insert_property(
    text: str,
    root: _Node,
    obj: _Node,
    key: str,
    value: object,
    parent_indent: str,
) -> str:
    """Insert one property before an object's closing brace."""
    newline = "\r\n" if "\r\n" in text else "\n"
    unit = _indent_unit(root, text)
    if obj.properties:
        child_indent = _line_indent(text, obj.properties[0].key_token.start)
        if not child_indent:
            child_indent = parent_indent + unit
    else:
        child_indent = parent_indent + unit
    rendered = _format_value(value, child_indent, newline)
    had_trailing_comma = bool(obj.properties and obj.properties[-1].comma is not None)
    suffix = "," if had_trailing_comma else ""
    block = f"{child_indent}{json.dumps(key)}: {rendered}{suffix}"

    closing = obj.end - 1
    if obj.properties and obj.properties[-1].comma is None:
        comma_at = obj.properties[-1].value.end
        text = text[:comma_at] + "," + text[comma_at:]
        closing += 1
    line_start = max(text.rfind("\n", 0, closing), text.rfind("\r", 0, closing)) + 1
    if text[line_start:closing].strip():
        return text[:closing] + block.lstrip(" \t") + text[closing:]
    return text[:line_start] + block + newline + parent_indent + text[closing:]


def _remove_property(text: str, obj: _Node, index: int) -> str:
    """Remove one property and the comma needed to keep its object valid."""
    prop = obj.properties[index]
    start = prop.key_token.start
    end = prop.value.end
    preceding_comma: _Token | None = None
    if prop.comma is not None:
        end = prop.comma.end
    elif index > 0 and obj.properties[index - 1].comma is not None:
        preceding_comma = obj.properties[index - 1].comma
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    if not text[line_start:start].strip():
        start = line_start
    newline_at = text.find("\n", end)
    if newline_at >= 0 and not text[end:newline_at].strip():
        end = newline_at + 1
    text = text[:start] + text[end:]
    if preceding_comma is not None:
        text = text[:preceding_comma.start] + text[preceding_comma.end:]
    return text


def set_mcp_entry(text: str, path: Path, server_key: str, entry: dict,
                  *, native_v2: bool = False) -> str:
    """Set one V1 or native V2 MCP entry, preserving every unrelated byte."""
    root = parse_object(text, path)
    if root.end == 0:
        newline = "\r\n" if "\r\n" in text else "\n"
        prefix = text
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        servers = {server_key: entry}
        document = json.dumps({"mcp": {"servers": servers} if native_v2 else servers},
                              indent=2, ensure_ascii=False)
        return prefix + document + newline
    mcp_matches = _properties(root, "mcp")
    if len(mcp_matches) > 1:
        raise ConfigFormatError(
            f"{path}: duplicate top-level 'mcp' keys; remove the duplicate before retrying"
        )
    if not mcp_matches:
        servers = {server_key: entry}
        return _insert_property(text, root, root, "mcp",
                                {"servers": servers} if native_v2 else servers, "")
    mcp_prop = mcp_matches[0]
    mcp = mcp_prop.value
    if mcp.kind != "object":
        raise ConfigFormatError(f"{path}: top-level 'mcp' must be an object")
    parent_indent = _line_indent(text, mcp_prop.key_token.start)
    if native_v2:
        containers = _properties(mcp, "servers")
        if len(containers) > 1:
            raise ConfigFormatError(f"{path}: duplicate 'mcp.servers' keys; remove the duplicate before retrying")
        if not containers:
            return _insert_property(text, root, mcp, "servers", {server_key: entry}, parent_indent)
        if containers[0].value.kind != "object":
            raise ConfigFormatError(f"{path}: 'mcp.servers' must be an object")
        if isinstance(containers[0].value.to_python().get("type"), str):
            raise ConfigFormatError(f"{path}: migrate the legacy server named 'servers' before adding native V2 servers")
        parent_indent = _line_indent(text, containers[0].key_token.start)
        mcp = containers[0].value

    had_duplicates = False
    while len(_properties(mcp, server_key)) > 1:
        had_duplicates = True
        duplicate = _properties(mcp, server_key)[0]
        text = _remove_property(text, mcp, mcp.properties.index(duplicate))
        root = parse_object(text, path)
        mcp = _properties(root, "mcp")[0].value
        if native_v2:
            mcp = _properties(mcp, "servers")[0].value
    matches = _properties(mcp, server_key)
    if matches:
        prop = matches[0]
        if not had_duplicates and prop.value.to_python() == entry:
            return text
        indent = _line_indent(text, prop.key_token.start)
        newline = "\r\n" if "\r\n" in text else "\n"
        rendered = _format_value(entry, indent, newline)
        return text[:prop.value.start] + rendered + text[prop.value.end:]
    return _insert_property(text, root, mcp, server_key, entry, parent_indent)


def remove_mcp_entry(text: str, path: Path, server_key: str,
                     *, native_v2: bool = False) -> tuple[str, bool]:
    """Remove every occurrence of one server from a V1 or native V2 MCP map."""
    changed = False
    while True:
        root = parse_object(text, path)
        mcp_matches = _properties(root, "mcp")
        if len(mcp_matches) > 1:
            raise ConfigFormatError(
                f"{path}: duplicate top-level 'mcp' keys; remove the duplicate before retrying"
            )
        if not mcp_matches:
            return text, changed
        mcp = mcp_matches[0].value
        if mcp.kind != "object":
            raise ConfigFormatError(f"{path}: top-level 'mcp' must be an object")
        if native_v2:
            containers = _properties(mcp, "servers")
            if len(containers) > 1:
                raise ConfigFormatError(f"{path}: duplicate 'mcp.servers' keys; remove the duplicate before retrying")
            if not containers:
                return text, changed
            mcp = containers[0].value
            if mcp.kind != "object":
                raise ConfigFormatError(f"{path}: 'mcp.servers' must be an object")
        matches = _properties(mcp, server_key)
        if not matches:
            return text, changed
        text = _remove_property(text, mcp, mcp.properties.index(matches[-1]))
        changed = True


def atomic_write(path: Path, text: str, *, expected: FileSnapshot | None = None) -> None:
    """Replace an owned config atomically; reject edits made since its snapshot."""
    if len(text.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise ConfigFormatError(f"{path}: updated configuration exceeds the 8 MiB limit")
    original = read_snapshot(path, missing_ok=True)
    target = original.target
    if expected is not None and original != expected:
        raise ConfigFormatError(f"{path}: configuration changed outside klyk; retry without overwriting it")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(original.identity[3]) if original.identity else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        # Check after the temporary file has been flushed, immediately before
        # replacement. A concurrent editor's newer content must be retained.
        if read_snapshot(path, missing_ok=True) != original:
            raise ConfigFormatError(f"{path}: configuration changed outside klyk; retry without overwriting it")
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_exact(path: Path) -> str:
    """Read bounded config text without normalizing CRLF or following unsafe files."""
    return read_snapshot(path).text
