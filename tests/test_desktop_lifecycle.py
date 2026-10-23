import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from autodispatch.desktop import run_desktop


class DesktopLifecycleTests(unittest.TestCase):
    def test_demo_serves_local_build_in_isolated_state_and_cleans_up_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            (dist / 'index.html').write_text('<html>desktop</html>')
            observed = {}

            def host(url, state_dir, **kwargs):
                observed.update(url=url, state_dir=state_dir, **kwargs)
                self.assertIn('mode=demo', url)
                self.assertTrue(Path(state_dir).parent.is_dir())
                self.assertTrue(kwargs['show_console'])
                self.assertEqual(kwargs['profile_dir'].parent, Path(state_dir).parent)
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertIn(b'desktop', response.read())
                raise RuntimeError('simulate host exit')

            with patch('autodispatch.desktop.sys.platform', 'win32'), patch('autodispatch.desktop.run_widget', host):
                with self.assertRaisesRegex(RuntimeError, 'simulate host exit'):
                    run_desktop(demo=True, ui_dist=dist)
            self.assertFalse(Path(observed['state_dir']).exists())
            with self.assertRaises(OSError):
                urllib.request.urlopen(observed['url'], timeout=1)

    def test_demo_cli_does_not_load_real_dispatcher_configuration(self):
        from autodispatch.cli import main
        with patch('autodispatch.cli.load_config') as config, patch('autodispatch.desktop.run_desktop') as desktop:
            self.assertEqual(main(['desktop', '--demo']), 0)
        config.assert_not_called()
        self.assertTrue(desktop.call_args.kwargs['demo'])
