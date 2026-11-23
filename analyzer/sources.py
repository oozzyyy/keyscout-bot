import asyncio
import gzip
import re
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; KeyScoutBot/0.1)"
MAX_PAGE_BYTES = 3_000_000
MSK = ZoneInfo("Europe/Moscow")


@dataclass
class FeedItem:
    key: str
    title: str
    body: str
    link: str
    published: datetime


class NotAFeed(Exception):
    def __init__(self, text: str):
        super().__init__("not a feed")
        self.text = text


def html_to_text(html: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        text = BeautifulSoup(html, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def clean_link(link: str) -> str:
    return re.sub(r"[?&]utm_[^#]*$", "", link.strip())


def sanitize_xml(xml: str) -> str:
    xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml)
    return re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)", "&amp;", xml)


def parse_feed(xml: str) -> list[FeedItem]:
    try:
        root = ET.fromstring(xml.encode())
    except ET.ParseError:
        root = ET.fromstring(sanitize_xml(xml).encode())
    atom = "{http://www.w3.org/2005/Atom}"
    now = datetime.now(timezone.utc)
    out = []
    for el in [*root.iter("item"), *root.iter(atom + "entry")]:
        def txt(*tags):
            for t in tags:
                node = el.find(t)
                if node is not None and (node.text or "").strip():
                    return node.text
            return ""

        title = html_to_text(txt("title", atom + "title"))
        body = html_to_text(txt("description", atom + "summary", atom + "content",
                                "{http://purl.org/rss/1.0/modules/content/}encoded"))
        link = txt("link").strip()
        if not link:
            node = el.find(atom + "link")
            link = node.get("href", "") if node is not None else ""
        link = clean_link(link)
        published = parse_date(txt("pubDate", atom + "published", atom + "updated")) or now
        key = link or txt("guid", atom + "id").strip() or title
        if key:
            out.append(FeedItem(key, title, body, link, published))
    return out


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    return re.sub(r"\s+", " ", root.get_text(" ")).strip()


HABR_FLOWS = ["develop", "admin", "design", "management", "marketing", "popsci"]


@dataclass
class Source:
    key: str
    name: str
    feeds: list[str] = field(default_factory=list)
    kind: str = "rss"
    backfill: str | None = None
    sitemap: str = ""
    aliases: list[str] = field(default_factory=list)
    entry: str = ""
    group: str = ""

    def __post_init__(self):
        self.entry = self.entry or next((a for a in self.aliases if "://" in a), "") or self.feeds[0]


def rss(key: str, name: str, url: str, domain: str, group: str, sitemap: str = "") -> "Source":
    return Source(key, name, [url], aliases=[domain], entry=url, group=group,
                  backfill="sitemap" if sitemap else None, sitemap=sitemap)


