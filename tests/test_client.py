"""Exercise the shell client's actual stdio transport without desktop access."""

import subprocess
import sys
import time
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from klyk.client import KlykClient, KlykError
from klyk import client as client_module


class StdioClientTests(unittest.TestCase):
    """Use tiny Python servers to reproduce pipe stalls and lifecycle failures."""

    def client(self, source, timeout=10):
        """Return a client whose child implements only the required wire fixture."""
        return KlykClient([sys.executable, "-u", "-c", source], timeout=timeout)

    def test_chatty_stderr_and_buffered_stdout_do_not_stall(self):
        """More than a pipe of stderr and coalesced stdout must still respond."""
        source = '''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    sys.stderr.write("diagnostic" * 20000)
    sys.stderr.flush()
    response = {"id": request["id"], "result": {"method": request["method"]}}
    sys.stdout.write('[]\\n{"method":"notification"}\\n' + json.dumps(response) + '\\n')
    sys.stdout.flush()
'''
        with self.client(source) as client:
            self.assertEqual(client.call("inspect"), {"method": "tools/call"})
            self.assertLessEqual(len(client._stderr_tail), 8192)
            proc = client._proc
        self.assertIsNotNone(proc.poll())
        self.assertTrue(all(p.closed for p in (proc.stdin, proc.stdout, proc.stderr)))

    def test_large_request_drains_stderr_before_server_reads_input(self):
        """A blocked server writer cannot deadlock a request larger than its pipe."""
        source = '''
import json, sys
while True:
    sys.stderr.write("idle diagnostics" * 20000)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line)
    if "id" in request:
        print(json.dumps({"id": request["id"], "result": {"ok": True}}), flush=True)
'''
        with self.client(source) as client:
            self.assertEqual(client.call("type_text", {"text": "x" * 200000}), {"ok": True})

    def test_server_not_reading_large_request_respects_timeout(self):
        """The write phase itself must obey the per-call deadline."""
        source = '''
import json, sys, time
request = json.loads(sys.stdin.readline())
print(json.dumps({"id": request["id"], "result": {}}), flush=True)
sys.stdin.readline()
time.sleep(30)
'''
        with self.client(source) as client:
            client._timeout = 1.0
            started = time.monotonic()
            with self.assertRaisesRegex(KlykError, "timed out.*sending request"):
                client.call("type_text", {"text": "x" * 200000})
            self.assertLess(time.monotonic() - started, 2.5)

    def test_partial_stdout_and_stderr_respect_request_timeout(self):
        """Partial lines on either pipe cannot make readline wait indefinitely."""
        source = '''
import sys, time
sys.stdin.readline()
sys.stderr.write("partial diagnostic")
sys.stderr.flush()
sys.stdout.write('{"id":')
sys.stdout.flush()
time.sleep(30)
'''
        client = self.client(source, timeout=1.0)
        started = time.monotonic()
        with mock.patch("klyk.client.subprocess.Popen", wraps=subprocess.Popen) as launch:
            with self.assertRaisesRegex(KlykError, "timed out.*diagnostics were omitted"):
                client.start()
        self.assertLess(time.monotonic() - started, 6)
        self.assertIsNone(client._proc)
        self.assertEqual(launch.call_count, 1)

    def test_noise_cannot_reset_request_deadline(self):
        """Frequent unrelated notifications must not extend the caller's timeout."""
        source = '''
import sys, time
sys.stdin.readline()
while True:
    print('{"method":"notification"}', flush=True)
    time.sleep(0.01)
'''
        started = time.monotonic()
        with self.assertRaisesRegex(KlykError, "timed out"):
            self.client(source, timeout=1.0).start()
        self.assertLess(time.monotonic() - started, 6)

    def test_failed_handshake_reaps_child_and_closes_pipes(self):
        """An initialize rejection must clean up even when __enter__ fails."""
        source = '''
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"id": request["id"], "error": {"message": "rejected"}}), flush=True)
sys.stdin.read()
'''
        processes = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            """Retain the real child for cleanup assertions after start raises."""
            proc = original(*args, **kwargs)
            processes.append(proc)
            return proc

        with mock.patch("klyk.client.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(KlykError, "rejected"):
                with self.client(source):
                    self.fail("handshake should fail")
        proc = processes[0]
        self.assertIsNotNone(proc.poll())
        self.assertTrue(all(p.closed for p in (proc.stdin, proc.stdout, proc.stderr)))

    def test_invalid_timeout_is_rejected_before_launch(self):
        """Timeouts must be bounded positive durations rather than NaN or infinity."""
        for timeout in (0, -1, float("nan"), float("inf"), True, "1", None, 10 ** 400):
            with self.subTest(timeout=timeout), self.assertRaises(KlykError):
                self.client("", timeout=timeout)

    def test_unterminated_response_has_a_memory_limit(self):
        """A child flooding stdout cannot grow the response buffer indefinitely."""
        source = '''
import sys, time
sys.stdin.readline()
sys.stdout.write('x' * 200000)
sys.stdout.flush()
time.sleep(30)
'''
        with mock.patch("klyk.client._MAX_RESPONSE_BYTES", 1024):
            with self.assertRaisesRegex(KlykError, "response exceeds"):
                self.client(source).start()

    def test_transport_errors_do_not_echo_private_protocol_payloads(self):
        """Even plain typed text in SDK-shaped stderr must never enter an error."""
        source = '''
import sys
sys.stdin.readline()
print("ValidationError: request={'text':'synthetic-private-request'}", file=sys.stderr, flush=True)
'''
        with self.assertRaises(KlykError) as captured:
            self.client(source).start()
        self.assertNotIn("synthetic-private-request", str(captured.exception))
        self.assertIn("klyk doctor", str(captured.exception))

    def test_chunked_credentials_and_oversized_stderr_are_not_retained(self):
        """Scrub records after assembly and omit oversized tails as one record."""
        source = '''
import os, sys, time
sys.stdin.readline()
os.write(2, b'access_to')
time.sleep(0.02)
os.write(2, b'ken=synthetic-oauth-secret\\n')
os.write(2, b'password=' + b's' * 12000 + b'-synthetic-private-tail\\n')
'''
        client = self.client(source)
        with self.assertRaises(KlykError):
            client.start()
        self.assertNotIn(b"synthetic-oauth-secret", client._stderr_tail)
        self.assertNotIn(b"synthetic-private-tail", client._stderr_tail)
        self.assertIn(b"access_token=***", client._stderr_tail)
        self.assertIn(b"oversized log line omitted", client._stderr_tail)

    def test_permission_failure_returns_actionable_fixed_text(self):
        """Recognized startup categories retain useful guidance without raw details."""
        source = '''
import sys
sys.stdin.readline()
print("Accessibility permission is required: synthetic-private-details", file=sys.stderr, flush=True)
'''
        with self.assertRaises(KlykError) as captured:
            self.client(source).start()
        self.assertIn("Accessibility permission", str(captured.exception))
        self.assertNotIn("synthetic-private-details", str(captured.exception))

    def test_malformed_protocol_responses_raise_plain_errors(self):
        """Unexpected error/result shapes must fail cleanly instead of traceback."""
        for body in ('"error":[]', '"result":[]', '"error":{"message":null}'):
            source = f'''import json,sys
request=json.loads(sys.stdin.readline())
print('{{"id":'+str(request['id'])+',{body}}}',flush=True)
'''
            with self.subTest(body=body), self.assertRaisesRegex(KlykError, "malformed protocol"):
                self.client(source).start()

    def test_excessively_nested_protocol_json_raises_plain_error(self):
        """Decoder recursion failures become plain errors across interpreter limits."""
        source = '''import sys
sys.stdin.readline()
print('[' * 2000 + '0' + ']' * 2000,flush=True)
'''
        # CPython's accepted JSON depth varies by build. Exercise the decoder's
        # failure path deterministically while still reading the complete pipe record.
        with mock.patch("klyk.client.json.loads", side_effect=RecursionError("synthetic decoder limit")) as decoder, \
             self.assertRaisesRegex(KlykError, "excessively nested JSON") as captured:
            self.client(source).start()
        decoder.assert_called_once_with('[' * 2000 + '0' + ']' * 2000 + '\n')
        self.assertNotIn("synthetic decoder limit", str(captured.exception))

    def test_nonfinite_request_is_rejected_without_writing_to_server(self):
        """Do not send nonstandard JSON numbers into the protocol error path."""
        source = '''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if "id" in request:
        print(json.dumps({"id":request["id"],"result":{}}),flush=True)
'''
        with self.client(source) as client:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaisesRegex(KlykError, "finite numbers"):
                    client.call("fixture", {"value": value})
            self.assertEqual(client.call("fixture", {"value": 1}), {})

    def test_default_launch_ignores_workspace_and_inherited_import_roots(self):
        """An unrelated current directory must not shadow server or stdlib modules."""
        source = '''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if "id" in request:
        print(json.dumps({"id":request["id"],"result":{"loaded":"trusted"}}),flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            package = trusted / "klyk"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "mcp_server.py").write_text(source)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = root / "untrusted-code-ran"
            injected = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed'); raise RuntimeError('untrusted module')"
            (workspace / "json.py").write_text(injected)
            (workspace / "klyk").mkdir()
            (workspace / "klyk" / "__init__.py").write_text(injected)
            original = subprocess.Popen

            def launch(*args, **kwargs):
                """Place only the synthetic child in the adversarial workspace."""
                return original(*args, cwd=workspace, **kwargs)

            with mock.patch("klyk.client._REPO_ROOT", trusted), \
                 mock.patch.dict(os.environ, {"PYTHONPATH": str(workspace) + os.pathsep}), \
                 mock.patch("klyk.client.subprocess.Popen", side_effect=launch):
                with KlykClient() as client:
                    self.assertIn("-P", client._cmd)
                    self.assertEqual(client.call("fixture"), {"loaded": "trusted"})
            self.assertFalse(marker.exists())


class CaptureCacheTests(unittest.TestCase):
    """Keep materialized screenshot paths useful within the finite cache capacity."""

    def test_future_dated_entries_cannot_evict_new_response_images(self):
        """Clock changes or restored metadata must preserve every newly returned path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(20):
                path = root / f"prior-{index}.png"
                path.write_bytes(b"old")
                os.utime(path, (time.time() + 3600, time.time() + 3600))
            with mock.patch.object(client_module, "_CAPTURE_DIR", root):
                result = client_module.materialize_images({"content": [{"type": "image", "data": "eA=="} for _ in range(3)]})
            self.assertTrue(all(Path(item["saved_path"]).exists() for item in result["content"]))
            self.assertEqual(len(list(root.glob("*.png"))), 20)

    def test_dangling_link_and_directory_do_not_disable_file_eviction(self):
        """Unexpected entries are preserved without allowing real screenshots to grow."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "dangling.png"
            link.symlink_to(root / "missing")
            folder = root / "directory.png"
            folder.mkdir()
            with mock.patch.object(client_module, "_CAPTURE_DIR", root):
                for _ in range(30):
                    result = client_module.materialize_images({"content": [{"type": "image", "data": "eA=="}]})
                    self.assertTrue(Path(result["content"][0]["saved_path"]).exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(folder.is_dir())
            self.assertEqual(len([path for path in root.iterdir() if path.is_file()]), 20)

    def test_excess_response_images_keep_inline_data_and_report_limit(self):
        """No response may hand back deleted paths when it contains over twenty images."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(client_module, "_CAPTURE_DIR", root):
                result = client_module.materialize_images({"content": [{"type": "image", "data": "eA=="} for _ in range(23)]})
            for item in result["content"][:20]:
                self.assertTrue(Path(item["saved_path"]).exists())
                self.assertNotIn("data", item)
            for item in result["content"][20:]:
                self.assertEqual(item["data"], "eA==")
                self.assertNotIn("saved_path", item)
                self.assertIn("20-image", item["save_error"])
            self.assertEqual(len(list(root.iterdir())), 20)

    def test_failed_image_write_removes_only_its_new_partial_file(self):
        """A failed write must keep inline pixels without accumulating broken files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = tempfile.NamedTemporaryFile

            def fail_write(*args, **kwargs):
                """Use a real private temporary file whose write raises an I/O error."""
                handle = original(*args, **kwargs)
                handle.write = mock.Mock(side_effect=OSError("Disk write failed"))
                return handle

            with mock.patch.object(client_module, "_CAPTURE_DIR", root), \
                 mock.patch("klyk.client.tempfile.NamedTemporaryFile", side_effect=fail_write):
                result = client_module.materialize_images({"content": [{"type": "image", "data": "eA=="}]})
            self.assertEqual(result["content"][0]["data"], "eA==")
            self.assertIn("save_error", result["content"][0])
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
