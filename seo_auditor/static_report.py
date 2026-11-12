"""Static mode for GitHub Actions: audit sites listed in sites.txt, keep history as JSON, render HTML.

Usage:
  python -m seo_auditor.static_report add https://example.com   # add a site to sites.txt
  python -m seo_auditor.static_report run                       # audit all sites, then build
  python -m seo_auditor.static_report build                     # only re-render HTML from saved data
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .audit import diff_reports, run_audit
from .checks import SEVERITY_ORDER

SITES_FILE = Path(os.environ.get("SITES_FILE", "sites.txt"))
DATA_DIR = Path(os.environ.get("REPORTS_DATA_DIR", "reports/data"))
OUT_DIR = Path(os.environ.get("REPORTS_OUT_DIR", "public"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "90"))
AUDIT_HOUR = int(os.environ.get("AUDIT_HOUR_UTC", "6"))


def slugify(url: str) -> str:
    p = urlparse(url)
    s = (p.netloc + p.path).lower().removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "site"


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        raise ValueError(f"Not a valid website URL: {raw!r}")
    return f"{p.scheme}://{p.netloc.lower()}{p.path or '/'}"


def read_sites() -> list[dict]:
    if not SITES_FILE.exists():
        return []
    sites, seen = [], set()
    for line in SITES_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            url = normalize_url(line)
        except ValueError as e:
            print(f"Skipping: {e}", file=sys.stderr)
            continue
        if url not in seen:
            seen.add(url)
            sites.append({"url": url, "slug": slugify(url)})
    return sites


def add_site(raw: str) -> str:
    url = normalize_url(raw)
    if url in {s["url"] for s in read_sites()}:
        print(f"{url} is already monitored")
        return url
    existing = SITES_FILE.read_text() if SITES_FILE.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    SITES_FILE.write_text(existing + url + "\n")
    print(f"Added {url}")
    return url


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def audit_one(site: dict) -> None:
    d = DATA_DIR / site["slug"]
    d.mkdir(parents=True, exist_ok=True)
    prev = _load(d / "latest.json", None)
    history = _load(d / "history.json", [])
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        report = run_audit(site["url"], max_pages=MAX_PAGES)
        if report["summary"]["pages_ok"] == 0:
            raise RuntimeError("Could not load any page (check the URL, or the site may block bots).")
        diff = diff_reports(prev["report"] if prev else None, report)
        latest = {"created_at": report["created_at"], "report": report, "diff": diff}
        (d / "latest.json").write_text(json.dumps(latest))
        (d / f"{report['created_at'][:10]}.json").write_text(json.dumps(latest))
        history.append({"created_at": now, "status": "done", "score": report["score"], "error": None})
        sm = report["summary"]
        print(f"✓ {site['url']}: score {report['score']}, {sm['pages_ok']}/{sm['pages_crawled']} pages OK, "
              f"{sm['links_checked']} links checked, {len(report['issues'])} issue types")
        for i in report["issues"]:
            print(f"    [{i['severity']}] {i['title']} ({i['count']})")
        _notify(site, report, diff)
    except Exception as e:
        history.append({"created_at": now, "status": "failed", "score": None, "error": str(e)[:300]})
        print(f"✗ {site['url']}: {e}", file=sys.stderr)
    (d / "history.json").write_text(json.dumps(history[-365:]))
    # prune old daily snapshots
    snaps = sorted(p for p in d.glob("????-??-??.json"))
    for old in snaps[:-KEEP_DAYS]:
        old.unlink()


def _notify(site, report, diff):
    if os.environ.get("WEBHOOK_URL"):
        from .jobs import notify  # reuses the Slack/Discord webhook formatter (reads WEBHOOK_URL)
        notify(site, report, diff)


def _sparkline(points):
    from .app_utils import sparkline
    return sparkline(points)


def build() -> None:
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                      autoescape=select_autoescape(["html"]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for site in read_sites():
        d = DATA_DIR / site["slug"]
        latest = _load(d / "latest.json", None)
        history = _load(d / "history.json", [])
        scores = [h["score"] for h in history if h["status"] == "done"]
        grouped = {sev: [] for sev in SEVERITY_ORDER}
        if latest:
            for i in latest["report"]["issues"]:
                grouped[i["severity"]].append(i)
        last = history[-1] if history else {}
        html = env.get_template("site.html").render(
            site=site, latest=latest, history=history, grouped=grouped, running=False,
            spark=_sparkline(scores), scores=scores, static=True, home_url="index.html", hour=AUDIT_HOUR,
            last_error=last.get("error") if last.get("status") == "failed" else None)
        (OUT_DIR / f"{site['slug']}.html").write_text(html)
        if latest:
            (OUT_DIR / f"{site['slug']}.json").write_text(json.dumps(latest, indent=1))
            with open(OUT_DIR / f"{site['slug']}-actions.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["priority", "category", "issue", "how_to_fix", "url", "detail"])
                for i in latest["report"]["issues"]:
                    for u in i["pages"]:
                        w.writerow([i["severity"], i["category"], i["title"], i["fix"], u, i["details"].get(u, "")])
        rows.append({**site, "score": latest["report"]["score"] if latest else None,
                     "delta": (latest["diff"]["score_delta"] if latest and not latest["diff"].get("first_run") else None),
                     "counts": latest["report"]["summary"]["issue_counts"] if latest else None,
                     "last_run": last.get("created_at"), "error": last.get("status") == "failed"})
    (OUT_DIR / "index.html").write_text(env.get_template("static_index.html").render(
        sites=rows, hour=AUDIT_HOUR, home_url="index.html",
        generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    (OUT_DIR / ".nojekyll").write_text("")
    print(f"Built {len(rows)} report(s) in {OUT_DIR}/")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("url")
    sub.add_parser("run"); sub.add_parser("build")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        try:
            add_site(args.url)
        except ValueError as e:
            sys.exit(str(e))
    elif args.cmd == "run":
        sites = read_sites()
        if not sites:
            print(f"No sites in {SITES_FILE}. Add one with: python -m seo_auditor.static_report add <url>")
        for s in sites:
            audit_one(s)
        build()
    else:
        build()


if __name__ == "__main__":
    main()
