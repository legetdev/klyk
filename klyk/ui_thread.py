"""
AppKit main-thread coordinator.

NSWindow and NSStatusBar enforce -[NSThread isMainThread] — they must
live on the true process main thread (pthread thread 0). klyk's MCP
server is asyncio-based, so we restructure the entrypoint:

  - The process main thread bootstraps NSApp + activation policy and
    eventually calls NSApp.run() (blocking).
  - A worker thread runs asyncio.run(_async_main()) — the existing MCP
    stdio server lives there. Closing stdin still terminates the
    process (the worker exits, then signals NSApp.stop_ which unblocks
    the main thread).

Cross-thread dispatch is via a Python Queue drained 50× per second by
an NSTimer scheduled on the main thread. The drain cost is ~1 µs per
tick when the queue is empty — invisible at the 20 ms period. Heavier
schemes (performSelectorOnMainThread_ with arbitrary Python callables)
require wrapping the callable in an NSObject-compatible container; the
queue+timer pattern stays inside pure Python.

Design considerations:
- 5. Failure coupling: every dispatched block runs in its own try/except.
  One bad block cannot block another, and cannot block tool dispatch.
- 9. Hidden state: this module owns one main-loop drain timer and the
  process-level NSApp. start() is idempotent; shutdown is wired into
  the asyncio worker's finally clause so the main thread exits cleanly.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Callable

log = logging.getLogger("klyk.ui")


class UIThread:
    """
    Singleton coordinator. Use:

      ui.install_on_main_thread()    # call from process main thread, once
      ui.run_blocking()              # call from main thread, blocks until shutdown()
      ui.dispatch(fn)                # call from any thread, non-blocking
      ui.shutdown()                  # call from worker thread to unblock main
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed = False
        self._ready_event = threading.Event()
        self._available = False
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        # AppKit objects — set on the main thread only.
        self._app = None
        self._drain_timer = None
        self._shutdown_requested = False

    # --- main-thread API -------------------------------------------------

    def install_on_main_thread(self) -> bool:
        """
        Bootstrap NSApp + activation policy + drain timer. MUST be called
        from the process's pthread main thread; otherwise NSStatusBar /
        NSWindow operations will assert later. Returns True on success,
        False on non-darwin platforms or AppKit import failures (in
        which case dispatch() degrades to a no-op).
        """
        if sys.platform != "darwin":
            self._available = False
            self._ready_event.set()
            return False
        with self._lock:
            if self._installed:
                return self._available
            self._installed = True
        try:
            from AppKit import (
                NSApplication,
                NSApplicationActivationPolicyAccessory,
            )
            from Foundation import NSTimer, NSRunLoop
        except Exception as e:
            log.error("ui_thread: AppKit/Foundation import failed: %s", e)
            self._available = False
            self._ready_event.set()
            return False

        try:
            self._app = NSApplication.sharedApplication()
            # Accessory: no Dock icon, no app menu. klyk's only AppKit
            # surfaces are the status item and the per-app Dock badges.
            self._app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

            def _drain(_timer):
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        item()
                    except Exception as e:
                        log.warning("ui dispatch raised %s: %s", type(e).__name__, e)

            self._drain_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.020, True, _drain,
            )
            # Also tick during menu tracking — without this, queued UI
            # work pauses while the menubar dropdown is open.
            NSRunLoop.currentRunLoop().addTimer_forMode_(
                self._drain_timer, "NSEventTrackingRunLoopMode",
            )
            self._available = True
            self._ready_event.set()
            log.info("ui_thread: NSApp installed on main thread")
            return True
        except Exception as e:
            log.error("ui_thread: NSApp install failed: %s", e, exc_info=True)
            self._available = False
            self._ready_event.set()
            return False

    def run_blocking(self) -> None:
        """
        Run the AppKit event loop on the calling (main) thread. Blocks
        until shutdown() is called from another thread. No-op on non-
        darwin.
        """
        if not self._available:
            return
        try:
            self._app.run()
        except Exception as e:
            log.error("ui_thread: NSApp.run raised %s: %s", type(e).__name__, e)

    def shutdown(self) -> None:
        """
        Stop the main loop. Safe to call from any thread. Idempotent.
        Used by the asyncio worker's finally clause when stdio closes.
        """
        if not self._available:
            return
        if self._shutdown_requested:
            return
        self._shutdown_requested = True

        def _stop():
            try:
                self._app.stop_(None)
                # NSApp.stop_ only takes effect after the next event is
                # processed; post an empty one to force the loop to exit
                # immediately rather than waiting for user input.
                from AppKit import NSEvent
                from Foundation import NSPoint
                # NSEventTypeApplicationDefined = 15; subtype 0 is our own.
                ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    15, NSPoint(0, 0), 0, 0, 0, None, 0, 0, 0,
                )
                self._app.postEvent_atStart_(ev, True)
            except Exception as e:
                log.warning("ui_thread.shutdown _stop raised: %s", e)

        self.dispatch(_stop)

    # --- any-thread API --------------------------------------------------

    def is_available(self) -> bool:
        return self._available

    def wait_ready(self, timeout: float = 2.0) -> bool:
        return self._ready_event.wait(timeout) and self._available

    def dispatch(self, fn: Callable[[], None]) -> None:
        """
        Run fn on the AppKit main thread. Non-blocking. Silent no-op if
        the UI thread isn't installed — instrumentation must never break
        the calling tool (independent failure surfaces).
        """
        if not self._available:
            return
        try:
            self._queue.put_nowait(fn)
        except Exception:
            pass

    def dispatch_sync(self, fn: Callable[[], object], timeout: float = 2.0) -> object:
        """
        Run fn on the AppKit main thread, BLOCK until it returns or
        raises. Raises TimeoutError if the queue drain falls behind.
        """
        if not self._available:
            raise RuntimeError("ui_thread not available")
        slot: list = [None, None]
        done = threading.Event()

        def _wrapper() -> None:
            try:
                slot[0] = fn()
            except Exception as e:
                slot[1] = e
            finally:
                done.set()

        self.dispatch(_wrapper)
        if not done.wait(timeout):
            raise TimeoutError("ui_thread.dispatch_sync timeout")
        if slot[1] is not None:
            raise slot[1]
        return slot[0]


# Module-level singleton — single AppKit owner per process.
ui = UIThread()
