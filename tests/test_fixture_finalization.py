"""Inert evidence finalization proves cleanup cannot publish a stale successful native report."""

import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from live_smoke import ROOT, finalize_report, stop_fixture_process


class FixtureFinalizationTests(unittest.TestCase):
    """Exercise independent cleanup and durable failure evidence without loading native desktop code."""

    def test_success_is_published_only_after_every_cleanup(self):
        """Each cleanup sees provisional failed disk evidence; only the final file becomes complete."""
        report={'completed':True,'checks':[{'name':'independent','passed':True}]}
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'report.json';steps=[]
            def cleanup():
                """Observe actual on-disk report state during cleanup."""
                self.assertFalse(json.loads(output.read_text())['completed']);steps.append('cleanup')
            finalize_report(report,output,(cleanup,cleanup))
            self.assertEqual(steps,['cleanup','cleanup'])
            self.assertTrue(json.loads(output.read_text())['completed'])

    def test_original_body_error_never_restores_success(self):
        """A client-context exit failure after provisional success remains failed after clean shutdown."""
        for error in ('AssertionError: native failure','KeyboardInterrupt',False,[],{}):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as directory:
                report={'completed':True,'error':error}
                output=Path(directory)/'report.json'
                finalize_report(report,output,())
                self.assertFalse(json.loads(output.read_text())['completed'])
                self.assertEqual(report['error'],error)
        with tempfile.TemporaryDirectory() as directory:
            report={'checks':[]};output=Path(directory)/'report.json'
            finalize_report(report,output,())
            self.assertFalse(report['completed'])

    def test_baseexceptions_fail_evidence_and_do_not_skip_later_cleanup(self):
        """Every BaseException is redacted, persisted, and re-raised after remaining cleanup runs."""
        for first in (ValueError('private-cleanup-sentinel'),KeyboardInterrupt('private-cleanup-sentinel'),
                      SystemExit('private-cleanup-sentinel'),BaseException('private-cleanup-sentinel')):
            with self.subTest(error=type(first).__name__),tempfile.TemporaryDirectory() as directory:
                output=Path(directory)/'report.json';report={'completed':True,'error':'Original body failure'};steps=[]
                def fail_first():
                    """Represent a native cleanup interruption without any native resource."""
                    steps.append('first');raise first
                def fail_second():
                    """A later independent cleanup can also fail without stopping the final cleanup."""
                    steps.append('second');raise RuntimeError('another-private-sentinel')
                with self.assertRaises(type(first)) as raised:
                    finalize_report(report,output,(fail_first,fail_second,lambda:steps.append('last')))
                self.assertIs(raised.exception,first)
                self.assertEqual(steps,['first','second','last'])
                retained=json.loads(output.read_text())
                self.assertFalse(retained['completed'])
                self.assertEqual(retained['error'],'Original body failure')
                self.assertEqual(retained['cleanup_errors'],[type(first).__name__,'RuntimeError'])
                self.assertNotIn('private',output.read_text())

    def test_initial_report_write_failure_still_cleans_and_persists_failure(self):
        """A report-storage error cannot skip cleanup or produce completed evidence on a later retry."""
        report={'completed':True};writes=[];steps=[];error=OSError('private-storage-sentinel')
        def write(body):
            """Fail the provisional write and capture the mandatory final attempt."""
            writes.append(json.loads(body))
            if len(writes)==1:raise error
        with self.assertRaises(OSError) as raised:
            finalize_report(report,SimpleNamespace(write_text=write),(lambda:steps.append('cleaned'),))
        self.assertIs(raised.exception,error)
        self.assertEqual(steps,['cleaned']);self.assertEqual(len(writes),2)
        self.assertFalse(writes[-1]['completed']);self.assertEqual(writes[-1]['report_error'],'OSError')
        self.assertNotIn('private',json.dumps(writes[-1]))

    def test_final_report_write_failure_keeps_provisional_disk_evidence_failed(self):
        """A failed final publication leaves the earlier durable incomplete report instead of stale success."""
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'report.json';calls=[];report={'completed':True}
            def write(body):
                """Keep the provisional disk file and refuse only its final replacement."""
                calls.append(body)
                if len(calls)==2:raise OSError('private-storage-sentinel')
                output.write_text(body)
            with self.assertRaises(OSError):
                finalize_report(report,SimpleNamespace(write_text=write),())
            self.assertFalse(json.loads(output.read_text())['completed'])
            self.assertFalse(report['completed']);self.assertEqual(report['report_error'],'OSError')

    def test_owned_process_waits_remain_bounded_after_kill(self):
        """Only owned live subprocesses are signaled and both graceful and post-kill waits have bounds."""
        events=[]
        def wait(*,timeout):
            """Simulate a slow owned process using a portable timeout exception."""
            events.append(('wait',timeout))
            if events.count(('wait',timeout))==1:raise subprocess.TimeoutExpired('owned fixture',timeout)
        process=SimpleNamespace(poll=lambda:None,terminate=lambda:events.append('terminate'),
                                wait=wait,kill=lambda:events.append('kill'))
        stop_fixture_process(process)
        self.assertEqual(events,['terminate',('wait',5),'kill',('wait',5)])
        events.clear();process.poll=lambda:0;process.wait=lambda **kw:events.append(('wait',kw['timeout']))
        stop_fixture_process(process);self.assertEqual(events,[('wait',5)])
        stop_fixture_process(None)

    def test_all_native_fixture_suites_finalize_after_baseexception(self):
        """Pure source checks keep native, background, and desktop evidence behind successful cleanup."""
        for filename in ('live_smoke.py','background_smoke.py','desktop_smoke.py'):
            with self.subTest(filename=filename):
                tree=ast.parse((ROOT/'tests'/filename).read_text())
                main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
                tries=[node for node in ast.walk(main) if isinstance(node,ast.Try) and any(
                    isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='finalize_report'
                    for final in node.finalbody for call in ast.walk(final))]
                self.assertEqual(len(tries),1)
                self.assertTrue(any(isinstance(handler.type,ast.Name) and handler.type.id=='BaseException'
                                    for handler in tries[0].handlers))


if __name__=='__main__':
    unittest.main()
