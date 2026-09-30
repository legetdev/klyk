"""Save-panel and window-target safety with a fully inert accessibility tree."""

import ctypes
import os
from types import SimpleNamespace
import time
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


def ax_namespace():
    """Build an AX fixture whose retained pointers and writes are only Python values."""
    target = os.path.join("/tmp", "Klyk exact target")
    nodes = {
        1: {"role": "AXApplication", "children": [2]},
        2: {"role": "AXSheet", "children": [3, 4, 5, 6, 7]},
        3: {"role": "AXStaticText", "AXValue": "Save As:"},
        4: {"role": "AXTextField", "AXTitle": "filename"},
        5: {"role": "AXButton", "AXTitle": "Save"},
        6: {"role": "AXOutline", "children": [8]},
        7: {"role": "AXPopUpButton", "AXDescription": "Where", "AXValue": "Favourite"},
        8: {"role": "AXRow", "AXURL": "file:///tmp/Klyk%20exact%20target", "AXSelected": "true", "children": [9]},
        9: {"role": "AXStaticText", "AXValue": "Favourite"},
    }
    writes = []
    api = MagicMock()
    api.AXUIElementCreateApplication.return_value = 1
    api.AXUIElementSetAttributeValue.side_effect = lambda element, attribute, value: writes.append((element.value, "select")) or 0
    cf = MagicMock()
    cf.CFRetain.side_effect = lambda pointer: pointer.value
    cf.CFArrayGetCount.side_effect = lambda pointer: len(nodes[pointer.value - 1000].get("children", []))
    cf.CFArrayGetValueAtIndex.side_effect = lambda pointer, index: nodes[pointer.value - 1000]["children"][index]

    def text(element, attribute):
        """Read only fixture attributes, leaving native APIs inaccessible."""
        if isinstance(element, ctypes.c_void_p):
            element = element.value
        key = "role" if attribute == b"AXRole" else attribute.decode()
        return nodes[element].get(key, "")

    ns = {"ctypes": ctypes, "os": os, "time": SimpleNamespace(monotonic=time.monotonic, sleep=lambda delay: None),
          "_appserv": api, "_cf": cf, "_AX_MESSAGING_TIMEOUT_SECONDS": .5, "_kCFBooleanTrue": True,
          "_check_stop": lambda: None, "_cfstr": lambda value: 50, "_ax_str": text,
          "_ax_read_attr_ptr": lambda element, attribute: element + 1000 if nodes[element].get("children") else 0,
          "_ax_set_value": lambda element, value: writes.append((element, value)) or True,
          "_ax_set_focused": lambda element: writes.append((element, "focus")) or True,
          "_ax_press": lambda element: writes.append((element, "press")) or True}
    return ns, nodes, writes, target


