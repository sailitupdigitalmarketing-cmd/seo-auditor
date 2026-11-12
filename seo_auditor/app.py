"""FastAPI web app: add sites, view audits and prioritized action items."""
from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import db, jobs
from .checks import SEVERITY_ORDER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app):
    db.init()
    db.reset_stale_running()
    sched = jobs.start_scheduler()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="SEO Auditor", lifespan=lifespan)


def sparkline(points: list[int], w=320, h=60) -> str:
    if len(points) < 2:
        return ""
    step = w / (len(points) - 1)
    lo, hi = max(0, min(points) - 5), min(100, max(points) + 5)
    rng = max(hi - lo, 1)
    coords = " ".join(f"{i*step:.1f},{h - ((p - lo) / rng) * h:.1f}" for i, p in enumerate(points))
    return coords


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "index.html", {
        "sites": db.sites(), "error": error, "hour": jobs.AUDIT_HOUR})


@app.post("/sites")
def add_site(url: str = Form(...), max_pages: int = Form(100)):
    try:
        clean = jobs.validate_url(url)
    except ValueError as e:
        return RedirectResponse(f"/?error={e}", status_code=303)
    sid = db.add_site(clean, max(1, min(max_pages, 500)))
    jobs.audit_in_background(sid)
    return RedirectResponse(f"/sites/{sid}", status_code=303)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_view(request: Request, site_id: int):
    s = db.site(site_id)
    if not s:
        raise HTTPException(404)
    latest = db.latest_report(site_id)
    hist = db.history(site_id)
    scores = [h["score"] for h in hist if h["status"] == "done"]
    grouped = {sev: [] for sev in SEVERITY_ORDER}
    if latest:
        for i in latest["report"]["issues"]:
            grouped[i["severity"]].append(i)
    return templates.TemplateResponse(request, "site.html", {
        "site": s, "latest": latest, "history": hist, "grouped": grouped,
        "running": db.is_running(site_id), "spark": sparkline(scores), "scores": scores,
        "last_error": hist[-1]["error"] if hist and hist[-1]["status"] == "failed" else None,
        "hour": jobs.AUDIT_HOUR})


@app.post("/sites/{site_id}/run")
def run_now(site_id: int):
    if not db.site(site_id):
        raise HTTPException(404)
    jobs.audit_in_background(site_id)
    return RedirectResponse(f"/sites/{site_id}", status_code=303)


@app.post("/sites/{site_id}/delete")
def delete(site_id: int):
    db.delete_site(site_id)
    return RedirectResponse("/", status_code=303)


@app.get("/sites/{site_id}/actions.csv")
def export_csv(site_id: int):
    latest = db.latest_report(site_id)
    if not latest:
        raise HTTPException(404, "No completed audit yet")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["priority", "category", "issue", "how_to_fix", "url", "detail"])
    for i in latest["report"]["issues"]:
        for u in i["pages"]:
            w.writerow([i["severity"], i["category"], i["title"], i["fix"], u, i["details"].get(u, "")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=seo-actions-{site_id}.csv"})


@app.get("/api/sites")
def api_sites():
    return db.sites()


@app.get("/api/sites/{site_id}/latest")
def api_latest(site_id: int):
    latest = db.latest_report(site_id)
    if not latest:
        raise HTTPException(404, "No completed audit yet")
    return JSONResponse(latest)


@app.get("/healthz")
def health():
    return {"ok": True}
