"""Run a full audit and produce a JSON-serialisable report."""
from __future__ import annotations

import datetime as dt
import statistics

from .checks import SEVERITY_ORDER, run_checks, score
from .crawler import crawl


def run_audit(url: str, max_pages: int = 100, **kw) -> dict:
    cr = crawl(url, max_pages=max_pages, **kw)
    issues = run_checks(cr)
    ok_pages = [p for p in cr.pages if p.status == 200 and p.is_html]
    counts = {sev: sum(1 for i in issues if i.severity == sev) for sev in SEVERITY_ORDER}
    return {
        "url": cr.start_url,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "score": score(issues, len(ok_pages)),
        "summary": {
            "pages_crawled": len(cr.pages),
            "pages_ok": len(ok_pages),
            "links_checked": len(cr.links),
            "avg_response_ms": int(statistics.mean([p.elapsed_ms for p in ok_pages])) if ok_pages else 0,
            "https": cr.https,
            "robots_txt": cr.robots_txt is not None,
            "sitemap": any(sm["status"] == 200 and not sm["error"] for sm in cr.sitemaps),
            "duration_s": cr.duration_s,
            "issue_counts": counts,
        },
        "issues": [i.to_dict() for i in issues],
        "pages": [
            {"url": p.final_url or p.url, "status": p.status, "title": p.title, "words": p.word_count,
             "ms": p.elapsed_ms, "depth": p.depth}
            for p in cr.pages
        ],
    }


def diff_reports(prev: dict | None, cur: dict) -> dict:
    """What changed since the previous audit: new / resolved issues and affected-page deltas."""
    if not prev:
        return {"first_run": True, "new": [], "resolved": [], "changed": [], "score_delta": 0}
    p_issues = {i["id"]: i for i in prev["issues"]}
    c_issues = {i["id"]: i for i in cur["issues"]}
    new = [c_issues[k]["title"] for k in c_issues.keys() - p_issues.keys()]
    resolved = [p_issues[k]["title"] for k in p_issues.keys() - c_issues.keys()]
    changed = []
    for k in c_issues.keys() & p_issues.keys():
        a, b = p_issues[k]["count"], c_issues[k]["count"]
        if a != b:
            changed.append({"title": c_issues[k]["title"], "from": a, "to": b})
    return {"first_run": False, "new": new, "resolved": resolved, "changed": changed,
            "score_delta": cur["score"] - prev["score"]}
