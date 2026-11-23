import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field


import llm

PER_KEYWORD = 40
TONES = {
    "positive": ("🟢", "позитивный"),
    "negative": ("🔴", "негативный"),
    "mixed": ("🟡", "смешанный"),
    "neutral": ("⚪️", "нейтральный"),
}
DISCLAIMER = "Это обзор новостного фона, а не инвестиционная рекомендация."


@dataclass
class Material:
    keyword: str
    title: str
    body: str
    link: str | None
    source: str
    published: str | None


@dataclass
class KeywordDigest:
    keyword: str
    tone: str
    score: float
    summary: str
    events: list[dict] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)
    n_materials: int = 0


@dataclass
class Digest:
    method: str
    items: list[KeywordDigest] = field(default_factory=list)
    note: str = ""


def select(materials: list[Material], keyword: str, limit: int = PER_KEYWORD) -> list[Material]:
    by_src: dict[str, list[Material]] = defaultdict(list)
    for m in sorted((m for m in materials if m.keyword == keyword), key=lambda m: m.published or "", reverse=True):
        by_src[m.source].append(m)
    out, queues = [], list(by_src.values())
    while queues and len(out) < limit:
        for q in list(queues):
            if q:
                out.append(q.pop(0))
            if not q:
                queues.remove(q)
            if len(out) >= limit:
                break
    return out


SYSTEM = """Ты — аналитик новостного фона для частного инвестора. Тебе дают свежие материалы \
российских СМИ по каждому объекту (компания, человек, тема), пронумерованные [1], [2]…

Для каждого объекта:
- tone и score — тон новостей именно для этого объекта: positive, negative, mixed или neutral; \
score от −1 (резко негативно) до +1 (резко позитивно). Смешанный — когда есть заметные события в обе стороны.
- summary — 2–4 предложения: что происходило с объектом за период, главное первым.
- events — 3–6 ключевых событий, у каждого номера материалов-источников в refs. Объединяй \
повторы одного события из разных СМИ в один пункт.
- factors — 2–5 факторов, которые могут влиять на компанию или её акции (риски и возможности), \
строго по материалам.

Правила: только факты из материалов, без домыслов. Не давай советов купить, продать или держать \
и не прогнозируй цену. Если часть материалов не про этот объект (совпадение слов, омоним) — \
не учитывай их и укажи это в summary. Если по объекту ничего существенного — так и напиши, \
tone neutral. Пиши по-русски, коротко и по делу."""

SCHEMA = {
    "type": "object",
    "properties": {
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "tone": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]},
                    "score": {"type": "number"},
                    "summary": {"type": "string"},
                    "events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"text": {"type": "string"},
                                           "refs": {"type": "array", "items": {"type": "integer"}}},
                            "required": ["text", "refs"],
                            "additionalProperties": False,
                        },
                    },
                    "factors": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["keyword", "tone", "score", "summary", "events", "factors"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["objects"],
    "additionalProperties": False,
}

