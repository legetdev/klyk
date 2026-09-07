"""Exercise the shell client's actual stdio transport without desktop access."""

import subprocess
import sys
import time
import unittest
from unittest import mock

from klyk.client import KlykClient, KlykError


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
            with self.assertRaisesRegex(KlykError, "timed out.*partial diagnostic"):
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
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(KlykError):
                self.client("", timeout=timeout)


if __name__ == "__main__":
    unittest.main()
