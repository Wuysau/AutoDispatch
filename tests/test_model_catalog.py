import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodispatch import model_catalog
from autodispatch.model import Blocked


class ModelCatalogTests(unittest.TestCase):
    def test_codex_efforts_come_from_each_model_not_a_fixed_enum(self):
        models = model_catalog.codex_models([
            {'model': 'first', 'supportedReasoningEfforts': [{'reasoningEffort': 'future-effort'}],
             'defaultReasoningEffort': 'future-effort'},
            {'model': 'second', 'supportedReasoningEfforts': []},
            {'model': 'hidden', 'hidden': True}])
        self.assertEqual([None, 'future-effort'], models[0]['reasoningEfforts'])
        self.assertEqual([None], models[1]['reasoningEfforts'])
        self.assertEqual(2, len(models))

    def test_opencode_variants_are_official_ids_and_private_fields_are_dropped(self):
        models = model_catalog.opencode_models([
            {'providerID': 'provider', 'id': 'model', 'enabled': True,
             'variants': [{'id': 'deep'}, {'id': 'hidden', 'enabled': False}],
             'headers': {'fixture': 'do-not-copy'}, 'settings': {'fixture': 'do-not-copy'}},
            {'providerID': 'provider', 'id': 'disabled', 'enabled': False}])
        self.assertEqual(['provider/model'], [row['model'] for row in models])
        self.assertEqual([None, 'deep'], models[0]['reasoningEfforts'])
        self.assertNotIn('do-not-copy', json.dumps(models))

    def test_codex_discovery_initializes_and_paginates_without_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fake_cli.py'
            script.write_text('''import json,sys
for line in sys.stdin:
    message=json.loads(line)
    method=message['method']
    assert method in ('initialize','initialized','model/list'), method
    if method=='initialized': continue
    if method=='initialize': result={}
    else:
        second=message['params'].get('cursor')=='next'
        result={'data':[{'model':'second' if second else 'first','supportedReasoningEfforts':[]}], 'nextCursor':None if second else 'next'}
    print(json.dumps({'id':message['id'],'result':result}),flush=True)
''')
            rows = model_catalog.discover({'repo': tmp, 'codex_command': [sys.executable, str(script)]}, 'Codex')
            self.assertEqual(['first', 'second'], [row['model'] for row in rows])

    def test_opencode_discovery_is_location_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fake_cli.py'
            response = {'location': {'directory': tmp}, 'data': [{'providerID': 'provider', 'id': 'model', 'variants': []}]}
            script.write_text('import json\nprint(' + repr(json.dumps(response)) + ')')
            config = {'repo': tmp, 'opencode_command': [sys.executable, str(script)]}
            self.assertEqual('provider/model', model_catalog.discover(config, 'OpenCode')[0]['model'])
            response['location']['directory'] = str(Path(tmp) / 'other-project')
            script.write_text('print(' + repr(json.dumps(response)) + ')')
            with self.assertRaises(Blocked):
                model_catalog.discover(config, 'OpenCode')

    def test_unavailable_or_empty_official_source_does_not_invent_fallbacks(self):
        for result in ([], OSError('unavailable')):
            with self.subTest(result=str(result)):
                with model_catalog._lock:
                    model_catalog._cache.clear()
                with patch.object(model_catalog, 'discover', side_effect=result if isinstance(result, Exception) else None,
                                  return_value=[]):
                    catalog = model_catalog.catalog({}, 'Codex', wait=True)
                self.assertEqual('unavailable', catalog['status'])
                self.assertEqual([], catalog['models'])

    @unittest.skipUnless(os.name == 'nt', 'Windows containment gate')
    def test_cli_cannot_start_before_windows_process_containment_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'launched'
            script = Path(tmp) / 'fake_cli.py'
            script.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("started")')
            config = {'repo': tmp, 'codex_command': [sys.executable, str(script)]}
            with patch.object(model_catalog, 'ProcessTree', side_effect=Blocked('containment unavailable')):
                with self.assertRaisesRegex(Blocked, 'containment unavailable'):
                    model_catalog.discover(config, 'Codex')
            self.assertFalse(marker.exists())
