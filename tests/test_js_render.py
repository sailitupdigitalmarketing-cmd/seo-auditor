"""JavaScript apps: the raw HTML is empty, so pages must be rendered in a headless browser."""
import http.server
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from seo_auditor.audit import run_audit  # noqa: E402

SITE = Path(__file__).parent / "site_js"


class SPAHandler(http.server.SimpleHTTPRequestHandler):
    """Serve index.html for every route except the deliberately broken one, like an SPA host."""
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SITE), **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/broken"):
            self.send_error(404)
            return
        self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/broken"):
            self.send_error(404)
            return
        self.path = "/index.html"
        super().do_HEAD()


@pytest.fixture(scope="module")
def report():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), SPAHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield run_audit("http://127.0.0.1:8766/", max_pages=10)
    finally:
        srv.shutdown(); srv.server_close()


def test_renders_js_and_follows_links(report):
    if not report["summary"]["js_rendered"]:
        pytest.skip("headless browser not available")
    urls = {p["url"] for p in report["pages"]}
    assert "http://127.0.0.1:8766/about" in urls
    ids = {i["id"]: i for i in report["issues"]}
    assert "js-only-content" in ids
    assert "h1-missing" not in ids and "thin-content" not in ids
    assert "http://127.0.0.1:8766/broken-js-link" in ids["page-4xx-5xx"]["pages"]
