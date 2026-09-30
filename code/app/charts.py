"""Oddiy SVG grafiklar (kutubxonasiz). Ranglar CSS o'zgaruvchilaridan olinadi (qorong'u rejimga mos)."""
from markupsafe import Markup, escape


def _fmt(v) -> str:
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 10_000:
        return f"{v / 1000:.0f}k"
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}k"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def line(values, labels=None, w=720, h=170, color="var(--pri)") -> Markup:
    vals = [float(v) if v is not None else None for v in values]
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return Markup('<div class="empty small">Grafik uchun ma\'lumot yetarli emas (kamida 2 kun)</div>')
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    L, R, T, B = 44, 10, 10, 24
    n = len(vals) - 1 or 1
    X = lambda i: L + (w - L - R) * i / n
    Y = lambda v: T + (h - T - B) * (1 - (v - lo) / (hi - lo))
    path = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
    grid = "".join(f'<line x1="{L}" x2="{w - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--bd)" stroke-width="1"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="10" fill="var(--mut)">{_fmt(v)}</text>'
                   for v in (lo, (lo + hi) / 2, hi))
    xl = ""
    if labels:
        for i in sorted({0, len(labels) // 2, len(labels) - 1}):
            xl += f'<text x="{X(i):.1f}" y="{h - 6}" text-anchor="middle" font-size="10" fill="var(--mut)">{escape(str(labels[i])[-5:])}</text>'
    area = f"{X(pts[0][0]):.1f},{h - B} {path} {X(pts[-1][0]):.1f},{h - B}"
    return Markup(f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" style="max-width:{w}px">{grid}{xl}'
                  f'<polygon points="{area}" fill="{color}" opacity=".10"/>'
                  f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/></svg>')


def bars(values, labels, w=720, h=170, color="var(--pri)") -> Markup:
    if not values or not any(values):
        return Markup('<div class="empty small">Ma\'lumot yo\'q</div>')
    hi = max(values) or 1
    L, R, T, B = 44, 10, 10, 24
    n = len(values)
    bw = (w - L - R) / n
    out = "".join(f'<rect x="{L + i * bw + 2:.1f}" y="{T + (h - T - B) * (1 - v / hi):.1f}" width="{max(bw - 4, 1):.1f}" '
                  f'height="{(h - T - B) * v / hi:.1f}" rx="3" fill="{color}" opacity=".85"><title>{escape(str(labels[i]))}: {_fmt(v)}</title></rect>'
                  for i, v in enumerate(values))
    step = max(1, n // 8)
    xl = "".join(f'<text x="{L + i * bw + bw / 2:.1f}" y="{h - 6}" text-anchor="middle" font-size="10" fill="var(--mut)">{escape(str(labels[i])[-5:])}</text>'
                 for i in range(0, n, step))
    axis = (f'<text x="{L - 6}" y="{T + 4}" text-anchor="end" font-size="10" fill="var(--mut)">{_fmt(hi)}</text>'
            f'<line x1="{L}" x2="{w - R}" y1="{h - B}" y2="{h - B}" stroke="var(--bd)"/>')
    return Markup(f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" style="max-width:{w}px">{axis}{out}{xl}</svg>')


def hbars(items, total=None, color="var(--pri)") -> Markup:
    """items: [(nom, son)] -> gorizontal chiziqlar (foiz bilan)."""
    items = [(k, v) for k, v in items if v]
    if not items:
        return Markup('<div class="empty small">Ma\'lumot yo\'q</div>')
    tot = total or sum(v for _, v in items) or 1
    rows = "".join(f'<div style="display:grid;grid-template-columns:130px 1fr 92px;gap:10px;align-items:center;margin:6px 0;font-size:13.5px">'
                   f'<span>{escape(str(k))}</span><span style="background:var(--surface2);border-radius:99px;height:10px;overflow:hidden">'
                   f'<i style="display:block;height:100%;width:{min(100, v / tot * 100):.1f}%;background:{color}"></i></span>'
                   f'<span class="mut">{_fmt(v)} · {v / tot * 100:.0f}%</span></div>' for k, v in items)
    return Markup(rows)
