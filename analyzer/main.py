import asyncio
import dataclasses
import json
import base64
import os
import re
import smtplib
import sqlite3
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import report as rpt
import digest
import llm
import sources
import xlsx_log
from sources import USER_AGENT, FeedItem

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "..", ".env"))

SNIPPETS_PER_KEYWORD = 3
MATCHED_PER_SITE = 40
DB_PATH = os.getenv("ANALYZER_DB", os.path.join(HERE, "items.db"))
REFRESH_MINUTES = int(os.getenv("FEED_REFRESH_MINUTES", "10"))
KEEP_DAYS = 7


class HistoryPoint(BaseModel):
    checked_at: str
    totals: dict[str, int]


class SearchRequest(BaseModel):
    sites: list[str] = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    history: list[HistoryPoint] = []
    window_hours: int = Field(24, ge=1, le=24 * 7)
    requester: dict | None = None
    trigger: str = "manual"
    bot_username: str = ""


class ReportRequest(SearchRequest):
    email: str


class Snippet(BaseModel):
    keyword: str
    text: str
    link: str | None = None
    published: str | None = None
    body: str = Field("", exclude=True)


class SiteResult(BaseModel):
    site: str
    ok: bool
    error: str | None = None
    mode: str = "page"
    items_total: int = 0
    coverage_hours: float = 0
    backfilling: bool = False
    counts: dict[str, int] = {}
    items: dict[str, int] = {}
    snippets: list[Snippet] = []
    matched: list[Snippet] = Field([], exclude=True)


class SearchResponse(BaseModel):
    checked_at: str
    window_hours: int
    results: list[SiteResult]
    totals: dict[str, int]
    summary: str
    chart_png_base64: str
    telegram_caption: str = ""
    telegram_details: str = ""
    telegram_digest: str = ""
    digest: dict | None = None
    email_html: str = Field("", exclude=True)


class ReportResponse(SearchResponse):
    emailed: bool
    email_error: str | None = None


@dataclass
class KeywordGroup:
    label: str
    variants: list[str]
    pattern: re.Pattern
    contextual: re.Pattern | None = None

    def find(self, text: str) -> list[re.Match]:
        main_hits = list(self.pattern.finditer(text))
        if not main_hits or not self.contextual:
            return main_hits
        rest = self.pattern.sub(lambda m: " " * len(m.group(0)), text)
        return sorted(main_hits + list(self.contextual.finditer(rest)), key=lambda m: m.start())


def split_manual(raw: str) -> list[str]:
    return [v.strip() for v in re.split(r"[/|]", raw) if v.strip()] or [raw.strip()]


def make_group(variants: list[str]) -> KeywordGroup:
    alts = sorted({v.lower() for v in variants if v not in MODEL_NAMES}, key=len, reverse=True)
    parts = [re.escape(a) for a in alts]
    models = [m for m in variants if m in MODEL_NAMES]
    contextual = None
    if models:
        m = "(?:" + "|".join(models) + ")"
        parts = [rf"claude\s+{m}(?:\s*[1-9](?:\.\d)?(?![\d.]))?"] + parts
        contextual = re.compile(rf"(?<!\w){m}\s*[1-9](?:\.\d)?(?![\d.])", re.IGNORECASE)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")", re.IGNORECASE)
    return KeywordGroup(label=variants[0], variants=variants, pattern=pattern, contextual=contextual)


async def resolve_keyword(raw: str) -> KeywordGroup:
    manual = split_manual(raw)
    if len(manual) > 1:
        return make_group(manual)
    return make_group(await expand_keyword(manual[0]))


