"""Majlislar: oylik davomat jadvali (rangli), Excel eksport, statistika va kunlik hisobot qatori."""
import io
import json
from collections import defaultdict
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import db, meet_sched as S, meet_track as T

EMERALD = "047857"
# holat: (yorliq, matn, Excel rangi)
STATES = {
    "present": ("✓", "Qatnashdi", "C6EFCE"),
    "late": ("K", "Kech qoldi", "FFEB9C"),
    "early": ("E", "Erta chiqdi", "F8CBAD"),
    "late_early": ("K+E", "Kech qoldi va erta chiqdi", "F4B183"),
    "absent": ("✗", "Kelmadi", "FFC7CE"),
    "cancel": ("—", "Bekor / o'tmadi", "D9D9D9"),
    "pending": ("·", "Hali o'tmagan", "FFFFFF"),
}


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


def cell_state(p: dict | None, les: dict) -> str:
    st = les["status"]
    if st in ("cancelled", "missed"):
        return "cancel"
    if p is None:
        return "absent" if st == "done" else "pending"
    if p["late"] and p["early"]:
        return "late_early"
    return "late" if p["late"] else "early" if p["early"] else "present"


def month_grid(group_id: int, ym: str) -> dict:
    a, b = month_bounds(ym)
    lessons = S.lessons_between(a, b, group_id)
    per = {}
    for l in lessons:
        per[l["id"]] = {T.norm(p["person"]): p for p in T.person_rows(l["id"]) if not p["is_teacher"]}
    names = {}
    for d in per.values():
        for k, p in d.items():
            names.setdefault(k, p["person"])
    for n in T.roster(group_id):
        names.setdefault(T.norm(n), n)
    rows = []
    for k, name in sorted(names.items(), key=lambda x: x[1].lower()):
        cells, tot = [], defaultdict(int)
        for l in lessons:
            p = per[l["id"]].get(k)
            st = cell_state(p, l)
            lab, txt, _ = STATES[st]
            tip = txt + (f" · {p['first_in'][11:16]}–{(p['last_out'] or '…')[11:16]} · {p['minutes']} daq" if p else "")
            cells.append({"state": st, "label": lab, "tip": tip})
            if st in ("present", "late", "early", "late_early"):
                tot["came"] += 1
                tot["late"] += st in ("late", "late_early")
                tot["early"] += st in ("early", "late_early")
                tot["min"] += p["minutes"]
            elif st == "absent":
                tot["absent"] += 1
        held = tot["came"] + tot["absent"]
        rows.append({"name": name, "cells": cells, "came": tot["came"], "late": tot["late"], "early": tot["early"], "absent": tot["absent"],
                     "minutes": tot["min"], "pct": round(100 * tot["came"] / held) if held else None})
    col_tot = []
    for i, l in enumerate(lessons):
        col_tot.append(sum(1 for r in rows if r["cells"][i]["state"] in ("present", "late", "early", "late_early")))
    return {"lessons": lessons, "rows": rows, "col_tot": col_tot, "ym": ym, "states": STATES}


def export_xlsx(group_id: int, ym: str) -> bytes:
    g = S.group(group_id)
    grid = month_grid(group_id, ym)
    wb = Workbook()
    ws = wb.active
    ws.title = "Davomat"
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head = ["№", "O'quvchi"] + [f"{l['day'][8:10]}.{l['day'][5:7]}\n{l['hhmm']}" for l in grid["lessons"]] + ["Keldi", "Kech", "Erta", "Kelmadi", "Daqiqa", "%"]
    ws.append([f"{g['title']} — {ym} oylik davomat"])
    ws["A1"].font = Font(bold=True, size=14, color=EMERALD)
    ws.append([])
    ws.append(head)
    for c in ws[3]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=EMERALD)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    for i, r in enumerate(grid["rows"], 1):
        ws.append([i, r["name"]] + [c["label"] for c in r["cells"]] + [r["came"], r["late"], r["early"], r["absent"], r["minutes"], r["pct"] if r["pct"] is not None else ""])
        row = ws.max_row
        for j, c in enumerate(r["cells"]):
            cell = ws.cell(row=row, column=3 + j)
            cell.fill = PatternFill("solid", fgColor=STATES[c["state"]][2])
            cell.alignment = Alignment(horizontal="center")
        for col in range(1, len(head) + 1):
            ws.cell(row=row, column=col).border = border
    ws.append(["", "Qatnashganlar"] + grid["col_tot"])
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 30
    for j in range(len(grid["lessons"]) + 6):
        ws.column_dimensions[get_column_letter(3 + j)].width = 9
    ws.row_dimensions[3].height = 32
    ws.freeze_panes = "C4"
    ws.append([])
    ws.append(["", "Ranglar:"])
    for k, (lab, txt, color) in STATES.items():
        ws.append(["", f"{lab}  {txt}"])
        ws.cell(row=ws.max_row, column=2).fill = PatternFill("solid", fgColor=color)
    ws2 = wb.create_sheet("Darslar")
    ws2.append(["Sana", "Vaqt", "Holat", "O'qituvchi", "Rejada (daq)", "Haqiqatda (daq)", "Qatnashdi", "Eng ko'pi bilan"])
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=EMERALD)
    for l in grid["lessons"]:
        n = len([p for p in T.person_rows(l["id"]) if not p["is_teacher"]])
        ws2.append([l["day"], f"{l['hhmm']}–{l['end_hhmm']}", l["status_uz"], l["teacher_name"], l["duration_min"], l["elapsed"], n, l.get("peak") or 0])
    for col, w in zip("ABCDEFGH", (12, 14, 18, 24, 12, 14, 11, 14)):
        ws2.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- statistika
