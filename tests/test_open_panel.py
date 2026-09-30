"""Verify native Open/Go to Folder targeting using an inert AX tree and owned references."""

from collections import Counter
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


def open_namespace():
    """Model the proved native blank-field shape without importing or calling desktop APIs."""
    nodes = {
        1: {"role": "AXApplication", "children": [2], "focused": 5},
        2: {"role": "AXWindow", "parent": 1, "children": [3, 11]},
        3: {"role": "AXSheet", "parent": 2, "AXDescription": "open", "children": [4, 10]},
        4: {"role": "AXSheet", "parent": 3, "children": [6, 7, 5, 8]},
        5: {"role": "AXTextField", "parent": 4, "AXValue": "", "AXFocused": "true", "writable": True, "actions": ["AXConfirm"]},
        6: {"role": "AXStaticText", "parent": 4, "AXValue": "Go to Folder"},
        7: {"role": "AXButton", "parent": 4, "AXTitle": "Close"},
        8: {"role": "AXTable", "parent": 4, "children": []},
        9: {"role": "AXButton", "parent": 4, "AXTitle": "Go", "actions": ["AXPress"]},
        10: {"role": "AXButton", "parent": 3, "AXTitle": "Open"},
        11: {"role": "AXTextField", "parent": 2, "AXValue": "/document text", "AXFocused": "true", "writable": True},
    }
    references = Counter()
    values = {}
    writes = []
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now

    def sleep(seconds):
        """Advance only the fixture clock so closure timeouts require no real wait."""
        clock.now += seconds

    def own(pointer):
        """Account for a copied or retained reference exactly as the native caller must."""
        if pointer:
            references[pointer] += 1
        return pointer

    def attribute(element, name):
        """Expose only the fixture's parent, focus and complete child array references."""
        node = nodes[element]
        if name == b"AXParent":
            return own(node.get("parent", 0))
        if name == b"AXFocusedUIElement":
            return own(node.get("focused", 0))
        if name == b"AXChildren" and "children" in node:
            return own(1000 + element)
        return 0

    def text(element, name):
        """Keep absent, empty and full values distinct from coordinates or labels."""
        return nodes[element].get("role" if name == b"AXRole" else name.decode(), "")

    def value_pointer(value):
        """Create a fake scalar pointer whose decoding never touches CoreFoundation."""
        pointer = 10_000 + len(values)
        values[pointer] = value
        return pointer

    def multiple(element, attributes):
        """Copy the five walker attributes with the same owned-reference contract as AX."""
        result = []
        for name in attributes:
            if name == b"AXChildren":
                result.append(attribute(element, name))
            else:
                result.append(own(value_pointer(text(element, name))))
        return result

    def array_values(pointer):
        """Decode fake child and action arrays while returning borrowed element pointers."""
        if pointer >= 2000:
            return [value_pointer(value) for value in nodes[pointer - 2000].get("actions", [])]
        return nodes[pointer - 1000]["children"]

    def action_names(element, output):
        """Publish only explicitly supported actions before any simulated mutation."""
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = own(2000 + element.value)
        return 0

    def set_value(element, value):
        """Record a write only to the retained native field and update its complete readback."""
        writes.append(("value", element, value))
        nodes[element]["AXValue"] = value
        return True

    def perform(element, action):
        """Confirm only the retained target, then detach its chooser from the outer Open panel."""
        writes.append((action.decode(), element))
        nodes[3]["children"].remove(4)
        nodes[1]["focused"] = 0
        return True

    cf = MagicMock()
    cf.CFRetain.side_effect = lambda pointer: own(pointer.value)
    cf.CFRelease.side_effect = lambda pointer: references.subtract([pointer.value])
    cf.CFEqual.side_effect = lambda first, second: first.value == second.value
    cf.CFArrayGetCount.side_effect = lambda pointer: len(array_values(pointer.value))
    cf.CFArrayGetValueAtIndex.side_effect = lambda pointer, index: array_values(pointer.value)[index]
    api = MagicMock()
    api.AXUIElementCreateApplication.side_effect = lambda pid: own(1)
    api.AXUIElementCopyActionNames.side_effect = action_names
    ns = {"ctypes": ctypes, "time": clock, "_cf": cf, "_appserv": api,
          "_AX_MESSAGING_TIMEOUT_SECONDS": .05, "_AX_WALK_ATTRS": (b"AXRole", b"AXTitle", b"AXDescription", b"AXValue", b"AXChildren"),
          "_ax_str": text, "_ax_read_attr_ptr": attribute, "_ax_read_multi": multiple,
          "_cftype_to_str": lambda pointer: values[pointer],
          "_ax_matches_pid": lambda element, pid: nodes[element].get("pid", 123) == pid,
          "_ax_attr_is_settable": lambda element, name: nodes[element].get("writable", False),
          "_ax_set_value": set_value, "_ax_perform_action": perform}
    clock.sleep = sleep
    load_functions("computer.py", {"ax_navigate_open_panel"}, ns)
    return ns, nodes, writes, references