async def llm_digest(keywords: list[str], picked: dict[str, list[Material]], window_h: int) -> list[KeywordDigest]:
    numbered: list[Material] = []
    parts = [f"Период: последние {window_h} ч."]
    for kw in keywords:
        parts.append(f"\n## Объект: {kw}")
        for m in picked[kw]:
            numbered.append(m)
            when = (m.published or "")[:16].replace("T", " ")
            parts.append(f"[{len(numbered)}] {m.source} · {when} · {m.title}"
                         + (f" — {m.body[:300]}" if m.body else ""))
    data = await llm.ask_json("digest", SYSTEM, "\n".join(parts), SCHEMA, max_tokens=8000, effort="medium")
    objects = data.get("objects") if isinstance(data.get("objects"), list) else []
    by_kw = {str(o.get("keyword", "")).strip().lower(): o for o in objects if isinstance(o, dict)}
    out = []
    for kw in keywords:
        o = by_kw.get(kw.lower())
        if not o:
            continue
        events = []
        for ev in o.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("text"):
                continue
            refs = [numbered[r - 1] for r in ev.get("refs") or [] if isinstance(r, int) and 1 <= r <= len(numbered)]
            events.append({"text": str(ev["text"]), "sources": sorted({m.source for m in refs}),
                           "link": next((m.link for m in refs if m.link), None)})
        try:
            score = max(-1.0, min(1.0, float(o.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0
        tone = o.get("tone") if o.get("tone") in TONES else "neutral"
        factors = [str(f) for f in o.get("factors") or [] if f]
        out.append(KeywordDigest(kw, tone, score, str(o.get("summary", "")), events, factors, len(picked[kw])))
    if not out:
        raise llm.LLMError("в ответе нет ни одного слова из запроса")
    return out


POSITIVE = [
    (r"рост|вырос|растет|растёт|повыс|повышен|увелич|подорожа", "рост"),
    (r"прибыл", "прибыль"), (r"рекорд", "рекорды"), (r"дивиденд", "дивиденды"), (r"выкуп", "выкуп акций"),
    (r"одобр", "одобрения"), (r"соглашени|контракт|сделк", "сделки и контракты"), (r"расшир|запуск|запустил", "развитие"),
    (r"успешн|превыс|улучш|восстанов", "улучшения"), (r"инвестиц", "инвестиции"), (r"партн[её]рств", "партнёрства"),
]
NEGATIVE = [
    (r"паден|упал|упад|снижен|снизил|сократ|подешев|обвал|просел", "снижение"),
    (r"убыт", "убытки"), (r"санкц", "санкции"), (r"штраф", "штрафы"),
    (r"иск(?:а|у|ом|и|ов|ам|ами|ах)?(?!\w)|суд(?:а|у|ом|е|ы|ов|ам|ами|ах|ебн\w*)?(?!\w)|претензи", "суды и иски"),
    (r"арест|обыск|расследован", "расследования"), (r"авари|пожар|взрыв", "аварии"),
    (r"атак|удар(?!ник)|беспилотн|дрон", "атаки"), (r"потер[ия]|потерял", "потери"), (r"кризис|банкрот", "кризис"),
    (r"огранич|запрет", "ограничения"), (r"дефицит|отток", "дефицит и отток"),
    (r"долг(?:а|у|ом|и|ов|ам|ами|ах|ов)?(?!\w)|задолжен", "долги"), (r"увольн|сокращени", "сокращения"),
    (r"скандал|угроз|остановк|приостанов", "угрозы и остановки"),
]


def _compile(lexicon):
    return [(re.compile(r"(?<!\w)(?:" + pat + r")\w*", re.IGNORECASE), label) for pat, label in lexicon]


_POS, _NEG = _compile(POSITIVE), _compile(NEGATIVE)


def _signals(text: str, lexicon) -> list[str]:
    return [label for rx, label in lexicon for _ in rx.findall(text)]


def dictionary_digest(keywords: list[str], picked: dict[str, list[Material]]) -> list[KeywordDigest]:
    out = []
    for kw in keywords:
        mats = picked[kw]
        scored = []
        themes: Counter = Counter()
        for m in mats:
            text = f"{m.title}. {m.title}. {m.body[:300]}"
            p, n = _signals(text, _POS), _signals(text, _NEG)
            themes.update(p + n)
            if p or n:
                scored.append(((len(p) - len(n)) / (len(p) + len(n)), m))
        pos = [m for sc, m in scored if sc > 0.2]
        neg = [m for sc, m in scored if sc < -0.2]
        score = sum(sc for sc, _ in scored) / len(scored) if scored else 0.0
        if pos and neg and min(len(pos), len(neg)) >= max(1, len(scored) // 4):
            tone = "mixed"
        elif score > 0.2:
            tone = "positive"
        elif score < -0.2:
            tone = "negative"
        else:
            tone = "neutral"
        top = ", ".join(t for t, _ in themes.most_common(3))
        summary = (f"{len(mats)} материалов: позитивных — {len(pos)}, негативных — {len(neg)}, "
                   f"остальные нейтральные." + (f" Чаще всего: {top}." if top else "")) if mats \
            else "Материалов за период нет."
        events = ([{"text": "Негатив: " + m.title, "sources": [m.source], "link": m.link}
                   for _, m in sorted((x for x in scored if x[0] < -0.2), key=lambda x: x[0])[:3]]
                  + [{"text": "Позитив: " + m.title, "sources": [m.source], "link": m.link}
                     for _, m in sorted((x for x in scored if x[0] > 0.2), key=lambda x: -x[0])[:3]])
        factors = [f"{t} ({c})" for t, c in themes.most_common(5)]
        out.append(KeywordDigest(kw, tone, round(score, 2), summary, events, factors, len(mats)))
    return out


def cache_key(keywords: list[str], picked: dict[str, list[Material]], window_h: int) -> str:
    raw = json.dumps([llm.provider(), llm.model("digest"), window_h, [(kw, sorted(m.link or m.title for m in picked[kw]))
                                                for kw in keywords]], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


async def make_digest(keywords: list[str], materials: list[Material], window_h: int, cache=None) -> Digest:
    picked = {kw: select(materials, kw) for kw in keywords}
    if not any(picked.values()):
        return Digest("none", [KeywordDigest(kw, "neutral", 0.0, "Материалов за период нет.") for kw in keywords])
    if llm.enabled():
        key = cache_key(keywords, picked, window_h)
        if cache and (hit := cache.get(key)):
            d = json.loads(hit)
            return Digest(d["method"], [KeywordDigest(**i) for i in d["items"]], d.get("note", ""))
        try:
            digest = Digest(llm.provider(), await llm_digest(keywords, picked, window_h))
            if cache:
                cache.set(key, json.dumps(asdict(digest), ensure_ascii=False))
            return digest
        except Exception as e:
            print(f"digest: {type(e).__name__}: {e}"[:300])
            return Digest("dictionary", dictionary_digest(keywords, picked),
                          f"{llm.label()} сейчас недоступен — тон оценён по словарю.")
    return Digest("dictionary", dictionary_digest(keywords, picked),
                  "Тон оценён по словарю, без ИИ. С ключом DeepSeek или Claude API выжимка будет подробной.")
