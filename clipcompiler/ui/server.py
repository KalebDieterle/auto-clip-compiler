"""Loopback-only HTTP server for the UI files and moment previews.

The UI is static and public on 127.0.0.1; preview media needs the per-session token and is served
only from the previews folder (no path traversal), with HTTP Range support so videos can seek.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, unquote, urlparse

NAME = re.compile(r'^[0-9a-f]{16,64}\.(mp4|jpg)$')


class Server:
    def __init__(self, web_dir, media_dir):
        self.web_dir, self.media_dir = Path(web_dir).resolve(), Path(media_dir)
        self.token = secrets.token_urlsafe(24)
        server = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(server.web_dir), **kwargs)

            def log_message(self, *args): pass  # Quiet: a windowed app has no console.

            def end_headers(self):
                self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
                super().end_headers()

            def do_GET(self):
                url = urlparse(self.path)
                if url.path.startswith('/media/'):
                    return self.media(unquote(url.path[len('/media/'):]), parse_qs(url.query).get('t', [''])[0])
                return super().do_GET()

            def media(self, name, token):
                if not secrets.compare_digest(token, server.token): return self.send_error(403)
                path = server.media_dir/name
                if not NAME.match(name) or not path.is_file(): return self.send_error(404)
                size = path.stat().st_size; start, end = 0, size-1
                ranged = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range', ''))
                if ranged and (ranged[1] or ranged[2]):
                    if ranged[1]:
                        start = int(ranged[1]); end = min(int(ranged[2]), size-1) if ranged[2] else size-1
                    else:
                        start = max(0, size-int(ranged[2]))
                    if start > end or start >= size:
                        self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
                    self.send_response(206); self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                else:
                    self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
                self.send_header('Accept-Ranges', 'bytes'); self.send_header('Content-Length', str(end-start+1))
                self.end_headers()
                with path.open('rb') as stream:
                    stream.seek(start); left = end-start+1
                    try:
                        while left > 0:
                            chunk = stream.read(min(65536, left))
                            if not chunk: break
                            self.wfile.write(chunk); left -= len(chunk)
                    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                        pass  # The video element hung up mid-range; normal while seeking.

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def media_url(self, name):
        return f'{self.url}/media/{name}?t={self.token}'

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name='ui-server').start()
        return self

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()