KNOWN = [
    Source("ria", "РИА Новости", ["https://ria.ru/export/rss2/archive/index.xml"], backfill="ria_sitemap",
           aliases=["ria.ru"], group="Новости"),
    Source("kommersant", "Коммерсантъ", ["https://www.kommersant.ru/RSS/news.xml"], aliases=["kommersant.ru"],
           group="Бизнес"),
    Source("habr", "Хабр",
           ["https://habr.com/ru/rss/news/?limit=100"]
           + [f"https://habr.com/ru/rss/flows/{f}/articles/?limit=100" for f in HABR_FLOWS],
           backfill="habr_news_pages",
           aliases=["habr.com", "https://habr.com/ru/rss/articles/?limit=100", "https://habr.com/ru/rss/articles/"],
           group="Технологии"),
    Source("vc", "vc.ru", kind="vc", aliases=["vc.ru", "https://vc.ru/rss/all", "https://vc.ru/rss"], group="Бизнес"),
    Source("3dnews", "3DNews", ["https://3dnews.ru/news/rss/"], aliases=["3dnews.ru"], group="Технологии"),
    rss("aif", "АиФ", "https://aif.ru/rss/news.php", "aif.ru", "Новости"),
    rss("interfax", "Интерфакс", "https://www.interfax.ru/rss.asp", "interfax.ru", "Новости",
        "https://www.interfax.ru/SEO_SiteMapLastChanges.xml"),
    rss("lenta", "Лента.ру", "https://lenta.ru/rss/news", "lenta.ru", "Новости", "https://lenta.ru/sitemap.xml.gz"),
    rss("iz", "Известия", "https://iz.ru/xml/rss/all.xml", "iz.ru", "Новости", "https://iz.ru/export/sitemap/last/xml"),
    rss("rg", "Российская газета", "https://rg.ru/xml/index.xml", "rg.ru", "Новости", "https://rg.ru/sitemaps/news.xml"),
    rss("rbc", "РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "rbc.ru", "Бизнес",
        "https://www.rbc.ru/sitemap_index.xml"),
    rss("vedomosti", "Ведомости", "https://www.vedomosti.ru/rss/news", "vedomosti.ru", "Бизнес"),
    rss("thebell", "The Bell", "https://thebell.io/feed", "thebell.io", "Бизнес"),
    rss("frank", "Frank Media", "https://frankmedia.ru/feed", "frankmedia.ru", "Бизнес"),
    rss("banki", "Банки.ру", "https://www.banki.ru/xml/news.rss", "banki.ru", "Бизнес"),
    rss("rb", "RB.ru", "https://rb.ru/feeds/all/", "rb.ru", "Бизнес", "https://rb.ru/sitemap-news.xml"),
    rss("cnews", "CNews", "https://www.cnews.ru/inc/rss/news.xml", "cnews.ru", "Технологии"),
    rss("ixbt", "iXBT", "https://www.ixbt.com/export/news.rss", "ixbt.com", "Технологии",
        "https://www.ixbt.com/export/google_news.xml"),
    rss("ferra", "Ferra.ru", "https://www.ferra.ru/exports/rss.xml", "ferra.ru", "Технологии"),
    rss("overclockers", "Overclockers", "https://overclockers.ru/rss/all.rss", "overclockers.ru", "Технологии",
        "https://overclockers.ru/sitemap/google_news.xml.gz"),
    rss("hightech", "Хайтек+", "https://hightech.plus/feed.rss", "hightech.plus", "Технологии"),
    rss("kod", "Код Дурова", "https://kod.ru/rss", "kod.ru", "Технологии", "https://kod.ru/sitemap-news.xml"),
    rss("securitylab", "SecurityLab", "https://www.securitylab.ru/_services/export/rss/", "securitylab.ru",
        "Технологии"),
    rss("nplus1", "N+1", "https://nplus1.ru/rss", "nplus1.ru", "Наука",
        "https://nplus1.ru/material-sitemap-news-1.xml"),
    rss("naked", "Naked Science", "https://naked-science.ru/feed", "naked-science.ru", "Наука"),
]

NAMES = {a: src.name for src in KNOWN for a in src.aliases if "://" not in a} | {"tass.ru": "ТАСС"}


