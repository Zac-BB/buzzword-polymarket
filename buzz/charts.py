"""Tiny server-side SVG charts, so the browser needs no chart library."""
import time
from html import escape


def _scale(values, lo_px, hi_px):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    return lo, hi, lambda v: hi_px - (v - lo) / (hi - lo) * (hi_px - lo_px)


def trend_class(points):
    if len(points) < 2:
        return "flat"
    d = points[-1][1] - points[0][1]
    return "up" if d > 1e-9 else "down" if d < -1e-9 else "flat"


def sparkline(points, width=120, height=32):
    if not points:
        return ""
    if len(points) == 1:
        points = points * 2
    vals = [p[1] for p in points]
    _, _, y = _scale(vals, 2, height - 2)
    n = len(vals) - 1
    pts = " ".join(f"{i / n * (width - 4) + 2:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark {trend_class(points)}" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" aria-hidden="true"><polyline points="{pts}"/></svg>')


def line_chart(points, opening=None, actual=None, width=640, height=240):
    """Step chart of the line, one step per bet, with the opening line dashed."""
    if not points:
        return '<p class="muted">No line history yet.</p>'
    if len(points) == 1:
        points = points * 2
    left, right, top, bottom = 44, 12, 12, 28
    vals = [p[1] for p in points]
    extra = [v for v in (opening, actual) if v is not None]
    lo, hi, y = _scale(vals + extra, top, height - bottom)
    n = len(vals) - 1
    x = lambda i: left + i / n * (width - left - right)

    path = [f"M{x(0):.1f},{y(vals[0]):.1f}"]
    for i in range(1, len(vals)):
        path.append(f"H{x(i):.1f}V{y(vals[i]):.1f}")
    d = "".join(path)
    area = d + f"V{height - bottom}H{x(0):.1f}Z"

    out = [f'<svg class="chart {trend_class(points)}" viewBox="0 0 {width} {height}" '
           f'role="img" aria-label="Line history">']
    # grid + y labels
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        yy = y(v)
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class="axis" x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    if opening is not None:
        yy = y(opening)
        out.append(f'<line class="opening" x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class="axis" x="{width - right}" y="{yy - 4:.1f}" text-anchor="end">open {opening:g}</text>')
    if actual is not None:
        yy = y(actual)
        out.append(f'<line class="actual" x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class="axis actual-label" x="{left + 4}" y="{yy - 4:.1f}">actual {actual}</text>')
    out.append(f'<path class="area" d="{area}"/>')
    out.append(f'<path class="stroke" d="{d}"/>')
    out.append(f'<circle class="dot" cx="{x(n):.1f}" cy="{y(vals[-1]):.1f}" r="4"/>')
    t0 = time.strftime("%b %d %H:%M", time.localtime(points[0][0]))
    t1 = time.strftime("%b %d %H:%M", time.localtime(points[-1][0]))
    out.append(f'<text class="axis" x="{left}" y="{height - 8}">{escape(t0)}</text>')
    out.append(f'<text class="axis" x="{width - right}" y="{height - 8}" text-anchor="end">{escape(t1)}</text>')
    out.append("</svg>")
    return "".join(out)
