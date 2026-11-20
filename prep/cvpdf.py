"""A CV (Markdown: cv.md or a tailored copy) as a PDF: Markdown -> HTML (prep/md.py, `cv_layout` and the
committed template prep/cv_template.html, standard library) -> PDF (WeasyPrint, the one third-party library).

The layout: the name large on two lines, then the title line and a focus line, then the contact block
(shown top right); below, a narrow column (summary, skills, education...) and a wide one (experience,
projects...). The CV's words are kept as written, only grouped: no heading is renamed.

The PDF goes next to the Markdown file, same name with .pdf. WeasyPrint is imported only here and
only when a PDF is made, so the rest of the app runs without it. When this interpreter cannot load it
(not installed, or Pango missing), the `pdf_python` setting names one that can: a venv outside any
worktree. That interpreter runs `python -m prep.cvpdf IN.md OUT.pdf` from this checkout.

    python -m prep.cvpdf cv.example.md data/example-cv.pdf   (try the template on the made-up CV)
"""

import argparse
import html
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path
from string import Template

from prep import research
from prep.config import REPO_ROOT
from prep.md import render_inline, render_markdown

TEMPLATE = Path(__file__).resolve().parent / "cv_template.html"
MAX_PAGES = 2  # a normal CV; more gets a warning
RENDER_TIMEOUT = 120  # seconds, for the pdf_python subprocess


class PdfUnavailable(Exception):
    """WeasyPrint cannot be loaded here and no `pdf_python` is set (or it failed). The message says why."""


def cv_body(text: str) -> str:
    """The CV's Markdown without the +++ front matter a tailored file starts with (a plain CV has none)."""
    try:
        return research.split_front_matter(text)[1]
    except ValueError:
        return text


def cv_title(body: str, fallback: str) -> str:
    """The PDF's title: the first heading (the candidate's name), else `fallback` (the file name)."""
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            name = re.sub(r"[*_`]", "", line.strip().lstrip("#")).strip()
            if name:
                return name
    return fallback


# "## " sections that go in the narrow column (matched by a word in the heading); the rest go in the wide one
SIDE_WORDS = ("summary", "profile", "about", "skills", "expertise", "tools", "education", "training",
              "courses", "certification", "languages", "awards", "interests")
# a header item that is contact detail: a label, an email, a link or a phone number (or a _LOCATION)
_LABEL = re.compile(r"^(location|address|mobile|phone|tel|email|e-mail|linkedin|github|web|website)\s*:", re.I)
# "Town, UK", "Town, NY" or "Town, Country": a location, not a focus list like "Python, Kubernetes"
_COUNTRIES = ("England|Scotland|Wales|Ireland|Northern Ireland|United Kingdom|United States|Germany|France|"
              "Netherlands|Belgium|Spain|Portugal|Italy|Switzerland|Austria|Poland|Ukraine|Sweden|Norway|"
              "Denmark|Finland|Canada|Australia|New Zealand|India|Singapore|Remote")
_LOCATION = re.compile(rf"^[A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*, (?:[A-Z]{{2}}|USA|{_COUNTRIES})$")
_CONTACT = re.compile(r"@|linkedin|github|https?://|www\.|^\+?[\d ()-]{8,}$")
# the contact block's order: location, mobile, email, LinkedIn, GitHub, anything else
_CONTACT_ORDER = (re.compile(rf"^(?i:location|address)\s*:|{_LOCATION.pattern}"),
                  re.compile(r"^(mobile|phone|tel)\s*:|^\+?[\d ()-]{8,}$", re.I),
                  re.compile(r"^e-?mail\s*:|@", re.I),
                  re.compile(r"linkedin", re.I),
                  re.compile(r"github", re.I))
_YEAR = re.compile(r"\b(19|20)\d\d\b|present", re.I)
_NOTE_WORDS = 7  # a header item longer than this is a sentence, shown under the title line


def _contact_rank(item: str) -> int:
    return next((n for n, rx in enumerate(_CONTACT_ORDER) if rx.search(item)), len(_CONTACT_ORDER))