def host_of(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    return (urlparse(url).hostname or url).removeprefix("www.")


def display_name(url: str) -> str:
    host = host_of(url)
    for domain, name in NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def resolve(site: str) -> Source:
    s = site.strip().rstrip("/")
    bare = s.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    for src in KNOWN:
        entries = {a.rstrip("/") for a in src.aliases + src.feeds}
        if s in entries or bare in {a.rstrip("/") for a in src.aliases}:
            return src
    url = s if s.startswith(("http://", "https://")) else "https://" + s
    return Source(key=url, name=display_name(url), feeds=[url], kind="auto")


async def fetch_recent(client: httpx.AsyncClient, src: Source, stop: datetime) -> tuple[list[FeedItem], datetime]:
    if src.kind == "vc":
        return await fetch_vc(client, stop)
    items: list[FeedItem] = []
    feed_oldest: list[datetime] = []
    for url in src.feeds:
        resp = await get_feed(client, url)
        body = resp.content[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="ignore")
        if not ("xml" in resp.headers.get("content-type", "") or body.lstrip().startswith("<?xml")):
            raise NotAFeed(page_text(body))
        got = parse_feed(body)
        items += got
        if got:
            feed_oldest.append(min(i.published for i in got))
    reach = max(feed_oldest) if feed_oldest else datetime.now(timezone.utc)
    return items, reach


async def fetch_vc(client: httpx.AsyncClient, stop: datetime, max_pages: int = 400) -> tuple[list[FeedItem], datetime]:
    base = "https://api.vc.ru/v2.8/timeline?allSite=true&sorting=new"
    items: list[FeedItem] = []
    url = base
    reach = datetime.now(timezone.utc)
    for _ in range(max_pages):
        resp = await client.get(url)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        page = result.get("items", [])
        if not page:
            break
        for it in page:
            d = it.get("data", {})
            if not d.get("isPublished", True) or d.get("isRemovedByUserRequest"):
                continue
            published = datetime.fromtimestamp(d.get("date", 0), timezone.utc)
            texts = [b.get("data", {}).get("text", "") for b in d.get("blocks", []) if b.get("type") == "text"]
            body = " ".join(html_to_text(t) for t in texts[:3])
            og = (d.get("ogDescription") or "").strip()
            if og and og[:60] not in body:
                body = og + " " + body
            link = d.get("url") or f"https://vc.ru/{d.get('id')}"
            items.append(FeedItem(str(d.get("id")), d.get("title") or d.get("ogTitle") or "",
                                  body.strip(), link, published))
            reach = min(reach, published)
        if reach <= stop or not result.get("lastId"):
            break
        url = f"{base}&lastId={result['lastId']}&lastSortingValue={result['lastSortingValue']}"
        await asyncio.sleep(0.15)
    return items, reach


async def backfill(client: httpx.AsyncClient, src: Source, start: datetime, end: datetime,
                   known_links: set[str], save=None) -> tuple[list[FeedItem], bool]:
    if src.backfill == "ria_sitemap":
        return await backfill_ria(client, start, end, known_links, save)
    if src.backfill == "habr_news_pages":
        return await backfill_habr_news(client, start)
    if src.backfill == "sitemap" and src.sitemap:
        return await backfill_sitemap(client, src.sitemap, start, end, known_links, save)
    return [], False


async def backfill_habr_news(client, start: datetime, max_pages: int = 20) -> tuple[list[FeedItem], bool]:
    items: list[FeedItem] = []
    for page in range(1, max_pages + 1):
        resp = await client.get(f"https://habr.com/ru/news/page{page}/")
        resp.raise_for_status()
        cards = re.findall(r"<article[^>]*>.*?</article>", resp.text, re.S)
        if not cards:
            break
        oldest = None
        for card in cards:
            t = re.search(r'<time[^>]*datetime="([^"]+)"', card)
            a = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', card, re.S)
            if not t or not a:
                continue
            published = parse_date(t.group(1))
            title = html_to_text(a.group(2))
            body = html_to_text(card).replace(title, "", 1)[:800]
            link = clean_link("https://habr.com" + a.group(1))
            items.append(FeedItem(link, title, body, link, published))
            oldest = published if oldest is None else min(oldest, published)
        if oldest and oldest <= start:
            return items, True
        await asyncio.sleep(0.3)
    return items, False


async def backfill_ria(client, start, end, known_links, save=None) -> tuple[list[FeedItem], bool]:
    d0, d1 = start.astimezone(MSK).strftime("%Y%m%d"), end.astimezone(MSK).strftime("%Y%m%d")
    return await backfill_sitemap(client, f"https://ria.ru/sitemap_article.xml?date_start={d0}&date_end={d1}",
                                  start, end, known_links, save)


async def get_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    data = resp.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="ignore")