class PanelSafetyTests(unittest.TestCase):
    """A stale or ambiguous save panel must fail before any unrelated action is delivered."""

    def test_exact_url_accepts_renamed_favourite(self):
        """A favourite's full directory URL is canonical even when its label was renamed."""
        ns, nodes, writes, target = ax_namespace()
        load_functions("computer.py", {"ax_navigate_save_panel"}, ns)
        self.assertEqual(ns["ax_navigate_save_panel"](123, target), "Favourite")
        self.assertEqual(writes, [(8, "select")])

    def test_matching_name_without_url_does_not_select(self):
        """A duplicate basename cannot navigate a save panel to an unproved directory."""
        ns, nodes, writes, target = ax_namespace()
        nodes[8].pop("AXURL")
        nodes[9]["AXValue"] = os.path.basename(target)
        load_functions("computer.py", {"ax_navigate_save_panel"}, ns)
        self.assertIsNone(ns["ax_navigate_save_panel"](123, target))
        self.assertEqual(writes, [])

    def test_document_sidebar_is_never_used_as_save_panel(self):
        """The underlying document's matching folder cannot substitute for a sheet location."""
        ns, nodes, writes, target = ax_namespace()
        nodes[1]["children"].append(10)
        nodes[10] = {"role": "AXOutline", "children": [8]}
        nodes[6]["children"] = []
        load_functions("computer.py", {"ax_navigate_save_panel"}, ns)
        self.assertIsNone(ns["ax_navigate_save_panel"](123, target))
        self.assertEqual(writes, [])

    def test_failed_selection_and_empty_location_never_report_success(self):
        """A write failure or empty Where value blocks fallback after the attempted mutation."""
        for error in (True, False):
            with self.subTest(error=error):
                ns, nodes, writes, target = ax_namespace()
                if error:
                    ns["_appserv"].AXUIElementSetAttributeValue.side_effect = lambda element, attribute, value: writes.append((element.value, "select")) or -25200
                else:
                    nodes[7]["AXValue"] = ""
                load_functions("computer.py", {"ax_navigate_save_panel"}, ns)
                with self.assertRaisesRegex(RuntimeError, "save-panel location change"):
                    ns["ax_navigate_save_panel"](123, target)
                self.assertEqual(writes, [(8, "select")])

    def test_duplicate_filename_fields_are_rejected_before_write(self):
        """Two Save As fields cannot cause the first field to be edited speculatively."""
        ns, nodes, writes, target = ax_namespace()
        nodes[2]["children"] += [10, 11]
        nodes[10] = {"role": "AXStaticText", "AXValue": "Save As:"}
        nodes[11] = {"role": "AXTextField", "AXTitle": "other filename"}
        load_functions("computer.py", {"ax_set_save_filename", "ax_focus_save_field"}, ns)
        self.assertFalse(ns["ax_set_save_filename"](123, "report.txt"))
        self.assertFalse(ns["ax_focus_save_field"](123))
        self.assertEqual(writes, [])

    def test_filename_write_targets_one_field_once(self):
        """A failed AX filename write does not continue toward another field."""
        ns, nodes, writes, target = ax_namespace()
        ns["_ax_set_value"] = lambda element, value: writes.append((element, value)) or False
        load_functions("computer.py", {"ax_set_save_filename"}, ns)
        self.assertFalse(ns["ax_set_save_filename"](123, "report.txt"))
        self.assertEqual(writes, [(4, "report.txt")])

    def test_duplicate_panel_buttons_are_rejected_before_press(self):
        """Matching a title in two buttons cannot trigger a destructive arbitrary choice."""
        ns, nodes, writes, target = ax_namespace()
        nodes[2]["children"].append(10)
        nodes[10] = {"role": "AXButton", "AXTitle": "Save"}
        load_functions("computer.py", {"ax_press_panel_button"}, ns)
        self.assertIsNone(ns["ax_press_panel_button"](123, ("Save",)))
        self.assertEqual(writes, [])

    def test_save_helpers_propagate_stop_before_writes(self):
        """Cancellation is never converted to a successful or retryable panel mutation."""
        for name, args in (("ax_set_save_filename", (123, "report.txt")),
                           ("ax_focus_save_field", (123,)),
                           ("ax_press_panel_button", (123, ("Save",))),
                           ("ax_navigate_save_panel", (123, "/tmp/Klyk exact target"))):
            with self.subTest(name=name):
                ns, nodes, writes, target = ax_namespace()
                ns["_check_stop"] = MagicMock(side_effect=RuntimeError("cancelled"))
                load_functions("computer.py", {name}, ns)
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    ns[name](*args)
                self.assertEqual(writes, [])

    def test_cyclic_panel_tree_has_a_global_budget(self):
        """Cycles and wide repeated children cannot turn a cancelled request into endless work."""
        for name, args in (("ax_set_save_filename", (123, "report.txt")),
                           ("ax_press_panel_button", (123, ("Save",)))):
            with self.subTest(name=name):
                ns, nodes, writes, target = ax_namespace()
                nodes[2]["children"] = [2] * 80
                load_functions("computer.py", {name}, ns)
                started = time.monotonic()
                self.assertFalse(ns[name](*args))
                self.assertLess(time.monotonic() - started, .3)
                self.assertEqual(writes, [])


