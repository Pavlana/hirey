"""A tailored CV (Markdown) as an ATS-friendly PDF: Markdown -> HTML (prep/md.py and the committed
template prep/cv_template.html, standard library) -> PDF (WeasyPrint, the one third-party library).

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
from prep.md import render_markdown

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


def cv_html(text: str, fallback_title: str = "CV") -> str:
    """The whole HTML page for a CV's Markdown (front matter dropped, every value escaped)."""
    body = cv_body(text)
    page = Template(TEMPLATE.read_text(encoding="utf-8"))
    return page.substitute(title=html.escape(cv_title(body, fallback_title)),
                           body=render_markdown(body, empty="", links=True))


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
