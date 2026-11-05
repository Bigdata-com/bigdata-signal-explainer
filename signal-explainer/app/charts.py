"""Server-rendered SVG chart of the sentiment series, so reports print and export without JavaScript."""
from __future__ import annotations

from datetime import date
from html import escape


def sentiment_svg(series: list[dict], window_start: date, as_of: date, inflections: list[dict],
                  peers: list[dict] | None = None, width: int = 760, height: int = 230) -> str:
    pts = [v for v in series if v.get("daily_sentiment") is not None]
    if len(pts) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 14, 14, 26
    ys = [v["daily_sentiment"] for v in pts]
    for p in peers or []:
        ys += [v["daily_sentiment"] for v in p.get("series") or [] if v.get("daily_sentiment") is not None]
    lo, hi = min(ys + [0]), max(ys + [0])
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08
    d0, d1 = date.fromisoformat(pts[0]["date"]), date.fromisoformat(pts[-1]["date"])
    days = max((d1 - d0).days, 1)

    def x(d: str | date) -> float:
        dd = date.fromisoformat(d) if isinstance(d, str) else d
        return pad_l + (dd - d0).days / days * (width - pad_l - pad_r)

    def y(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * (height - pad_t - pad_b)

    def path(vals: list[dict]) -> str:
        return " ".join(f"{'M' if i == 0 else 'L'}{x(v['date']):.1f},{y(v['daily_sentiment']):.1f}"
                        for i, v in enumerate(vals))

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
           f'aria-label="Daily sentiment with the lookback window shaded">']
    wx0, wx1 = x(max(window_start, d0)), x(min(as_of, d1))
    out.append(f'<rect x="{wx0:.1f}" y="{pad_t}" width="{max(wx1 - wx0, 1):.1f}" '
               f'height="{height - pad_t - pad_b}" class="c-window"/>')
    for t in _ticks(lo, hi):
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(t):.1f}" y2="{y(t):.1f}" '
                   f'class="{"c-zero" if abs(t) < 1e-9 else "c-grid"}"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y(t) + 4:.1f}" class="c-axis" text-anchor="end">{t:+.2f}</text>')
    for p in peers or []:
        sv = [v for v in p.get("series") or [] if v.get("daily_sentiment") is not None and v["date"] >= pts[0]["date"]]
        if len(sv) > 1:
            out.append(f'<path d="{path(sv)}" class="c-peer"><title>{escape(p.get("name") or "")}</title></path>')
    out.append(f'<path d="{path(pts)}" class="c-line"/>')
    by_date = {v["date"]: v["daily_sentiment"] for v in pts}
    for f in inflections:
        if f["date"] in by_date:
            out.append(f'<circle cx="{x(f["date"]):.1f}" cy="{y(by_date[f["date"]]):.1f}" r="4.5" '
                       f'class="{"c-up" if f["delta"] >= 0 else "c-down"}"><title>{f["date"]}: {f["delta"]:+.3f}</title></circle>')
    for d in _month_ticks(d0, d1):
        out.append(f'<text x="{x(d):.1f}" y="{height - 7}" class="c-axis" text-anchor="middle">{d.strftime("%b %y")}</text>')
    out.append("</svg>")
    return "".join(out)


def _ticks(lo: float, hi: float) -> list[float]:
    step = next(s for s in (0.05, 0.1, 0.2, 0.25, 0.5) if (hi - lo) / s <= 6)
    t = round(lo / step) * step
    out = []
    while t <= hi:
        if t >= lo:
            out.append(round(t, 4))
        t += step
    return out


def _month_ticks(d0: date, d1: date) -> list[date]:
    out, y, m = [], d0.year, d0.month
    months = (d1.year - d0.year) * 12 + d1.month - d0.month
    every = max(1, months // 6 + (1 if months % 6 else 0))
    i = 0
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        d = date(y, m, 1)
        if d > d1:
            return out
        if i % every == 0:
            out.append(d)
        i += 1
