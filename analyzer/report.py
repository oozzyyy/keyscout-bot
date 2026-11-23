import html
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import logging

import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

REPORT_TZ = ZoneInfo(os.getenv("REPORT_TZ", "Europe/Moscow"))
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

SURFACE = "#fcfcfb"
TILE = "#f3f2ee"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BRAND = "#2a78d6"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]

FONT = ["Arial", "Helvetica", "DejaVu Sans"]


@dataclass
class SiteRow:
    host: str
    ok: bool
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Item:
    keyword: str
    title: str
    link: str | None
    host: str
    published: str | None = None


@dataclass
class ReportData:
    checked_at: str
    window_h: int
    keywords: list[str]
    totals: dict[str, int]
    items_by_kw: dict[str, int]
    prev: dict[str, int] | None
    sites: list[SiteRow]
    n_materials: int
    trend: list[tuple[str, dict[str, int]]]
    items: list[Item]
    partial: list[tuple[str, float, bool]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    digest: object | None = None
    bot_username: str = ""


COPYRIGHT = "© Ivan Zh (@o1928x)"
COPYRIGHT_URL = "https://t.me/o1928x"
WATERMARK = "KeyScout"


TONE_STYLE = {
    "positive": ("#0ca30c", "позитив"),
    "negative": ("#d03b3b", "негатив"),
    "mixed": ("#fab219", "смешанный"),
    "neutral": ("#898781", "нейтрально"),
}
TONE_ICON = {"positive": "▲", "negative": "▼", "mixed": "◆", "neutral": "●"}


def tint(hex_color: str, k: float = 0.22, base: str = "#fcfcfb") -> str:
    a = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * k + y * (1 - k)):02x}" for x, y in zip(a, b))


def tone_of(data: "ReportData", kw: str):
    if not data.digest:
        return None
    return next((d for d in data.digest.items if d.keyword == kw), None)


def color_of(data: ReportData, kw: str) -> str:
    return SERIES[data.keywords.index(kw) % len(SERIES)]


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def local_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(REPORT_TZ)


def human_time(iso: str) -> str:
    d = local_dt(iso)
    return f"{d.day} {MONTHS[d.month - 1]}, {d:%H:%M}"


def delta_text(data: ReportData, kw: str) -> str:
    if not data.prev or kw not in data.prev:
        return ""
    d = data.totals.get(kw, 0) - data.prev[kw]
    if d == 0:
        return "без изменений"
    return f"{'▲' if d > 0 else '▼'} {abs(d)} с прошлой проверки"


DW, SCALE, DPI, PAD = 560, 2, 200, 28
MAX_SITE_ROWS = 10


class Canvas:
    def __init__(self, height: float):
        self.h = height
        self.fig = plt.figure(figsize=(DW * SCALE / DPI, height * SCALE / DPI), dpi=DPI)
        self.fig.patch.set_facecolor(SURFACE)
        ax = self.fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, DW)
        ax.set_ylim(height, 0)
        ax.axis("off")
        self.ax = ax
        self._renderer = None

    @staticmethod
    def pt(px: float) -> float:
        return px * SCALE * 72 / DPI

    def text(self, x, y, s, px, color=INK, weight="normal", ha="left", va="baseline", z=6):
        return self.ax.text(x, y, s, fontsize=self.pt(px), color=color, fontweight=weight,
                            ha=ha, va=va, family=FONT, zorder=z)

    def width(self, t) -> float:
        self._renderer = self._renderer or self.fig.canvas.get_renderer()
        return t.get_window_extent(self._renderer).width / SCALE

    def wrap(self, x, y, s, px, max_w, color=MUTED, line_h=None) -> float:
        line_h = line_h or px * 1.45
        words, line = s.split(), ""
        for w in words:
            probe = self.text(x, y, (line + " " + w).strip(), px, color)
            fits = self.width(probe) <= max_w
            probe.remove()
            if fits or not line:
                line = (line + " " + w).strip()
            else:
                self.text(x, y, line, px, color)
                y, line = y + line_h, w
        if line:
            self.text(x, y, line, px, color)
            y += line_h
        return y

    def hline(self, x0, x1, y, color=GRID):
        self.ax.add_line(Line2D([x0, x1], [y, y], color=color, linewidth=self.pt(0.5)))

    def vline(self, x, y0, y1, color=AXIS):
        self.ax.add_line(Line2D([x, x], [y0, y1], color=color, linewidth=self.pt(0.5)))

    def box(self, x, y, w, h, color, r=8):
        r = max(0.0, min(r, w / 2, h / 2))
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                         linewidth=0, facecolor=color))

    def bar(self, x, y, w, h, color, r=2):
        self.box(x, y, w, h, color, r)
        self.ax.add_patch(Rectangle((x, y), min(w, r), h, linewidth=0, facecolor=color))

    def dot(self, x, y, color, r=3, ring=1.5):
        if ring:
            self.ax.add_patch(Circle((x, y), r + ring, facecolor=SURFACE, linewidth=0, zorder=4))
        self.ax.add_patch(Circle((x, y), r, facecolor=color, linewidth=0, zorder=5))

    def watermark(self, x0, y0, x1, y1, sub: str = "") -> None:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        size = max(15.0, min(30.0, (y1 - y0) * 0.3))
        if sub:
            cy -= size * 0.28
        self.text(cx, cy, WATERMARK, size, INK, "bold", ha="center", va="center", z=0.5).set_alpha(0.07)
        if sub:
            self.text(cx, cy + size * 0.82, sub, max(9.0, size * 0.4), INK, "bold",
                      ha="center", va="center", z=0.5).set_alpha(0.1)

    def crop(self, height: float) -> None:
        self.h = height
        self.fig.set_size_inches(DW * SCALE / DPI, height * SCALE / DPI)
        self.ax.set_ylim(height, 0)

    def png(self) -> bytes:
        buf = io.BytesIO()
        self.fig.savefig(buf, format="png", dpi=DPI, facecolor=SURFACE)
        plt.close(self.fig)
        return buf.getvalue()


