"""SQLite storage for sites and audit history."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("SEO_DB_PATH", "data/seo.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  max_pages INTEGER NOT NULL DEFAULT 100,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,            -- running | done | failed
  score INTEGER,
  report TEXT,
  diff TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_audits_site ON audits(site_id, id DESC);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)


def add_site(url: str, max_pages: int = 100) -> int:
    with _lock, conn() as c:
        row = c.execute("SELECT id FROM sites WHERE url=?", (url,)).fetchone()
        if row:
            c.execute("UPDATE sites SET active=1 WHERE id=?", (row["id"],))
            return row["id"]
        return c.execute("INSERT INTO sites(url,max_pages,created_at) VALUES(?,?,?)",
                         (url, max_pages, now())).lastrowid


def delete_site(site_id: int):
    with _lock, conn() as c:
        c.execute("DELETE FROM audits WHERE site_id=?", (site_id,))
        c.execute("DELETE FROM sites WHERE id=?", (site_id,))


def sites(active_only=False) -> list[dict]:
    q = """SELECT s.*, a.score AS last_score, a.created_at AS last_run, a.status AS last_status
           FROM sites s LEFT JOIN audits a ON a.id = (SELECT MAX(id) FROM audits WHERE site_id=s.id)"""
    if active_only:
        q += " WHERE s.active=1"
    with conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY s.id")]


def site(site_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        return dict(r) if r else None


def start_audit(site_id: int) -> int:
    with _lock, conn() as c:
        return c.execute("INSERT INTO audits(site_id,created_at,status) VALUES(?,?,'running')",
                         (site_id, now())).lastrowid


def finish_audit(audit_id: int, report: dict | None, diff: dict | None, error: str | None = None):
    with _lock, conn() as c:
        c.execute("UPDATE audits SET status=?, score=?, report=?, diff=?, error=? WHERE id=?",
                  ("failed" if error else "done", report["score"] if report else None,
                   json.dumps(report) if report else None, json.dumps(diff) if diff else None, error, audit_id))


def is_running(site_id: int) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM audits WHERE site_id=? AND status='running'", (site_id,)).fetchone() is not None


def latest_report(site_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM audits WHERE site_id=? AND status='done' ORDER BY id DESC LIMIT 1",
                      (site_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["report"] = json.loads(d["report"])
    d["diff"] = json.loads(d["diff"]) if d["diff"] else None
    return d


def history(site_id: int, limit: int = 60) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, created_at, status, score, error FROM audits WHERE site_id=? ORDER BY id DESC LIMIT ?",
                         (site_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def reset_stale_running():
    """Audits left 'running' by a restart are marked failed."""
    with _lock, conn() as c:
        c.execute("UPDATE audits SET status='failed', error='Interrupted by restart' WHERE status='running'")
