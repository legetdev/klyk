"""Verify save results distinguish existing files from observed updates."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from server_support import load_server, payload


class SaveDialogTests(unittest.IsolatedAsyncioTestCase):
    """Run the real save handler against inert AX boundaries and temporary files."""

    def setUp(self):
        """Configure a ready panel and isolate native actions from the desktop."""
        self.server = load_server()
        session = SimpleNamespace(app="Fixture", pid=42, mode="autonomous")
        self.server._get_session = AsyncMock(return_value=(session, False))
        self.server._await_frontmost = AsyncMock(return_value=True)
        self.server.ownership.is_owner.return_value = True
        self.server.computer.ax_focus_save_field.return_value = True
        self.server.computer.ax_navigate_save_panel.return_value = "Temporary folder"
        self.server.computer.ax_set_save_filename.return_value = True
        self.server.computer.ax_read_alert.return_value = None

    async def save(self, path):
        """Exercise the production save handler and decode its result."""
        return payload(await self.server.call_tool("handle_system_dialog", {
            "app": "Fixture", "action": "save", "path": str(path),
        }))

    async def test_unchanged_existing_destination_is_not_saved(self):
        """An old destination must never count as proof that Save succeeded."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "existing.txt"
            path.write_text("old content")
            result = await self.save(path)
        self.assertFalse(result["ok"])
        self.assertFalse(result["saved"])
        self.assertIn("already existed", result["error"])
        self.server.computer.ax_read_alert.assert_any_call(42)

    async def test_fresh_and_updated_files_with_closed_panel_are_saved(self):
        """Creation and observed replacement keep the supported save workflow."""
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "saved.txt"
                if existing:
                    path.write_text("old")

                def press(pid, buttons):
                    """Model the destination write performed by pressing Save."""
                    if buttons == ("Save",):
                        path.write_text("new contents")
                    return True

                self.server.computer.ax_press_panel_button.side_effect = press
                result = await self.save(path)
                self.assertTrue(result["ok"])
                self.assertTrue(result["saved"])

    async def test_changed_destination_with_open_panel_is_not_saved(self):
        """A changed file cannot hide an unfinished save or confirmation panel."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "saved.txt"

            def press(pid, buttons):
                """Model a file appearing while the save panel still remains."""
                if buttons == ("Save",):
                    path.write_text("draft")
                return True

            def alert(pid, include_save_panel=False):
                """Expose the remaining Save As panel only in the final check."""
                return {"text": "Save As", "buttons": ["Save"], "save_panel": True} if include_save_panel else None

            self.server.computer.ax_press_panel_button.side_effect = press
            self.server.computer.ax_read_alert.side_effect = alert
            result = await self.save(path)
        self.assertFalse(result["saved"])
        self.assertIn("remained open", result["error"])


if __name__ == "__main__":
    unittest.main()
