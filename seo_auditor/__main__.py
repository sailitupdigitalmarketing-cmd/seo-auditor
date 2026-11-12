import argparse, json
from .audit import run_audit

ap = argparse.ArgumentParser(description="Run a one-off SEO audit")
ap.add_argument("url"); ap.add_argument("--max-pages", type=int, default=50)
ap.add_argument("--json", action="store_true")
a = ap.parse_args()
r = run_audit(a.url, max_pages=a.max_pages)
if a.json:
    print(json.dumps(r, indent=2))
else:
    print(f"{r['url']}  score={r['score']}  {r['summary']}")
    for i in r["issues"]:
        print(f"[{i['severity'].upper():8}] {i['title']} ({i['count']})")
