"""Desktop app entry point: a pywebview (Edge WebView2) window over the local UI server.

Installed builds run as `{app}\\python\\pythonw.exe -m clipcompiler.ui.main`: Python's own signed
launcher, so Windows Smart App Control never has to judge an unsigned exe of ours.
"""
import logging
import os
from pathlib import Path
import threading
import time

from .. import APP_NAME, __version__, paths, settings
from .api import Api
from .server import Server

log = logging.getLogger(__name__)
APP_ID = 'KalebDieterle.AutoClipCompiler'   # Must match the shortcut's AppUserModelID (packaging\installer.iss).
ICON = Path(__file__).resolve().parent/'app.ico'


def web_dir():
    return Path(__file__).resolve().parent/'web'


def own_taskbar_identity():
    """Group under our own taskbar button and icon, not Python's."""
    if os.name == 'nt':
        import ctypes
        try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except OSError as exc: log.warning('AppUserModelID: %s', exc)


def set_window_icon(window):
    """The window would otherwise show pythonw's icon; swap in ours on the WinForms UI thread."""
    try:
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, 'Icon', Icon(str(ICON)))))
    except Exception as exc:
        log.warning('window icon: %s', exc)


def main():
    import webview
    settings.setup_logging()
    log.info('%s %s starting', APP_NAME, __version__)
    own_taskbar_identity()
    server = Server(web_dir(), paths.previews_dir()).start()
    api = Api(server)
    window = webview.create_window(APP_NAME, server.url + '/index.html', js_api=api, width=1320, height=860,
                                   min_size=(1100, 720), background_color='#0B0C10', text_select=False)
    api._window = window

    def closing():
        api.cancel()  # Judgments already returned are cached; nothing paid for is lost.
    window.events.closing += closing
    window.events.shown += lambda: set_window_icon(window)
    if os.environ.get('ACC_SELFTEST') == '1':
        window.events.loaded += lambda: threading.Thread(target=selftest, args=(window,), daemon=True).start()
    webview.start(gui='edgechromium', debug=os.environ.get('ACC_DEBUG') == '1', private_mode=True)
    server.stop()


def selftest(window):
    """Smoke test for builds: page booted through the JS bridge, bundled ffmpeg, Credential Manager, icon."""
    screen = None
    for _ in range(40):
        time.sleep(.25)
        screen = window.evaluate_js("document.querySelector('.screen.on') ? document.querySelector('.screen.on').dataset.screen : null")
        if screen: break
    import keyring
    import sys
    from ..media import find_ffmpeg
    ffmpeg, backend = find_ffmpeg(), type(keyring.get_keyring()).__module__
    try:
        icon_ok = window.native.Icon is not None and window.native.Icon.Width > 0
    except Exception:
        icon_ok = False
    ok = screen and ffmpeg and backend.startswith('keyring.backends.Windows')
    log.info('SELFTEST %s screen=%s ffmpeg=%s keyring=%s icon=%s python=%s', 'OK' if ok else 'FAILED', screen,
             ffmpeg and ffmpeg[0], backend, icon_ok, sys.executable)
    window.destroy()


if __name__ == '__main__':
    main()
