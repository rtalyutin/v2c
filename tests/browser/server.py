"""Static fixture server for browser tests; application HTTP is mocked by Playwright."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "src" / "v2c" / "web"


class PanelFiles(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_GET(self):
        if self.path.startswith("/web/"):
            self.path = self.path[4:]
        return super().do_GET()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8877), PanelFiles).serve_forever()
