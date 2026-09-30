"""
Log capture for native and Electron app sessions.

Buffers are bounded (deque maxlen=500 per channel) so long-lived sessions
against chatty apps (Chrome, Electron renderer, anything spamming stderr)
cannot OOM the MCP server or balloon the verdict response. Older lines
are silently dropped — the most recent 500 are what the agent needs to
diagnose failure.

Stderr captured from launched apps is run through a small set of regex
scrubbers before being stored. The motivation is privacy hygiene: if a
target app emits a password, bearer token, API key, AWS access key, JWT,
or `Authorization: Bearer …` header to stderr (real-world failure mode
in misbehaving apps), it would otherwise persist in the in-memory buffer
and be echoed back in `verdict` / `get_logs` payloads — and from there
into LLM context. Scrubbing the values at capture time is cheap,
defensive, and avoids exfiltration of secrets that aren't klyk's to
hold.
"""

import copy
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import select
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque

from .private_files import open_private

_LOG_CHANNEL_CAP = 500
_LOG_LINE_CAP = 8192


class PrivateLogHandler(RotatingFileHandler):
    """Keep every newly opened or rotated diagnostic log owner-only."""

    def _open(self):
        """Secure the descriptor before the first byte, including after rotation."""
        return open_private(self.baseFilename, "a", encoding=self.encoding or "utf-8")

    def format(self, record):
        """Bound and scrub diagnostics without retaining exception tracebacks."""
        safe = copy.copy(record)
        if isinstance(safe.args, tuple):
            safe.args = tuple(type(value).__name__ if isinstance(value, BaseException) else value
                              for value in safe.args)
        elif isinstance(safe.args, dict):
            safe.args = {key: type(value).__name__ if isinstance(value, BaseException) else value
                         for key, value in safe.args.items()}
        message = safe.getMessage()
        if len(message) > _LOG_LINE_CAP or len(message.encode("utf-8", errors="replace")) > _LOG_LINE_CAP:
            message = "[oversized diagnostic record omitted]"
        message = _scrub(message)
        message = re.sub(
            r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]",
            lambda match: f"\\u{ord(match.group()):04x}", message,
        )
        if len(message.encode("utf-8", errors="replace")) > _LOG_LINE_CAP:
            message = "[oversized diagnostic record omitted]"
        safe.msg, safe.args = message, ()
        safe.exc_info = safe.exc_text = safe.stack_info = None
        return super().format(safe)

    def handleError(self, record):
        """A failed write must not replay the original private record on stderr."""
        pass


