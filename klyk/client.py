#!/usr/bin/env python3
"""
Universal stdio client for the klyk MCP server — the no-MCP shell front door
(the universal-access front door for shell-driven agents).

Why this exists: MCP integration quality varies wildly per client harness.
Rather than depend on a given agent's MCP plumbing, this speaks the MCP
JSON-RPC handshake to klyk directly over stdio. Any agent that can run a
shell command (the `klyk-call` CLI) can drive the full klyk tool surface.

Two layers, one core:
  - KlykClient : reusable, persistent session (spawn klyk once, many calls).
  - main()      : the `klyk-call` CLI — `--list`, `--tool NAME --args JSON`, `--batch`.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import select
import subprocess
import sys
import time
from pathlib import Path

# This module lives inside the `klyk` package; the repo root is its grandparent.
# Putting the repo root on PYTHONPATH lets `-m klyk.mcp_server` resolve when
# running from a source checkout that isn't pip-installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_PROTOCOL_VERSION = "2024-11-05"


class KlykError(RuntimeError):
    """Raised when the klyk server returns a JSON-RPC error or fails to start."""


class KlykClient:
    """A persistent JSON-RPC stdio client for the klyk MCP server.

    Spawns the server once and keeps it alive across many tool calls, so a
    multi-step flow (look -> click -> verify) pays the launch cost only once.
    Use as a context manager so the server is always shut down cleanly.

    Not safe for concurrent use from multiple threads/tasks: it shares one
    stdio pipe, so callers must serialize requests.
    """

    def __init__(self, server_cmd=None, timeout=30.0):
        # Canonical launch is `python -m klyk.mcp_server` (matches the shipped
        # MCP config); never a bare file path, because the server uses relative
        # imports and only resolves as a package module.
        self._cmd = server_cmd or [sys.executable, "-m", "klyk.mcp_server"]
        if not math.isfinite(timeout) or timeout <= 0:
            raise KlykError("timeout must be a finite positive number")
        self._timeout = timeout
        self._proc = None
        self._next_id = 0
        self._stdout_buffer = bytearray()
        self._stderr_tail = b""
        self._stderr_open = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        """Launch the server and complete the MCP initialize handshake."""
        if self._proc is not None:
            raise KlykError("client already started")
        self._stdout_buffer.clear()
        self._stderr_tail = b""
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(_REPO_ROOT), env.get("PYTHONPATH", "")) if p
        )
        self._proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
        self._stderr_open = True
        try:
            os.set_blocking(self._proc.stdin.fileno(), False)
            init = self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "klyk-stdio-client", "version": "1.0"},
                },
            )
            # The server expects initialized before any tool call.
            self._notify("notifications/initialized")
            return init
        except BaseException:
            # __exit__ is never called if the context manager's __enter__ fails.
            self.close()
            raise

    def close(self):
        """Close every pipe and reap the child, including failed handshakes."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            self._proc = None
            self._stderr_open = False

    # -- public API --------------------------------------------------------
    def list_tools(self):
        """Return the server's full tool list (name, description, input schema)."""
        return self._request("tools/list", {}).get("tools", [])

    def call(self, tool_name, arguments=None):
        """Invoke a single klyk tool and return its result payload."""
        return self._request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )

    # -- JSON-RPC plumbing -------------------------------------------------
    def _send(self, message, deadline=None):
        """Send even large requests without blocking on a chatty server's pipes."""
        if not self._proc:
            raise KlykError("client not started")
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        try:
            pending = memoryview((json.dumps(message) + "\n").encode("utf-8"))
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._connection_error(
                        f"timed out after {self._timeout}s sending request to klyk"
                    )
                pipes = [self._proc.stdout]
                if self._stderr_open:
                    pipes.append(self._proc.stderr)
                ready, writable, _ = select.select(pipes, [self._proc.stdin], [], remaining)
                self._consume_output(ready)
                if writable:
                    try:
                        written = os.write(self._proc.stdin.fileno(), pending[:65536])
                    except BlockingIOError:
                        continue
                    pending = pending[written:]
        except OSError as exc:
            raise KlykError(f"could not send request to klyk: {exc}") from exc

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method, params):
        self._next_id += 1
        req_id = self._next_id
        deadline = time.monotonic() + self._timeout
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, deadline)
        return self._await_response(req_id, deadline)

    def _await_response(self, req_id, deadline):
        """Read stdout until the response with our id arrives (skip noise)."""
        while True:
            line = self._read_line(deadline)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore any non-JSON line the server may emit
            if not isinstance(msg, dict) or msg.get("id") != req_id:
                continue  # skip notifications / unrelated responses
            if "error" in msg:
                raise KlykError(msg["error"].get("message", str(msg["error"])))
            return msg.get("result", {})

    def _read_line(self, deadline):
        """Read complete stdout lines while draining stderr under one deadline.

        Binary reads avoid TextIOWrapper read-ahead hiding ready lines from
        select, and never block waiting for a newline after a partial write.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._connection_error(
                    f"timed out after {self._timeout}s waiting for klyk"
                )
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._stdout_buffer[:newline + 1])
                del self._stdout_buffer[:newline + 1]
                return line.decode("utf-8", errors="replace")
            pipes = [self._proc.stdout]
            if self._stderr_open:
                pipes.append(self._proc.stderr)
            ready, _, _ = select.select(pipes, [], [], remaining)
            self._consume_output(ready)

    def _consume_output(self, ready):
        """Drain ready pipes during both request writes and response reads."""
        # Drain stderr first so diagnostics emitted just before EOF survive.
        if self._proc.stderr in ready:
            chunk = os.read(self._proc.stderr.fileno(), 65536)
            self._stderr_open = bool(chunk)
            self._stderr_tail = (self._stderr_tail + chunk)[-8192:]
        if self._proc.stdout in ready:
            chunk = os.read(self._proc.stdout.fileno(), 65536)
            if not chunk:
                raise self._connection_error(
                    "klyk server closed the connection unexpectedly"
                )
            self._stdout_buffer.extend(chunk)

    def _connection_error(self, message):
        """Attach a bounded recent diagnostic tail to transport failures."""
        err = " | ".join(self._stderr_tail.decode("utf-8", errors="replace").splitlines()[-5:])
        return KlykError(message + (f"; stderr: {err}" if err else ""))


def _emit(obj):
    """Print a result as pretty JSON on stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --- vision for the shell front door -------------------------------------
