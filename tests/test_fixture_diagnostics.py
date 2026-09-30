"""Inert native adapters verify that failed-fixture diagnostics remain bounded and read-only."""

from collections import Counter
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from live_smoke import accurate_fixture_text, fixture_panel_diagnostic


class FakeAX:
    """Expose only read APIs and count every copied reference without importing native frameworks."""

    def __init__(self, *, step=0):
        """Model a host sheet with a service-owned empty path field and one selected file row."""
        self.now=0.;self.step=step;self.reads=[];self.references=Counter();self.values={};self.next_pointer=1000
        self.array_children={};self.actions={3:['AXConfirm'],4:['AXConfirm']};self.action_status=0;self.action_reads=[]
        self.nodes={
            1:{'AXRole':'AXApplication','AXWindows':[2],'AXFocusedUIElement':4},
            2:{'AXRole':'AXWindow','AXTitle':'Open','AXChildren':[3],'AXParent':1},
            3:{'AXRole':'AXSheet','AXTitle':'Go to the folder:','AXChildren':[4,5,6,7,8],'AXParent':2},
            4:{'AXRole':'AXTextField','AXValue':'','AXFocused':'true','AXPlaceholderValue':'/folder',
               'AXDescription':'path','AXTitle':'','AXSubrole':'AXSearchField','AXPosition':(10,20),
               'AXSize':(200,30),'AXParent':3,'pid':2144},
            5:{'AXRole':'AXButton','AXTitle':'Go','AXParent':3},
            6:{'AXRole':'AXButton','AXTitle':'Cancel','AXParent':3},
            7:{'AXRole':'AXStaticText','AXValue':'Go to the folder:','AXParent':3},
            8:{'AXRole':'AXRow','AXURL':'file:///tmp/generated.txt','AXDocument':'generated.txt',
               'AXSelected':'true','AXParent':3},
        }
        self.computer=SimpleNamespace(ax_snapshot=self.snapshot,ax_focused_summary=self.focus_summary,
            _appserv=SimpleNamespace(AXUIElementCreateApplication=self.application,
                AXUIElementSetMessagingTimeout=self.messaging_timeout,AXUIElementGetPid=self.element_pid,
                AXUIElementCopyActionNames=self.copy_actions),
            _cf=SimpleNamespace(CFRetain=self.retain,CFRelease=self.release,CFArrayGetCount=self.array_count,
                CFArrayGetValueAtIndex=self.array_item),_ax_read_attr_ptr=self.attribute,
            _ax_read_multi=self.attributes,_cftype_to_str=self.text,_decode_pos_size=self.geometry,
            _ax_attr_is_settable=self.settable)

    def record(self, name):
        """Advance an inert native read clock, making budget regressions observable."""
        self.reads.append((name,self.now));self.now+=self.step

    def own(self, pointer):
        """Record an owned reference exactly as a Core Foundation copy API would."""
        self.references[pointer]+=1
        return pointer

    def retain(self, pointer):
        """Keep one native object identity alive until the probe releases its explicit hold."""
        return self.own(pointer.value)

    def value(self, data):
        """Give nonempty and empty scalar values distinct retained Python handles."""
        pointer=self.next_pointer;self.next_pointer+=1
        self.values[pointer]=data
        return self.own(pointer)

    def application(self, pid):
        """Permit only the generated fixture host application query."""
        self.record('application')
        if pid!=123:raise AssertionError('unowned host query')
        return self.own(1)

    def messaging_timeout(self, app, seconds):
        """Read-loop timeout metadata is the only allowed AX setter in this inert adapter."""
        if app.value!=1 or seconds!=.05:raise AssertionError('unexpected timeout metadata')

    def snapshot(self, pid, **kwargs):
        """Return a host-wide snapshot whose comparison-derived focus differs from raw AXFocused."""
        if (pid!=123 or kwargs.get('max_results')!=400 or kwargs.get('max_children_per_node')!=100
                or not 0<kwargs.get('deadline_seconds',0)<=.65 or set(kwargs)!={'max_results','max_children_per_node','deadline_seconds'}):
            raise AssertionError('unscoped or unbounded snapshot')
        return [{'role':'AXTextField','value':'','focused':False,'x':110,'y':35,'width':200,'height':30}]

    def focus_summary(self, pid):
        """Supply the existing host focus summary without any global focus query."""
        if pid!=123:raise AssertionError('unowned focus query')
        return {'focused':{'role':'AXTextField','label':'','value':''}}

    def attribute(self, element, name):
        """Retain only a host-descendant reference or a copied child array."""
        self.record(name.decode())
        data=self.nodes[element].get(name.decode())
        if data is None:return 0
        return self.own(data) if isinstance(data,int) else self.value(data)

    def attributes(self, element, names):
        """Return one copied value per requested native metadata attribute."""
        self.record('attributes')
        return [self.value(self.nodes[element][name.decode()]) if name.decode() in self.nodes[element] else 0
                for name in names]

    def element_pid(self, element, output):
        """Expose the candidate field's service PID without looking up another application."""
        self.record('element_pid')
        ctypes.cast(output,ctypes.POINTER(ctypes.c_int))[0]=self.nodes[element.value].get('pid',123)
        return 0

    def settable(self, element, attribute):
        """Read writability only; no AXValue or AXFocused mutation API exists in this adapter."""
        self.record('is_settable')
        return attribute==b'AXValue'

    def copy_actions(self, element, output):
        """Copy advertised names with owned array callbacks, never perform an action."""
        self.record('copy_action_names')
        self.action_reads.append(element.value)
        names=[self.value(name) for name in self.actions.get(element.value,[])]
        array=self.value(names);self.array_children[array]=names
        ctypes.cast(output,ctypes.POINTER(ctypes.c_void_p))[0]=array
        return self.action_status

    def release(self, pointer):
        """Reject over-release and prove every copied native handle is balanced."""
        if self.references[pointer.value]<=0:raise AssertionError('over-release')
        self.references[pointer.value]-=1
        if self.references[pointer.value]==0:
            for child in self.array_children.pop(pointer.value,[]):
                self.release(ctypes.c_void_p(child))

    def array_count(self, pointer):
        """Measure an inert copied descendant array."""
        return len(self.values[pointer.value])

    def array_item(self, pointer, index):
        """Return a borrowed descendant that remains alive with the copied array."""
        return self.values[pointer.value][index]

    def text(self, pointer):
        """Decode fixture scalars including an actual empty value."""
        return str(self.values[pointer])

    def geometry(self, position, size):
        """Decode the two copied geometry values without any real display query."""
        return (*self.values[position],*self.values[size])

    def run(self):
        """Use only the deterministic clock and this explicit read API allowlist."""
        with patch('live_smoke.time',SimpleNamespace(monotonic=lambda:self.now)):
            return fixture_panel_diagnostic(self.computer,123)


