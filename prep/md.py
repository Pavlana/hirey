"""A small, safe subset of Markdown as HTML, shared by the dashboard (research, fit and CV files) and
the tailored CV PDF (prep/cvpdf.py). Every piece of text is escaped; no images or raw HTML."""

import html
import re

_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
# [text](https://…) or [text](mailto:…), matched in the escaped text: the URL can hold no quote or
# angle bracket (escaped to &quot; &lt; &gt;, and "&" is the only "&…;" an escaped URL may carry).
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^\s()\"'<>]+)\)")


def _h(text: str) -> str:
    return html.escape(text, quote=True)


_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _inline(text: str, links: bool = False) -> str:
    """Escape, then turn `code` into <code> (its text kept as is), **bold** and *italic* into <strong>
    and <em> (and, with `links`, [text](url) with an http(s) or mailto URL into <a>). The markers are
    matched in the escaped text, so nothing from the file can add other markup."""
    parts = _CODE_SPAN.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{_h(part)}</code>")
            continue
        styled = _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", _h(part)))
        out.append(_LINK.sub(r'<a href="\2">\1</a>', styled) if links else styled)
    return "".join(out)


def render_inline(text: str, links: bool = False) -> str:
    """One line of Markdown (no block markup) as escaped HTML: `code`, **bold**, *italic* and, with
    `links`, http(s) and mailto links."""
    return _inline(text, links)


def render_markdown(text: str, empty: str = '<p class="muted">No notes.</p>', links: bool = False,
                    breaks: bool = False) -> str:
    """Headings, bullet lists, paragraphs, ``` fenced code blocks, `code`, **bold** and *italic*; with
    `links`, also [text](url) links
    (the dashboard leaves them as text). With `breaks`, a line break inside a paragraph stays one
    (<br>; typed notes) instead of joining the lines. `empty` is the HTML for a text with nothing in it."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            body = "<br>".join(_inline(p, links) for p in para) if breaks else _inline(" ".join(para), links)
            out.append(f"<p>{body}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(i, links)}</li>" for i in items) + "</ul>")
            items.clear()

    code: list[str] | None = None  # the lines of an open ``` block
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            if code is None:
                code = []
            else:
                out.append("<pre><code>" + _h("\n".join(code)) + "</code></pre>")
                code = None
        elif code is not None:
            code.append(line.rstrip())
        elif not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = min(len(stripped) - len(stripped.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{_inline(stripped.lstrip('#').strip(), links)}</h{level}>")
        elif stripped[:2] in ("- ", "* "):
            if para:
                flush()
            items.append(stripped[2:].strip())
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    if code is not None:  # an unclosed block runs to the end
        out.append("<pre><code>" + _h("\n".join(code)) + "</code></pre>")
    return "".join(out) or empty
