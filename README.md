# SEO Auditor

Enter any website URL and it is audited automatically every day. Each audit gives a 0–100 score, prioritized action items (Critical → Low) with concrete fixes, the affected pages, and a "what changed since yesterday" summary.

## What it checks
- **Technical:** HTTPS, robots.txt (missing / blocks everything / no sitemap line), XML sitemap (missing, lists broken/redirecting/noindex URLs, indexable pages missing from it), mixed content
- **Meta tags:** title and meta description (missing, too long/short, duplicated across pages)
- **Indexability:** noindex pages, missing or cross-domain canonical
- **Content:** missing/multiple H1, skipped heading levels, thin content, images without alt, missing `lang`, unfriendly URLs
- **Links:** broken internal and external links, pages returning 4xx/5xx, redirect chains
- **Mobile & social:** viewport tag, Open Graph tags
- **JavaScript sites:** sites built as JavaScript apps (React, Vue, Lovable, Wix-style builders) are rendered in headless Chromium so the audit sees what Google sees, and flags content that only appears after JavaScript runs
- **Structured data:** JSON-LD missing on the homepage or invalid
- **Performance (server side):** slow responses, very large HTML

## Setup on GitHub (recommended, no server)
The workflow in `.github/workflows/daily-seo-audit.yml` audits every site in `sites.txt` daily at 06:00 UTC, saves the history in `reports/data/`, and publishes the reports to GitHub Pages.

1. Push this code to a GitHub repository.
2. In the repo, go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Add a website in either of these ways:
   - Edit `sites.txt` on GitHub and add one URL per line. Saving the file starts an audit right away.
   - Go to **Actions → Daily SEO audit → Run workflow** and enter the URL.
4. After a few minutes the report is live at `https://<your-user>.github.io/<repo>/`.

Optional:
- **Notifications:** add a repository secret `WEBHOOK_URL` (a Slack or Discord incoming webhook) to receive the score, new/resolved issues and top action items after each audit.
- **Crawl size:** add a repository variable `MAX_PAGES` (default 100).
- **Different time:** change the `cron` line in the workflow.

Note: GitHub Pages sites are public on free plans, so anyone with the link can see the reports.

## Run with Docker (optional self-hosted web app)
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

Static mode locally: `python -m seo_auditor.static_report add https://example.com && python -m seo_auditor.static_report run`, then open `public/index.html`.

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `AUDIT_HOUR_UTC` | `6` | Hour (UTC) the daily audit of all sites runs |
| `SEO_DB_PATH` | `data/seo.db` | SQLite database file |
| `WEBHOOK_URL` | – | Slack/Discord-style webhook; receives the score, new/resolved issues and top action items after every audit |
| `RENDER_JS` | `auto` | `auto` renders pages in headless Chromium when the raw HTML looks like an empty JS app; `always` or `never` to force |
| `ALLOW_PRIVATE_URLS` | `0` | Set to `1` to allow auditing localhost/private IPs (blocked by default for safety) |

## API
- `GET /api/sites`: all monitored sites with their last score
- `GET /api/sites/{id}/latest`: full latest report (JSON)
- `GET /sites/{id}/actions.csv`: action items as CSV

## Tests
`pip install pytest && pytest -q`, which audits a local fixture site with known problems in `tests/site`.