class RecycledChildAX(FakeAX):
    """Reuse a completed sibling leaf address only when no owned reference keeps it alive."""

    def __init__(self):
        """Place the selected row after a short subtree whose borrowed leaf can be recycled."""
        super().__init__()
        self.reused=False
        self.nodes[3]['AXChildren']=[20,8]
        self.nodes[20]={'AXRole':'AXGroup','AXChildren':[90],'AXParent':3}
        self.nodes[90]={'AXRole':'AXStaticText','AXValue':'earlier generated label','AXParent':20}
        self.nodes[8]['AXChildren']=[]

    def value(self, data):
        """A copied child array owns its borrowed node references until the array is released."""
        pointer=super().value(data)
        if isinstance(data,list) and all(child in self.nodes for child in data):
            self.array_children[pointer]=[self.own(child) for child in data]
        return pointer

    def attributes(self, element, names):
        """Allocate a later Cell/List/path subtree at the old leaf address if it has been freed."""
        if element==8 and b'AXChildren' in names:
            pointer=90 if self.references[90]==0 else 91
            self.reused=pointer==90
            self.nodes[8]['AXChildren']=[pointer]
            self.nodes[pointer]={'AXRole':'AXCell','AXSelected':'true','AXChildren':[92],'AXParent':8}
            self.nodes[92]={'AXRole':'AXList','AXChildren':[93],'AXParent':pointer}
            self.nodes[93]={'AXRole':'AXStaticText','AXValue':'/tmp/generated.txt','AXParent':92}
        return super().attributes(element,names)


