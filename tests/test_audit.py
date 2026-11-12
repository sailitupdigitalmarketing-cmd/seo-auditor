"""End-to-end test: audit a local fixture site with known SEO problems."""
import functools
import http.server
import threading
from pathlib import Path

import pytest

from seo_auditor.audit import diff_reports, run_audit

SITE = Path(__file__).parent / "site"


@pytest.fixture(scope="module")
def report():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield run_audit("http://127.0.0.1:8765/", max_pages=50)
    finally:
        srv.shutdown()


def ids(report):
    return {i["id"]: i for i in report["issues"]}


def test_detects_known_issues(report):
    found = ids(report)
    for expected in ["no-https", "desc-missing", "viewport-missing", "h1-missing", "h1-multiple",
                     "heading-skip", "canonical-offsite", "schema-invalid", "img-alt-missing",
                     "title-duplicate", "page-4xx-5xx", "sitemap-bad-url", "sitemap-noindex",
                     "noindex", "url-unfriendly", "lang-missing"]:
        assert expected in found, expected


def test_no_false_positives_on_good_homepage(report):
    found = ids(report)
    home = "http://127.0.0.1:8765/"
    for iid in ["title-missing", "desc-missing", "h1-missing", "viewport-missing", "schema-missing"]:
        assert home not in found.get(iid, {"pages": []})["pages"], iid
    assert "robots-missing" not in found and "sitemap-missing" not in found


def test_priorities_sorted_and_score(report):
    order = ["critical", "high", "medium", "low"]
    sev = [order.index(i["severity"]) for i in report["issues"]]
    assert sev == sorted(sev)
    assert 0 <= report["score"] < 100


def test_diff(report):
    prev = {**report, "issues": report["issues"] + [{"id": "x", "title": "Old issue", "count": 1}], "score": 50}
    d = diff_reports(prev, report)
    assert d["resolved"] == ["Old issue"] and d["score_delta"] == report["score"] - 50
