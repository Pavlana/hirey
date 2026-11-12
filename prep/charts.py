"""Inline SVG charts for the dashboard: donut, treemap, funnel, columns and trend.

Charts are rendered on the server, so they need no script and no library. They use SVG
presentation attributes and CSS classes only, because the Content-Security-Policy forbids
inline styles. Colours are shade steps `s1` (darkest) to `s10` (lightest) of the page's
hue, set in style.css by a theme class on <body>. A step class sets `color`, and shapes
paint with currentColor; text drawn on a filled block uses the matching ink class `k<n>`. Every label is HTML-escaped. There is
no xmlns attribute: inline SVG in HTML does not need one.

Each chart returns "" when there is nothing to draw, so the caller can show an empty state.
"""

import math
from html import escape

SHADES = tuple(f"s{i}" for i in range(1, 11))  # dark to light

Item = tuple[str, int, str]  # (label, value, colour)


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _c(shade: str) -> str:
    """The CSS class for a shade step; anything else falls back to the middle step."""
    return shade if shade in SHADES else "s5"


def _k(shade: str) -> str:
    """The ink class (readable text colour) for text drawn on a block of `shade`."""
    return "k" + _c(shade)[1:]


def _n(x: float) -> str:
    """A compact number for SVG attributes."""
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _fit(text: str, width: float, char: float = 7.2) -> str:
    """Shorten a label so it fits in `width` user units (about `char` per character)."""
    room = int(width // char)
    if room >= len(text):
        return text
    return text[: max(room - 1, 1)] + "…" if room > 1 else ""


def donut(items: list[Item], label: str) -> str:
    """A donut of `items` with the total and `label` in the middle, plus a legend."""
    total = sum(v for _, v, _ in items if v > 0)
    if total <= 0:
        return ""
    size, r, width = 180, 68, 26
    mid = size / 2
    circumference = 2 * math.pi * r
    shown = [i for i in items if i[1] > 0]
    gap = 4 if len(shown) > 1 else 0
    arcs, offset = [], 0.0
    for name, value, color in shown:
        length = circumference * value / total
        arcs.append(
            f'<circle class="{_c(color)}" cx="{_n(mid)}" cy="{_n(mid)}" r="{r}" fill="none" stroke="currentColor" '
            f'stroke-width="{width}" stroke-dasharray="{_n(max(length - gap, .5))} {_n(circumference)}" '
            f'stroke-dashoffset="{_n(-offset)}"><title>{_e(name)}: {value}</title></circle>'
        )
        offset += length
    svg = (
        f'<svg class="chart donut" viewBox="0 0 {size} {size}" role="img" aria-label="{_e(label)}: {total}">'
        f'<circle class="track" cx="{_n(mid)}" cy="{_n(mid)}" r="{r}" fill="none" stroke-width="{width}"/>'
        f'<g transform="rotate(-90 {_n(mid)} {_n(mid)})">{"".join(arcs)}</g>'
        f'<text class="donut-total" x="{_n(mid)}" y="{_n(mid + 6)}" text-anchor="middle">{total}</text>'
        f'<text class="donut-label" x="{_n(mid)}" y="{_n(mid + 26)}" text-anchor="middle">{_e(label)}</text></svg>'
    )
    rows = "".join(
        f'<li><span class="swatch {_c(color)}"></span><span class="legend-label">{_e(name)}</span>'
        f'<span class="legend-n">{value}</span><span class="legend-pct">{value / total:.0%}</span></li>'
        for name, value, color in shown
    )
    return f'<div class="donut-wrap">{svg}<ul class="legend">{rows}</ul></div>'


def treemap(groups: list[tuple[str, str, list[Item]]], width: int = 440, height: int = 250) -> str:
    """Slice-and-dice treemap: one column per group (width by group total), split into
    blocks by item value. `groups` holds (group label, group colour, items)."""
    groups = [(g, c, [i for i in items if i[1] > 0]) for g, c, items in groups]
    groups = [g for g in groups if g[2]]
    total = sum(v for _, _, items in groups for _, v, _ in items)
    if total <= 0:
        return ""
    head, gap = 24, 4
    usable_w = width - gap * (len(groups) - 1)
    parts, x = [], 0.0
    for glabel, gcolor, items in groups:
        gtotal = sum(v for _, v, _ in items)
        w = usable_w * gtotal / total
        title = _fit(f"{glabel} · {gtotal}", w - 4) or str(gtotal)
        parts.append(f'<text class="tm-head {_c(gcolor)}" x="{_n(x + 2)}" y="16" fill="currentColor">{_e(title)}</text>')
        usable_h = height - head - gap * (len(items) - 1)
        y = float(head)
        for name, value, color in items:
            h = usable_h * value / gtotal
            parts.append(
                f'<rect class="{_c(color)}" x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" rx="6" '
                f'fill="currentColor"><title>{_e(glabel)} · {_e(name)}: {value}</title></rect>'
            )
            if h >= 22 and w >= 34:
                text = _fit(f"{name} {value}", w - 14)
                if text:
                    parts.append(f'<text class="tm-text {_k(color)}" x="{_n(x + 8)}" y="{_n(y + 16)}">{_e(text)}</text>')
            y += h + gap
        x += w + gap
    return (f'<svg class="chart treemap" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Treemap of {total} items">{"".join(parts)}</svg>')


def funnel(stages: list[tuple[str, int, float | None, str]], width: int = 440) -> str:
    """Centred bars, widest at the top, joined by soft bands. Stages are
    (label, count, share or None, colour) in funnel order."""
    top = max((n for _, n, _, _ in stages), default=0)
    if top <= 0:
        return ""
    row, bar, label_w, value_w = 40, 28, 114, 72
    area = width - label_w - value_w
    centre = label_w + area / 2
    parts, prev = [], None
    for i, (name, n, share, color) in enumerate(stages):
        w = area * n / top
        x, y = centre - w / 2, i * row
        if prev and n:
            px, pw, pcolor = prev
            parts.append(
                f'<polygon class="{_c(pcolor)} funnel-band" fill="currentColor" points="'
                f'{_n(px)},{_n(y - row + bar)} {_n(px + pw)},{_n(y - row + bar)} {_n(x + w)},{_n(y)} {_n(x)},{_n(y)}"/>'
            )
        pct = "–" if share is None else f"{share:.0%}"
        shape = (
            f'<rect class="{_c(color)}" x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{bar}" rx="7" fill="currentColor">'
            if n else  # nobody reached it: a small marker instead of a sliver
            f'<circle class="funnel-empty" cx="{_n(centre)}" cy="{_n(y + bar / 2)}" r="3">'
        )
        parts.append(
            shape + f"<title>{_e(name)}: {n} ({pct})</title></{'rect' if n else 'circle'}>"
            f'<text class="chart-label" x="0" y="{_n(y + bar / 2 + 5)}">{_e(_fit(name, label_w - 8))}</text>'
            f'<text class="chart-value" x="{width}" y="{_n(y + bar / 2 + 5)}" text-anchor="end">{n} · {pct}</text>'
        )
        prev = (x, w, color)
    height = (len(stages) - 1) * row + bar
    return (f'<svg class="chart funnel" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Funnel of {len(stages)} stages">{"".join(parts)}</svg>')


def _axis(top: int, x0: float, width: int, y_of) -> str:
    """Three horizontal gridlines (0, half, top) with labels."""
    marks = sorted({0, top // 2, top}) if top > 1 else [0, top]
    return "".join(
        f'<line class="grid" x1="{_n(x0)}" x2="{width}" y1="{_n(y_of(m))}" y2="{_n(y_of(m))}"/>'
        f'<text class="axis" x="{_n(x0 - 6)}" y="{_n(y_of(m) + 4)}" text-anchor="end">{m}</text>'
        for m in marks
    )


def columns(points: list[tuple[str, int]], target: int | None = None, color: str = "s6",
            hit_color: str = "s9", width: int = 440, height: int = 210) -> str:
    """Vertical columns with value labels and an optional dashed target line; columns that
    reach the target use `hit_color`."""
    if not points:
        return ""
    left, bottom, top_pad = 26, 22, 16
    top = max([n for _, n in points] + [target or 0, 1])
    plot_h = height - bottom - top_pad
    y_of = lambda v: top_pad + plot_h * (1 - v / top)  # noqa: E731
    slot = (width - left) / len(points)
    bw = slot * .58
    parts = [_axis(top, left, width, y_of)]
    for i, (label, n) in enumerate(points):
        x = left + i * slot + (slot - bw) / 2
        y = y_of(n)
        hit = target is not None and target > 0 and n >= target
        parts.append(
            f'<rect class="{_c(hit_color if hit else color)}" x="{_n(x)}" y="{_n(y)}" width="{_n(bw)}" '
            f'height="{_n(top_pad + plot_h - y)}" rx="4" fill="currentColor"><title>{_e(label)}: {n}</title></rect>'
            f'<text class="axis" x="{_n(x + bw / 2)}" y="{height - 4}" text-anchor="middle">{_e(label)}</text>'
        )
        if n:
            parts.append(f'<text class="chart-value" x="{_n(x + bw / 2)}" y="{_n(y - 5)}" text-anchor="middle">{n}</text>')
    if target:
        ty = y_of(target)
        parts.append(
            f'<line class="target" x1="{left}" x2="{width}" y1="{_n(ty)}" y2="{_n(ty)}"/>'
            f'<text class="target-label" x="{width}" y="{_n(ty - 5)}" text-anchor="end">target {target}</text>'
        )
    return (f'<svg class="chart columns" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Column chart of {len(points)} periods">{"".join(parts)}</svg>')


def trend(points: list[tuple[str, int]], low: int = 1, high: int = 5, color: str = "s7",
          width: int = 220, height: int = 44) -> str:
    """A small line of scores on a fixed `low`..`high` scale, oldest first, with a dot (and a
    tooltip) per point. One point is a dot only."""
    if not points:
        return ""
    pad = 6
    span = max(high - low, 1)
    step = (width - 2 * pad) / max(len(points) - 1, 1)
    x_of = lambda i: pad + i * step if len(points) > 1 else width / 2  # noqa: E731
    y_of = lambda v: pad + (height - 2 * pad) * (1 - (min(max(v, low), high) - low) / span)  # noqa: E731
    parts = [f'<line class="grid" x1="0" x2="{width}" y1="{_n(y_of(m))}" y2="{_n(y_of(m))}"/>' for m in (low, high)]
    if len(points) > 1:
        coords = " ".join(f"{_n(x_of(i))},{_n(y_of(v))}" for i, (_, v) in enumerate(points))
        parts.append(f'<polyline class="trend-line {_c(color)}" points="{coords}" fill="none" stroke="currentColor"/>')
    parts += [
        f'<circle class="{_c(color)}" cx="{_n(x_of(i))}" cy="{_n(y_of(v))}" r="3.5" fill="currentColor">'
        f"<title>{_e(label)}: {_e(v)}</title></circle>"
        for i, (label, v) in enumerate(points)
    ]
    return (f'<svg class="chart trend" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Trend of {len(points)} scores">{"".join(parts)}</svg>')
