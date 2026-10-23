import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autodispatch.api import DispatchApi
from autodispatch.execution import ExecutionTrace, MAX_LINE, read_events, read_summary
from autodispatch.process import Runner
from autodispatch.store import Store


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stream_is_visible_before_process_exit_and_final_receipt_still_matches(self):
        store = Store(self.root)
        release = self.root / 'release'
        script = self.root / 'child.py'
        script.write_text("import sys,time,pathlib\nprint('tool is running',flush=True)\n"
                          "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.05)\n"
                          "print('finished',flush=True)\n", encoding='utf-8')
        results = []
        def run():
            results.append(Runner(self.root, store).run(
                [sys.executable, str(script), str(release)], self.root, 15, tid='IP-05', stage='batch-repair'))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            api = DispatchApi({'state_dir': str(self.root)}, type('Plan', (), {'tasks': {'IP-05': object()}})())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                detail = api.execution('IP-05')
                if any(e['message'] == 'tool is running' for e in detail['events']):
                    break
                time.sleep(.05)
            else:
                self.fail('No live output received before child exit')
            self.assertTrue(thread.is_alive())
            self.assertEqual('alive', detail['execution']['processState'])
            self.assertEqual('batch-repair', detail['execution']['stage'])
            self.assertIsNotNone(detail['execution']['lastOutputAt'])
            self.assertIsNotNone(detail['execution']['lastActivityAt'])
            self.assertEqual('tool is running', detail['execution']['currentOperation'])
            self.assertEqual([], api.execution('IP-05', f"after={detail['cursor']}")['events'])
        finally:
            release.touch()
            thread.join(20)
            store.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(0, results[0].code)
        self.assertEqual('tool is running\nfinished\n', results[0].output.replace('\r\n', '\n'))
        rec = Store.snapshot(self.root)['IP-05']
        final = read_summary(self.root, 'IP-05', rec)
        self.assertEqual('finished', final['processState'])
        self.assertEqual(0, final['exitCode'])

    def test_split_secrets_and_utf8_are_redacted_before_any_persistent_output(self):
        trace = ExecutionTrace(self.root, 'IP-05', 'implement', time.time())
        trace.feed(b'token=sk-abcdefghij')
        self.assertNotIn('sk-abcdefghij', (trace.folder / 'events.jsonl').read_text(encoding='utf-8'))
        trace.feed(b'klmnopqrstuv\n')
        payload = '读取文件完成\n'.encode('utf-8')
        trace.feed(payload[:2])
        trace.feed(payload[2:])
        trace.finish(0)
        content = (trace.folder / 'events.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('sk-abcdefghijklmnopqrstuv', content)
        self.assertNotIn('sk-abcdefghij', content)
        self.assertIn('读取文件完成', content)

    def test_tool_operations_and_no_reasoning_events(self):
        trace = ExecutionTrace(self.root, 'IP-29', 'implement', time.time())
        rows = [
            {'type': 'tool_use', 'part': {'tool': 'edit', 'state': {
                'status': 'running', 'input': {'filePath': 'web/src/App.tsx'}}}},
            {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'python -m unittest'}},
            {'type': 'reasoning', 'part': {'text': 'private reasoning text'}},
        ]
        for row in rows:
            trace.feed((json.dumps(row) + '\n').encode())
        events, _ = read_events(self.root, 'IP-29', trace.id)
        self.assertIn('web/src/App.tsx', events[1]['message'])
        self.assertIn('python -m unittest', events[2]['message'])
        self.assertNotIn('private reasoning text', (trace.folder / 'events.jsonl').read_text(encoding='utf-8'))

    def test_json_secrets_keep_valid_structured_telemetry(self):
        trace = ExecutionTrace(self.root, 'IP-29', 'implement', time.time())
        trace.feed((json.dumps({'type': 'tool_use', 'part': {'tool': 'shell', 'state': {
            'status': 'running', 'input': {'command': 'tool --token=sk-abcdefghijklmnopqrstuv',
                                         'api_key': 'fictional-secret'}}}}) + '\n').encode())
        summary = read_summary(self.root, 'IP-29', {'execution_id': trace.id})
        events, _ = read_events(self.root, 'IP-29', trace.id)
        self.assertEqual('live', summary['telemetry'])
        self.assertIn('shell', summary['currentOperation'])
        self.assertEqual('tool', events[-1]['kind'])
        for p in trace.folder.iterdir():
            text = p.read_text(encoding='utf-8')
            self.assertNotIn('sk-abcdefghijklmnopqrstuv', text)
            self.assertNotIn('fictional-secret', text)

    def test_authorization_header_values_are_fully_redacted(self):
        for scheme in ['Bearer', 'Basic']:
            with self.subTest(scheme=scheme):
                trace = ExecutionTrace(self.root, 'IP-29', 'implement', time.time())
                trace.feed((json.dumps({'type': 'text', 'part': {
                    'text': f'Authorization: {scheme} example-private-value'}}) + '\n').encode())
                trace.feed(f'Authorization: {scheme} another-private-value\n'.encode())
                summary = read_summary(self.root, 'IP-29', {'execution_id': trace.id})
                events, _ = read_events(self.root, 'IP-29', trace.id)
                text = json.dumps([summary, events])
                self.assertNotIn('example-private-value', text)
                self.assertNotIn('another-private-value', text)
                for p in trace.folder.iterdir():
                    self.assertNotIn('private-value', p.read_text(encoding='utf-8'))

    def test_malformed_tool_type_and_lone_surrogate_do_not_break_receipts(self):
        store = Store(self.root)
        try:
            for ending in ['\\n', '']:
                with self.subTest(ending=ending):
                    script = "import sys;sys.stdout.write('{\"item\":{\"type\":[]}}\\n{\"type\":\"text\",\"part\":{\"text\":\"\\\\ud800\"}}" + ending + "');sys.stdout.flush()"
                    result = Runner(self.root, store).run([sys.executable, '-c', script], self.root, 5, tid='IP-05')
                    self.assertEqual(0, result.code)
                    self.assertEqual(0, store.records()['IP-05']['last_exit_code'])
        finally:
            store.close()

    def test_overlong_lines_are_dropped_and_history_is_bounded(self):
        trace = ExecutionTrace(self.root, 'IP-05', 'implement', time.time())
        trace.feed(b'x' * (MAX_LINE + 1))
        trace.feed(b'never show this tail\n')
        for i in range(230):
            trace.feed(f'event {i}\n'.encode())
        events, truncated = read_events(self.root, 'IP-05', trace.id)
        self.assertTrue(truncated)
        self.assertEqual(200, len(events))
        content = (trace.folder / 'events.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('never show this tail', content)
        self.assertLess(len(content), MAX_LINE)

    def test_attempt_reset_and_legacy_process_status_are_explicit(self):
        store = Store(self.root)
        try:
            store.update('IP-05', 'RUNNING', pid=os.getpid(), stage='legacy')
            api = DispatchApi({'state_dir': str(self.root)}, type('Plan', (), {'tasks': {'IP-05': object()}})())
            old = api.execution('IP-05')
            self.assertEqual('unavailable', old['execution']['telemetry'])
            self.assertEqual('alive', old['execution']['processState'])
            trace = ExecutionTrace(self.root, 'IP-05', 'repair', time.time())
            store.update('IP-05', execution_id=trace.id)
            new = api.execution('IP-05', 'attempt=' + '0'*32 + '&after=9999')
            self.assertEqual(1, new['cursor'])
            self.assertEqual(trace.id, new['execution']['id'])
        finally:
            store.close()

    def test_telemetry_failure_does_not_change_command_or_receipt(self):
        store = Store(self.root)
        try:
            with patch('autodispatch.execution.ExecutionTrace.feed', side_effect=RuntimeError('broken telemetry')):
                result = Runner(self.root, store).run([sys.executable, '-c', "print('still drained')"],
                                                     self.root, 5, tid='IP-05')
            self.assertEqual(0, result.code)
            self.assertIn('still drained', result.output)
            self.assertEqual(0, store.records()['IP-05']['last_exit_code'])
        finally:
            store.close()
