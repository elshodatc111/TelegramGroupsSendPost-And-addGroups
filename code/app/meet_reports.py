"""Majlislar: oy yordamchilari va kunlik hisobotga qo'shiladigan bitta varaq (Excel, «Majlislar»)."""
from datetime import datetime

from openpyxl.styles import Font, PatternFill

from . import meet_sched as S

EMERALD = "047857"


def month_bounds(ym: str):
    y, m = int(ym[:4]), int(ym[5:7])
    a = datetime(y, m, 1)
    b = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return S.fmt(a), S.fmt(b)


def shift_month(ym: str, d: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + d
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y:04d}-{m:02d}"


def daily_rows(day: str) -> list[list]:
    out = []
    for l in S.lessons_between(day + " 00:00:00", day + " 23:59:59"):
        ms = [m for m in l.get("meetings", [])]
        out.append([f"{l['hhmm']}–{l['end_hhmm']}", l["group_title"], l["teacher_name"], l["status_uz"], len(ms),
                    ", ".join(sorted({m["account_label"] for m in ms}))])
    return out


def add_daily_sheet(wb, day: str):
    """dailyreport.build() ga bitta qator bilan ulanadi. Majlis bo'lmasa varaq qo'shilmaydi."""
    S.ready()
    rows = daily_rows(day)
    if not rows:
        return
    ws = wb.create_sheet("Majlislar")
    ws.append(["Vaqt", "Guruh", "O'qituvchi", "Holat", "Zoom majlislar soni", "Zoom akkauntlar"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=EMERALD)
    for r in rows:
        ws.append(r)
    for col, w in zip("ABCDEF", (14, 30, 24, 18, 20, 36)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