# A tool result that contains a screenshot arrives as an MCP "image" content
# block carrying the PNG as inline base64. Printing that to stdout is the worst
# of both worlds: it floods the agent's context with tens of KB of base64 AND
# the agent still can't SEE it (stdout is text). Instead we write each image to
# a PNG on disk and hand back the path, so any agent reads it with its own image
# viewer (Claude Code's Read, Gemini's @path, etc.). One front-door change gives
# klyk-call the same observe→verify loop the native MCP transport has.
_CAPTURE_DIR = Path.home() / ".klyk" / "captures"
_CAPTURE_KEEP = 20          # bounded cache: keep the most recent N, evict older
_img_seq = 0                # process-local counter for unique capture filenames


def _prune_captures(keep: int = _CAPTURE_KEEP):
    """Bound the capture cache: delete all but the `keep` most recent PNGs.
    Best-effort — a failed unlink (e.g. a racing process) is non-fatal."""
    try:
        pngs = sorted(_CAPTURE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in pngs[:-keep] if keep else pngs:
        try:
            stale.unlink()
        except OSError:
            pass


def materialize_images(result, tool="image"):
    """Replace inline base64 image blocks in a tool result with on-disk PNG paths.

    Used by the `klyk-call` CLI front door — it runs on the same machine as the
    agent, so a file path is viewable. Native MCP clients render inline images
    directly and never call this.

    Mutates and returns `result`. A no-op when the result carries no image — so
    text-only tools (inspect's element list, read_text, screen_info, action
    results) print exactly as before, with zero added latency or files."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result

    global _img_seq
    saved = []
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "image" and item.get("data")):
            continue
        try:
            raw = base64.b64decode(item["data"])
        except Exception:
            continue  # leave a malformed block untouched rather than crash
        try:
            _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            _img_seq += 1
            safe = "".join(c if c.isalnum() else "-" for c in str(tool))[:24] or "image"
            path = _CAPTURE_DIR / f"{safe}-{os.getpid()}-{_img_seq}.png"
            path.write_bytes(raw)
        except OSError as e:
            item["save_error"] = str(e)  # surface, but don't lose the call
            continue
        # Swap the heavy inline payload for a viewable path reference.
        item.pop("data", None)
        item["saved_path"] = str(path)
        saved.append(str(path))

    if saved:
        _prune_captures()
        result["image_hint"] = (
            "Screenshot(s) written to disk — open the saved_path with your image "
            "reader to view (klyk-call returns a path, not inline pixels)."
        )
    return result


def main(argv=None):
    """`klyk-call` CLI: list tools, call one tool, or batch calls from stdin."""
    parser = argparse.ArgumentParser(
        prog="klyk-call",
        description="Universal stdio client for the klyk MCP server.",
    )
    parser.add_argument("--list", action="store_true", help="List all tools (name, description, parameter names) and exit")
    parser.add_argument("--schema", metavar="TOOL", help="Print the full JSON input schema for one tool and exit")
    parser.add_argument("--tool", help="Name of the tool to call")
    parser.add_argument("--args", help="Tool arguments as a JSON object string")
    parser.add_argument(
        "--batch",
        action="store_true",
        help='Read newline-delimited {"tool":..,"args":..} from stdin; run each over ONE session',
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-call timeout (seconds)")
    # Convenience shorthand for the most common tool (inspect).
    parser.add_argument("--app", help="Shorthand: sets args.app")
    parser.add_argument("--detail", choices=["full", "slim"], help="Shorthand: sets args.detail")
    opts = parser.parse_args(argv)

    def _parse_json(text, label):
        """Parse a JSON object argument, failing with a plain-language message."""
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KlykError(f"{label} is not valid JSON ({exc.msg})") from None
        if not isinstance(value, dict):
            raise KlykError(f"{label} must be a JSON object, e.g. '{{\"app\": \"Finder\"}}'")
        return value

    try:
        with KlykClient(timeout=opts.timeout) as client:
            if opts.list:
                # Lean overview: name, description, and parameter NAMES (required vs
                # optional) so agents never guess param names. Full types/enums are a
                # gated drill-down via --schema (keeps this discovery payload small).
                out = []
                for t in client.list_tools():
                    schema = t.get("inputSchema") or {}
                    props = list((schema.get("properties") or {}).keys())
                    required = schema.get("required") or []
                    out.append({
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "required": required,
                        "optional": [p for p in props if p not in required],
                        **({"anyOf": schema["anyOf"]} if "anyOf" in schema else {}),
                    })
                _emit(out)
                return 0

            if opts.schema:
                match = next((t for t in client.list_tools() if t["name"] == opts.schema), None)
                if match is None:
                    raise KlykError(f"unknown tool: {opts.schema} (run --list to see tools)")
                _emit({
                    "name": match["name"],
                    "description": match.get("description", ""),
                    "inputSchema": match.get("inputSchema", {}),
                })
                return 0

            if opts.batch:
                for raw in sys.stdin:
                    raw = raw.strip()
                    if not raw:
                        continue
                    spec = _parse_json(raw, "--batch line")
                    if "tool" not in spec:
                        raise KlykError('--batch line is missing the required "tool" field')
                    _emit(materialize_images(client.call(spec["tool"], spec.get("args", {})), spec["tool"]))
                return 0

            if not opts.tool:
                parser.error("one of --list, --schema, --tool, or --batch is required")

            args = _parse_json(opts.args, "--args") if opts.args else {}
            if opts.app is not None:
                args["app"] = opts.app
            if opts.detail is not None:
                args["detail"] = opts.detail
            _emit(materialize_images(client.call(opts.tool, args), opts.tool))
            return 0
    except KlykError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
