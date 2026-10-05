"""Inert native-view doubles verify that switches report saved access truth."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from klyk import connection_policy as policy, controls
from klyk.ui_thread import ui


class View:
    """Retain the visible state a native switch or text field would receive."""

    def __init__(self):
        self.state = None
        self.enabled = True
        self.text = ""

    def setState_(self, value):
        """Observe the confirmed switch position."""
        self.state = value

    def setEnabled_(self, value):
        """Observe whether the user can interact while one write is pending."""
        self.enabled = value

    def setStringValue_(self, value):
        """Observe status and banner text without creating native labels."""
        self.text = value

    def setToolTip_(self, value):
        """Observe the menu-bar access summary."""
        self.text = value


class Item:
    """Provide only the status-item button used by a controller refresh."""

    def __init__(self):
        self.view = View()

    def button(self):
        """Return an inert status button."""
        return self.view


class ControlsTests(unittest.TestCase):
    """Run asynchronous switch writes against an actual isolated policy file."""

    def setUp(self):
        """Keep file writes private and all view mutation inside controlled doubles."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "connections.json"
        location = patch.object(policy, "policy_path", return_value=self.path)
        location.start()
        self.addCleanup(location.stop)
        policy.initialize()
        self.panel = controls.Controls(("codex", "claude", "other"))
        self.panel.rows = {key: (View(), View()) for key in self.panel.keys}
        self.panel._footer = View()
        self.panel._quit = View()
        self.panel._item = Item()
        self.pending = []
        def dispatch_controls(callback, *, guarded):
            """Independent controls must remain usable when all computer access is Off."""
            self.assertFalse(guarded)
            self.pending.append(callback)
        dispatch = patch.object(ui, "dispatch", side_effect=dispatch_controls)
        dispatch.start()
        self.addCleanup(dispatch.stop)

    def complete_write(self):
        """Wait for one real writer, then explicitly service its inert UI callback."""
        deadline = time.monotonic() + 2
        while not self.pending and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.pending, "The asynchronous switch did not finish")
        self.pending.pop(0)()

    def test_startup_requires_a_built_interface(self):
        """A failed detached launch must not be reported as usable controls."""
        with patch.object(controls.sys, "platform", "darwin"), \
                patch.object(controls, "running", return_value=False), \
                patch.object(controls.subprocess, "Popen") as launch, \
                patch.object(controls, "_wait_ready", return_value=False):
            launch.return_value.poll.return_value = None
            with self.assertRaisesRegex(RuntimeError, "could not start"):
                controls.start_background()
            launch.return_value.terminate.assert_called_once()
            launch.return_value.wait.assert_called_once_with(timeout=3)

    def test_periodic_refresh_recovers_a_full_ui_queue(self):
        """A saved switch must leave Saving even when its immediate callback is dropped."""
        with patch.object(ui, "dispatch", return_value=False):
            self.panel.change("codex", True)
            deadline = time.monotonic() + 2
            while self.panel._completed.empty() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(self.panel._completed.empty())
        self.panel.refresh()
        self.assertFalse(self.panel.busy)
        self.assertEqual(self.panel.rows["codex"][0].state, 1)
        self.assertTrue(self.panel.rows["codex"][0].enabled)

    def test_saved_state_and_busy_are_distinct(self):
        """A pending change cannot imply access already changed or block another client."""
        policy.set_enabled("codex", True)
        self.panel.busy.add("codex")
        self.panel.refresh()
        codex, status = self.panel.rows["codex"]
        self.assertEqual(codex.state, 1)
        self.assertFalse(codex.enabled)
        self.assertEqual(status.text, "Saving…")
        self.assertTrue(self.panel.rows["claude"][0].enabled)
        self.assertFalse(self.panel._quit.enabled)

    def test_failed_write_reverts_to_truth_and_can_be_retried(self):
        """An I/O failure must not falsely show On or permanently disable every switch."""
        self.panel.rows["codex"][0].state = 1
        with patch.object(policy, "set_enabled", side_effect=PermissionError("private test")):
            self.panel.change("codex", True)
            self.complete_write()
        self.assertEqual(self.panel.rows["codex"][0].state, 0)
        self.assertTrue(self.panel.rows["codex"][0].enabled)
        self.assertIn("Could not save", self.panel._footer.text)
        self.panel.change("codex", True)
        self.complete_write()
        self.assertEqual(self.panel.rows["codex"][0].state, 1)
        self.assertNotIn("Could not save", self.panel._footer.text)

    def test_external_off_during_save_wins_visible_state(self):
        """The completed UI must reread the latest file rather than trust its requested value."""
        save = policy.set_enabled

        def concurrent_change(key, value):
            """Independently revoke immediately after the requested write succeeds."""
            save(key, value)
            save(key, False)

        with patch.object(policy, "set_enabled", side_effect=concurrent_change):
            self.panel.change("codex", True)
            self.complete_write()
        self.assertEqual(self.panel.rows["codex"][0].state, 0)
        self.assertEqual(self.panel.rows["codex"][1].text, "Off")
        self.assertIn("all access off", self.panel._item.view.text)

    def test_invalid_settings_show_off_and_a_plain_banner(self):
        """Unreadable state must not present a usable On switch or a blocking alert."""
        self.path.write_text("broken")
        self.panel.refresh()
        self.assertTrue(all(row[0].state == 0 and not row[0].enabled for row in self.panel.rows.values()))
        self.assertIn("could not be read", self.panel._footer.text)
        policy_state = policy._default()
        import json
        self.path.write_text(json.dumps(policy_state))
        self.panel.refresh()
        self.assertTrue(all(row[0].enabled for row in self.panel.rows.values()))

    def test_saved_enabled_environment_remains_visible_after_config_removal(self):
        """The owner can still turn off a saved permission whose client config disappeared."""
        policy.set_enabled("cursor", True)
        with patch.object(controls.clients, "current_entry", return_value=None):
            self.assertIn("cursor", controls.configured_environments())
        policy.set_enabled("cursor", False)
        with patch.object(controls.clients, "current_entry", return_value=None):
            self.assertNotIn("cursor", controls.configured_environments())

    def test_status_counts_access_outside_the_panels_original_rows(self):
        """A later external preference change cannot falsely report all access off."""
        policy.set_enabled("cursor", True)
        self.panel.refresh()
        self.assertIn("1 environment allowed", self.panel._item.view.text)
        self.assertNotIn("all access off", self.panel._item.view.text)
        policy.set_enabled("cursor", False)
        self.panel.refresh()
        self.assertIn("all access off", self.panel._item.view.text)

    def test_discovery_never_runs_opencode_version_detection(self):
        """Config discovery cannot delay a switch with an unrelated CLI subprocess."""
        checked = []

        def entry(client):
            """Record only noncore configuration reads, with Cursor configured."""
            checked.append(client.key)
            return {} if client.key == "cursor" else None

        with patch.object(controls.clients, "current_entry", side_effect=entry):
            self.assertEqual(controls.configured_environments(), ("codex", "claude", "opencode", "cursor", "other"))
        self.assertNotIn("opencode", checked)


if __name__ == "__main__":
    unittest.main()
