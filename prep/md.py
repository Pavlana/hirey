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


def _inline(text: str, links: bool = False) -> str:
    """Escape, then turn **bold** and *italic* into <strong> and <em> (and, with `links`, [text](url)
    with an http(s) or mailto URL into <a>). The markers are matched in the escaped text, so nothing
    from the file can add other markup."""
    out = _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", _h(text)))
    return _LINK.sub(r'<a href="\2">\1</a>', out) if links else out


def render_markdown(text: str, empty: str = '<p class="muted">No notes.</p>', links: bool = False) -> str:
    """Headings, bullet lists, paragraphs, **bold** and *italic*; with `links`, also [text](url) links
    (the dashboard leaves them as text). `empty` is the HTML for a text with nothing in it."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para), links)}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(i, links)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
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
    return "".join(out) or empty