def configure_logging(path: str) -> logging.Logger:
    """Persist Klyk diagnostics only; SDK protocol payloads never enter this file."""
    logger = logging.getLogger("klyk")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for previous in logger.handlers[:]:
        if getattr(previous, "_klyk_owned", False):
            logger.removeHandler(previous)
            previous.close()
    try:
        # Existing rotations may predate owner-only creation. Never follow links.
        for backup in range(1, 6):
            candidate = Path(f"{path}.{backup}")
            if candidate.exists():
                with open_private(candidate):
                    pass
        handler = PrivateLogHandler(path, maxBytes=10 * 1024 * 1024,
                                    backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    except OSError:
        # Diagnostics must neither prevent connection nor fall back to leaking
        # private exception contents through the caller's stderr/root logger.
        handler = logging.NullHandler()
    handler._klyk_owned = True
    logger.addHandler(handler)
    return logger

# Sensitive-value scrubbers. Each pattern's match is replaced with the
# leading key/label (group 1) plus `=***`. Patterns deliberately leave the
# *key* visible — the agent still sees "password=" so it can reason about
# the surrounding failure — and just hide the *value*.
_CREDENTIAL_KEY = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key|"
    r"(?:access|refresh|id|session)[_-]?token|(?:client|consumer)[_-]?secret|"
    r"aws[_-]?(?:secret[_-]?)?access[_-]?key|private[_-]?key|auth(?:orization)?)"
)
_SCRUBBERS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    # JSON strings can contain whitespace and escaped quotes; redact the whole
    # value, including quoted keys that the plain key/value rule cannot match.
    (
        re.compile(
            r'(?i)("' + _CREDENTIAL_KEY + r'"\s*:\s*)"(?:\\.|[^"\\])*"'
        ),
        lambda m: f'{m.group(1)}"***"',
    ),
    # `Authorization: Bearer …` headers — before the generic key/value rule
    # so the generic rule does not consume only the word "Bearer" and leave
    # the actual token visible. Tokens are intentionally any non-whitespace
    # text, including short and punctuation-bearing values.
    (
        re.compile(r'(?i)\b(Bearer)\s+(\S+)'),
        lambda m: f"{m.group(1)} ***",
    ),
    # Quoted plaintext/Python-repr credentials and multi-word HTTP auth values.
    (
        re.compile(
            r'''(?i)(["']?''' + _CREDENTIAL_KEY + r'''["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
        ),
        lambda m: f"{m.group(1)}{m.group(2)[0]}***{m.group(2)[0]}",
    ),
    # Crashed writers and timeout diagnostics can end inside a quoted value.
    (
        re.compile(
            r'''(?i)(["']?''' + _CREDENTIAL_KEY + r'''["']?\s*[:=]\s*)("(?:\\.|[^"\\])*\\?|'(?:\\.|[^'\\])*\\?)$'''
        ),
        lambda m: f"{m.group(1)}{m.group(2)[0]}***{m.group(2)[0]}",
    ),
    (
        re.compile(r'(?i)\b(Authorization\s*:\s*)(?:Basic|Digest)\s+[^\r\n]+'),
        lambda m: f"{m.group(1)}***",
    ),
    (
        re.compile(r'(?i)\b((?:Cookie|Set-Cookie)\s*:\s*)[^\r\n]+'),
        lambda m: f"{m.group(1)}***",
    ),
    # key=value / key: value (password, secret, token, api[_-]key, etc.)
    (
        re.compile(
            r'(?i)\b(' + _CREDENTIAL_KEY + r')\s*[:=]\s*(?!(?:bearer)\b)\S+'
        ),
        lambda m: f"{m.group(1)}=***",
    ),
    # AWS access keys (AKIA*, ASIA*, plain 20-char IDs are the typical pattern).
    (
        re.compile(r'\b((?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16})\b'),
        lambda m: f"{m.group(1)[:4]}***",
    ),
    # JWT tokens — three base64url segments separated by dots.
    (
        re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
        lambda m: "***JWT***",
    ),
]


def _scrub(line: str) -> str:
    """Apply every credential scrubber once in deliberate order.

    Bearer headers run before the generic key/value rule so their token cannot
    remain visible. The operation is idempotent on already-scrubbed lines.
    """
    for pattern, repl in _SCRUBBERS:
        line = pattern.sub(repl, line)
    return line


class LogRecordBuffer:
    """Assemble bounded stderr records before scrubbing, including split credentials."""

    def __init__(self) -> None:
        """Retain at most one 8 KiB partial record between byte reads."""
        self._pending = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes) -> list[str]:
        """Return complete safe lines; discard every fragment of oversized records."""
        lines = []
        fragments = chunk.split(b"\n")
        for index, fragment in enumerate(fragments):
            complete = index < len(fragments) - 1
            if not self._discarding:
                if len(self._pending) + len(fragment) > _LOG_LINE_CAP:
                    self._pending.clear()
                    self._discarding = True
                    lines.append("[oversized log line omitted]")
                else:
                    self._pending.extend(fragment)
            if complete:
                if not self._discarding:
                    line = self.partial()
                    if line:
                        lines.append(line)
                self._pending.clear()
                self._discarding = False
        return lines

    def partial(self) -> str:
        """Return a scrubbed final/timeout diagnostic without exposing discarded tails."""
        return _scrub(self._pending.decode("utf-8", errors="replace").rstrip())


def _new_buffer() -> Deque[str]:
    return deque(maxlen=_LOG_CHANNEL_CAP)


@dataclass
class LogBuffer:
    console_errors: Deque[str] = field(default_factory=_new_buffer)
    network_failures: Deque[str] = field(default_factory=_new_buffer)
    app_errors: Deque[str] = field(default_factory=_new_buffer)

    def to_dict(self, max_chars: int = 0) -> dict:
        # Convert to list at read time so JSON serialization stays trivial.
        channels = {
            "console_errors": list(self.console_errors),
            "network_failures": list(self.network_failures),
            "app_errors": list(self.app_errors),
        }
        truncated = False
        # The 500-line-per-channel cap bounds memory, but a chatty app with long
        # lines (Finder, Electron) can still emit a payload that blows the MCP
        # token budget. When a char budget is given, drop the OLDEST lines —
        # chattiest channel first — until the total fits, keeping the most-recent
        # (most diagnostic) lines.
        if max_chars and max_chars > 0:
            total = sum(len(s) for ch in channels.values() for s in ch)
            if total > max_chars:
                truncated = True
                for key in ("app_errors", "network_failures", "console_errors"):
                    while total > max_chars and channels[key]:
                        total -= len(channels[key].pop(0))
        out: dict = {**channels, "_capped_at": _LOG_CHANNEL_CAP}
        if truncated:
            out["_truncated"] = True
            out["_note"] = (
                "Oldest log lines were dropped to fit the response size budget; "
                "the most-recent lines are retained."
            )
        return out


class NativeLogCapture:
    """Captures stderr lines from a process launched by Klyk."""

    def __init__(self, pid: int):
        self._pid = pid
        self._buffer = LogBuffer()

    @property
    def buffer(self) -> LogBuffer:
        return self._buffer

    def append_stderr(self, line: str) -> None:
        # Drop oversize records in full: keeping their tail can leak a credential
        # whose identifying key was discarded at the truncation boundary.
        self._buffer.app_errors.append(
            "[oversized log line omitted]"
            if len(line) > _LOG_LINE_CAP or len(line.encode("utf-8", errors="replace")) > _LOG_LINE_CAP
            else _scrub(line.rstrip())
        )


class StderrReader:
    """
    Reads from a pipe in a background daemon thread and appends lines to a LogBuffer.
    Real pipe reads use nonblocking descriptors and short polling, so stop never
    waits for the app or an inherited writer descriptor to reach EOF.
    """

    def __init__(self, pipe, log_buffer: LogBuffer) -> None:
        self._pipe = pipe
        self._buffer = log_buffer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Read bounded records and discard all fragments of an oversized line."""
        try:
            try:
                fd = self._pipe.fileno()
            except (AttributeError, OSError):
                fd = None  # In-memory fixture streams have no OS descriptor.
            if fd is not None:
                os.set_blocking(fd, False)
            records = LogRecordBuffer()
            while not self._stop.is_set():
                if fd is None:
                    raw = self._pipe.readline(_LOG_LINE_CAP + 1)
                else:
                    ready, _, _ = select.select([fd], [], [], 0.05)
                    if not ready:
                        continue
                    try:
                        raw = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                if not raw:
                    final = records.partial()
                    if final:
                        self._buffer.app_errors.append(final)
                    break
                if isinstance(raw, str):
                    raw = raw.encode("utf-8", errors="replace")
                self._buffer.app_errors.extend(records.feed(raw))
        except Exception:
            pass
        finally:
            try:
                self._pipe.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Stop under a short bound without acquiring a blocked buffered-reader lock."""
        self._stop.set()
        self._thread.join(timeout=0.3)