class OpenPanelTests(unittest.TestCase):
    """No document, incomplete chooser or unverified attempt can become an Open fallback."""

    def run_helper(self, ns, references):
        """Verify the result and balanced references even when native effects are uncertain."""
        try:
            return ns["ax_navigate_open_panel"](123, "/tmp/requested-file.txt")
        finally:
            self.assertFalse({pointer: count for pointer, count in references.items() if count})

    def test_empty_focused_native_field_is_written_and_confirmed(self):
        """The actual macOS field needs chooser identity, not a preexisting slash-prefixed value."""
        ns, nodes, writes, references = open_namespace()
        self.assertEqual(self.run_helper(ns, references), "/tmp/requested-file.txt")
        self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt"), ("AXConfirm", 5)])

    def test_placeholder_and_suggestions_can_change_after_the_exact_write(self):
        """Observed macOS placeholder removal cannot replace retained focus, ancestry or value proof."""
        for confirmation, marker in ((action, marker) for action in ("field", "chooser", "button") for marker in ("missing", "path")):
            with self.subTest(confirmation=confirmation, marker=marker):
                ns, nodes, writes, references = open_namespace()
                if confirmation != "field":
                    nodes[5]["actions"] = []
                    if confirmation == "chooser": nodes[4]["actions"] = ["AXConfirm"]
                    else: nodes[4]["children"].append(9)
                setter = ns["_ax_set_value"]

                def set_value(element, value):
                    """Replace or remove the prompt and populate suggestions when the field becomes nonempty."""
                    setter(element, value)
                    if marker == "missing": nodes[4]["children"].remove(6)
                    else: nodes[6]["AXValue"] = value
                    nodes[8]["children"] = [11] * 10_000
                    return True

                ns["_ax_set_value"] = set_value
                self.assertEqual(self.run_helper(ns, references), "/tmp/requested-file.txt")
                target = 5 if confirmation == "field" else 4 if confirmation == "chooser" else 9
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt"),
                                          ("AXPress" if confirmation == "button" else "AXConfirm", target)])

    def test_replacement_field_sheet_or_window_after_write_is_never_confirmed(self):
        """Matching roles, values and geometry cannot authorize a different retained field, sheet or window."""
        for replaced in (5, 4, 3, 2):
            with self.subTest(replaced=replaced):
                ns, nodes, writes, references = open_namespace()
                setter = ns["_ax_set_value"]

                def set_value(element, value):
                    """Substitute a same-owner, same-content object while keeping the old references alive."""
                    setter(element, value)
                    nodes[12] = {**nodes[replaced]}
                    if replaced == 5:
                        nodes[1]["focused"] = 12
                        nodes[4]["children"] = [12 if child == 5 else child for child in nodes[4]["children"]]
                    else:
                        nodes[{4: 5, 3: 4, 2: 3}[replaced]]["parent"] = 12
                        parent = {4: 3, 3: 2, 2: 1}[replaced]
                        nodes[parent]["children"] = [12 if child == replaced else child for child in nodes[parent]["children"]]
                    return True

                ns["_ax_set_value"] = set_value
                with self.assertRaisesRegex(RuntimeError, "target changed"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_chooser_confirm_and_scoped_go_button_are_supported(self):
        """An advertised native chooser action or its sole Go button needs no global Return."""
        for button in (False, True):
            with self.subTest(button=button):
                ns, nodes, writes, references = open_namespace()
                nodes[5]["actions"] = []
                if button:
                    nodes[4]["children"].append(9)
                else:
                    nodes[4]["actions"] = ["AXConfirm"]
                self.assertEqual(self.run_helper(ns, references), "/tmp/requested-file.txt")
                self.assertEqual(writes[-1], ("AXPress", 9) if button else ("AXConfirm", 4))

    def test_document_or_outer_panel_field_is_never_written(self):
        """Focused document text beginning with '/' is not evidence of a Go to Folder chooser."""
        for parent in (2, 3):
            with self.subTest(parent=parent):
                ns, nodes, writes, references = open_namespace()
                nodes[1]["focused"] = 11
                nodes[11]["parent"] = parent
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(writes, [])

    def test_foreign_chooser_descendants_cannot_authorize_a_path_write(self):
        """A foreign prompt, auxiliary control or Go button cannot become trusted chooser evidence."""
        for element in (6, 7, 8, 9):
            with self.subTest(element=element):
                ns, nodes, writes, references = open_namespace()
                if element == 9:
                    nodes[5]["actions"] = []
                    nodes[4]["children"].append(9)
                nodes[element]["pid"] = 456
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(writes, [])

    def test_selected_owner_changes_during_action_read_block_first_write(self):
        """Earlier scope identity cannot authorize a field write after native action metadata changes ownership."""
        for element in (2, 3, 4, 5, 9):
            with self.subTest(element=element):
                ns, nodes, writes, references = open_namespace()
                if element == 9:
                    nodes[5]["actions"] = []
                    nodes[4]["children"].append(9)
                read = ns["_appserv"].AXUIElementCopyActionNames.side_effect

                def actions(target, output):
                    """Change the retained control's PID after the initial scope walk but before any write."""
                    status = read(target, output)
                    nodes[element]["pid"] = 456
                    return status

                ns["_appserv"].AXUIElementCopyActionNames.side_effect = actions
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(writes, [])

    def test_foreign_owner_after_path_write_prevents_confirmation(self):
        """A changed retained field, chooser, outer panel or Go-button owner blocks confirmation."""
        for element in (2, 3, 4, 5, 9):
            with self.subTest(element=element):
                ns, nodes, writes, references = open_namespace()
                if element == 9:
                    nodes[5]["actions"] = []
                    nodes[4]["children"].append(9)
                setter = ns["_ax_set_value"]

                def set_value(target, value):
                    """Keep stable pointers and labels while transferring a synthetic native element's PID."""
                    setter(target, value)
                    nodes[element]["pid"] = 456
                    return True

                ns["_ax_set_value"] = set_value
                with self.assertRaisesRegex(RuntimeError, "target changed"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_confirmation_owner_is_rechecked_after_refreshed_action_read(self):
        """A last action-name read cannot transfer the selected native confirmation target unnoticed."""
        for element in (2, 3, 4, 5, 9):
            with self.subTest(element=element):
                ns, nodes, writes, references = open_namespace()
                if element != 5:
                    nodes[5]["actions"] = []
                    if element == 4:
                        nodes[4]["actions"] = ["AXConfirm"]
                    else:
                        nodes[4]["children"].append(9)
                read = ns["_appserv"].AXUIElementCopyActionNames.side_effect

                def actions(target, output):
                    """Change ownership after refreshed ancestry and full path-value verification."""
                    status = read(target, output)
                    if writes:
                        nodes[element]["pid"] = 456
                    return status

                ns["_appserv"].AXUIElementCopyActionNames.side_effect = actions
                with self.assertRaisesRegex(RuntimeError, "target changed owner"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_scope_changes_during_initial_action_read_prevent_first_write(self):
        """Action metadata cannot authorize writing into an unfocused or detached retained field."""
        for failure in ("focus", "role", "parent"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                read = ns["_appserv"].AXUIElementCopyActionNames.side_effect

                def actions(target, output):
                    """Invalidate the exact field immediately after reading its advertised confirmation."""
                    status = read(target, output)
                    if failure == "focus": nodes[5]["AXFocused"] = "false"
                    elif failure == "role": nodes[5]["role"] = "AXTextArea"
                    else: nodes[5]["parent"] = 2
                    return status

                ns["_appserv"].AXUIElementCopyActionNames.side_effect = actions
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(writes, [])

    def test_scope_and_readback_are_checked_after_confirmation_action_metadata(self):
        """Late action-name reads cannot change focus, ancestry or the full verified path unnoticed."""
        for failure in ("focus", "role", "parent", "value"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                read = ns["_appserv"].AXUIElementCopyActionNames.side_effect

                def actions(target, output):
                    """Change the selected field only after its first exact path write."""
                    status = read(target, output)
                    if writes:
                        if failure == "focus": nodes[5]["AXFocused"] = "false"
                        elif failure == "role": nodes[5]["role"] = "AXTextArea"
                        elif failure == "parent": nodes[5]["parent"] = 2
                        else: nodes[5]["AXValue"] = "/different-file.txt"
                    return status

                ns["_appserv"].AXUIElementCopyActionNames.side_effect = actions
                with self.assertRaisesRegex(RuntimeError, "target changed"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_foreign_outer_panel_child_cannot_prove_chooser_closure(self):
        """Unrelated child ownership is unknown closure evidence and cannot authorize final Open."""
        ns, nodes, writes, references = open_namespace()
        perform = ns["_ax_perform_action"]

        def action(element, name):
            """Detach the chooser but replace trusted outer-panel evidence with a foreign child."""
            perform(element, name)
            nodes[10]["pid"] = 456
            return True

        ns["_ax_perform_action"] = action
        with self.assertRaisesRegex(RuntimeError, "did not close"):
            self.run_helper(ns, references)
        self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt"), ("AXConfirm", 5)])

    def test_missing_or_ambiguous_chooser_evidence_refuses_before_write(self):
        """Foreign ownership, missing prompt, duplicate fields and truncated walks all fail closed."""
        for failure in ("pid", "prompt", "fields", "width", "cycle", "web", "focus", "readonly", "button"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                if failure == "pid": nodes[5]["pid"] = 456
                elif failure == "prompt": nodes[6]["AXValue"] = "Document content"
                elif failure == "fields": nodes[4]["children"].append(11)
                elif failure == "width": nodes[4]["children"] = [6] * 81
                elif failure == "cycle": nodes[4]["children"] = [4]
                elif failure == "web": nodes[7]["role"] = "AXWebArea"
                elif failure == "focus": nodes[5]["AXFocused"] = "false"
                elif failure == "readonly": nodes[5]["writable"] = False
                else:
                    nodes[12] = {**nodes[9]}
                    nodes[4]["children"] += [9, 12]
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(writes, [])

    def test_large_suggestion_table_does_not_expand_path_control_scope(self):
        """Suggestion labels are deliberately outside the path-control search, not silently truncated."""
        ns, nodes, writes, references = open_namespace()
        nodes[8]["children"] = [11] * 10_000
        self.assertEqual(self.run_helper(ns, references), "/tmp/requested-file.txt")

    def test_seen_reference_lifetime_prevents_recycled_address_skipping(self):
        """Released sibling arrays cannot recycle a seen label address into an unseen writable field or foreign node."""
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                ns, nodes, writes, references = open_namespace()
                nodes[4]['children'] = [6, 20, 21, 5]
                nodes[20] = {'role': 'AXGroup', 'parent': 4, 'children': [7]}
                nodes[21] = {'role': 'AXGroup', 'parent': 4, 'children': []}
                nodes[7] = {'role': 'AXStaticText', 'parent': 20, 'AXValue': 'Ordinary layout text'}
                arrays = {}
                allocated = []
                read = ns['_ax_read_multi']
                release = ns['_cf'].CFRelease.side_effect

                def multiple(element, attributes):
                    """A copied child array owns its borrowed elements; a later array can reuse only a dead address."""
                    if element == 21:
                        pointer = 7 if not references[7] else 12
                        allocated.append(pointer)
                        nodes[pointer] = {'role': 'AXTextField', 'parent': 21, 'writable': True,
                                          'AXValue': '', 'pid': 456 if foreign else 123}
                        nodes[21]['children'] = [pointer]
                    raw = read(element, attributes)
                    children = raw[4]
                    if children:
                        arrays[children] = list(nodes[element]['children'])
                        references.update(arrays[children])
                    return raw

                def release_array(pointer):
                    """Drop borrowed node lifetimes when the last copied array releases, preserving explicit retains."""
                    release(pointer)
                    if not references[pointer.value]:
                        for child in arrays.pop(pointer.value, []):
                            references.subtract([child])

                ns['_ax_read_multi'] = multiple
                ns['_cf'].CFRelease.side_effect = release_array
                self.assertIsNone(self.run_helper(ns, references))
                self.assertEqual(allocated, [12])
                self.assertEqual(writes, [])

    def test_unsupported_confirm_refuses_before_path_write(self):
        """No advertised action means no speculative path edit or global Return fallback."""
        ns, nodes, writes, references = open_namespace()
        nodes[5]["actions"] = []
        with self.assertRaisesRegex(RuntimeError, "no accessible confirmation action"):
            self.run_helper(ns, references)
        self.assertEqual(writes, [])

    def test_failed_or_oversized_action_metadata_never_authorizes_a_write(self):
        """A tempting first action cannot override an error or an incomplete bounded action list."""
        for failure in ("failed_read", "too_many_actions"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                if failure == "too_many_actions":
                    nodes[5]["actions"] = ["AXConfirm"] + ["AXRaise"] * 32
                else:
                    read = ns["_appserv"].AXUIElementCopyActionNames.side_effect

                    def actions(element, output):
                        """Return an owned value together with failure, as a native error may do."""
                        read(element, output)
                        return -25200

                    ns["_appserv"].AXUIElementCopyActionNames.side_effect = actions
                with self.assertRaisesRegex(RuntimeError, "no accessible confirmation action"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [])

    def test_changed_go_button_after_write_is_never_pressed(self):
        """Retaining an old button cannot authorize a detached, replaced or relabeled action."""
        for failure in ("detached", "replaced", "relabeled", "parent"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                nodes[5]["actions"] = []
                nodes[4]["children"].append(9)
                setter = ns["_ax_set_value"]

                def set_value(element, value):
                    """Change only the scoped confirmation control after the verified field write."""
                    setter(element, value)
                    if failure == "relabeled": nodes[9]["AXTitle"] = "Close"
                    elif failure == "parent": nodes[9]["parent"] = 2
                    else:
                        nodes[4]["children"].remove(9)
                        if failure == "replaced":
                            nodes[12] = {**nodes[9]}
                            nodes[4]["children"].append(12)
                    return True

                ns["_ax_set_value"] = set_value
                with self.assertRaisesRegex(RuntimeError, "target changed"):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_bad_write_readback_or_target_change_blocks_confirmation(self):
        """An accepted but changed or unreadable field value never falls through to Open."""
        for failure in ("write", "readback", "changed", "unreadable"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()
                setter = ns["_ax_set_value"]

                def set_value(element, value):
                    """Simulate a native uncertainty after the first attempted field mutation."""
                    setter(element, value)
                    if failure == "readback": nodes[5]["AXValue"] = "unexpected"
                    if failure == "changed": nodes[1]["focused"] = 11
                    return failure != "write"

                ns["_ax_set_value"] = set_value
                if failure == "unreadable":
                    read = ns["_ax_str"]

                    def text(element, attribute):
                        """Unreadable full values are inconclusive rather than an invented equality."""
                        if writes and element == 5 and attribute == b"AXValue": raise ValueError("unreadable")
                        return read(element, attribute)

                    ns["_ax_str"] = text
                with self.assertRaises(RuntimeError):
                    self.run_helper(ns, references)
                self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_failed_commit_or_unclosed_chooser_blocks_success(self):
        """A reported native action needs independent complete child evidence of chooser closure."""
        for failure in ("commit", "closure", "missing_children", "wide_children"):
            with self.subTest(failure=failure):
                ns, nodes, writes, references = open_namespace()

                def perform(element, action):
                    """Leave the chooser unresolved or make closure evidence unavailable."""
                    writes.append((action.decode(), element))
                    if failure == "missing_children": nodes[3].pop("children")
                    if failure == "wide_children": nodes[3]["children"] = [10] * 201
                    return failure != "commit"

                ns["_ax_perform_action"] = perform
                with self.assertRaises(RuntimeError):
                    self.run_helper(ns, references)
                self.assertEqual(len(writes), 2)

    def test_stop_after_write_never_confirms(self):
        """The worker checks its stop signal between the exact write and native confirmation."""
        ns, nodes, writes, references = open_namespace()

        def check_stop():
            """Interrupt after one simulated write without any keyboard or mouse input."""
            if writes:
                raise RuntimeError("cancelled")

        ns["_check_stop"] = check_stop
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_helper(ns, references)
        self.assertEqual(writes, [("value", 5, "/tmp/requested-file.txt")])

    def test_closure_child_reads_respect_the_shared_deadline(self):
        """Many individually slow native child reads cannot multiply the closure timeout."""
        ns, nodes, writes, references = open_namespace()
        perform = ns["_ax_perform_action"]
        read = ns["_ax_str"]

        def action(element, name):
            """Expose a large but permitted child list only after confirming the exact path."""
            perform(element, name)
            nodes[3]["children"] = [10] * 200
            return True

        def text(element, attribute):
            """Advance the inert clock to model a native read near its messaging timeout."""
            if len(writes) == 2:
                ns["time"].now += .05
            return read(element, attribute)

        ns["_ax_perform_action"] = action
        ns["_ax_str"] = text
        with self.assertRaisesRegex(RuntimeError, "did not close"):
            self.run_helper(ns, references)
        self.assertLess(ns["time"].now, 1.2)
        self.assertEqual(len(writes), 2)


if __name__ == "__main__":
    unittest.main()
