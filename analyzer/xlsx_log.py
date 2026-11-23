import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.getenv("SEARCH_LOG_XLSX", os.path.join(HERE, "..", "data", "searches.xlsx"))
MSK = ZoneInfo(os.getenv("REPORT_TZ", "Europe/Moscow"))

SHEET = "Поиски"
COLUMNS = [
    ("Время (МСК)", 17),
    ("Telegram", 18),
    ("Имя", 16),
    ("Chat ID", 13),
    ("Слово", 18),
    ("Искали варианты", 40),
    ("Сайты", 46),
    ("Период, ч", 10),
    ("Упоминаний", 12),
    ("Материалов", 12),
    ("Запуск", 22),
]
TRIGGER_COL = 10
TRIGGERS = {
    "manual": "вручную",
    "auto": "автообновление",
    "email": "на почту",
    "webapp": "отчёт из приложения",
    "webapp-email": "отчёт из приложения на почту",
    "app": "открыл обзор в приложении",
    "авто (раз в час)": "автообновление",
}

FONT = Font(name="Arial", size=10)
HEAD_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="2A78D6")
THIN = Side(style="thin", color="E1E0D9")


def _new_workbook() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    for col, (title, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(row=1, column=col, value=title)
        c.font, c.fill = HEAD_FONT, HEAD_FILL
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"
    return wb


def _read_rows() -> list[list]:
    if not os.path.exists(LOG_PATH):
        return []
    wb = load_workbook(LOG_PATH, read_only=True)
    try:
        rows = []
        for r in wb[SHEET].iter_rows(min_row=2, values_only=True):
            if any(v is not None for v in r):
                rows.append((list(r) + [None] * len(COLUMNS))[:len(COLUMNS)])
        return rows
    finally:
        wb.close()


def append(requester: dict, trigger: str, window_h: int, sites: list[str],
           rows: list[tuple[str, list[str], int, int]], when: datetime) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    local = when.astimezone(MSK).replace(tzinfo=None, microsecond=0)
    username = requester.get("username") or ""
    new = [[
        local,
        f"@{username}" if username else "",
        requester.get("name") or "",
        requester.get("chat_id"),
        word,
        ", ".join(v for v in variants if v.lower() != word.lower()),
        ", ".join(sites),
        window_h,
        mentions,
        materials,
        TRIGGERS.get(trigger, trigger),
    ] for word, variants, mentions, materials in rows]

    data = _read_rows() + new
    for r in data:
        r[TRIGGER_COL] = TRIGGERS.get(r[TRIGGER_COL], r[TRIGGER_COL])
    data = [r for _, r in sorted(enumerate(data), key=lambda p: (
        p[1][0] if isinstance(p[1][0], datetime) else datetime.min, -p[0]), reverse=True)]

    wb = _new_workbook()
    ws = wb[SHEET]
    for row in data:
        ws.append(row)
        r = ws.max_row
        for col in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=r, column=col)
            cell.font = FONT
            cell.border = Border(bottom=THIN)
            cell.alignment = Alignment(vertical="top", wrap_text=col in (6, 7))
        ws.cell(row=r, column=1).number_format = "DD.MM.YYYY HH:MM"
        ws.cell(row=r, column=4).number_format = "0"
        for col in (8, 9, 10):
            ws.cell(row=r, column=col).number_format = "#,##0"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 1)}"
    tmp = LOG_PATH + ".tmp"
    wb.save(tmp)
    os.replace(tmp, LOG_PATH)
