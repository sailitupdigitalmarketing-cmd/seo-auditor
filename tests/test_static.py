"""Static (GitHub Actions) mode: add sites, audit, keep history, build HTML."""
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from seo_auditor import static_report as sr

SITE = Path(__file__).parent / "site"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SITES_FILE", tmp_path / "sites.txt")
    monkeypatch.setattr(sr, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(sr, "OUT_DIR", tmp_path / "public")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield tmp_path
    srv.shutdown(); srv.server_close()


def test_add_validates_and_dedupes(env):
    sr.add_site("http://127.0.0.1:8765/")
    sr.add_site("http://127.0.0.1:8765")
    with pytest.raises(ValueError):
        sr.add_site("not a url")
    assert [s["url"] for s in sr.read_sites()] == ["http://127.0.0.1:8765/"]


def test_run_twice_builds_report_with_history(env):
    sr.add_site("http://127.0.0.1:8765/")
    sr.main(["run"]); sr.main(["run"])
    slug = sr.read_sites()[0]["slug"]
    hist = json.loads((env / "data" / slug / "history.json").read_text())
    assert len(hist) == 2 and all(h["status"] == "done" for h in hist)
    latest = json.loads((env / "data" / slug / "latest.json").read_text())
    assert latest["diff"]["first_run"] is False
    pub = env / "public"
    for f in ["index.html", f"{slug}.html", f"{slug}-actions.csv", f"{slug}.json", ".nojekyll"]:
        assert (pub / f).exists(), f
    assert "Action items" in (pub / f"{slug}.html").read_text()
