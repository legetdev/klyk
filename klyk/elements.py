"""Stable element numbers for one app session's observed accessibility elements.

Each observation (inspect) hands the table retained accessibility references. An
element that is the same native object as one seen before keeps its number, so
"element 12" stays the Save button across observations. Numbers never repeat in
a session. The table holds at most `capacity` references; the least recently
observed are released first, and a lookup miss names this table so the agent
knows to observe again.

Native reference handling is injected (retain/release/equal/hash), which keeps
this module importable and testable without macOS.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

CAPACITY = 400


@dataclass
class Entry:
    """One tracked element: its owned native reference and last observed facts."""

    number: int
    ref: int
    pid: int
    window_id: int | None
    role: str = ""
    label: str = ""
    observed: int = 0
    extra: dict = field(default_factory=dict)


class ElementTable:
    """Bounded mapping from element numbers to owned accessibility references."""

    def __init__(self, release, equal, hash_value, capacity: int = CAPACITY):
        self._release = release
        self._equal = equal
        self._hash = hash_value
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[int, Entry] = OrderedDict()
        self._buckets: dict[int, list[int]] = {}
        self._next = 1
        self._observation = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def adopt(self, pid: int, window_id: int | None, elements: list[dict]) -> None:
        """Take ownership of each element's `_ref` and write its stable `element` number.

        Every dict that carries `_ref` is consumed: the key is removed and the
        reference is either stored or released here, never leaked to the caller.
        """
        with self._lock:
            self._observation += 1
            for element in elements:
                ref = element.pop("_ref", None)
                if not ref:
                    continue
                try:
                    bucket_key = int(self._hash(ref))
                except Exception:
                    bucket_key = 0
                number = None
                for candidate in self._buckets.get(bucket_key, ()):
                    entry = self._entries.get(candidate)
                    if entry is not None and entry.pid == pid and self._safe_equal(entry.ref, ref):
                        number = candidate
                        break
                if number is not None:
                    self._release(ref)  # Keep the original reference; the new one is a duplicate.
                    entry = self._entries[number]
                    self._entries.move_to_end(number)
                else:
                    number = self._next
                    self._next += 1
                    entry = Entry(number, ref, pid, window_id)
                    self._entries[number] = entry
                    self._buckets.setdefault(bucket_key, []).append(number)
                entry.window_id = window_id
                entry.role = str(element.get("role") or "")
                entry.label = str(element.get("label") or element.get("value") or "")
                entry.observed = self._observation
                element["element"] = number
            self._evict()

    def _safe_equal(self, left: int, right: int) -> bool:
        try:
            return bool(self._equal(left, right))
        except Exception:
            return False

    def _evict(self) -> None:
        while len(self._entries) > self._capacity:
            number, entry = self._entries.popitem(last=False)
            self._drop_bucket(number)
            self._release(entry.ref)

    def _drop_bucket(self, number: int) -> None:
        for key, numbers in list(self._buckets.items()):
            if number in numbers:
                numbers.remove(number)
                if not numbers:
                    del self._buckets[key]
                return

    def get(self, number: int) -> Entry | None:
        """Return the tracked element, or None when it was never seen or was evicted."""
        with self._lock:
            return self._entries.get(number)

    def latest_number(self) -> int:
        """Highest number issued so far (0 when none)."""
        with self._lock:
            return self._next - 1

    def clear(self) -> None:
        """Release every owned reference; called when the session closes."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._buckets.clear()
        for entry in entries:
            try:
                self._release(entry.ref)
            except Exception:
                pass


def missing_message(number: int, app: str, latest: int) -> str:
    """Explain a lookup miss and name the cache, so the agent knows the next step."""
    if number < 1 or number > latest:
        return (f"Element {number} was never listed for {app}. Use an element number from "
                "the latest inspect of this app, or call inspect.")
    return (f"Element {number} is no longer tracked for {app} (the element table keeps the "
            f"{CAPACITY} most recently observed elements). Call inspect again.")