class FixtureDiagnosticTests(unittest.TestCase):
    """Reject unbounded, mutating, lossy, and leaking failure evidence without local desktop effects."""

    def test_completed_sibling_cannot_hide_selected_cell_at_a_recycled_address(self):
        """Selected child evidence survives valid native allocator reuse with no cap or timeout."""
        fake=RecycledChildAX();report=fake.run();elements=report['raw_panel_elements']
        row=next(item for item in elements if item['AXRole']=='AXRow')
        self.assertEqual(row['children_count'],1)
        self.assertFalse(row['children_truncated'])
        self.assertTrue(any(item['AXRole']=='AXCell' for item in elements))
        self.assertTrue(any(item['AXRole']=='AXList' for item in elements))
        self.assertTrue(any(item.get('AXValue')=='/tmp/generated.txt' for item in elements))
        self.assertFalse(report['raw_node_limit_reached'])
        self.assertFalse(report['deadline_reached'])
        self.assertNotIn('raw_probe_error',report)
        self.assertFalse(any(fake.references.values()))

    def test_late_decode_failure_releases_every_visited_element(self):
        """A failure after several sibling visits releases holds and redacts its private message."""
        fake=RecycledChildAX();original=fake.computer._cftype_to_str

        def decode(pointer):
            """Fail only while describing the later selected child, after visiting the earlier subtree."""
            if fake.values[pointer]=='AXCell':
                raise ValueError('private-late-diagnostic-sentinel')
            return original(pointer)

        fake.computer._cftype_to_str=decode
        report=fake.run()
        self.assertEqual(report['raw_probe_error'],'ValueError')
        self.assertNotIn('private-late-diagnostic-sentinel',str(report))
        self.assertFalse(any(fake.references.values()))

    def test_raw_empty_focus_service_pid_and_parent_scope_are_preserved(self):
        """An empty service-owned field still retains raw focus, safe writability, and real parent scope."""
        fake=FakeAX();report=fake.run();focus=report['raw_host_focus']
        self.assertEqual(focus['AXValue'],'')
        self.assertEqual(focus['AXFocused'],'true')
        self.assertFalse(report['elements'][0]['focused'])
        self.assertEqual(focus['element_pid'],2144)
        self.assertEqual(focus['AXPlaceholderValue'],'/folder')
        self.assertEqual(focus['settable'],{'AXValue':True,'AXFocused':False})
        self.assertEqual(focus['action_names'],['AXConfirm'])
        self.assertEqual(report['focused_summary_source'],'raw_host_focus')
        self.assertEqual(report['focused_summary']['focused']['value'],focus['AXValue'])
        self.assertEqual(focus['parents'][0]['action_names'],['AXConfirm'])
        self.assertEqual([parent['AXRole'] for parent in focus['parents']],['AXSheet','AXWindow','AXApplication'])
        row=next(element for element in report['raw_panel_elements'] if element['AXRole']=='AXRow')
        self.assertEqual((row['AXURL'],row['AXSelected']),('file:///tmp/generated.txt','true'))
        self.assertNotIn('raw_probe_error',report)
        self.assertFalse(any(fake.references.values()))

    def test_wide_and_cyclic_trees_and_text_payloads_are_bounded(self):
        """A huge malformed tree cannot exceed the 400-node, eight-parent, or 256-character evidence caps."""
        fake=FakeAX();fake.nodes[3]['AXChildren']=list(range(10,510));fake.nodes[4]['AXParent']=4
        for element in range(10,510):fake.nodes[element]={'AXRole':'AXStaticText','AXValue':'x'*1000}
        fake.computer.ax_snapshot=lambda *args,**kwargs:[{'role':'AXStaticText','value':'x'*1000,'ignored':'unbounded'}]*700
        report=fake.run()
        self.assertEqual(len(report['elements']),400)
        self.assertEqual(len(report['elements'][0]['value']),256)
        self.assertNotIn('ignored',report['elements'][0])
        self.assertEqual(report['raw_visited_nodes'],400)
        self.assertEqual(len(report['raw_host_focus']['parents']),8)
        self.assertTrue(all(len(element.get('AXValue') or '')<=256 for element in report['raw_panel_elements']))
        self.assertFalse(any(fake.references.values()))

    def test_expired_shared_budget_stops_additional_native_reads(self):
        """Read calls stop at the shared deadline, allowing only the last already-in-flight operation."""
        for focused in (4,3):
            with self.subTest(focused=focused):
                fake=FakeAX(step=.11);fake.nodes[1]['AXFocusedUIElement']=focused
                report=fake.run()
                self.assertTrue(report['deadline_reached'])
                self.assertLess(report['elapsed_ms'],1700)
                self.assertTrue(all(start<1.5 for name,start in fake.reads))
                self.assertFalse(any(fake.references.values()))
        fake=FakeAX(step=2.)
        self.assertEqual(fake.run()['raw_visited_nodes'],0)
        self.assertEqual(fake.reads,[('application',0.)])

    def test_focused_sheet_metadata_and_selected_path_precede_outer_file_lists(self):
        """The observed post-confirmation sheet exposes actions and selected path before a wide file view."""
        fake=FakeAX(step=.001)
        fake.nodes[1]['AXFocusedUIElement']=3
        fake.nodes[3].update(AXParent=9,AXFocused='true',AXDefaultButton=5)
        fake.nodes[4]['pid']=123
        fake.nodes[9]={'AXRole':'AXSheet','AXDescription':'open','AXChildren':[3],'AXParent':2}
        fake.nodes[8].update(AXChildren=[10])
        fake.nodes[10]={'AXRole':'AXCell','AXSelected':'true','AXChildren':[11],'AXParent':8}
        fake.nodes[11]={'AXRole':'AXList','AXDescription':'path','AXChildren':[12],'AXParent':10}
        fake.nodes[12]={'AXRole':'AXStaticText','AXValue':'/tmp/generated.txt','AXParent':11}
        fake.actions.update({5:['AXPress'],8:['AXPress'],10:['AXPress'],11:['AXConfirm']})
        fake.nodes[2]['AXChildren']=list(range(20,620))+[9]
        for element in range(20,620):
            fake.nodes[element]={'AXRole':'AXStaticText','AXValue':'outer file','AXParent':2}
        report=fake.run();focus=report['raw_host_focus']
        self.assertEqual(focus['AXRole'],'AXSheet')
        self.assertEqual(focus['action_names'],['AXConfirm'])
        self.assertEqual([item['AXRole'] for item in focus['parents']],['AXSheet','AXWindow','AXApplication'])
        self.assertEqual(focus['default_button']['AXRole'],'AXButton')
        self.assertEqual(focus['default_button']['action_names'],['AXPress'])
        for role in ('AXRow','AXCell','AXList'):
            item=next(item for item in report['raw_panel_elements'] if item['AXRole']==role)
            self.assertTrue(item['prioritized'])
            self.assertEqual(item['element_pid'],123)
            self.assertTrue(item['action_names'])
            self.assertEqual(item['parents'][-1]['AXRole'],'AXApplication')
        values=[item.get('AXValue') for item in report['raw_panel_elements']]
        self.assertLess(values.index('/tmp/generated.txt'),values.index('outer file'))
        self.assertEqual(report['raw_visited_nodes'],400)
        self.assertTrue(report['raw_node_limit_reached'])
        self.assertFalse(any(fake.references.values()))

    def test_foreign_focused_sheet_and_default_button_are_never_prioritized(self):
        """A copied host-focus reference cannot authorize following a foreign chooser or default button."""
        for foreign in ('sheet','button','child'):
            with self.subTest(foreign=foreign):
                fake=FakeAX()
                fake.nodes[1]['AXFocusedUIElement']=3
                fake.nodes[3].update(AXDefaultButton=5)
                fake.nodes[4]['pid']=123
                element=3 if foreign=='sheet' else 5 if foreign=='button' else 8
                fake.nodes[element]['pid']=456
                fake.actions[5]=['AXPress']
                report=fake.run()
                self.assertNotIn(element,fake.action_reads)
                if foreign=='sheet':
                    self.assertEqual(report['raw_host_focus']['element_pid'],456)
                    self.assertEqual(report['raw_host_focus']['scope_rejected'],'foreign_or_unverified_owner')
                    self.assertNotIn('AXDefaultButton',[name for name,started in fake.reads])
                elif foreign=='button':
                    self.assertEqual(report['raw_host_focus']['default_button']['element_pid'],456)
                    self.assertEqual(report['raw_host_focus']['default_button']['scope_rejected'],'foreign_or_unverified_owner')
                else:
                    self.assertFalse(any(item['AXRole']=='AXRow' for item in report['raw_panel_elements']))
                self.assertFalse(any(fake.references.values()))

    def test_focused_sheet_default_metadata_errors_still_balance_references(self):
        """A decode failure inside the copied default button remains private and releases every reference."""
        fake=FakeAX()
        fake.nodes[1]['AXFocusedUIElement']=3
        fake.nodes[3]['AXDefaultButton']=5
        fake.nodes[5].update(AXPosition=(10,20),AXSize=(30,20))

        def failing(*args):
            """Fail only in the owned default button's metadata decoding, without any native mutation."""
            raise ValueError('private-default-button-sentinel')

        fake.computer._decode_pos_size=failing
        report=fake.run()
        self.assertEqual(report['raw_probe_error'],'ValueError')
        self.assertNotIn('private-default-button-sentinel',str(report))
        self.assertFalse(any(fake.references.values()))

    def test_action_metadata_is_capped_and_released_even_on_native_error(self):
        """Long advertised actions and failed copies cannot bloat diagnostics, mutate, or retain arrays."""
        fake=FakeAX();fake.nodes[4]['AXRole']='AXTextArea'
        fake.actions[4]=['x'*500]*50
        report=fake.run();focus=report['raw_host_focus']
        self.assertEqual(focus['AXRole'],'AXTextArea')
        self.assertEqual(len(focus['action_names']),16)
        self.assertTrue(all(len(name)==128 for name in focus['action_names']))
        self.assertTrue(focus['action_names_truncated'])
        self.assertFalse(any(fake.references.values()))
        fake=FakeAX();fake.action_status=-25204
        focus=fake.run()['raw_host_focus']
        self.assertEqual(focus['action_names_status'],-25204)
        self.assertEqual(focus['action_names'],[])
        self.assertFalse(any(fake.references.values()))

    def test_decode_failure_is_redacted_and_releases_all_copied_handles(self):
        """A failed decoder cannot leak exception contents, retain AX objects, or mask the original failure."""
        fake=FakeAX()
        def failing(*args):
            """Inject an inert decoding failure while native copied values are held."""
            raise ValueError('private-fixture-diagnostic-sentinel')
        fake.computer._decode_pos_size=failing
        report=fake.run()
        self.assertEqual(report['raw_probe_error'],'ValueError')
        self.assertNotIn('private-fixture-diagnostic-sentinel',str(report))
        self.assertFalse(any(fake.references.values()))


