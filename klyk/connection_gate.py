"""Generation-bound computer requests, native workers and final protocol replies.

Revocation stops subsequent work without disconnecting stdio. An OS operation
already submitted can finish; its result and every later stage are discarded.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

from . import connection_policy as policy

_current = contextvars.ContextVar("klyk_access_request", default=None)
_cleaning = contextvars.ContextVar("klyk_access_cleanup", default=False)
_active_lock = threading.RLock()
_active: set = set()
_responses: dict = {}
_MAX_REQUESTS = 256


@dataclass(eq=False)
class Request:
    """Retain the original generation through a queue, batch and native callbacks."""

    token: tuple[str, int]
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: object = None
    loop: object = None


def current_scope() -> Request | None:
    """Return the inherited request without creating a fresh permission token."""
    return _current.get()


def capture_scope() -> Request:
    """Capture permission before queuing work, preserving an existing request."""
    value = current_scope()
    if value is None:
        value = Request(policy.token())
    checkpoint(value)
    return value


def valid(value: Request) -> bool:
    """Reject cancelled work and a generation invalidated even by a quick Off/On."""
    return not value.cancelled.is_set() and policy.allows(value.token)


def checkpoint(value: Request | None = None, *, allow_cleanup: bool = False) -> None:
    """Deny new computer activity; only internal release cleanup may bypass Off."""
    if allow_cleanup and _cleaning.get():
        return
    value = current_scope() if value is None else value
    if value is None:
        policy.token()
    elif not valid(value):
        raise policy.AccessDisabled()


@contextmanager
def scope(value: Request):
    """Propagate an exact generation to a thread or main-thread callback."""
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


@contextmanager
def request():
    """Register a top-level request before it waits for any dispatcher lock."""
    inherited = capture_scope()
    value = Request(inherited.token, cancelled=inherited.cancelled,
                    task=asyncio.current_task(), loop=asyncio.get_running_loop())
    with _active_lock:
        if len(_active) >= _MAX_REQUESTS:
            raise policy.AccessDisabled("Klyk has too many pending requests; retry after they finish.")
        _active.add(value)
    try:
        with scope(value):
            checkpoint()
            yield value
    finally:
        with _active_lock:
            _active.discard(value)


@contextmanager
def cleanup():
    """Permit only internally registered ups and restoration of borrowed state."""
    token = _cleaning.set(True)
    try:
        yield
    finally:
        _cleaning.reset(token)


class GateExecutor(ThreadPoolExecutor):
    """Preserve request context and reject jobs revoked while in the worker queue."""

    def submit(self, function, /, *args, **kwargs):
        """Check both worker entry and completion, including unshielded native reads."""
        context = contextvars.copy_context()
        value = current_scope()

        def run():
            """Non-request transport helpers stay independent of computer permission."""
            if value is not None:
                checkpoint(value)
            result = function(*args, **kwargs)
            if value is not None:
                checkpoint(value)
            return result

        return super().submit(context.run, run)


class _NativeCall:
    """Guard a ctypes call while preserving its real restype/argtypes metadata."""

    def __init__(self, function):
        """Retain the real native function without invoking it."""
        object.__setattr__(self, "_function", function)

    def __call__(self, *args, **kwargs):
        """Authorize this native stage immediately before its invocation."""
        checkpoint(allow_cleanup=True)
        return self._function(*args, **kwargs)

    def __getattr__(self, name):
        """Expose the underlying ctypes signature and diagnostic metadata."""
        return getattr(self._function, name)

    def __setattr__(self, name, value):
        """Apply ctypes signature configuration to the actual native function."""
        setattr(self._function, name, value)


class _NativeLibrary:
    """Guard OS-facing library functions, leaving handles and CF release usable."""

    def __init__(self, library):
        """Keep one local library handle and cache guarded function wrappers."""
        object.__setattr__(self, "_library", library)
        object.__setattr__(self, "_calls", {})

    def __getattr__(self, name):
        """Wrap native functions lazily without replacing non-callable handles."""
        value = getattr(self._library, name)
        if not callable(value):
            return value
        calls = self._calls
        if name not in calls:
            calls[name] = _NativeCall(value)
        return calls[name]

    def __setattr__(self, name, value):
        """Preserve library configuration and invalidate a replaced function."""
        setattr(self._library, name, value)
        self._calls.pop(name, None)


def protect_library(library):
    """Wrap one loaded OS library once; loading it remains the lazy runtime's job."""
    if library is None or isinstance(library, _NativeLibrary):
        return library
    return _NativeLibrary(library)


