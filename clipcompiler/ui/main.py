"""Desktop app entry point: a pywebview (Edge WebView2) window over the local UI server."""
import logging
import os
from pathlib import Path
import sys
import threading
import time

from .. import APP_NAME, __version__, paths, settings
from .api import Api
from .server import Server

log = logging.getLogger(__name__)


def web_dir():
    bundled = Path(getattr(sys, '_MEIPASS', ''))/'clipcompiler'/'ui'/'web'
    return bundled if bundled.is_dir() else Path(__file__).resolve().parent/'web'


def main():
    import webview
    settings.setup_logging()
    log.info('%s %s starting', APP_NAME, __version__)
    server = Server(web_dir(), paths.previews_dir()).start()
    api = Api(server)
    window = webview.create_window(APP_NAME, server.url + '/index.html', js_api=api, width=1320, height=860,
                                   min_size=(1100, 720), background_color='#0B0C10', text_select=False)
    api._window = window

    def closing():
        api.cancel()  # Judgments already returned are cached; nothing paid for is lost.
    window.events.closing += closing
    if os.environ.get('ACC_SELFTEST') == '1':
        window.events.loaded += lambda: threading.Thread(target=selftest, args=(window,), daemon=True).start()
    webview.start(gui='edgechromium', debug=os.environ.get('ACC_DEBUG') == '1', private_mode=True)
    server.stop()


def selftest(window):
    """Smoke test for builds: the page booted through the JS bridge and shows a screen. Logs, then exits."""
    screen = None
    for _ in range(40):
        time.sleep(.25)
        screen = window.evaluate_js("document.querySelector('.screen.on') ? document.querySelector('.screen.on').dataset.screen : null")
        if screen: break
    import keyring
    from ..media import find_ffmpeg
    ffmpeg, backend = find_ffmpeg(), type(keyring.get_keyring()).__module__
    ok = screen and ffmpeg and backend.startswith('keyring.backends.Windows')
    log.info('SELFTEST %s screen=%s ffmpeg=%s keyring=%s', 'OK' if ok else 'FAILED', screen, ffmpeg and ffmpeg[0], backend)
    window.destroy()


if __name__ == '__main__':
    main()
