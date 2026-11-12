# SEO Auditor

Enter any website URL and it is audited automatically every day. Each audit gives a 0–100 score, prioritized action items (Critical → Low) with concrete fixes, the affected pages, and a "what changed since yesterday" summary.

## What it checks
- **Technical:** HTTPS, robots.txt (missing / blocks everything / no sitemap line), XML sitemap (missing, lists broken/redirecting/noindex URLs, indexable pages missing from it), mixed content
- **Meta tags:** title and meta description (missing, too long/short, duplicated across pages)
- **Indexability:** noindex pages, missing or cross-domain canonical
- **Content:** missing/multiple H1, skipped heading levels, thin content, images without alt, missing `lang`, unfriendly URLs
- **Links:** broken internal and external links, pages returning 4xx/5xx, redirect chains
- **Mobile & social:** viewport tag, Open Graph tags
- **Structured data:** JSON-LD missing on the homepage or invalid
- **Performance (server side):** slow responses, very large HTML

## Run with Docker
```bash
docker build -t seo-auditor .
docker run -d -p 8000:8000 -v seo-data:/data --name seo-auditor seo-auditor
```
Open http://localhost:8000, enter a URL, and click **Start audit**.

## Run locally
```bash
pip install -r requirements.txt
uvicorn seo_auditor.app:app --port 8000
```
One-off audit from the command line: `python -m seo_auditor https://example.com --max-pages 50`

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `AUDIT_HOUR_UTC` | `6` | Hour (UTC) the daily audit of all sites runs |
| `SEO_DB_PATH` | `data/seo.db` | SQLite database file |
| `WEBHOOK_URL` | – | Slack/Discord-style webhook; receives the score, new/resolved issues and top action items after every audit |
| `ALLOW_PRIVATE_URLS` | `0` | Set to `1` to allow auditing localhost/private IPs (blocked by default for safety) |

## API
- `GET /api/sites`: all monitored sites with their last score
- `GET /api/sites/{id}/latest`: full latest report (JSON)
- `GET /sites/{id}/actions.csv`: action items as CSV

## Tests
`pip install pytest && pytest -q`, which audits a local fixture site with known problems in `tests/site`.
