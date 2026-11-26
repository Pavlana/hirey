"""Inline SVG charts for the Pipeline stats and Evals pages: funnel, columns, trend and lines.

Charts are rendered on the server, so they need no script and no library. They use SVG
presentation attributes and CSS classes only, because the Content-Security-Policy forbids
inline styles. Colours are shade steps `s1` (darkest) to `s10` (lightest) of the page's
hue, set in style.css by a theme class on <body>. A step class sets `color`, and shapes
paint with currentColor; text drawn on a filled block uses the matching ink class `k<n>`. Every label is HTML-escaped. There is
no xmlns attribute: inline SVG in HTML does not need one.

Each chart returns "" when there is nothing to draw, so the caller can show an empty state.
"""

from html import escape

SHADES = tuple(f"s{i}" for i in range(1, 11))  # dark to light



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


def _axis(top: int, x0: float, width: int, y_of, fmt=str) -> str:
    """Three horizontal gridlines (0, half, top) with labels."""
    marks = sorted({0, top // 2, top}) if top > 1 else [0, top]
    return "".join(
        f'<line class="grid" x1="{_n(x0)}" x2="{width}" y1="{_n(y_of(m))}" y2="{_n(y_of(m))}"/>'
        f'<text class="axis" x="{_n(x0 - 6)}" y="{_n(y_of(m) + 4)}" text-anchor="end">{_e(fmt(m))}</text>'
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


def lines(labels: list[str], series: list[tuple[str, str, list[float | None]]], width: int = 360,
          height: int = 150) -> str:
    """Rates (0..1) over a shared x axis, one line per series (name, shade, values; None leaves a gap
    in the dots). Gridlines at 0, 50 and 100%; each x label under its column."""
    if not labels or not any(v is not None for _, _, vals in series for v in vals):
        return ""
    left, right, top, bottom = 36, 8, 8, 22
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(len(labels) - 1, 1)
    x_of = lambda i: left + (i * step if len(labels) > 1 else plot_w / 2)  # noqa: E731
    y_of = lambda v: top + plot_h * (1 - min(max(v, 0.0), 1.0))  # noqa: E731
    parts = [f'<line class="grid" x1="{left}" x2="{width - right}" y1="{_n(y_of(m))}" y2="{_n(y_of(m))}"/>'
             f'<text class="axis" x="{left - 6}" y="{_n(y_of(m) + 4)}" text-anchor="end">{m:.0%}</text>'
             for m in (0.0, 0.5, 1.0)]
    parts += [f'<text class="axis" x="{_n(x_of(i))}" y="{height - 6}" text-anchor="middle">{_e(label)}</text>'
              for i, label in enumerate(labels)]
    for name, shade, values in series:
        pts = [(i, v) for i, v in enumerate(values) if v is not None]
        if len(pts) > 1:
            coords = " ".join(f"{_n(x_of(i))},{_n(y_of(v))}" for i, v in pts)
            parts.append(f'<polyline class="trend-line {_c(shade)}" points="{coords}" fill="none" stroke="currentColor"/>')
        parts += [f'<circle class="{_c(shade)}" cx="{_n(x_of(i))}" cy="{_n(y_of(v))}" r="3.5" fill="currentColor">'
                  f"<title>{_e(name)} at {_e(labels[i])}: {v:.0%}</title></circle>" for i, v in pts]
    return (f'<svg class="chart lines" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{_e(", ".join(n for n, _, _ in series))} over {len(labels)} points">{"".join(parts)}</svg>')
