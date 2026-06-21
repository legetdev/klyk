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

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

_LOG_CHANNEL_CAP = 500

# Sensitive-value scrubbers. Each pattern's match is replaced with the
# leading key/label (group 1) plus `=***`. Patterns deliberately leave the
# *key* visible — the agent still sees "password=" so it can reason about
# the surrounding failure — and just hide the *value*.
_SCRUBBERS: list[tuple[re.Pattern, str]] = [
    # key=value / key: value (password, secret, token, api[_-]key, bearer, etc.)
    (
        re.compile(
            r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|'
            r'auth(?:orization)?|bearer)\s*[:=]\s*\S+'
        ),
        lambda m: f"{m.group(1)}=***",
    ),
    # `Authorization: Bearer <token>` HTTP headers — case-insensitive.
    (
        re.compile(r'(?i)\b(Bearer)\s+[A-Za-z0-9._\-+/=]{8,}'),
        lambda m: f"{m.group(1)} ***",
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
    """Apply every credential scrubber once. Order doesn't matter — patterns
    target disjoint shapes. Idempotent on already-scrubbed lines."""
    for pattern, repl in _SCRUBBERS:
        line = pattern.sub(repl, line)
    return line


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
        self._buffer.app_errors.append(_scrub(line.rstrip()))


class StderrReader:
    """
    Reads from a pipe in a background daemon thread and appends lines to a LogBuffer.
    Call `stop()` (or close the underlying pipe externally) to make the reader
    unblock and exit — without this, the thread sits on the pipe forever once the
    session closes, leaking the FD and a daemon thread per dead session.
    """

    def __init__(self, pipe, log_buffer: LogBuffer) -> None:
        self._pipe = pipe
        self._buffer = log_buffer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for raw in self._pipe:
                if self._stop.is_set():
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                line = raw.rstrip()
                if line:
                    self._buffer.app_errors.append(_scrub(line))
        except Exception:
            pass

    def stop(self) -> None:
        """Signal the reader to exit and close the pipe so the blocking read returns."""
        self._stop.set()
        try:
            self._pipe.close()
        except Exception:
            pass