def masthead(name_line: str, lines: list[str]) -> str:
    """The header: the name (first word, then the rest, on two lines), the title line, the focus line
    ("A • B • C"), any longer sentence, then the contact block. `lines` are the Markdown lines between
    "# Name" and the first "## " section; their " · "-separated items are sorted into those parts. A
    role after " — " in the name line is the title."""
    name, _, role = name_line.lstrip("#").strip().partition(" — ")
    items = [i.strip() for line in lines for i in line.split(" · ") if i.strip()]
    contact = sorted((i for i in items if _LABEL.search(i) or _CONTACT.search(i) or _LOCATION.match(i)),
                     key=_contact_rank)
    rest = [i for i in items if i not in contact]
    notes = [i for i in rest if len(i.split()) > _NOTE_WORDS]
    rest = [i for i in rest if i not in notes]
    title = role.strip() or (rest.pop(0) if rest else "")
    focus = [f.strip() for i in rest for f in i.split(",") if f.strip()]
    first, _, last = name.partition(" ")
    out = ['<header class="mast">',
           f'<h2 class="name"><span>{render_inline(first)}</span> <span>{render_inline(last)}</span></h2>']
    if title:
        out.append(f'<p class="title">{render_inline(title)}</p>')
    if focus:
        out.append('<p class="focus">' + " • ".join(render_inline(f) for f in focus) + "</p>")
    out += [f'<p class="note">{render_inline(n, links=True)}</p>' for n in notes]
    if contact:  # after the title lines in reading order; the template's grid shows it top right
        out.append('<p class="contact">' + "<br>".join(render_inline(c, links=True) for c in contact) + "</p>")
    out.append("</header>")
    return "".join(out)


def _role(title: str, when: str, org: str) -> str:
    when_html = f' <span class="when">{when.replace(", ", " / ")}</span>' if when else ""
    org_html = f'<p class="org">{org}</p>' if org else ""
    return f'<p class="role"><strong>{title}</strong>{when_html}</p>{org_html}'


def _role_heading(match: re.Match) -> str:
    """"### Title · Company · Mon YYYY – Present": the title, the dates (the first later part with a
    year or "Present"), then the other parts as the company line."""
    parts = match.group(1).split(" · ")
    at = next((i for i, part in enumerate(parts) if i and _YEAR.search(part)), None)
    org = " · ".join(part for i, part in enumerate(parts) if i and i != at)
    return _role(parts[0], parts[at] if at is not None else "", org)


def _role_paragraph(match: re.Match) -> str:
    """"**Title — Company**, City, 2019–2024": the title, then city and dates, then the company line."""
    title, _, org = match.group(1).partition(" — ")
    return _role(title, match.group(2).lstrip(",").strip(), org)


def _roles(section: str) -> str:
    section = re.sub(r"<p><strong>([^<]*)</strong>((?:,[^<]*)?)</p>", _role_paragraph, section)
    return re.sub(r"<h4>(.*?)</h4>", _role_heading, section)


def cv_layout(body: str) -> str:
    """The CV's Markdown (no front matter) as the page's HTML: the header, then the sections in two
    columns. A CV without a "# Name" heading is rendered as it is, in one column."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if start is None:
        return render_markdown(body, empty="", links=True)
    end = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    before = render_markdown("\n".join(lines[:start]), empty="", links=True)
    head = masthead(lines[start], [line for line in lines[start + 1:end] if line.strip()])
    side, main = [], []
    for part in re.split(r"(?=<h3>)", render_markdown("\n".join(lines[end:]), empty="", links=True)):
        if not part:
            continue
        heading = re.match(r"<h3>(.*?)</h3>", part)
        to_side = heading is not None and any(w in heading.group(1).lower() for w in SIDE_WORDS)
        (side if to_side else main).append(f'<section class="block">{_roles(part)}</section>')
    return (before + head + '<div class="cols"><div class="side">' + "".join(side)
            + '</div><div class="main">' + "".join(main) + "</div></div>")


def cv_html(text: str, fallback_title: str = "CV") -> str:
    """The whole HTML page for a CV's Markdown (front matter dropped, every value escaped)."""
    body = cv_body(text)
    page = Template(TEMPLATE.read_text(encoding="utf-8"))
    return page.substitute(title=html.escape(cv_title(body, fallback_title)), body=cv_layout(body))


