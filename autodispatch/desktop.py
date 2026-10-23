"""Local desktop UI lifecycle. It never launches the task dispatcher."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from .api import make_server
from .host import run_widget
from .model import Blocked


def run_desktop(config: dict | None = None, *, demo: bool = False, port: int = 0,
                ui_dist: Path | None = None) -> None:
    if sys.platform != 'win32':
        raise Blocked('The desktop Acrylic host requires Windows')
    dist = ui_dist or Path(__file__).resolve().parent.parent / 'ui' / 'dist'
    if not (dist / 'index.html').is_file():
        raise Blocked('Build the interface first: npm --prefix ui run build')
    if port != 0 and not 1024 <= port <= 65535:
        raise Blocked('Desktop port must be 0 (automatic) or 1024–65535')
    if not demo and config is None:
        raise Blocked('A dispatcher configuration is required for live monitoring')

    # Demo owns a fresh state directory, command channel and WebView profile.
    with tempfile.TemporaryDirectory(prefix='autodispatch-desktop-demo-' if demo else 'autodispatch-desktop-ui-') as sandbox:
        cfg = {'state_dir': sandbox, 'repo': sandbox, 'pins': {}} if demo else dict(config)
        if demo:
            # Only public executable paths for read-only official model discovery;
            # never load the running configuration, approval, or dispatcher state.
            example = json.loads((Path(__file__).resolve().parent.parent / 'config.example.json').read_text())
            cfg.update({key: example[key] for key in ('codex_command', 'opencode_command')})
        cfg['ui_state_dir'] = str(Path(sandbox) / 'ui')
        server = make_server(cfg, port, static_dir=dist)
        worker = threading.Thread(target=server.serve_forever, daemon=True, name='desktop-ui-api')
        worker.start()
        url = f'http://127.0.0.1:{server.server_address[1]}/?mode=' + ('demo' if demo else 'live')
        print(json.dumps({'desktop_url': url, 'mode': 'demo' if demo else 'monitor',
                          'state_dir': cfg['state_dir'], 'ui_state_dir': cfg['ui_state_dir']}, ensure_ascii=False), flush=True)
        try:
            profile = Path(sandbox) / 'webview-profile' if demo else Path(
                os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AutoDispatch' / 'desktop-profile'
            run_widget(url, cfg['ui_state_dir'], show_console=True, profile_dir=profile)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