def nice_ticks(vmax: float, n: int = 3) -> list[int]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    step = max(1, int(step))
    top = int(-(-vmax // step) * step)
    return list(range(0, top + 1, step))


def render_card(data: ReportData) -> bytes:
    kws = data.keywords[:8]
    tiles = kws[:6]
    multi = len(kws) > 1
    bar_t = 10 if multi else 12
    inner = DW - 2 * PAD
    show_trend = len(data.trend) >= 2
    notes = footer_notes(data)
    wm_sub = f"@{data.bot_username}" if data.bot_username else ""

    per_row = min(3, max(len(tiles), 1))
    tile_rows = -(-len(tiles) // per_row)
    single = len(tiles) == 1
    has_tone = bool(data.digest and any(i.n_materials for i in data.digest.items))
    tile_h, tile_gap = (84 if single else (118 if has_tone else 96)), 8
    y_tiles = PAD + 84
    y_bars = y_tiles + tile_rows * (tile_h + tile_gap) + 26

    found = sorted((st for st in data.sites if st.ok and any(st.counts.get(k, 0) for k in kws)),
                   key=lambda st: -sum(st.counts.get(k, 0) for k in kws))
    shown, rest = found[:MAX_SITE_ROWS], found[MAX_SITE_ROWS:]
    group_h = len(kws) * bar_t + (len(kws) - 1) * 2
    row_gap = 14
    legend_h = 22 if multi and shown else 0
    bars_h = 30 + legend_h + (len(shown) * (group_h + row_gap) if shown else 24) + (20 if rest else 0)
    y_trend = y_bars + bars_h + 18
    plot_h = 130
    trend_h = 44 + plot_h + 34 if show_trend else 22
    notes_h = sum(15 * (1 + len(n) // 70) for n in notes) + (8 if notes else 0)
    height = y_trend + trend_h + notes_h + PAD - 6
    c = Canvas(height)

    t = c.text(PAD, PAD + 8, "KeyScout", 13, BRAND, "bold")
    c.text(PAD + c.width(t) + 6, PAD + 8, "·  отчёт", 13, MUTED)
    c.text(PAD, PAD + 40, f"Упоминания за {data.window_h} ч", 26, INK, "bold")
    n_src = len(data.sites)
    sub = f"{human_time(data.checked_at)}  ·  {data.n_materials} "
    sub += plural(data.n_materials, "материал", "материала", "материалов")
    sub += f"  ·  {n_src} {plural(n_src, 'источник', 'источника', 'источников')}"
    c.text(PAD, PAD + 62, sub, 13, INK2)

    tile_w = (inner - tile_gap * (per_row - 1)) / per_row
    for i, kw in enumerate(tiles):
        x = PAD + (i % per_row) * (tile_w + tile_gap)
        y = y_tiles + (i // per_row) * (tile_h + tile_gap)
        c.box(x, y, tile_w, tile_h, TILE, r=10)
        c.dot(x + 16, y + 19, color_of(data, kw), r=3.5, ring=0)
        c.text(x + 25, y + 23, shorten(kw, 40 if single else 16), 13, INK2)
        n, m = data.totals.get(kw, 0), data.items_by_kw.get(kw, 0)
        d = delta_text(data, kw)
        tone = tone_of(data, kw)
        if tone and n:
            col, label = TONE_STYLE[tone.tone]
            if single:
                tl = c.text(x + tile_w - 18, y + 23, f"{TONE_ICON[tone.tone]} {label}", 11, INK, ha="right", z=7)
                c.box(x + tile_w - 24 - c.width(tl), y + 11, c.width(tl) + 12, 17, tint(col), r=8.5)
            else:
                tl = c.text(x + 18, y + tile_h - 13, f"{TONE_ICON[tone.tone]} {label}", 11, INK, z=7)
                c.box(x + 12, y + tile_h - 25, c.width(tl) + 12, 17, tint(col), r=8.5)
        if single:
            v = c.text(x + 12, y + 64, str(n), 34, INK, "bold")
            c.text(x + 12 + c.width(v) + 10, y + 64,
                   f"{plural(n, 'упоминание', 'упоминания', 'упоминаний')} в {m} "
                   f"{plural(m, 'материале', 'материалах', 'материалах')}", 13, MUTED)
            if d:
                c.text(x + tile_w - 14, y + 64, d, 12, INK2, ha="right")
            continue
        c.text(x + 12, y + 56, str(n), 30, INK, "bold")
        c.text(x + 12, y + 73, f"в {m} {plural(m, 'материале', 'материалах', 'материалах')}", 11.5, MUTED)
        if d:
            dt = c.text(x + 12, y + 88, d, 11.5, INK2)
            if c.width(dt) > tile_w - 20:
                dt.set_text(d.split(" с прошлой")[0])

    y = y_bars
    t = c.text(PAD, y + 14, "По источникам", 17, INK, "bold")
    c.text(PAD + c.width(t) + 8, y + 14, f"нашлось на {len(found)} из {n_src}", 12, MUTED)
    y += 30
    if not shown:
        c.text(PAD, y + 12, "Совпадений нет ни на одном источнике.", 13, MUTED)
    if legend_h:
        lx, ly = PAD, y
        for kw in kws:
            lt = c.text(lx + 13, ly + 10, shorten(kw, 14), 12, INK2)
            w = 13 + c.width(lt) + 14
            if lx + w > DW - PAD:
                lt.remove()
                lx, ly = PAD, ly + 18
                lt = c.text(lx + 13, ly + 10, shorten(kw, 14), 12, INK2)
            c.box(lx, ly + 1.5, 8, 8, color_of(data, kw), r=2)
            lx += 13 + c.width(lt) + 14
        y = ly + legend_h

    if shown:
        labels = [c.text(PAD, 0, shorten(st.host, 18), 13, INK, va="center") for st in shown]
        label_w = max([c.width(t) for t in labels] + [50]) + 14
        bx0, bx1 = PAD + label_w, DW - PAD - 26
        vmax = max([st.counts.get(k, 0) for st in shown for k in kws] + [1])
        scale = (bx1 - bx0) / vmax
        top = y
        for st, lt in zip(shown, labels):
            lt.set_y(y + group_h / 2)
            for j, kw in enumerate(kws):
                by = y + j * (bar_t + 2)
                val = st.counts.get(kw, 0)
                length = max(val * scale, 3) if val > 0 else 0
                if val > 0:
                    c.bar(bx0, by, length, bar_t, color_of(data, kw))
                c.text(bx0 + length + 5, by + bar_t / 2, str(val), 11.5,
                       INK2 if val else MUTED, va="center_baseline")
            y += group_h + row_gap
        c.watermark(bx0, top - 4, bx1, y - row_gap + 4, wm_sub)
        c.vline(bx0, top - 4, y - row_gap + 4)
        if rest:
            n_rest = sum(st.counts.get(k, 0) for st in rest for k in kws)
            c.text(PAD, y + 4, f"и ещё {len(rest)} {plural(len(rest), 'источник', 'источника', 'источников')}: "
                   f"{n_rest} {plural(n_rest, 'упоминание', 'упоминания', 'упоминаний')}", 12, MUTED)

    y = y_trend
    t = c.text(PAD, y + 14, "Динамика", 17, INK, "bold")
    if not show_trend:
        c.text(PAD + c.width(t) + 10, y + 14, "появится после следующей проверки", 12, MUTED)
    else:
        c.text(PAD, y + 32, f"упоминаний за {data.window_h} ч на момент каждой проверки", 12, MUTED)
        pts = data.trend[-24:]
        py0, py1 = y + 48, y + 48 + plot_h
        ticks = nice_ticks(max([p[1].get(k, 0) for p in pts for k in kws] + [1]))
        px0, px1 = PAD + 26, DW - PAD - 26
        ys = lambda v: py1 - (v / ticks[-1]) * (py1 - py0)
        xs = [px0 + i * (px1 - px0) / (len(pts) - 1) for i in range(len(pts))]
        c.watermark(px0, py0, px1, py1, wm_sub)
        for tv in ticks:
            c.hline(px0, px1, ys(tv), AXIS if tv == 0 else GRID)
            c.text(px0 - 7, ys(tv), f"{tv:,}".replace(",", " "), 11, MUTED, ha="right", va="center_baseline")
        ends = []
        for kw in kws:
            vals = [p[1].get(kw, 0) for p in pts]
            col = color_of(data, kw)
            c.ax.add_line(Line2D(xs, [ys(v) for v in vals], color=col, linewidth=c.pt(1.5),
                                 solid_capstyle="round", solid_joinstyle="round", zorder=3))
            c.dot(xs[-1], ys(vals[-1]), col)
            ends.append((ys(vals[-1]), vals[-1]))
        placed = []
        for ey, ev in sorted(ends):
            if all(abs(ey - p) >= 12 for p in placed):
                c.text(xs[-1] + 8, ey, str(ev), 11.5, INK2, va="center_baseline")
                placed.append(ey)
        multi_day = local_dt(pts[0][0]).date() != local_dt(pts[-1][0]).date()
        fmt = "%d.%m %H:%M" if multi_day else "%H:%M"
        max_labels = 3 if multi_day else 5
        step = max(1, -(-(len(pts) - 1) // (max_labels - 1)))
        idx = list(range(0, len(pts), step))
        if idx[-1] != len(pts) - 1:
            if len(pts) - 1 - idx[-1] < step / 2:
                idx.pop()
            idx.append(len(pts) - 1)
        for i in idx:
            ha = "left" if i == 0 else "right" if i == len(pts) - 1 else "center"
            c.text(xs[i], py1 + 16, local_dt(pts[i][0]).strftime(fmt), 11, MUTED, ha=ha)

    ny = y_trend + trend_h + 8
    for n in notes:
        ny = c.wrap(PAD, ny, n, 11, inner, MUTED)
    fy = (ny if notes else y_trend + trend_h) + 18
    c.hline(PAD, DW - PAD, fy - 14)
    c.text(PAD, fy, COPYRIGHT, 11, MUTED)
    if data.bot_username:
        c.text(DW - PAD, fy, f"@{data.bot_username}", 11, MUTED, ha="right")
    c.crop(fy + PAD - 8)
    return c.png()


def footer_notes(data: ReportData) -> list[str]:
    notes = []
    if data.partial:
        parts = [f"{h} — {hrs:.0f} ч" + (" (догружаю архив)" if busy else "") for h, hrs, busy in data.partial]
        more = len(parts) - 4
        listed = ", ".join(parts[:4]) + (f" и ещё {more}" if more > 0 else "")
        notes.append(f"Пока есть не все {data.window_h} ч: {listed}. Остальное соберётся само, пока бот работает.")
    if data.pages:
        notes.append("Без RSS, проверена текущая страница: " + ", ".join(data.pages))
    if len(data.keywords) > 8:
        notes.append(f"На графике первые 8 слов из {len(data.keywords)}, остальные — в тексте отчёта.")
    return notes


def shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def e(s) -> str:
    return html.escape(str(s), quote=False)


def telegram_caption(data: ReportData) -> str:
    lines = [f"<b>📊 KeyScout · за {data.window_h} ч</b>",
             f"<i>{e(human_time(data.checked_at))} · {data.n_materials} "
             f"{plural(data.n_materials, 'материал', 'материала', 'материалов')}</i>", ""]
    for kw in data.keywords:
        n, m = data.totals.get(kw, 0), data.items_by_kw.get(kw, 0)
        line = f"<b>{e(kw)}</b> — {n} {plural(n, 'упоминание', 'упоминания', 'упоминаний')}"
        if n:
            line += f" в {m} {plural(m, 'материале', 'материалах', 'материалах')}"
        d = delta_text(data, kw)
        if d and d != "без изменений":
            line += f" <i>({e(d.replace(' с прошлой проверки', ''))})</i>"
        lines.append(line)
    out = "\n".join(lines)
    return out if len(out) <= 1000 else out[:990] + "…"


def items_by_keyword(data: ReportData, per_kw: int = 5) -> dict[str, list[Item]]:
    grouped: dict[str, list[Item]] = {}
    for it in sorted(data.items, key=lambda i: i.published or "", reverse=True):
        lst = grouped.setdefault(it.keyword, [])
        if len(lst) < per_kw:
            lst.append(it)
    return {kw: grouped[kw] for kw in data.keywords if kw in grouped}


def tg_len(text_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)).encode("utf-16-le")) // 2


def telegram_digest(data: ReportData) -> str:
    dg = data.digest
    if not dg or dg.method == "none":
        return ""
    import digest as dmod
    items = [it for it in dg.items if it.n_materials]
    if not items:
        return ""

    def block(it, max_events: int, max_factors: int) -> str:
        icon, label = dmod.TONES[it.tone]
        score = f"{it.score:+.1f}".replace("-", "−")
        lines = [f"<b>{e(it.keyword)}</b> · {icon} {label} тон ({score}) · "
                 f"{it.n_materials} {plural(it.n_materials, 'материал', 'материала', 'материалов')}",
                 e(it.summary)]
        if it.events[:max_events]:
            lines.append("<b>📌 Ключевые события</b>")
            for ev in it.events[:max_events]:
                src = f" <i>({e(', '.join(ev['sources']))})</i>" if ev.get("sources") else ""
                text = e(shorten(ev["text"], 220))
                lines.append(f"• <a href=\"{html.escape(ev['link'])}\">{text}</a>{src}" if ev.get("link")
                             else f"• {text}{src}")
        if it.factors[:max_factors]:
            lines.append("<b>⚖️ Что может влиять</b>")
            lines += [f"• {e(f)}" for f in it.factors[:max_factors]]
        return "\n\n".join(lines)

    footer = f"<i>{e(dg.note + ' ' if dg.note else '')}{dmod.DISCLAIMER}</i>"
    for max_events, max_factors in ((6, 5), (4, 3), (3, 2), (2, 1), (1, 0)):
        out = "\n\n".join(["<b>🧠 Выжимка</b>",
                           "\n\n— — —\n\n".join(block(it, max_events, max_factors) for it in items),
                           footer])
        if tg_len(out) <= 4000:
            return out
    return out


def email_digest(data: ReportData) -> str:
    dg = data.digest
    if not dg or dg.method == "none":
        return ""
    import digest as dmod
    blocks = []
    for it in dg.items:
        if not it.n_materials:
            continue
        col, _ = TONE_STYLE[it.tone]
        _, label = dmod.TONES[it.tone]
        events = "".join(
            '<li style="margin:0 0 6px">'
            + (f'<a href="{html.escape(ev["link"])}" style="color:{INK};text-decoration:none">{e(ev["text"])}</a>'
               if ev.get("link") else e(ev["text"]))
            + (f' <span style="color:{MUTED};font-size:12px">({e(", ".join(ev["sources"]))})</span>'
               if ev.get("sources") else "") + "</li>" for ev in it.events[:6])
        factors = (f'<p style="margin:8px 0 0;font-size:13px;color:{INK2}"><i>Что может влиять:</i> '
                   f'{e("; ".join(it.factors[:5]))}</p>' if it.factors else "")
        blocks.append(f"""<div style="margin:16px 0 0">
  <div style="font-size:16px;font-weight:700;color:{INK}">{e(it.keyword)}
    <span style="font-size:13px;font-weight:400;color:{INK2}"><span style="display:inline-block;width:9px;height:9px;border-radius:5px;background:{col};margin:0 4px 0 8px"></span>{label} тон ({it.score:+.1f})</span></div>
  <p style="margin:6px 0 8px;font-size:14px;line-height:1.5;color:{INK}">{e(it.summary)}</p>
  <ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.45;color:{INK}">{events}</ul>{factors}
</div>""")
    if not blocks:
        return ""
    note = f'<p style="margin:14px 0 0;font-size:12px;color:{MUTED}">{e(dg.note + " " if dg.note else "")}{dmod.DISCLAIMER}</p>'
    return (f'<tr><td style="padding:4px 28px 10px"><div style="font-size:20px;font-weight:700;color:{INK};'
            f'margin-top:14px">Выжимка</div>{"".join(blocks)}{note}</td></tr>')


def telegram_details(data: ReportData) -> str:
    parts = []
    grouped = items_by_keyword(data)
    if grouped:
        body = []
        for kw, items in grouped.items():
            body.append(f"<b>{e(kw)}</b>")
            for it in items:
                title = e(shorten(it.title, 110))
                body.append(f"• <a href=\"{html.escape(it.link)}\">{title}</a> · {e(it.host)}"
                            if it.link else f"• {title} · {e(it.host)}")
            body.append("")
        parts.append("<b>📰 Свежие материалы</b>")
        parts.append("<blockquote expandable>" + "\n".join(body).strip() + "</blockquote>")
    else:
        parts.append(f"За {data.window_h} ч совпадений не нашлось. Попробуйте другие слова: ☰ Меню → 🔑 Слова.")
    for n in footer_notes(data):
        parts.append(f"<i>⏳ {e(n)}</i>")
    if data.failed:
        parts.append("⚠️ Не удалось открыть: " + e(", ".join(data.failed)))
    out = "\n".join(parts)
    while len(out) > 4000 and grouped:
        for kw in list(grouped):
            if len(grouped[kw]) > 1:
                grouped[kw].pop()
        data_items = [i for lst in grouped.values() for i in lst]
        data = ReportData(**{**data.__dict__, "items": data_items})
        return telegram_details(data)
    return out


def email_html(data: ReportData) -> str:
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif"
    totals = " &nbsp;·&nbsp; ".join(
        f'<span style="color:{INK2}">{e(kw)}</span> <b style="color:{INK}">{data.totals.get(kw, 0)}</b>'
        for kw in data.keywords)
    items_html = []
    for kw, items in items_by_keyword(data).items():
        items_html.append(f'<p style="margin:22px 0 10px;font-size:13px;font-weight:700;color:{MUTED};'
                          f'text-transform:uppercase;letter-spacing:.04em">{e(kw)}</p>')
        for it in items:
            title = e(it.title)
            link = (f'<a href="{html.escape(it.link)}" style="color:{INK};text-decoration:none">{title}</a>'
                    if it.link else title)
            items_html.append(f'<p style="margin:0 0 12px;font-size:15px;line-height:1.45;color:{INK}">{link}'
                              f'<br><span style="font-size:12px;color:{MUTED}">{e(it.host)}</span></p>')
    if not items_html:
        items_html.append(f'<p style="font-size:14px;color:{INK2}">За {data.window_h} ч совпадений не нашлось.</p>')
    notes = "".join(f'<p style="margin:4px 0;font-size:12px;color:{MUTED}">{e(n)}</p>' for n in footer_notes(data))
    if data.failed:
        notes += f'<p style="margin:4px 0;font-size:12px;color:{MUTED}">Не удалось открыть: {e(", ".join(data.failed))}</p>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>KeyScout</title></head>
<body style="margin:0;background:{TILE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{TILE};font-family:{font}">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:{SURFACE};border-radius:16px">
<tr><td style="padding:22px 28px 6px;font-size:14px;line-height:1.5">
  <b style="color:{BRAND}">KeyScout</b> <span style="color:{MUTED}">· {e(human_time(data.checked_at))}</span><br>{totals}
</td></tr>
{email_digest(data)}
<tr><td style="padding:6px 16px 0"><img src="cid:chart" alt="Отчёт KeyScout: упоминания за {data.window_h} ч" width="568" style="width:100%;max-width:568px;display:block;border:0"></td></tr>
<tr><td style="padding:4px 28px 8px"><div style="font-size:20px;font-weight:700;color:{INK};margin-top:18px">Свежие материалы</div>{''.join(items_html)}</td></tr>
<tr><td style="padding:14px 28px 26px;border-top:1px solid {GRID}">{notes}<p style="margin:8px 0 0;font-size:12px;color:{MUTED}">Это письмо прислал бот KeyScout. Слова, сайты и почту можно поменять в Telegram.</p><p style="margin:6px 0 0;font-size:12px;color:{MUTED}">© Ivan Zh (<a href="{COPYRIGHT_URL}" style="color:{MUTED}">@o1928x</a>)</p></td></tr>
</table></td></tr></table></body></html>"""
