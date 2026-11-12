"""Run audits for stored sites, store results, notify, and schedule daily runs."""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .audit import diff_reports, run_audit

log = logging.getLogger("seo_auditor")
AUDIT_HOUR = int(os.environ.get("AUDIT_HOUR_UTC", "6"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # Slack/Discord/any JSON webhook
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS", "0") == "1"


def validate_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Please enter a website URL.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("That doesn't look like a valid website URL.")
    if not ALLOW_PRIVATE:  # basic SSRF protection: refuse internal addresses
        try:
            infos = socket.getaddrinfo(p.hostname, None)
        except socket.gaierror:
            raise ValueError(f"Could not resolve {p.hostname}.")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("Internal/private addresses can't be audited.")
    return f"{p.scheme}://{p.netloc.lower()}{p.path or '/'}"


def audit_site(site_id: int):
    s = db.site(site_id)
    if not s or db.is_running(site_id):
        return
    prev = db.latest_report(site_id)
    aid = db.start_audit(site_id)
    try:
        report = run_audit(s["url"], max_pages=s["max_pages"])
        if report["summary"]["pages_ok"] == 0:
            raise RuntimeError("Could not load any page from this site (check the URL or whether it blocks bots).")
        diff = diff_reports(prev["report"] if prev else None, report)
        db.finish_audit(aid, report, diff)
        notify(s, report, diff)
        log.info("Audited %s score=%s", s["url"], report["score"])
    except Exception as e:  # keep the scheduler alive
        log.exception("Audit failed for %s", s["url"])
        db.finish_audit(aid, None, None, error=str(e)[:500])


def audit_in_background(site_id: int):
    threading.Thread(target=audit_site, args=(site_id,), daemon=True).start()


def audit_all():
    for s in db.sites(active_only=True):
        audit_site(s["id"])


def notify(site: dict, report: dict, diff: dict):
    if not WEBHOOK_URL:
        return
    top = [i for i in report["issues"] if i["severity"] in ("critical", "high")][:5]
    delta = diff.get("score_delta", 0)
    lines = [f"*SEO audit: {site['url']}* — score {report['score']}/100"
             + ("" if diff.get("first_run") else f" ({'+' if delta >= 0 else ''}{delta})")]
    if diff.get("new"):
        lines.append("New issues: " + "; ".join(diff["new"][:5]))
    if diff.get("resolved"):
        lines.append("Resolved: " + "; ".join(diff["resolved"][:5]))
    if top:
        lines.append("Top action items:\n" + "\n".join(f"• [{i['severity']}] {i['title']} ({i['count']})" for i in top))
    try:
        httpx.post(WEBHOOK_URL, json={"text": "\n".join(lines)}, timeout=10)
    except Exception:
        log.warning("Webhook notification failed")


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(audit_all, CronTrigger(hour=AUDIT_HOUR, minute=0), id="daily-audit",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.start()
    log.info("Daily audits scheduled at %02d:00 UTC", AUDIT_HOUR)
    return sched
