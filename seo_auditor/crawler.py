"""Async same-site crawler that extracts SEO-relevant data from each page."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "SEOAuditorBot/0.1 (+automated site audit)"
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4",
            ".mp3", ".css", ".js", ".ico", ".woff", ".woff2", ".ttf", ".xml", ".json")


@dataclass
class Page:
    url: str
    status: int = 0
    final_url: str = ""
    redirects: int = 0
    elapsed_ms: int = 0
    size_bytes: int = 0
    content_type: str = ""
    error: str = ""
    depth: int = 0
    title: str | None = None
    meta_description: str | None = None
    canonical: str | None = None
    robots_meta: str = ""
    viewport: bool = False
    lang: str | None = None
    og: dict = field(default_factory=dict)
    h1: list = field(default_factory=list)
    headings: list = field(default_factory=list)  # list of heading levels in order
    images_missing_alt: list = field(default_factory=list)
    image_count: int = 0
    word_count: int = 0
    internal_links: list = field(default_factory=list)
    external_links: list = field(default_factory=list)
    jsonld: list = field(default_factory=list)  # parsed @type values
    jsonld_errors: int = 0
    mixed_content: list = field(default_factory=list)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type


@dataclass
class LinkResult:
    url: str
    status: int
    error: str = ""
    found_on: list = field(default_factory=list)


@dataclass
class CrawlResult:
    start_url: str
    pages: list
    links: dict  # url -> LinkResult (for non-crawled targets + crawled)
    robots_txt: str | None
    robots_status: int
    sitemaps: list  # list of dicts {url, status, urls:[...], error}
    https: bool
    duration_s: float


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path or "/"
    return p._replace(path=path, netloc=p.netloc.lower(), scheme=p.scheme.lower()).geturl()


def same_site(a: str, b: str) -> bool:
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb


def parse_html(page: Page, html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    if soup.html and soup.html.get("lang"):
        page.lang = soup.html.get("lang")
    if soup.title and soup.title.string is not None:
        page.title = soup.title.string.strip()
    elif soup.title:
        page.title = soup.title.get_text(strip=True)
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").lower()
        prop = (m.get("property") or "").lower()
        content = (m.get("content") or "").strip()
        if name == "description":
            page.meta_description = content
        elif name == "robots":
            page.robots_meta = content.lower()
        elif name == "viewport":
            page.viewport = True
        elif prop.startswith("og:"):
            page.og[prop] = content
    link = soup.find("link", rel=lambda v: v and "canonical" in [r.lower() for r in (v if isinstance(v, list) else [v])])
    if link and link.get("href"):
        page.canonical = urljoin(page.final_url or page.url, link["href"].strip())
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        lvl = int(h.name[1])
        page.headings.append(lvl)
        if lvl == 1:
            page.h1.append(h.get_text(" ", strip=True))
    imgs = soup.find_all("img")
    page.image_count = len(imgs)
    for img in imgs:
        if img.get("alt") is None or (img.get("alt", "").strip() == "" and img.get("role") != "presentation"):
            src = img.get("src") or img.get("data-src") or ""
            page.images_missing_alt.append(urljoin(page.final_url, src) if src else "(inline)")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
            items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
            for it in items:
                if isinstance(it, dict) and it.get("@type"):
                    t = it["@type"]
                    page.jsonld.extend(t if isinstance(t, list) else [t])
        except Exception:
            page.jsonld_errors += 1
    base = page.final_url or page.url
    if urlparse(base).scheme == "https":
        for tag, attr in (("img", "src"), ("script", "src"), ("link", "href"), ("iframe", "src")):
            for el in soup.find_all(tag):
                v = el.get(attr)
                if v and v.startswith("http://") and not (tag == "link" and "stylesheet" not in (el.get("rel") or [])):
                    page.mixed_content.append(v)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absu = normalize(urljoin(base, href))
        if urlparse(absu).scheme not in ("http", "https"):
            continue
        (page.internal_links if same_site(absu, base) else page.external_links).append(absu)
    for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    body = soup.body or soup
    page.word_count = len(body.get_text(" ", strip=True).split())


class Crawler:
    def __init__(self, start_url: str, max_pages: int = 100, concurrency: int = 8,
                 timeout: float = 15.0, check_external: bool = True, max_external: int = 200):
        if not urlparse(start_url).scheme:
            start_url = "https://" + start_url
        self.start_url = normalize(start_url)
        self.max_pages = max_pages
        self.concurrency = concurrency
        self.timeout = timeout
        self.check_external = check_external
        self.max_external = max_external
        self.robots = urllib.robotparser.RobotFileParser()

    async def _fetch(self, client: httpx.AsyncClient, url: str, depth: int) -> tuple[Page, str]:
        page = Page(url=url, depth=depth)
        t0 = time.perf_counter()
        try:
            r = await client.get(url)
            page.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            page.status = r.status_code
            page.final_url = normalize(str(r.url))
            page.redirects = len(r.history)
            page.size_bytes = len(r.content)
            page.content_type = r.headers.get("content-type", "").lower()
            xrobots = r.headers.get("x-robots-tag", "").lower()
            html = r.text if page.is_html else ""
            if html:
                parse_html(page, html)
            if xrobots:
                page.robots_meta = (page.robots_meta + "," + xrobots).strip(",")
            return page, html
        except Exception as e:  # network errors
            page.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            page.error = f"{type(e).__name__}: {e}"[:200]
            return page, ""

    async def _check_link(self, client: httpx.AsyncClient, url: str) -> LinkResult:
        try:
            r = await client.head(url)
            if r.status_code in (403, 405, 400, 501) or r.status_code >= 500:
                r = await client.get(url)
            return LinkResult(url, r.status_code)
        except Exception as e:
            return LinkResult(url, 0, f"{type(e).__name__}")

    async def _load_robots_and_sitemaps(self, client: httpx.AsyncClient, origin: str):
        robots_txt, robots_status = None, 0
        sitemap_urls = []
        try:
            r = await client.get(origin + "/robots.txt")
            robots_status = r.status_code
            if r.status_code == 200 and "<html" not in r.text[:500].lower():
                robots_txt = r.text
                self.robots.parse(robots_txt.splitlines())
                for line in robots_txt.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sitemap_urls.append(line.split(":", 1)[1].strip())
            else:
                self.robots.parse([])
        except Exception:
            self.robots.parse([])
        if not sitemap_urls:
            sitemap_urls = [origin + "/sitemap.xml"]
        sitemaps = []
        seen = set()
        queue = list(sitemap_urls)
        while queue and len(sitemaps) < 20:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            entry = {"url": sm, "status": 0, "urls": [], "error": ""}
            try:
                r = await client.get(sm)
                entry["status"] = r.status_code
                if r.status_code == 200:
                    soup = BeautifulSoup(r.content, "xml")
                    if soup.find("sitemapindex"):
                        queue.extend(loc.get_text(strip=True) for loc in soup.find_all("loc"))
                        entry["index"] = True
                    elif soup.find("urlset"):
                        entry["urls"] = [normalize(loc.get_text(strip=True)) for loc in soup.find_all("loc")]
                    else:
                        entry["error"] = "Not a valid XML sitemap"
            except Exception as e:
                entry["error"] = type(e).__name__
            sitemaps.append(entry)
        return robots_txt, robots_status, sitemaps

    async def run(self) -> CrawlResult:
        t0 = time.perf_counter()
        limits = httpx.Limits(max_connections=self.concurrency * 2)
        async with httpx.AsyncClient(follow_redirects=True, timeout=self.timeout, limits=limits,
                                     headers={"User-Agent": USER_AGENT}) as client:
            # Resolve the start URL first (handles http->https and www redirects)
            first, html = await self._fetch(client, self.start_url, 0)
            base = first.final_url or self.start_url
            p = urlparse(base)
            origin = f"{p.scheme}://{p.netloc}"
            robots_txt, robots_status, sitemaps = await self._load_robots_and_sitemaps(client, origin)

            pages: dict[str, Page] = {first.url: first}
            seen = {first.url, base}
            queue: list[tuple[str, int]] = []

            def enqueue_from(pg: Page):
                for l in pg.internal_links:
                    if l in seen:
                        continue
                    if l.lower().endswith(SKIP_EXT):
                        continue
                    seen.add(l)
                    if self.robots.can_fetch(USER_AGENT, l):
                        queue.append((l, pg.depth + 1))

            enqueue_from(first)
            # Seed with sitemap URLs too, so orphan pages get audited
            for sm in sitemaps:
                for u in sm["urls"]:
                    if u not in seen and same_site(u, base):
                        seen.add(u)
                        queue.append((u, 1))

            sem = asyncio.Semaphore(self.concurrency)

            async def worker(url, depth):
                async with sem:
                    return await self._fetch(client, url, depth)

            while queue and len(pages) < self.max_pages:
                batch = []
                while queue and len(pages) + len(batch) < self.max_pages and len(batch) < self.concurrency * 2:
                    batch.append(queue.pop(0))
                results = await asyncio.gather(*(worker(u, d) for u, d in batch))
                for pg, _ in results:
                    pages[pg.url] = pg
                    enqueue_from(pg)

            # Link check: internal links that weren't crawled + external links
            crawled = {}
            for pg in pages.values():
                crawled[pg.url] = pg.status
                if pg.final_url:
                    crawled.setdefault(pg.final_url, pg.status)
            found_on: dict[str, list] = {}
            for pg in pages.values():
                for l in set(pg.internal_links) | (set(pg.external_links) if self.check_external else set()):
                    found_on.setdefault(l, []).append(pg.url)
            links: dict[str, LinkResult] = {}
            to_check = []
            ext_count = 0
            for l, srcs in found_on.items():
                if l in crawled:
                    links[l] = LinkResult(l, crawled[l], found_on=srcs)
                else:
                    if not same_site(l, base):
                        ext_count += 1
                        if ext_count > self.max_external:
                            continue
                    elif l.lower().endswith(SKIP_EXT):
                        pass
                    to_check.append(l)

            async def lworker(u):
                async with sem:
                    return await self._check_link(client, u)

            for lr in await asyncio.gather(*(lworker(u) for u in to_check)):
                lr.found_on = found_on[lr.url]
                links[lr.url] = lr

        return CrawlResult(
            start_url=base, pages=list(pages.values()), links=links, robots_txt=robots_txt,
            robots_status=robots_status, sitemaps=sitemaps, https=base.startswith("https://"),
            duration_s=round(time.perf_counter() - t0, 1),
        )


def crawl(start_url: str, **kw) -> CrawlResult:
    return asyncio.run(Crawler(start_url, **kw).run())