class NativeValueTests(unittest.TestCase):
    """Complete native text reads cannot falsely verify truncated or absent field values."""

    def test_utf16_read_preserves_nul_emoji_and_long_value(self):
        """Decode complete UTF-16 rather than a NUL-terminated prefix or a fixed small buffer."""
        text = "begin\0😀" + "x" * 3000 + "end"
        encoded = text.encode("utf-16-le")
        cf = MagicMock()
        cf.CFStringGetLength.return_value = len(encoded) // 2
        cf.CFStringGetCharacters.side_effect = lambda pointer, span, buffer: ctypes.memmove(buffer, encoded, len(encoded))
        ns = {"ctypes": ctypes, "_cf": cf, "CFRange": lambda start, length: (start, length)}
        load_functions("computer.py", {"_cfstr_to_py"}, ns)
        self.assertEqual(ns["_cfstr_to_py"](123), text)

    def test_oversized_native_value_is_not_empty_text(self):
        """A resource limit blocks equality checks rather than inventing an empty value."""
        cf = MagicMock()
        cf.CFStringGetLength.return_value = 100_001
        ns = {"ctypes": ctypes, "_cf": cf}
        load_functions("computer.py", {"_cfstr_to_py"}, ns)
        with self.assertRaisesRegex(ValueError, "safe text limit"):
            ns["_cfstr_to_py"](123)
        cf.CFStringGetCharacters.assert_not_called()

    def test_exact_string_creation_preserves_embedded_nul(self):
        """A field write never silently truncates caller text at its first NUL byte."""
        cf = MagicMock()
        cf.CFStringCreateWithBytes.return_value = 123
        ns = {"_cf": cf, "kCFStringEncodingUTF8": 0x08000100}
        load_functions("computer.py", {"_cfstr"}, ns)
        value = "before\0after😀"
        self.assertEqual(ns["_cfstr"](value), 123)
        cf.CFStringCreateWithBytes.assert_called_once_with(None, value.encode("utf-8"), len(value.encode("utf-8")), 0x08000100, False)

    def test_empty_write_requires_an_actual_value_read(self):
        """A missing AXValue cannot count as proof that an empty-string edit succeeded."""
        ns = {"ctypes": ctypes, "_cf": MagicMock(), "_check_stop": lambda: None,
              "_ax_element_at": lambda *args: 10, "_ax_matches_pid": lambda *args: True,
              "_ax_str_attr": lambda *args: "AXTextField", "_AX_TEXT_INPUT_ROLES": {"AXTextField"},
              "_ax_is_web_backed": lambda *args: False, "_ax_attr_is_settable": lambda *args: True,
              "_cfstr": lambda *args: 30, "_ax_set_attr_value": lambda *args: True,
              "_ax_read_attr_ptr": lambda *args: 0}
        load_functions("computer.py", {"ax_set_value_at"}, ns)
        result = ns["ax_set_value_at"](10, 20, "", expected_pid=123)
        self.assertTrue(result["attempted"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "unverified")

    def test_oversized_read_releases_owned_attribute(self):
        """Read failures cannot leak native values across repeated observations."""
        cf = MagicMock()
        ns = {"ctypes": ctypes, "_cf": cf, "_ax_read_attr_ptr": lambda *args: 123,
              "_cftype_to_str": MagicMock(side_effect=ValueError("oversized"))}
        load_functions("computer.py", {"_ax_str_attr"}, ns)
        with self.assertRaisesRegex(ValueError, "oversized"):
            ns["_ax_str_attr"](10, b"AXValue")
        self.assertEqual(cf.CFRelease.call_args.args[0].value, 123)


if __name__ == "__main__":
    unittest.main()
