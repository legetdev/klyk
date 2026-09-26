"""Exercise privacy boundaries with real files, pipes, and malicious paths."""

import base64
import io
import logging
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from klyk import client, logs, ownership
from klyk.private_files import open_private
from server_support import load_server, payload


class PrivateFileTests(unittest.TestCase):
    """Private files must stay private independently of the user's umask."""

    def test_private_file_rejects_links_and_special_files_without_damage(self):
        """Do not truncate, chmod, or block on attacker-selected targets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "unrelated"
            target.write_bytes(b"preserve")
            target.chmod(0o644)
            link = root / "link"
            link.symlink_to(target)
            hardlink = root / "hardlink"
            os.link(target, hardlink)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            for path in (link, hardlink, fifo):
                with self.subTest(path=path), self.assertRaises(OSError):
                    with open_private(path, "wb"):
                        self.fail("unsafe file opened")
            self.assertEqual(target.read_bytes(), b"preserve")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_capture_creation_is_private_unique_and_preserves_existing_files(self):
        """Cache writes never reuse predicted names and tighten legacy directories."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captures = root / "captures"
            captures.mkdir(mode=0o755)
            unrelated = root / "unrelated"
            unrelated.write_bytes(b"preserve")
            (captures / f"inspect-{os.getpid()}-1.png").symlink_to(unrelated)
            previous_umask = os.umask(0)
            try:
                with patch.object(client, "_CAPTURE_DIR", captures):
                    paths = []
                    for _ in range(2):
                        result = client.materialize_images({"content": [{"type": "image",
                            "data": base64.b64encode(b"private pixels").decode()}]}, "inspect")
                        path = Path(result["content"][0]["saved_path"])
                        self.assertEqual(path.read_bytes(), b"private pixels")
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                        paths.append(path)
            finally:
                os.umask(previous_umask)
            self.assertNotEqual(*paths)
            self.assertEqual(stat.S_IMODE(captures.stat().st_mode), 0o700)
            self.assertEqual(unrelated.read_bytes(), b"preserve")

    def test_capture_directory_symlink_is_refused(self):
        """Do not chmod or populate a directory reached through a cache symlink."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o755)
            link = root / "captures"
            link.symlink_to(target, target_is_directory=True)
            with patch.object(client, "_CAPTURE_DIR", link):
                result = client.materialize_images({"content": [{"type": "image", "data": "eA=="}]})
            self.assertIn("save_error", result["content"][0])
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_owner_file_rejects_symlink_and_keeps_custom_parent_permissions(self):
        """A forged token cannot overwrite a victim or chmod a shared parent."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            target = root / "unrelated"
            target.write_text("preserve")
            owner = root / "owner"
            owner.symlink_to(target)
            with patch.object(ownership, "OWNER_PATH", owner):
                self.assertFalse(ownership.is_owner())
                with self.assertRaises(RuntimeError):
                    ownership.claim_ownership()
            self.assertEqual(target.read_text(), "preserve")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)


class DiagnosticPrivacyTests(unittest.TestCase):
    """Persist diagnostics without protocol contents or oversized records."""

    def test_rotation_and_sdk_logs_preserve_privacy(self):
        """Actual rotations stay 0600 and root/SDK debug records stay out."""
        logger = logging.getLogger("klyk")
        old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
        logger.handlers = []
        old_root_level = logging.getLogger().level
        previous_umask = os.umask(0)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "klyk.log"
                path.write_text("legacy diagnostic\n")
                backup = Path(f"{path}.1")
                backup.write_text("legacy backup\n")
                backup.chmod(0o644)
                logs.configure_logging(str(path))
                handler = logger.handlers[0]
                handler.maxBytes = 180
                logging.getLogger().setLevel(logging.DEBUG)
                logging.getLogger("mcp.server.lowlevel.server").debug("Received message: secret-sentinel")
                for _ in range(20):
                    logger.info("tool completed without retaining its inputs")
                handler.close()
                for item in Path(directory).iterdir():
                    self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
                    self.assertNotIn("secret-sentinel", item.read_text())
        finally:
            os.umask(previous_umask)
            logging.getLogger().setLevel(old_root_level)
            for handler in logger.handlers:
                handler.close()
            logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate

    def test_unsafe_log_path_degrades_without_writing(self):
        """A symlinked log must not prevent startup or change its target."""
        logger = logging.getLogger("klyk")
        old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
        logger.handlers = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "target"
                target.write_text("preserve")
                link = Path(directory) / "log"
                link.symlink_to(target)
                logs.configure_logging(str(link)).error("private exception")
                self.assertEqual(target.read_text(), "preserve")
                self.assertIsInstance(logger.handlers[0], logging.NullHandler)
        finally:
            logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate

    def test_oversized_stderr_drops_all_fragments_and_keeps_following_lines(self):
        """Bound actual reads and never expose a truncated credential's tail."""
        buffer = logs.LogBuffer()
        data = b"password=" + b"s" * 100000 + b"-private-tail\nnormal diagnostic\n"
        reader = logs.StderrReader(io.BytesIO(data), buffer)
        reader._thread.join(timeout=2)
        self.assertFalse(reader._thread.is_alive())
        self.assertEqual(list(buffer.app_errors), ["[oversized log line omitted]", "normal diagnostic"])

    def test_quoted_and_basic_auth_credentials_are_scrubbed(self):
        """Spaces, Python repr, and Basic auth must not leak residual values."""
        for value in ("password='private words'", 'token="private words"',
                      "{'password': 'private words'}", "Authorization: Basic private-base64"):
            with self.subTest(value=value):
                self.assertNotIn("private", logs._scrub(value))
                self.assertNotIn("words", logs._scrub(value))


class ToolLogPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real dispatcher with harmless, rejected requests."""

    async def test_schema_error_does_not_persist_request_values(self):
        """jsonschema error strings contain the input; never put them in logs."""
        server = load_server()
        with self.assertLogs(server.log, level="INFO") as captured:
            result = payload(await server.call_tool("type_text", {"app": "Fixture", "text": ["secret-sentinel"]}))
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-sentinel", "\n".join(captured.output))

    async def test_nonfinite_wait_parameters_are_rejected_before_observation(self):
        """Infinity must not bypass JSON Schema and turn a poll sleep into a hang."""
        server = load_server()
        for value in (float('inf'), float('-inf'), float('nan')):
            with self.subTest(value=value):
                result = payload(await server.call_tool('wait_for_visual', {
                    'app': 'Fixture', 'template_id': 'fixture', 'poll_interval': value}))
                self.assertFalse(result['ok'])
                self.assertIn('finite', result['error'])
        server.registry.get_by_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