def stats(ym: str) -> dict:
    """Oylik: har o'qituvchi necha soat dars o'tdi va nechta guruhga; guruh bo'yicha jami soat."""
    a, b = month_bounds(ym)
    lessons = S.lessons_between(a, b)
    teachers, groups_ = {}, {}
    for l in lessons:
        if l["status"] not in ("done", "live"):
            continue
        mins = l["elapsed"]
        tname = l["teacher_name"] or "O'qituvchisiz"
        t = teachers.setdefault(tname, {"name": tname, "lessons": 0, "minutes": 0, "groups": set()})
        t["lessons"] += 1
        t["minutes"] += mins
        t["groups"].add(l["group_id"])
        g = groups_.setdefault(l["group_id"], {"title": l["group_title"], "lessons": 0, "minutes": 0, "planned": 0})
        g["lessons"] += 1
        g["minutes"] += mins
    for l in lessons:
        if l["status"] != "cancelled":
            groups_.setdefault(l["group_id"], {"title": l["group_title"], "lessons": 0, "minutes": 0, "planned": 0})["planned"] += 1
    tl = sorted(({**t, "groups": len(t["groups"]), "hours": round(t["minutes"] / 60, 1)} for t in teachers.values()), key=lambda x: -x["minutes"])
    gl = sorted(({**g, "hours": round(g["minutes"] / 60, 1)} for g in groups_.values()), key=lambda x: -x["minutes"])
    counts = defaultdict(int)
    for l in lessons:
        counts[l["status"]] += 1
    return {"teachers": tl, "groups": gl, "counts": dict(counts), "total_hours": round(sum(t["minutes"] for t in tl) / 60, 1), "ym": ym}


# ---------------------------------------------------------------- kunlik hisobotga qo'shiladigan varaq
def daily_rows(day: str) -> list[list]:
    nxt = S.fmt(S.parse(day + " 00:00:00").replace(hour=23, minute=59, second=59))
    out = []
    for l in S.lessons_between(day + " 00:00:00", nxt):
        sm = T.lesson_summary(l["id"]) if l["status"] in ("done", "live") else None
        out.append([f"{l['hhmm']}–{l['end_hhmm']}", l["group_title"], l["teacher_name"], l["status_uz"], l["elapsed"],
                    len(sm["students"]) if sm else 0, len(sm["late"]) if sm else 0, len(sm["early"]) if sm else 0, len(sm["absent"]) if sm else 0])
    return out


def add_daily_sheet(wb, day: str):
    """dailyreport.build() ga bitta qator bilan ulanadi. Majlis bo'lmasa varaq qo'shilmaydi."""
    S.ready()
    rows = daily_rows(day)
    if not rows:
        return
    ws = wb.create_sheet("Majlislar")
    ws.append(["Vaqt", "Guruh", "O'qituvchi", "Holat", "O'tgan vaqt (daq)", "Qatnashdi", "Kech qoldi", "Erta chiqdi", "Kelmadi"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=EMERALD)
    for r in rows:
        ws.append(r)
    for col, w in zip("ABCDEFGHI", (14, 30, 24, 18, 16, 11, 11, 11, 11)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
