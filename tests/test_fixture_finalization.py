"""Inert evidence finalization proves cleanup cannot publish a stale successful native report."""

import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from live_smoke import ROOT, finalize_report, fixture_clipboard_items, stop_fixture_process


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


class FixtureClipboardCallerTests(unittest.TestCase):
    """Execute actual fixture caller snippets with opaque items and no AppKit clipboard access."""

    def _caller(self, filename, snapshot):
        """Load only the actual capture, unknown-state guard, and restore expression from each suite."""
        tree=ast.parse((ROOT/'tests'/filename).read_text())
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        assignment=next(node for node in ast.walk(main) if isinstance(node,ast.Assign) and any(
            isinstance(target,ast.Name) and target.id=='clipboard' for target in node.targets))
        guard=next(node for node in ast.walk(main) if isinstance(node,ast.If)
                   and isinstance(node.test,ast.Compare) and isinstance(node.test.left,ast.Name)
                   and node.test.left.id=='clipboard' and any(isinstance(operation,ast.Is) for operation in node.test.ops))
        self.assertLess(assignment.lineno,guard.lineno)
        restored=[]
        def restore(items):
            """A native writeObjects caller requires item objects, never the snapshot's generation tuple."""
            self.assertIsInstance(items,list);restored.append(items)
        namespace={'computer':SimpleNamespace(_snapshot_pasteboard=lambda:snapshot,_restore_pasteboard=restore),
                   'fixture_clipboard_items':fixture_clipboard_items}
        snippet=ast.fix_missing_locations(ast.Module(body=[assignment,guard],type_ignores=[]))
        restore_expression=next(node for node in ast.walk(main) if isinstance(node,ast.Call)
                                and isinstance(node.func,ast.Attribute) and node.func.attr=='_restore_pasteboard')
        return snippet,ast.fix_missing_locations(ast.Expression(restore_expression)),namespace,restored

    def test_actual_native_and_desktop_callers_restore_only_typed_items(self):
        """A nonempty or legitimately empty captured item list survives both real callers without its count."""
        for filename in ('live_smoke.py','desktop_smoke.py'):
            for items in ([],[object(),object()]):
                with self.subTest(filename=filename,empty=not items):
                    snippet,restore,namespace,restored=self._caller(filename,(items,7))
                    exec(compile(snippet,'<fixture capture caller>','exec'),namespace)
                    eval(compile(restore,'<fixture restore caller>','eval'),namespace)
                    self.assertIs(namespace['clipboard'],items)
                    self.assertEqual(len(restored),1);self.assertIs(restored[0],items)

    def test_unknown_or_malformed_capture_stops_both_callers_before_restore(self):
        """An unknown clipboard never becomes an empty list or a destructive clear in either fixture."""
        snapshots=(None,[],[[],7],([object()],None),([object()],'7'),(None,7),([],True),([],7,'extra'))
        for filename in ('live_smoke.py','desktop_smoke.py'):
            for snapshot in snapshots:
                with self.subTest(filename=filename,shape=type(snapshot).__name__):
                    snippet,restore,namespace,restored=self._caller(filename,snapshot)
                    with self.assertRaisesRegex(RuntimeError,'could not be preserved'):
                        exec(compile(snippet,'<fixture capture caller>','exec'),namespace)
                    self.assertIsNone(namespace['clipboard']);self.assertEqual(restored,[])

    def test_desktop_cleanup_skips_unknown_clipboard_in_actual_finalizer_callback(self):
        """The cleanup callback remains a no-op when startup rejected the clipboard snapshot."""
        tree=ast.parse((ROOT/'tests/desktop_smoke.py').read_text());restored=[]
        callback=next(node for node in ast.walk(tree) if isinstance(node,ast.Lambda) and any(
            isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr=='_restore_pasteboard'
            for call in ast.walk(node)))
        expression=ast.fix_missing_locations(ast.Expression(callback))
        function=eval(compile(expression,'<fixture finalizer callback>','eval'),
                      {'computer':SimpleNamespace(_restore_pasteboard=restored.append),'clipboard':None})
        function();self.assertEqual(restored,[])


if __name__=='__main__':
    unittest.main()