async def backfill_sitemap(client, url: str, start: datetime, end: datetime, known_links: set[str],
                           save=None) -> tuple[list[FeedItem], bool]:
    text = await get_text(client, url)
    if "<sitemapindex" in text:
        children = []
        for block in re.findall(r"<sitemap>(.*?)</sitemap>", text, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            lm = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            if loc and (not lm or (parse_date(lm.group(1)) or start) >= start):
                children.append(loc.group(1).strip())
        text = "".join([await get_text(client, c) for c in children[:6]])

    titled: list[FeedItem] = []
    urls: list[tuple[str, datetime]] = []
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc = re.search(r"<loc>([^<]+)</loc>", block)
        date = re.search(r"<news:publication_date>([^<]+)<", block) or re.search(r"<lastmod>([^<]+)<", block)
        if not loc or not date:
            continue
        loc, d = html_to_text(loc.group(1)), parse_date(date.group(1))
        if not d or not (start <= d <= end + timedelta(minutes=5)) or loc in known_links:
            continue
        title = re.search(r"<news:title>(.*?)</news:title>", block, re.S)
        if title:
            titled.append(FeedItem(loc, html_to_text(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title.group(1), flags=re.S)),
                                   "", loc, d))
        else:
            urls.append((loc, d))
    if save and titled:
        save(titled)

    sem = asyncio.Semaphore(3)
    failed = 0

    async def one(loc: str, lastmod: datetime) -> FeedItem | None:
        nonlocal failed
        head = None
        async with sem:
            for attempt in range(4):
                try:
                    head = await read_head(client, loc)
                    break
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    if code == 429:
                        await asyncio.sleep(retry_after(e.response, 5 * (attempt + 1)))
                        continue
                    if code < 500:
                        return None
                    await asyncio.sleep(1.5 * (attempt + 1))
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
            await asyncio.sleep(0.2)
        if head is None:
            failed += 1
            return None

        def meta(prop: str) -> str:
            for tag in re.findall(r"<meta\b[^>]*>", head, re.S):
                if re.search(r"""(?:property|name)=["']""" + re.escape(prop) + """["']""", tag):
                    m = re.search(r"""content=(["'])(.*?)\1""", tag, re.S)
                    return m.group(2) if m else ""
            return ""

        title = html_to_text(meta("og:title"))
        if not title:
            return None
        pub, published = meta("article:published_time"), lastmod
        if re.fullmatch(r"\d{8}T\d{4}", pub):
            published = datetime.strptime(pub, "%Y%m%dT%H%M").replace(tzinfo=MSK).astimezone(timezone.utc)
        elif parse_date(pub):
            published = parse_date(pub)
        return FeedItem(loc, title, html_to_text(meta("og:description")), loc, published)

    items: list[FeedItem] = list(titled)
    from_pages = 0
    for k in range(0, len(urls), 100):
        got = [i for i in await asyncio.gather(*(one(u, lm) for u, lm in urls[k:k + 100])) if i]
        if save:
            save(got)
        items += got
        from_pages += len(got)
    ok_network = failed <= max(3, len(urls) // 20)
    ok_parsed = len(urls) < 10 or from_pages >= len(urls) // 2
    return items, ok_network and ok_parsed


def retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return min(30.0, float(resp.headers.get("Retry-After", default)))
    except ValueError:
        return default


async def get_feed(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url)
    if resp.status_code == 429:
        await asyncio.sleep(retry_after(resp, 10))
        resp = await client.get(url)
    resp.raise_for_status()
    return resp


async def read_head(client: httpx.AsyncClient, url: str, limit: int = 150_000) -> str:
    buf = b""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if b"</head>" in buf or len(buf) > limit:
                break
    return buf.decode("utf-8", errors="ignore")