def pdf_path(md_path: Path) -> Path:
    return md_path.with_suffix(".pdf")


def _render_here(md_path: Path, out: Path) -> int:
    """Render with the WeasyPrint in this interpreter (ImportError or OSError when it cannot be loaded).
    Written to a temporary file first, so a failed render never leaves half a PDF."""
    from weasyprint import HTML  # imported here: only the PDF needs it

    document = HTML(string=cv_html(md_path.read_text(encoding="utf-8"), md_path.stem),
                    base_url=str(md_path.parent)).render()
    tmp = out.with_name(f".{out.name}.tmp")
    document.write_pdf(tmp)
    os.replace(tmp, out)
    return len(document.pages)


def write_pdf(md_path: Path, pdf_python: Path | None = None, out: Path | None = None) -> tuple[Path, int]:
    """Make the PDF for a CV's Markdown file; returns (its path, the page count). Uses this
    interpreter's WeasyPrint, else `pdf_python`; raises PdfUnavailable when neither works."""
    out = out or pdf_path(md_path)
    try:
        return out, _render_here(md_path, out)
    except (ImportError, OSError) as e:  # OSError: WeasyPrint installed, Pango not found
        if isinstance(e, OSError) and not _is_load_error(e):
            raise
        if pdf_python is None:
            raise PdfUnavailable("WeasyPrint is not available to this Python; set pdf_python in the settings "
                                 "to a venv's python that has it (README, \"PDF of a tailored CV\")") from None
    return out, _render_with(pdf_python, md_path, out)


def _is_load_error(e: OSError) -> bool:
    """An OSError from loading Pango or another library (not from reading or writing the files)."""
    return "library" in str(e).lower() or "dlopen" in str(e).lower()


def _render_with(python: Path, md_path: Path, out: Path) -> int:
    try:
        done = subprocess.run([str(python), "-m", "prep.cvpdf", str(md_path), str(out)], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PdfUnavailable(f"pdf_python ({python}) could not run: {type(e).__name__}") from None
    found = re.search(r"^(\d+) pages?\b", done.stdout, re.M)
    if done.returncode != 0 or not found:
        last = (done.stderr.strip().splitlines() or ["no output"])[-1][:200]
        raise PdfUnavailable(f"pdf_python ({python}) failed: {last}")
    return int(found.group(1))


_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")


def page_count(pdf: Path) -> int | None:
    """The pages of an existing PDF without WeasyPrint: page objects counted in the file and in its
    Flate-compressed streams (WeasyPrint keeps them in object streams). None when the file is
    missing or no page is found."""
    try:
        data = pdf.read_bytes()
    except OSError:
        return None
    count = len(_PAGE.findall(data))
    for raw in _STREAM.findall(data):
        try:
            count += len(_PAGE.findall(zlib.decompress(raw)))
        except zlib.error:
            continue
    return count or None


def page_note(pages: int) -> str:
    """The page count line the CLI prints, with a warning above MAX_PAGES."""
    return f"{pages} page{'s' if pages != 1 else ''}" + (
        f": WARNING, over {MAX_PAGES}; cut older or unrelated roles to one line" if pages > MAX_PAGES else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.cvpdf", description="A CV's Markdown as a PDF.")
    parser.add_argument("markdown", type=Path)
    parser.add_argument("pdf", type=Path, nargs="?", help="default: the same name with .pdf")
    parser.add_argument("--html", action="store_true", help="print the HTML instead (no WeasyPrint needed)")
    args = parser.parse_args(argv)
    if args.html:
        print(cv_html(args.markdown.read_text(encoding="utf-8"), args.markdown.stem))
        return 0
    out = args.pdf or pdf_path(args.markdown)
    pages = _render_here(args.markdown, out)  # never the pdf_python hop: this is what that hop runs
    print(f"{page_note(pages)}\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
