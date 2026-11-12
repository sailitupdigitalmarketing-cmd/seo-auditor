"""SEO checks: turn crawl data into prioritized, actionable issues."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

from .crawler import CrawlResult

SEVERITY_WEIGHT = {"critical": 10, "high": 6, "medium": 3, "low": 1}
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


@dataclass
class Issue:
    id: str
    category: str
    severity: str
    title: str
    why: str
    fix: str
    pages: list = field(default_factory=list)  # affected URLs (or details)
    details: dict = field(default_factory=dict)  # url -> extra info

    def to_dict(self):
        d = asdict(self)
        d["count"] = len(self.pages)
        return d


class IssueSet:
    def __init__(self):
        self.issues: dict[str, Issue] = {}

    def add(self, id, category, severity, title, why, fix, page=None, detail=None):
        iss = self.issues.get(id)
        if not iss:
            iss = self.issues[id] = Issue(id, category, severity, title, why, fix)
        if page is not None and page not in iss.pages:
            iss.pages.append(page)
            if detail is not None:
                iss.details[page] = detail
        return iss


def run_checks(cr: CrawlResult) -> list[Issue]:
    s = IssueSet()
    html_pages = [p for p in cr.pages if p.status == 200 and p.is_html and not p.error]
    indexable = [p for p in html_pages if "noindex" not in p.robots_meta]

    # ---------- Technical / site-level ----------
    if not cr.https:
        s.add("no-https", "Technical", "critical", "Site is not served over HTTPS",
              "Google uses HTTPS as a ranking signal and browsers mark HTTP sites as 'Not secure'.",
              "Install a TLS certificate (e.g. free Let's Encrypt) and 301-redirect all http:// URLs to https://.",
              page=cr.start_url)
    if cr.robots_txt is None:
        s.add("robots-missing", "Technical", "medium", "robots.txt is missing",
              "robots.txt tells crawlers what to index and where your sitemap lives.",
              "Create /robots.txt with at least:\nUser-agent: *\nAllow: /\nSitemap: https://yourdomain/sitemap.xml",
              page=cr.start_url, detail=f"HTTP {cr.robots_status}")
    else:
        lines = [l.strip().lower() for l in cr.robots_txt.splitlines()]
        ua_all, blocks_all = False, False
        for l in lines:
            if l.startswith("user-agent:"):
                ua_all = l.split(":", 1)[1].strip() == "*"
            elif ua_all and l.replace(" ", "") == "disallow:/":
                blocks_all = True
        if blocks_all:
            s.add("robots-blocks-all", "Technical", "critical", "robots.txt blocks the entire site",
                  "'Disallow: /' for all user agents stops search engines from crawling any page.",
                  "Remove 'Disallow: /' under 'User-agent: *' in robots.txt (unless this is a staging site).",
                  page=cr.start_url)
        if not any(l.startswith("sitemap:") for l in lines):
            s.add("robots-no-sitemap", "Technical", "low", "robots.txt does not reference a sitemap",
                  "Listing your sitemap in robots.txt helps all search engines discover it.",
                  "Add a line 'Sitemap: https://yourdomain/sitemap.xml' to robots.txt.", page=cr.start_url)
    valid_sitemaps = [sm for sm in cr.sitemaps if sm["status"] == 200 and not sm["error"]]
    if not valid_sitemaps:
        s.add("sitemap-missing", "Technical", "high", "No valid XML sitemap found",
              "A sitemap helps search engines discover and prioritise all your pages.",
              "Generate an XML sitemap (most CMSs have a plugin) at /sitemap.xml and submit it in Google Search Console.",
              page=cr.sitemaps[0]["url"] if cr.sitemaps else cr.start_url,
              detail=(cr.sitemaps[0].get("error") or f"HTTP {cr.sitemaps[0]['status']}") if cr.sitemaps else None)
    else:
        sm_urls = {u for sm in valid_sitemaps for u in sm["urls"]}
        site_host = urlparse(cr.start_url).netloc.lower().removeprefix("www.")
        foreign = [u for u in sm_urls if urlparse(u).netloc.lower().removeprefix("www.") != site_host]
        if foreign and len(foreign) == len(sm_urls):
            s.add("sitemap-wrong-domain", "Technical", "high", "Sitemap lists URLs from a different domain",
                  "Search engines ignore sitemap URLs that aren't on the site's own domain, so none of your pages are submitted.",
                  f"Regenerate the sitemap so every <loc> uses this site's domain ({site_host}).",
                  page=valid_sitemaps[0]["url"], detail=f"{len(foreign)} URLs, e.g. {sorted(foreign)[0]}")
        elif foreign:
            for u in sorted(foreign)[:50]:
                s.add("sitemap-wrong-domain", "Technical", "medium", "Sitemap lists URLs from a different domain",
                      "Search engines ignore sitemap URLs that aren't on the site's own domain.",
                      f"Remove or fix these entries so they use {site_host}.", page=u)
        if not sm_urls:
            s.add("sitemap-empty", "Technical", "high", "Sitemap contains no URLs",
                  "An empty sitemap gives search engines nothing to discover.",
                  "Make sure your sitemap generator lists every public page.", page=valid_sitemaps[0]["url"])
        status_by_url = {}
        for p in cr.pages:
            status_by_url[p.url] = p
        for u in sm_urls:
            p = status_by_url.get(u)
            if p and (p.status != 200 or p.redirects):
                s.add("sitemap-bad-url", "Technical", "medium", "Sitemap lists URLs that redirect or error",
                      "Sitemaps should only contain final, indexable 200 URLs; others waste crawl budget.",
                      "Update the sitemap to list only canonical URLs that return HTTP 200.",
                      page=u, detail=f"HTTP {p.status}" + (f", {p.redirects} redirect(s)" if p.redirects else ""))
            elif p and "noindex" in p.robots_meta:
                s.add("sitemap-noindex", "Technical", "medium", "Sitemap lists noindex pages",
                      "Listing pages you've asked Google not to index sends mixed signals.",
                      "Remove noindex pages from the sitemap, or remove the noindex tag if the page should rank.", page=u)
        crawled_ok = {p.final_url for p in indexable}
        missing = [u for u in crawled_ok if u not in sm_urls]
        for u in missing[:200]:
            s.add("not-in-sitemap", "Technical", "low", "Indexable pages missing from sitemap",
                  "Pages absent from the sitemap may be discovered and recrawled more slowly.",
                  "Add these pages to your XML sitemap (or make sure the sitemap generator includes them).", page=u)

    # ---------- Per-page checks ----------
    titles, descs = defaultdict(list), defaultdict(list)
    for p in cr.pages:
        if p.error:
            s.add("page-unreachable", "Links", "high", "Pages could not be loaded",
                  "Timeouts or connection errors stop users and crawlers reaching content.",
                  "Check server uptime, DNS and firewall rules for these URLs.", page=p.url, detail=p.error)
            continue
        if p.status >= 400:
            sev = "critical" if p.depth == 0 else "high"
            s.add("page-4xx-5xx", "Links", sev, "Linked pages return errors (4xx/5xx)",
                  "Broken pages waste crawl budget and frustrate users.",
                  "Restore these pages, or 301-redirect them to the closest relevant page and update internal links.",
                  page=p.url, detail=f"HTTP {p.status}")
            continue
        if p.redirects >= 2:
            s.add("redirect-chain", "Links", "medium", "Redirect chains (2+ hops)",
                  "Each hop slows the page and dilutes link equity.",
                  "Point links and redirects directly at the final URL.", page=p.url,
                  detail=f"{p.redirects} hops → {p.final_url}")
        if not p.is_html or p.status != 200:
            continue
        url = p.final_url or p.url
        noindex = "noindex" in p.robots_meta
        private_path = re.search(r"/(log-?in|sign-?in|sign-?up|register|forgot|reset|account|admin|cart|checkout|"
                                 r"thank|auth|dashboard)", urlparse(url).path.lower())
        if noindex and not private_path:
            s.add("noindex", "Indexability", "medium" if p.depth else "critical",
                  "Pages are set to noindex",
                  "These pages are excluded from search results. Fine for private/thank-you pages, harmful otherwise.",
                  "Remove the 'noindex' robots meta tag / X-Robots-Tag header from pages that should rank.", page=url)

        if p.rendered and p.raw_word_count < 50 and p.word_count >= 50:
            s.add("js-only-content", "Indexability", "medium", "Content only appears after JavaScript runs",
                  "Google can render JavaScript but does it later and less reliably; Bing, AI search tools and social "
                  "previews (WhatsApp, LinkedIn, Facebook) often see an empty page.",
                  "Serve pre-rendered HTML: enable server-side rendering or static pre-rendering in your framework/site "
                  "builder, or use a pre-rendering service, so the raw HTML already contains the text, headings and links.",
                  page=url, detail=f"{p.raw_word_count} words without JavaScript vs {p.word_count} with it")
        # Pages kept out of search don't need title/description fixes
        if not noindex:
            # Title
            if not p.title:
                s.add("title-missing", "Meta tags", "critical", "Missing <title> tag",
                      "The title is the headline shown in Google results and a primary ranking signal.",
                      "Add a unique <title> of 30–60 characters with the page's main keyword near the start.", page=url)
            else:
                if not noindex:
                    titles[p.title.strip().lower()].append(url)
                L = len(p.title)
                if L > 60:
                    s.add("title-long", "Meta tags", "medium", "Title too long (>60 chars)",
                          "Google truncates long titles, hiding your message.",
                          "Shorten titles to ≤60 characters, keeping the key phrase first.", page=url, detail=f"{L} chars: {p.title}")
                elif L < 30:
                    s.add("title-short", "Meta tags", "low", "Title too short (<30 chars)",
                          "Short titles miss the chance to target descriptive keywords.",
                          "Expand titles to 30–60 characters, e.g. '<Primary keyword> – <Benefit> | <Brand>'.", page=url,
                          detail=f"{L} chars: {p.title}")
            # Description
            if not p.meta_description:
                s.add("desc-missing", "Meta tags", "high", "Missing meta description",
                      "Without one Google picks random page text for the snippet, often lowering click-through rate.",
                      "Write a unique 120–160 character meta description summarising the page with a call to action.", page=url)
            else:
                if not noindex:
                    descs[p.meta_description.strip().lower()].append(url)
                L = len(p.meta_description)
                if L > 160:
                    s.add("desc-long", "Meta tags", "low", "Meta description too long (>160 chars)",
                          "Long descriptions get truncated in search results.",
                          "Trim to 120–160 characters.", page=url, detail=f"{L} chars")
                elif L < 70:
                    s.add("desc-short", "Meta tags", "low", "Meta description too short (<70 chars)",
                          "Short descriptions under-use valuable search result space.",
                          "Expand to 120–160 characters describing the page's value.", page=url, detail=f"{L} chars")
        # Canonical
        if not p.canonical:
            s.add("canonical-missing", "Indexability", "medium", "Missing canonical tag",
                  "Canonicals prevent duplicate-content issues from URL parameters, www/non-www, etc.",
                  'Add <link rel="canonical" href="<this page\'s preferred URL>"> in the <head>.', page=url)
        elif urlparse(p.canonical).netloc.lower().removeprefix("www.") != urlparse(url).netloc.lower().removeprefix("www."):
            s.add("canonical-offsite", "Indexability", "high", "Canonical points to another domain",
                  "This tells Google to index the other domain instead of yours.",
                  "Point the canonical to this page's own URL unless the cross-domain canonical is intentional.",
                  page=url, detail=p.canonical)
        # Headings
        if not p.h1:
            s.add("h1-missing", "Content", "high", "Missing H1 heading",
                  "The H1 tells users and search engines what the page is about.",
                  "Add one <h1> per page containing the page's primary topic/keyword.", page=url)
        elif len(p.h1) > 1:
            s.add("h1-multiple", "Content", "low", "Multiple H1 headings",
                  "Several H1s dilute the main topic signal.",
                  "Keep one H1 and change the others to H2/H3.", page=url, detail=f"{len(p.h1)} H1s")
        prev = 0
        for lvl in p.headings:
            if prev and lvl > prev + 1:
                s.add("heading-skip", "Content", "low", "Heading levels are skipped (e.g. H2 → H4)",
                      "A logical heading outline helps accessibility and content understanding.",
                      "Use headings in order (H1 → H2 → H3) without skipping levels.", page=url,
                      detail=f"H{prev} → H{lvl}")
                break
            prev = lvl
        # Content
        if p.word_count < 250 and not noindex:
            s.add("thin-content", "Content", "medium", "Thin content (<250 words)",
                  "Pages with little text rarely rank for competitive queries.",
                  "Expand with useful, original content (FAQs, details, examples) or noindex/merge low-value pages.",
                  page=url, detail=f"{p.word_count} words")
        if p.images_missing_alt:
            s.add("img-alt-missing", "Content", "medium", "Images missing alt text",
                  "Alt text helps image search ranking and is required for accessibility.",
                  "Add short descriptive alt attributes to each image (use alt=\"\" only for purely decorative images).",
                  page=url, detail=f"{len(p.images_missing_alt)} of {p.image_count} images")
        # Mobile / misc
        if not p.viewport:
            s.add("viewport-missing", "Mobile", "high", "Missing mobile viewport tag",
                  "Without it the page won't render properly on phones; Google indexes mobile-first.",
                  'Add <meta name="viewport" content="width=device-width, initial-scale=1"> to the <head>.', page=url)
        if not p.lang:
            s.add("lang-missing", "Content", "low", "Missing <html lang> attribute",
                  "The lang attribute helps search engines serve the right language audience.",
                  'Add a language to the root element, e.g. <html lang="en">.', page=url)
        if not p.og.get("og:title") or not p.og.get("og:image"):
            s.add("og-missing", "Social", "low", "Missing Open Graph tags (og:title / og:image)",
                  "Open Graph tags control how links look when shared on social media and chat apps.",
                  "Add og:title, og:description, og:image and og:url meta tags.", page=url)
        if not p.jsonld and p.depth == 0:
            s.add("schema-missing", "Structured data", "medium", "No structured data (JSON-LD) on homepage",
                  "Schema markup enables rich results (logos, ratings, FAQs, breadcrumbs) in Google.",
                  "Add JSON-LD for Organization/LocalBusiness and WebSite on the homepage; validate with Google's Rich Results Test.",
                  page=url)
        if p.jsonld_errors:
            s.add("schema-invalid", "Structured data", "high", "Invalid JSON-LD structured data",
                  "Malformed JSON-LD is ignored by Google.",
                  "Fix the JSON syntax and validate with https://search.google.com/test/rich-results.",
                  page=url, detail=f"{p.jsonld_errors} invalid block(s)")
        if p.mixed_content:
            s.add("mixed-content", "Technical", "high", "Mixed content (HTTP resources on HTTPS pages)",
                  "Browsers block or warn about insecure resources, breaking pages and trust.",
                  "Load all images, scripts and styles over https://.", page=url,
                  detail=", ".join(p.mixed_content[:3]))
        # Performance (server-side)
        if p.elapsed_ms > 1500:
            s.add("slow-response", "Performance", "high" if p.elapsed_ms > 3000 else "medium",
                  "Slow server response (>1.5s)",
                  "Slow pages rank lower and lose visitors; Core Web Vitals are a ranking factor.",
                  "Enable caching/CDN, optimise database queries, and upgrade hosting if needed.", page=url,
                  detail=f"{p.elapsed_ms} ms")
        if p.size_bytes > 1_500_000:
            s.add("heavy-html", "Performance", "medium", "Very large HTML (>1.5 MB)",
                  "Heavy HTML slows rendering, especially on mobile.",
                  "Remove inline data/scripts, paginate long lists and enable gzip/brotli compression.", page=url,
                  detail=f"{p.size_bytes // 1024} KB")
        if url.count("?") or any(c.isupper() for c in urlparse(url).path) or "_" in urlparse(url).path:
            s.add("url-unfriendly", "Content", "low", "URLs are not search-friendly",
                  "Short lowercase hyphenated URLs are easier to read, share and rank.",
                  "Use lowercase, hyphen-separated URLs without query parameters for content pages.", page=url)

    for t, urls in titles.items():
        if len(urls) > 1:
            for u in urls:
                s.add("title-duplicate", "Meta tags", "high", "Duplicate titles across pages",
                      "Identical titles make pages compete with each other and confuse Google.",
                      "Give every page a unique title describing its specific content.", page=u, detail=t[:80])
    for d, urls in descs.items():
        if len(urls) > 1:
            for u in urls:
                s.add("desc-duplicate", "Meta tags", "medium", "Duplicate meta descriptions",
                      "Duplicate descriptions reduce click-through and signal duplicate content.",
                      "Write a unique meta description for every page.", page=u, detail=d[:80])

    # ---------- Links ----------
    for l, lr in cr.links.items():
        if any(l == p.url for p in cr.pages):
            continue  # already reported as a page error
        broken = lr.status >= 400 or (lr.status == 0 and lr.error)
        if not broken or lr.status in (401, 403, 429, 999):
            continue  # skip bot-blocking responses
        internal = urlparse(l).netloc.lower().removeprefix("www.") == urlparse(cr.start_url).netloc.lower().removeprefix("www.")
        if internal:
            s.add("broken-internal", "Links", "high", "Broken internal links",
                  "Links to missing pages waste crawl budget and frustrate visitors.",
                  "Fix or remove these links, or 301-redirect the target to a live page.",
                  page=l, detail={"status": lr.status or lr.error, "found_on": lr.found_on[:5]})
        else:
            s.add("broken-external", "Links", "medium", "Broken external links",
                  "Links to dead external pages hurt user experience and perceived quality.",
                  "Update these links to a working source or remove them.",
                  page=l, detail={"status": lr.status or lr.error, "found_on": lr.found_on[:5]})

    issues = list(s.issues.values())
    issues.sort(key=lambda i: (SEVERITY_ORDER.index(i.severity), -len(i.pages)))
    return issues


def score(issues: list[Issue], page_count: int) -> int:
    """0-100 score. Each issue deducts weight scaled by share of pages affected (min 30% for site-wide)."""
    page_count = max(page_count, 1)
    penalty = 0.0
    for i in issues:
        share = min(1.0, len(i.pages) / page_count)
        penalty += SEVERITY_WEIGHT[i.severity] * (0.3 + 0.7 * share)
    return max(0, round(100 - penalty))