EXPAND_SYSTEM = """Ты помогаешь сервису, который ищет упоминания темы в новостях и статьях \
(русские и англоязычные источники). Пользователь присылает одно ключевое слово. \
Верни список строк для поиска, по которым находятся материалы именно про эту тему:
- само слово;
- написание на русском и на английском, транслитерации и частые ошибки в написании;
- названия продуктов, компаний и людей, которые почти всегда означают ту же тему;
- если это продукт — его линейки, версии и модели (для «клод»: claude, claude code, \
антропик, anthropic, antropic, opus, sonnet, haiku, fable, mythos);
- если это компания — всё, что даёт полную сводку о ней: официальное и английское название, \
биржевой тикер, руководство (генеральный директор, председатель совета директоров, ключевые \
топ-менеджеры), основатели и учредители, крупные акционеры и владельцы, дочерние компании, \
бренды и продукты, известные проекты. Например, для «лукойл»: lukoil, тикер LKOH, фамилии \
руководителей и основных акционеров, названия «дочек» и брендов АЗС. Бери только тех людей \
и компании, кто занимает эту роль сейчас и о ком ты уверен; не выдумывай;
- если это человек — фамилия и имя на русском и английском, его компании и должности.

Люди: если фамилия редкая (Алекперов), достаточно фамилии — она найдёт и «Алекперова»; \
если распространённая (Иванов, Воробьёв), пиши имя и фамилию. Названия, которые сами по себе \
обычные слова (Opus, «Восток»), включай, только если без них сводка будет неполной.

Поиск идёт без учёта регистра по началу слова: «клод» уже находит «Клода» и «клод код», \
поэтому падежные формы и словосочетания, начинающиеся с другого варианта, не нужны. \
Слово могли набрать не в той раскладке клавиатуры (например, «сдфгву» вместо «claude» \
или «rkjl» вместо «клод») — тебе передаётся и вариант в другой раскладке. Определи, какое \
слово человек имел в виду, верни его в поле word и подбирай варианты для него.

Не добавляй общих слов, которые дадут много лишних совпадений (для «клод» не добавляй «ИИ» \
или «нейросеть»), и вариантов короче 3 букв. Если слово многозначное, бери самый вероятный \
смысл для новостей о технологиях, бизнесе и обществе. Обычно 5–25 вариантов."""

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}, "variants": {"type": "array", "items": {"type": "string"}}},
    "required": ["word", "variants"],
    "additionalProperties": False,
}


RU_KEYS = "ёйцукенгшщзхъфывапролджэячсмитьбю"
EN_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
_TO_EN = str.maketrans(RU_KEYS, EN_KEYS)
_TO_RU = str.maketrans(EN_KEYS, RU_KEYS)


def swap_layout(word: str) -> str | None:
    w = word.lower()
    if re.fullmatch(r"[а-яё ]+", w):
        out = w.translate(_TO_EN)
        return out if re.fullmatch(r"[a-z ]{3,}", out) else None
    if re.fullmatch(r"[a-z ]+", w):
        out = w.translate(_TO_RU)
        return out if re.fullmatch(r"[а-яё ]{3,}", out) else None
    return None


MODEL_NAMES = {"opus", "sonnet", "haiku", "fable", "mythos"}
_CLAUDE_FAMILY = ["клод", "claude", "claude code", "антропик", "anthropic", "antropic",
                  "opus", "sonnet", "haiku", "fable", "mythos"]
BUILTIN_EXPANSIONS = {w: _CLAUDE_FAMILY for w in ("клод", "claude", "антропик", "anthropic")}


def expansion_enabled() -> bool:
    return llm.enabled()


async def expand_keyword(word: str) -> list[str]:
    key = word.strip().lower()
    swapped = swap_layout(word)
    for k in (key, swapped):
        if k in BUILTIN_EXPANSIONS:
            return [word] + [v for v in BUILTIN_EXPANSIONS[k] if v != key]
    row = db().execute("SELECT variants FROM expansions WHERE word=?", (key,)).fetchone()
    if row:
        saved = json.loads(row[0])
        if isinstance(saved, dict):
            return tidy_expansion(word, saved["word"], saved["variants"])
        return tidy_expansion(word, word, saved)
    if not expansion_enabled():
        return [word] + ([swapped] if swapped else [])

    try:
        parsed = await llm.ask_json(
            "expand", EXPAND_SYSTEM,
            f"Слово: «{word}»" + (f"\nТе же клавиши в другой раскладке: «{swapped}»" if swapped else ""),
            EXPAND_SCHEMA, max_tokens=2000, effort="low")
    except (anthropic.APIError, llm.LLMError) as e:
        print(f"expand {word!r}: {type(e).__name__}: {e}"[:300])
        return [word]
    found = parsed.get("variants")
    result = tidy_expansion(word, str(parsed.get("word") or word), found if isinstance(found, list) else [])
    db().execute("INSERT OR REPLACE INTO expansions VALUES (?,?)",
                 (key, json.dumps({"word": result[0], "variants": result[1:]}, ensure_ascii=False)))
    db().commit()
    return result


