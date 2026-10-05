"""Status-only callbacks stay available after computer access is switched Off."""

import unittest
from unittest.mock import patch
from klyk import menubar, connection_policy, ownership


class ControlsStatusTests(unittest.TestCase):
    """Verify status scheduling without constructing any native UI or owner files."""

    def test_off_driver_hides_without_owner_probe(self):
        """An Off server must hide its eye even if its recorded owner is missing."""
        controller = menubar.MenuBarController()
        with patch.object(connection_policy, "enabled", return_value=False), \
                patch.object(ownership, "current_owner") as owner:
            self.assertFalse(controller._is_active_driver())
            owner.assert_not_called()

    def test_full_ui_queue_does_not_strand_status_refresh(self):
        """A dropped refresh must permit a later retry while all computer access is Off."""
        controller = menubar.MenuBarController()
        controller._installed = True
        with patch.object(menubar.ui, "dispatch", return_value=False) as dispatch:
            controller.request_refresh()
            self.assertFalse(controller._refresh_pending)
            controller.request_refresh()
            self.assertEqual(dispatch.call_count, 2)
            self.assertEqual(dispatch.call_args.kwargs, {"guarded": False})

    def test_unknown_or_other_ownership_hides_the_activity_eye(self):
        """An enabled but non-driving client cannot claim the activity indicator."""
        controller = menubar.MenuBarController()
        with patch.object(connection_policy, "enabled", return_value=True):
            for owner in (0, -1, menubar.os.getpid() + 1):
                with self.subTest(owner=owner), patch.object(ownership, "current_owner", return_value=owner):
                    self.assertFalse(controller._is_active_driver())
            with patch.object(ownership, "current_owner", return_value=menubar.os.getpid()):
                self.assertTrue(controller._is_active_driver())

    def test_full_ui_queue_does_not_claim_successful_status_install(self):
        """Native status construction may retry after a rejected queue insertion."""
        controller = menubar.MenuBarController()
        with patch.object(menubar.ui, "is_available", return_value=True), \
                patch.object(menubar.ui, "dispatch", return_value=False) as dispatch:
            self.assertFalse(controller.install_if_needed())
            self.assertFalse(controller._installed)
            self.assertFalse(controller._subscribed)
            self.assertEqual(dispatch.call_args.kwargs, {"guarded": False})


if __name__ == "__main__":
    unittest.main()