def bind_response(request_id, value: Request) -> None:
    """Retain permission until the actual stdout writer handles the tool reply."""
    if request_id is None:
        return
    key = (type(request_id), request_id)
    with _active_lock:
        if key in _responses:
            # Duplicate JSON-RPC ids cannot safely identify either result.
            _responses[key] = None
        elif len(_responses) < _MAX_REQUESTS:
            _responses[key] = value
        else:
            raise policy.AccessDisabled("Klyk has too many pending replies; retry after they finish.")


def blocked_payload() -> dict:
    """Return a fixed banner with no captured pixels, AX values or clipboard data."""
    return {"ok": False, "blocked": "access_off", "message": str(policy.AccessDisabled())}


class GuardedWriter:
    """Check the original generation at the final blocking stdout write boundary."""

    def __init__(self, stream):
        """Retain the SDK's actual blocking stdout stream."""
        self._stream = stream

    def write(self, text: str) -> int:
        """Replace revoked tool replies before any frame is committed to the wire."""
        original_length = len(text)
        try:
            message = json.loads(text)
            key = (type(message.get("id")), message.get("id"))
        except (ValueError, TypeError, AttributeError):
            return self._stream.write(text)
        missing = object()
        with _active_lock:
            value = _responses.get(key, missing)
            if value is not None:
                _responses.pop(key, None)
            # A duplicate id remains poisoned for this transport. Removing it
            # after the first reply would let the other unidentified reply pass.
        if value is not missing and (value is None or not valid(value)):
            message = {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": json.dumps(blocked_payload())}],
                "isError": True,
            }}
            text = json.dumps(message) + "\n"
        self._stream.write(text)
        # Commit this complete frame here; a later SDK flush holds no old pixels.
        self._stream.flush()
        return original_length

    def flush(self):
        """Honor the transport's extra flush without retaining another frame."""
        return self._stream.flush()

    def __getattr__(self, name):
        """Delegate standard text-stream metadata to the SDK-owned stream."""
        return getattr(self._stream, name)


@asynccontextmanager
async def filtered_stdio(transport):
    """Keep the SDK's fd diversion and add a generation check to its real writer."""
    import anyio

    stdio = importlib.import_module("mcp.server.stdio")
    restore = None
    if hasattr(stdio, "_claim_fd") and hasattr(stdio, "_UnownedTextWrapper"):
        # SDK 2 claims stdout to isolate accidental native print() output.
        buffer, restore = stdio._claim_fd(1, sys.stdout, "wb", stdio._open_stdout_diversion)
        stream = stdio._UnownedTextWrapper(buffer, encoding="utf-8")
    else:
        # SDK 1 deliberately never closes the process's standard handles.
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    try:
        async with transport(stdout=anyio.wrap_file(GuardedWriter(stream))) as streams:
            yield streams
    finally:
        try:
            stream.detach()
        finally:
            if restore is not None:
                restore()


def start_watch(on_revoked):
    """Cancel blocked calls independently of the call lock, at most 50 ms per poll."""
    stopped = threading.Event()
    client = policy.current_client()

    def watch():
        """Do not wait for a native worker before updating other request cancellation."""
        previous = None
        retry_cleanup = False
        while not stopped.is_set():
            clients = policy.snapshot()["clients"]
            state = clients.get(client, {"enabled": False, "generation": 0})
            current = (state["enabled"], state["generation"])
            with _active_lock:
                # One bounded snapshot per tick keeps cancellation prompt even
                # when many requests are waiting for the dispatcher lock.
                revoked = [value for value in _active if value.cancelled.is_set()
                           or not clients.get(value.token[0], {}).get("enabled", False)
                           or clients[value.token[0]]["generation"] != value.token[1]]
            for value in revoked:
                value.cancelled.set()
                if value.task is not None and not value.task.done():
                    try:
                        value.loop.call_soon_threadsafe(value.task.cancel)
                    except RuntimeError:
                        pass  # Normal transport teardown can already have closed the loop.
            changed = previous is not None and previous[0] and current != previous
            if changed or revoked or retry_cleanup:
                try:
                    retry_cleanup = on_revoked() is False
                except Exception:
                    retry_cleanup = True
            previous = current
            stopped.wait(0.05)

    thread = threading.Thread(target=watch, name="klyk-access-watch", daemon=True)
    thread.start()

    def stop():
        """Stop the local watcher and briefly drain it without waiting on a native call."""
        stopped.set()
        if thread is not threading.current_thread():
            thread.join(timeout=0.3)

    return stop