_MIXED_SCRIPT = re.compile(r"(?=\w*[a-z])(?=\w*[а-яё])\w+", re.IGNORECASE)


def tidy_expansion(word: str, meant: str, variants: list) -> list[str]:
    def clean(v) -> str:
        v = re.sub(r"\s+", " ", str(v)).strip(" -–—.,;:«»\"'")
        return "" if len(v) < 3 or _MIXED_SCRIPT.search(v) else v

    meant = clean(meant) or word.strip()
    if meant.lower() == word.strip().lower():
        meant = word.strip()
    seen, out = {meant.lower()}, [meant]
    for v in list(variants) + [word]:
        v = clean(v)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


SCHEMA_VERSION = 2
MAX_WINDOW_H = 24
_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH)
        if _db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            _db.executescript("DROP TABLE IF EXISTS items; DROP TABLE IF EXISTS feeds;")
            _db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                feed TEXT, key TEXT, title TEXT, body TEXT, link TEXT, published TEXT,
                PRIMARY KEY (feed, key));
            CREATE INDEX IF NOT EXISTS items_feed_pub ON items(feed, published);
            CREATE TABLE IF NOT EXISTS feeds (
                feed TEXT PRIMARY KEY, last_fetch TEXT, covered_since TEXT);
            CREATE TABLE IF NOT EXISTS expansions (word TEXT PRIMARY KEY, variants TEXT);
            CREATE TABLE IF NOT EXISTS digests (key TEXT PRIMARY KEY, json TEXT, created TEXT);
        """)
    return _db


def save_items(feed: str, items: list[FeedItem]) -> None:
    db().executemany(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)",
        [(feed, i.key, i.title, i.body, i.link, i.published.isoformat()) for i in items],
    )
    db().commit()


def load_items(feed: str, since: datetime) -> list[FeedItem]:
    rows = db().execute(
        "SELECT key, title, body, link, published FROM items WHERE feed=? AND published>=? "
        "ORDER BY published DESC", (feed, since.isoformat())).fetchall()
    return [FeedItem(r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4])) for r in rows]


def known_links(feed: str) -> set[str]:
    return {r[0] for r in db().execute("SELECT link FROM items WHERE feed=?", (feed,))}


def feed_state(feed: str) -> tuple[datetime | None, datetime | None]:
    row = db().execute("SELECT last_fetch, covered_since FROM feeds WHERE feed=?", (feed,)).fetchone()
    if not row:
        return None, None
    return tuple(datetime.fromisoformat(v) if v else None for v in row)


def set_feed_state(feed: str, last_fetch: datetime, covered_since: datetime) -> None:
    db().execute("INSERT OR REPLACE INTO feeds VALUES (?,?,?)",
                 (feed, last_fetch.isoformat(), covered_since.isoformat()))
    db().commit()


def prune_items() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    db().execute("DELETE FROM items WHERE published<?", (cutoff,))
    db().commit()


_locks: dict[str, asyncio.Lock] = {}
BACKFILLING: set[str] = set()


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})


async def collect(client: httpx.AsyncClient, src: sources.Source, deep: bool,
                  max_age: timedelta | None = None) -> None:
    lock = _locks.setdefault(src.key, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        now = datetime.now(timezone.utc)
        last, covered = feed_state(src.key)
        if max_age and last and now - last < max_age:
            return
        target = now - timedelta(hours=MAX_WINDOW_H, minutes=15)
        stop = last - timedelta(minutes=10) if last else (target if deep else now - timedelta(hours=1))
        items, reach = await sources.fetch_recent(client, src, stop)
        save_items(src.key, items)
        covered = covered if (last and covered and reach <= last) else reach
        set_feed_state(src.key, now, covered)
        if deep and covered > target and src.backfill:
            BACKFILLING.add(src.key)
            try:
                extra, complete = await sources.backfill(client, src, target, covered, known_links(src.key),
                                                         save=lambda batch: save_items(src.key, batch))
                save_items(src.key, extra)
                if complete:
                    set_feed_state(src.key, now, target)
            finally:
                BACKFILLING.discard(src.key)


def make_snippet(text: str, start: int, end: int, radius: int = 110) -> str:
    a, b = max(0, start - radius), min(len(text), end + radius)
    if a > 0:
        a = text.find(" ", a) + 1 or a
    if b < len(text):
        b = text.rfind(" ", 0, b) if text.rfind(" ", 0, b) > end else b
    return ("…" if a > 0 else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def analyze_items(res: SiteResult, items: list[FeedItem], groups: list[KeywordGroup]) -> None:
    for g in groups:
        n_hits = n_items = 0
        for it in items:
            hits = len(g.find(it.title + " . " + it.body))
            if not hits:
                continue
            n_hits += hits
            n_items += 1
            if n_items <= MATCHED_PER_SITE:
                res.matched.append(Snippet(keyword=g.label, text=it.title, link=it.link,
                                           published=it.published.isoformat(), body=it.body[:600]))
            if n_items <= SNIPPETS_PER_KEYWORD:
                res.snippets.append(Snippet(keyword=g.label, text=it.title or it.body[:200], link=it.link,
                                            published=it.published.isoformat()))
        res.counts[g.label], res.items[g.label] = n_hits, n_items


def analyze_page(res: SiteResult, text: str, groups: list[KeywordGroup]) -> None:
    for g in groups:
        matches = g.find(text)
        res.counts[g.label] = res.items[g.label] = len(matches)
        last_end = -1
        found = 0
        for m in matches:
            if m.start() < last_end:
                continue
            res.snippets.append(Snippet(keyword=g.label, text=make_snippet(text, m.start(), m.end())))
            last_end, found = m.end() + 220, found + 1
            if found >= SNIPPETS_PER_KEYWORD:
                break


async def scan_site(client, site: str, groups: list[KeywordGroup], since: datetime, window_h: int) -> SiteResult:
    src = sources.resolve(site)
    res = SiteResult(site=site, ok=True)
    try:
        await collect(client, src, deep=False, max_age=timedelta(minutes=5))
    except sources.NotAFeed as page:
        res.mode = "page"
        analyze_page(res, page.text, groups)
        return res
    except Exception as e:
        if feed_state(src.key)[0] is None:
            return SiteResult(site=site, ok=False, error=f"{type(e).__name__}: {e}"[:200])
    items = load_items(src.key, since)
    covered = feed_state(src.key)[1]
    now = datetime.now(timezone.utc)
    res.mode = "rss"
    res.items_total = len(items)
    res.coverage_hours = min(window_h, (now - covered).total_seconds() / 3600) if covered else 0
    res.backfilling = src.key in BACKFILLING
    analyze_items(res, items, groups)
    return res


def busy_sources(keys: set[str] | None = None) -> list[str]:
    names = {s.key: s.name for s in sources.KNOWN}
    return [names.get(k, sources.display_name(k))
            for k, lock in _locks.items() if lock.locked() and (keys is None or k in keys)]


async def wait_for_collection(keys: set[str], timeout: float = 240) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while busy_sources(keys) and loop.time() < deadline:
        await asyncio.sleep(2)


async def run_search(req: SearchRequest) -> SearchResponse:
    await wait_for_collection({sources.resolve(s).key for s in req.sites})
    groups = await asyncio.gather(*(resolve_keyword(k) for k in req.keywords))
    labels = [g.label for g in groups]
    since = datetime.now(timezone.utc) - timedelta(hours=req.window_hours)
    async with http_client() as client:
        results = await asyncio.gather(*(scan_site(client, s, groups, since, req.window_hours) for s in req.sites))

    totals = Counter({k: 0 for k in labels})
    for r in results:
        totals.update(r.counts)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if req.requester:
        try:
            xlsx_log.append(req.requester, req.trigger, req.window_hours, [short_host(s) for s in req.sites],
                            [(g.label, g.variants, totals.get(g.label, 0),
                              sum(r.items.get(g.label, 0) for r in results if r.ok)) for g in groups],
                            datetime.now(timezone.utc))
        except Exception as e:
            print(f"search log: {type(e).__name__}: {e}")
    materials = [digest.Material(keyword=sn.keyword, title=sn.text, body=sn.body, link=sn.link,
                                 source=short_host(r.site), published=sn.published)
                 for r in results if r.ok for sn in r.matched]
    dg = await digest.make_digest(labels, materials, req.window_hours, cache=DigestCache())
    data = make_report_data(list(results), labels, dict(totals), req.history, now, req.window_hours)
    data.digest = dg
    data.bot_username = req.bot_username
    return SearchResponse(
        checked_at=now,
        window_hours=req.window_hours,
        results=list(results),
        totals=dict(totals),
        summary=build_summary(list(results), dict(totals), req.history, req.window_hours),
        chart_png_base64=base64.b64encode(rpt.render_card(data)).decode(),
        telegram_caption=rpt.telegram_caption(data),
        telegram_details=rpt.telegram_details(data),
        telegram_digest=rpt.telegram_digest(data),
        digest=dataclasses.asdict(dg),
        email_html=rpt.email_html(data),
    )


class DigestCache:
    def get(self, key: str) -> str | None:
        row = db().execute("SELECT json FROM digests WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        db().execute("INSERT OR REPLACE INTO digests VALUES (?,?,?)",
                     (key, value, datetime.now(timezone.utc).isoformat()))
        db().execute("DELETE FROM digests WHERE created < ?",
                     ((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(),))
        db().commit()


def make_report_data(results: list[SiteResult], labels: list[str], totals: dict[str, int],
                     history: list[HistoryPoint], now: str, window_h: int) -> rpt.ReportData:
    ok = [r for r in results if r.ok]
    items_by_kw = Counter({k: 0 for k in labels})
    for r in ok:
        items_by_kw.update(r.items)
    return rpt.ReportData(
        checked_at=now,
        window_h=window_h,
        keywords=labels,
        totals=totals,
        items_by_kw=dict(items_by_kw),
        prev=history[-1].totals if history else None,
        sites=[rpt.SiteRow(host=short_host(r.site), ok=r.ok, counts=r.counts) for r in results],
        n_materials=sum(r.items_total for r in ok),
        trend=[(h.checked_at, h.totals) for h in history] + [(now, totals)],
        items=[rpt.Item(keyword=sn.keyword, title=sn.text, link=sn.link, host=short_host(r.site),
                           published=sn.published) for r in ok for sn in r.snippets],
        partial=[(short_host(r.site), r.coverage_hours, r.backfilling) for r in ok
                 if r.mode == "rss" and r.coverage_hours < window_h - 0.25],
        failed=[short_host(r.site) for r in results if not r.ok],
        pages=[short_host(r.site) for r in ok if r.mode == "page"],
    )


async def collect_all() -> None:
    srcs = {src.key: src for src in sources.KNOWN}
    for (feed,) in db().execute("SELECT feed FROM feeds").fetchall():
        if "://" in feed:
            srcs.setdefault(feed, sources.resolve(feed))
        elif feed not in srcs:
            db().execute("DELETE FROM feeds WHERE feed=?", (feed,))
            db().execute("DELETE FROM items WHERE feed=?", (feed,))
            db().commit()
    async with http_client() as client:
        async def one(src):
            try:
                await collect(client, src, deep=True)
            except Exception as e:
                print(f"collect {src.key}: {type(e).__name__}: {e}"[:200])
        await asyncio.gather(*(one(s) for s in srcs.values()))
    prune_items()


async def collect_forever():
    while True:
        await collect_all()
        await asyncio.sleep(REFRESH_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(collect_forever())
    yield
    task.cancel()


app = FastAPI(title="KeyScout Analyzer", lifespan=lifespan)


def short_host(url: str) -> str:
    return sources.display_name(url)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def build_summary(results: list[SiteResult], totals: dict[str, int], history: list[HistoryPoint], window_h: int) -> str:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    items_by_kw = Counter()
    for r in ok:
        items_by_kw.update(r.items)

    n_items = sum(r.items_total for r in ok)
    found = [r for r in ok if sum(r.counts.values())]
    per_site = ", ".join(f"{short_host(r.site)} {sum(r.counts.values())}" for r in
                         sorted(found, key=lambda r: -sum(r.counts.values())))
    lines = [f"🔎 За последние {window_h} ч — проверено сайтов: {len(ok)}/{len(results)}"]
    if n_items:
        lines.append(f"Просмотрено материалов: {n_items} на {len(results)} источниках")
        if per_site:
            lines.append(f"Нашлось: {per_site}")

    if sum(totals.values()) == 0:
        lines.append("\nСовпадений не найдено.")
    else:
        lines.append("")
        prev = history[-1].totals if history else {}
        for kw, n in sorted(totals.items(), key=lambda x: -x[1]):
            delta = ""
            if kw in prev:
                d = n - prev[kw]
                delta = f" ({'+' if d >= 0 else ''}{d} с прошлой проверки)"
            m = items_by_kw[kw]
            lines.append(f"• «{kw}»: {n} {plural(n, 'упоминание', 'упоминания', 'упоминаний')}"
                         f" в {m} {plural(m, 'материале', 'материалах', 'материалах')}{delta}")
        best = max(ok, key=lambda r: sum(r.counts.values()))
        lines.append(f"Больше всего: {short_host(best.site)}")

        lines.append("\nСвежие материалы:")
        for r in ok:
            for sn in r.snippets[:3]:
                lines.append(f"— [{short_host(r.site)}] {sn.text}" + (f"\n  {sn.link}" if sn.link else ""))

    partial = [r for r in ok if r.mode == "rss" and r.coverage_hours < window_h - 0.25]
    if partial:
        lines.append(f"\n⏳ Пока есть не все {window_h} ч: " + ", ".join(
            f"{short_host(r.site)} — {r.coverage_hours:.0f} ч" + (" (догружаю архив)" if r.backfilling else "")
            for r in partial) + ". Остальное соберётся само, пока бот работает.")
    pages = [r for r in ok if r.mode == "page"]
    if pages:
        lines.append("ℹ️ Без RSS, проверена текущая страница: " + ", ".join(short_host(r.site) for r in pages))
    if failed:
        lines.append("⚠️ Не удалось открыть: " + ", ".join(short_host(r.site) for r in failed))
    return "\n".join(lines)


def send_email(to: str, subject: str, body: str, html_body: str, png: bytes) -> None:
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP не настроен (SMTP_HOST в .env)")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = (os.getenv("SMTP_PASSWORD") or "").replace(" ", "")
    sender = os.getenv("SMTP_FROM") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("KeyScout", sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(body)
    msg.add_alternative(html_body, subtype="html")
    msg.get_payload()[1].add_related(png, maintype="image", subtype="png", cid="<chart>")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


@app.get("/health")
def health():
    return {"ok": True}


class ExpandRequest(BaseModel):
    keywords: list[str] = Field(min_length=1)


class ExpandedKeyword(BaseModel):
    label: str
    variants: list[str]
    auto: bool


class ExpandResponse(BaseModel):
    enabled: bool
    keywords: list[ExpandedKeyword]


@app.post("/expand", response_model=ExpandResponse)
async def expand(req: ExpandRequest):
    groups = await asyncio.gather(*(resolve_keyword(k) for k in req.keywords))
    return ExpandResponse(enabled=expansion_enabled(), keywords=[
        ExpandedKeyword(label=g.label, variants=g.variants, auto=len(split_manual(k)) == 1)
        for g, k in zip(groups, req.keywords)])


@app.get("/status")
def status():
    return {"busy": busy_sources()}


@app.get("/sources")
def list_sources():
    return [{"key": s.key, "name": s.name, "url": s.entry, "group": s.group} for s in sources.KNOWN]


@app.get("/log.xlsx")
def download_log():
    if not os.path.exists(xlsx_log.LOG_PATH):
        raise HTTPException(404, "журнал пока пуст")
    return FileResponse(xlsx_log.LOG_PATH, filename="keyscout_searches.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    return await run_search(req)


@app.post("/report", response_model=ReportResponse)
async def report(req: ReportRequest):
    if "@" not in req.email:
        raise HTTPException(400, "некорректный email")
    res = await run_search(req)
    try:
        await asyncio.to_thread(
            send_email,
            req.email,
            f"KeyScout: упоминания за {res.window_hours} ч — {rpt.human_time(res.checked_at)}",
            res.summary,
            res.email_html,
            base64.b64decode(res.chart_png_base64),
        )
        return ReportResponse(**res.model_dump(), emailed=True)
    except Exception as e:
        return ReportResponse(**res.model_dump(), emailed=False, email_error=str(e)[:200])


class DashboardRequest(BaseModel):
    sites: list[str] = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    window_hours: int = Field(24, ge=1, le=24 * 7)
    wait_seconds: float = Field(15, ge=0, le=240)
    requester: dict | None = None


APP_LOG_EVERY = 30 * 60
_app_logged: dict[tuple, float] = {}


def log_app_view(req: DashboardRequest, groups: list[KeywordGroup], totals: Counter, items: Counter) -> None:
    if not req.requester:
        return
    key = (req.requester.get("chat_id"), tuple(g.label.lower() for g in groups), req.window_hours)
    now = datetime.now(timezone.utc)
    if now.timestamp() - _app_logged.get(key, 0) < APP_LOG_EVERY:
        return
    _app_logged[key] = now.timestamp()
    try:
        xlsx_log.append(req.requester, "app", req.window_hours, [short_host(s) for s in req.sites],
                        [(g.label, g.variants, totals.get(g.label, 0), items.get(g.label, 0)) for g in groups], now)
    except Exception as e:
        print(f"search log: {type(e).__name__}: {e}")


def excerpt(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit // 2 else limit].rstrip(" ,.;:—-") + "…"


@app.post("/dashboard")
async def dashboard(req: DashboardRequest):
    keys = {sources.resolve(s).key for s in req.sites}
    await wait_for_collection(keys, timeout=req.wait_seconds)
    groups = await asyncio.gather(*(resolve_keyword(k) for k in req.keywords))
    labels = [g.label for g in groups]
    since = datetime.now(timezone.utc) - timedelta(hours=req.window_hours)
    async with http_client() as client:
        results = await asyncio.gather(*(scan_site(client, s, groups, since, req.window_hours) for s in req.sites))

    ok = [r for r in results if r.ok]
    totals, items = Counter({k: 0 for k in labels}), Counter({k: 0 for k in labels})
    for r in ok:
        totals.update(r.counts)
        items.update(r.items)
    log_app_view(req, list(groups), totals, items)
    materials = []
    for r in ok:
        name = short_host(r.site)
        if r.mode == "page":
            materials += [{"keyword": sn.keyword, "title": sn.text, "link": r.site, "source": name,
                           "published": None, "excerpt": ""} for sn in r.snippets]
        else:
            materials += [{"keyword": sn.keyword, "title": sn.text, "link": sn.link, "source": name,
                           "published": sn.published, "excerpt": excerpt(sn.body)} for sn in r.matched]
    dg = await digest.make_digest(
        labels, [digest.Material(keyword=sn.keyword, title=sn.text, body=sn.body, link=sn.link,
                                 source=short_host(r.site), published=sn.published)
                 for r in ok for sn in r.matched],
        req.window_hours, cache=DigestCache())
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_hours": req.window_hours,
        "keywords": [{"query": q, "label": g.label, "variants": g.variants} for q, g in zip(req.keywords, groups)],
        "totals": dict(totals),
        "items": dict(items),
        "n_materials": sum(r.items_total for r in ok),
        "sites": [{"site": r.site, "name": short_host(r.site), "ok": r.ok, "error": r.error, "mode": r.mode,
                   "counts": r.counts, "items": r.items, "items_total": r.items_total,
                   "coverage_hours": round(r.coverage_hours, 1), "backfilling": r.backfilling}
                  for r in results],
        "materials": materials,
        "busy": busy_sources(keys),
        "digest": dataclasses.asdict(dg),
    }