class AccurateOCRFixtureTests(unittest.TestCase):
    """A positive tool invocation cannot qualify accurate OCR using failed, unrelated, or inconsistent data."""

    def _result(self, text='Beta baseline'):
        """Represent the successful real OS26 wire shape with independent fixture text."""
        return {'ok':True,'via':'ocr','level':'accurate','count':1,
                'observations':[{'text':text}],'full_text':text}

    def test_real_positive_shape_matches_only_the_selected_fixture_text(self):
        """Actual matching observations and reading-order text qualify; another sibling's text does not."""
        result=self._result()
        self.assertTrue(accurate_fixture_text(result,'Beta baseline'))
        self.assertFalse(accurate_fixture_text(result,'Alpha baseline'))
        self.assertFalse(accurate_fixture_text(self._result('Not Beta baseline'),'Beta baseline'))
        self.assertFalse(accurate_fixture_text(self._result('Beta baseline changed'),'Beta baseline'))

    def test_failed_or_wrong_mode_call_cannot_qualify_matching_cached_text(self):
        """The observed OS27 failure and truthy status or wrong backend labels fail despite matching text."""
        changes=({'ok':False,'error':'Text recognition could not complete: no native result'},
                 {'ok':'true'},{'ok':1},{'via':'ax'},{'level':'fast'},
                 {'error':False},{'error':{}},{'error':[]})
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(accurate_fixture_text({**self._result(),**change},'Beta baseline'))

    def test_empty_malformed_or_inconsistent_observations_fail(self):
        """Empty or malformed result bodies and invented counts never become successful coverage."""
        changes=({'observations':[],'count':0},{'observations':None},{'observations':'Beta baseline'},
                 {'observations':[{'text':123}]},{'count':True},{'count':2},{'count':1.0},
                 {'full_text':None},{'full_text':['Beta baseline']})
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(accurate_fixture_text({**self._result(),**change},'Beta baseline'))
        for expected in ('',None,123):
            self.assertFalse(accurate_fixture_text(self._result(),expected))
        self.assertFalse(accurate_fixture_text(None,'Beta baseline'))

    def test_full_text_and_observations_must_independently_match(self):
        """A forged summary or unrelated observation list cannot substitute for actual recognized text."""
        self.assertFalse(accurate_fixture_text({**self._result('Alpha baseline'),'full_text':'Beta baseline'},'Beta baseline'))
        self.assertFalse(accurate_fixture_text({**self._result(),'full_text':'Alpha baseline'},'Beta baseline'))
        self.assertFalse(accurate_fixture_text({**self._result(),'full_text':'Not Beta baseline'},'Beta baseline'))


if __name__=='__main__':
    unittest.main()
