"""Tailored CV as a PDF: the HTML from the template, the CLI (`tailor pdf`, the PDF on `tailor save`), the
pdf_python fallback, the panel's PDF link and the /research/pdf route. cv.example.md and made-up text
only; the real render is skipped when WeasyPrint (or Pango) cannot be loaded."""

import io
import re
import subprocess
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import cvpdf, db, research, web
from prep.md import render_markdown
from tests.helpers import IsolatedTestCase
from tests.test_research import write
from tests.test_tailor import CV, TAILORED
from tests.test_web import HOST, TODAY, WebTestCase

try:
    import weasyprint  # noqa: F401
    WEASYPRINT = True
except (ImportError, OSError):  # OSError: installed, but Pango cannot be loaded
    WEASYPRINT = False

# About research.TAILOR_MAX_WORDS words of made-up CV in the usual shape (narrow-column sections, roles as
# headings, two-line bullets): should still fit two pages.
ROLE = ("### Senior Engineer · Example Widgets {n} (fictional) · Jan 2010 – Dec 2012\n"
        + "".join(f"- Built and ran example system {i} for twelve product teams, moving their releases to a shared "
                  "pipeline and cutting the time from merge to production by half.\n" for i in range(4)) + "\n")
LONG_CV = ("# Sam Sample\n\nEngineer · Python, Go · Leeds, UK\nsam@example.com · github.com/sam-sample\n\n"
           "## Summary\n\n" + "Engineer who builds example platforms for many teams and runs them in production. " * 7
           + "\n\n## Skills\n\n"
           + "".join(f"- **Area {i}**\n" + "".join(f"  - {s}\n" for s in ("Python", "Go", "Terraform", "Kubernetes",
                                                                         "Azure", "AWS", "GitHub Actions"))
                     for i in range(6))
           + "\n## Education\n\n**BSc Examples:** University of Example (fictional), 2009\n\n## Experience\n\n"
           + "".join(ROLE.format(n=n) for n in range(7)) + "Example extra words.\n")


class HtmlTest(IsolatedTestCase):
    def test_page_from_the_example_cv(self) -> None:
        page = cvpdf.cv_html(TAILORED, "CV_Globex_Eng_200126_1")
        self.assertIn("<title>Alex Example — Cloud Platform Engineer</title>", page)
        self.assertIn("<h3>Experience</h3>", page)
        self.assertIn("<li>Moved 40 services to Terraform-managed infrastructure.</li>", page)
        self.assertNotIn("+++", page, "front matter dropped")
        self.assertNotIn("tailored_at", page)
        self.assertNotIn("$", page, "every placeholder filled")
        for tag in ("<table", "<img", "<script", "<link", "http://"):
            self.assertNotIn(tag, page)
        self.assertIn("size: A4", page)
        self.assertIn("font-variant-ligatures: none", page)

    def test_escapes_and_links(self) -> None:
        page = cvpdf.cv_html("# <b>Name</b>\n\n[Site](https://example.com/a?b=1&c=2) · [Mail](mailto:x@example.com)"
                             " · [Bad](javascript:alert(1))\n\n<script>alert(1)</script>\n")
        self.assertIn("<title>&lt;b&gt;Name&lt;/b&gt;</title>", page)
        self.assertIn('<a href="https://example.com/a?b=1&amp;c=2">Site</a>', page)
        self.assertIn('<a href="mailto:x@example.com">Mail</a>', page)
        self.assertIn("[Bad](javascript:alert(1))", page, "other schemes stay text")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_a_plain_cv_without_front_matter(self) -> None:
        self.assertIn('<h2 class="name"><span>Alex</span> <span>Example</span></h2>', cvpdf.cv_html(CV))
        self.assertIn("<title>fallback</title>", cvpdf.cv_html("no heading here", "fallback"))

    def test_the_dashboard_still_shows_links_as_text(self) -> None:
        self.assertEqual(render_markdown("[a](https://example.com)"), "<p>[a](https://example.com)</p>")
        self.assertEqual(render_markdown(""), '<p class="muted">No notes.</p>')

    def test_page_note(self) -> None:
        self.assertEqual(cvpdf.page_note(1), "1 page")
        self.assertEqual(cvpdf.page_note(2), "2 pages")
        self.assertIn("WARNING, over 2", cvpdf.page_note(3))


# A made-up CV in the shape of a real one: the name, header lines with " · " items, "## " sections,
# roles as "### Title · Company · Dates" headings.
HEADED_CV = """# Sam Sample

Platform Engineer · Python, Kubernetes · Leeds, UK
GitHub: github.com/sam-sample · Mobile: +44 7700 900456 · Email: sam@example.com

## Summary

Engineer who builds example platforms.

## Experience

### Platform Engineer · Example Widgets (fictional) · Jan 2020 – Present
- Built example pipelines.

## Projects

### Example tool · personal project, 2025 · Python
- Wrote an example tool.

## Skills

- **Languages**
  - Python
  - Go

## Education & Training

**BSc Examples:** University of Example (fictional), 2012
"""


class LayoutTest(IsolatedTestCase):
    def test_header_parts_in_reading_order(self) -> None:
        page = cvpdf.cv_layout(HEADED_CV)
        self.assertIn('<h2 class="name"><span>Sam</span> <span>Sample</span></h2>', page)
        self.assertIn('<p class="title">Platform Engineer</p>', page)
        self.assertIn('<p class="focus">Python • Kubernetes</p>', page)
        self.assertIn('<p class="contact">Leeds, UK<br>+44 7700 900456<br>sam@example.com<br>'
                      "github.com/sam-sample</p>", page,
                      "location, mobile, email, LinkedIn, GitHub")
        self.assertIn('<div class="about"><section class="summary"><p>Engineer who builds example platforms.</p>'
                      "</section></div>", page, "the summary text top right, without its heading")
        order = [page.index(x) for x in ('class="name"', 'class="title"', 'class="focus"', 'class="contact"',
                                         'class="summary"', 'class="side"', 'class="main"')]
        self.assertEqual(order, sorted(order), "name, title lines, contact, summary, then the columns")

    def test_sections_split_into_two_columns(self) -> None:
        page = cvpdf.cv_layout(HEADED_CV)
        head, cols = page.split('<div class="cols">')
        side, main = cols.split('<div class="main">')
        self.assertNotIn("<h3>Summary</h3>", page)
        self.assertIn('<section class="summary">', head)
        for heading in ("Skills", "Education &amp; Training"):
            self.assertIn(f"<h3>{heading}</h3>", side)
        for heading in ("Experience", "Projects"):
            self.assertIn(f"<h3>{heading}</h3>", main)
        self.assertEqual(page.count('<section class="block">'), 4)
        self.assertIn("<h3>Skills</h3><ul><li><strong>Languages</strong><ul><li>Python</li><li>Go</li></ul></li></ul>",
                      side, "a skills group: its name, then one item per bullet")
        no_summary = cvpdf.cv_layout(HEADED_CV.replace("## Summary", "## Interests"))
        self.assertIn('<div class="about"></div>', no_summary, "no summary: the right side stays empty")

    def test_role_lines(self) -> None:
        page = cvpdf.cv_layout(HEADED_CV)
        self.assertIn('<p class="role"><strong>Platform Engineer</strong> <span class="when">Jan 2020 – Present</span>'
                      '</p><p class="org">Example Widgets (fictional)</p>', page)
        self.assertIn('<p class="role"><strong>Example tool</strong> <span class="when">personal project / 2025</span>'
                      '</p><p class="org">Python</p>', page, "the part with a year is the dates, wherever it is")
        self.assertIn("<p><strong>BSc Examples:</strong> University of Example (fictional), 2012</p>", page,
                      "a bold label followed by text stays a paragraph")
        example = cvpdf.cv_layout(CV)
        self.assertIn('<p class="title">Cloud Platform Engineer</p>', example, 'a role after " — " in the name')
        self.assertIn('<p class="role"><strong>Senior Platform Engineer</strong> <span class="when">2019–2024</span>'
                      '</p><p class="org">Brightwater Widgets (fictional)</p>', example)

    def test_words_kept(self) -> None:
        def words(text: str) -> list[str]:
            return sorted(re.findall(r"[a-z0-9]+", re.sub(r"<[^>]+>|&\w+;", " ", text).lower()))
        source = re.sub(r"[#*·—-]", " ", HEADED_CV.replace("## Summary\n", ""))
        source = re.sub(r"\b(Mobile|Email|GitHub): ", "", source)
        self.assertEqual(words(cvpdf.cv_layout(HEADED_CV)), words(source),
                         "every word kept (but the summary's heading and the contact labels), none added")

    def test_escaped_and_without_a_name_heading(self) -> None:
        page = cvpdf.cv_layout("# <b>Sam</b> Sample\n\n<i>x</i> · sam@example.com\n\n## Summary\n\n<script>\n")
        self.assertNotIn("<b>", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;b&gt;Sam&lt;/b&gt;", page)
        self.assertEqual(cvpdf.cv_layout("No heading.\n\n- a point\n"), "<p>No heading.</p><ul><li>a point</li></ul>")


class FallbackTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.md = write(self.tmp / "private" / "cv-tailored" / "CV_X_Eng_200126_1.md", TAILORED)

    def test_without_weasyprint_or_pdf_python(self) -> None:
        with mock.patch.object(cvpdf, "_render_here", side_effect=ImportError("no weasyprint")):
            with self.assertRaises(cvpdf.PdfUnavailable) as e:
                cvpdf.write_pdf(self.md)
        self.assertIn("pdf_python", str(e.exception))

    def test_pango_missing_counts_as_unavailable(self) -> None:
        err = OSError("cannot load library 'libgobject-2.0-0': dlopen failed")
        with mock.patch.object(cvpdf, "_render_here", side_effect=err), self.assertRaises(cvpdf.PdfUnavailable):
            cvpdf.write_pdf(self.md)
        with mock.patch.object(cvpdf, "_render_here", side_effect=PermissionError("denied")), \
                self.assertRaises(PermissionError, msg="a file error is not a missing library"):
            cvpdf.write_pdf(self.md)

    def test_pdf_python_renders_from_this_checkout(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="3 pages: WARNING, over 2; ...\n/x.pdf\n", stderr="")
        with mock.patch.object(cvpdf, "_render_here", side_effect=ImportError), \
                mock.patch.object(cvpdf.subprocess, "run", return_value=done) as run:
            out, pages = cvpdf.write_pdf(self.md, Path("/venv/bin/python"))
        self.assertEqual((out, pages), (self.md.with_suffix(".pdf"), 3))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/venv/bin/python", "-m", "prep.cvpdf", str(self.md), str(out)])
        self.assertEqual(run.call_args.kwargs["cwd"], cvpdf.REPO_ROOT)

    def test_pdf_python_failing(self) -> None:
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nModuleNotFoundError: weasyprint\n")
        with mock.patch.object(cvpdf, "_render_here", side_effect=ImportError), \
                mock.patch.object(cvpdf.subprocess, "run", return_value=done), \
                self.assertRaises(cvpdf.PdfUnavailable) as e:
            cvpdf.write_pdf(self.md, Path("/venv/bin/python"))
        self.assertIn("ModuleNotFoundError", str(e.exception))


def fake_pdf(pages: int):
    """A stand-in for cvpdf.write_pdf: writes a stub file, reports `pages`."""
    def write_pdf(md_path: Path, pdf_python: Path | None = None) -> tuple[Path, int]:
        out = cvpdf.pdf_path(md_path)
        out.write_bytes(b"%PDF-1.7 stub")
        return out, pages
    return write_pdf


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", status="to_apply")
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        self.v1 = write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 1), TAILORED)

    def save(self) -> tuple[int, str, str]:
        with mock.patch("sys.stdin", io.StringIO("note")):
            return self.run_cli("tailor", "save", str(self.app))

    def test_save_makes_the_pdf(self) -> None:
        with mock.patch.object(cvpdf, "write_pdf", side_effect=fake_pdf(1)):
            code, out, _ = self.save()
        self.assertEqual(code, 0)
        self.assertIn(f"PDF: {self.v1.with_suffix('.pdf')} (1 page)", out)

    def test_save_without_weasyprint_still_saves(self) -> None:
        with mock.patch.object(cvpdf, "write_pdf", side_effect=cvpdf.PdfUnavailable("WeasyPrint is not available")):
            code, out, _ = self.save()
        self.assertEqual(code, 0)
        self.assertIn("Saved tailored CV v1", out)
        self.assertIn("no PDF: WeasyPrint is not available; make it later with tailor pdf", out)
        with mock.patch.object(cvpdf, "write_pdf", side_effect=ValueError("bad css")):
            write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 2), TAILORED)
            code, out, _ = self.save()
        self.assertEqual(code, 0)
        self.assertIn("no PDF: the render failed (ValueError)", out)

    def test_pdf_command(self) -> None:
        code, _, err = self.run_cli("tailor", "pdf", str(self.app))
        self.assertEqual(code, 2)
        self.assertIn("no tailored CV yet", err)
        with mock.patch.object(cvpdf, "write_pdf", side_effect=cvpdf.PdfUnavailable("x")):
            self.save()
        v2 = write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 2), TAILORED)
        with mock.patch.object(cvpdf, "write_pdf", side_effect=cvpdf.PdfUnavailable("x")):
            self.save()
        with mock.patch.object(cvpdf, "write_pdf", side_effect=fake_pdf(3)) as made:
            code, out, _ = self.run_cli("tailor", "pdf", str(self.app))
            self.assertEqual(made.call_args.args[0], v2, "the latest version by default")
            self.assertIn("3 pages: WARNING, over 2", out)
            self.run_cli("tailor", "pdf", str(self.app), "--version", "1")
            self.assertEqual(made.call_args.args[0], self.v1)
        code, _, err = self.run_cli("tailor", "pdf", str(self.app), "--version", "9")
        self.assertEqual(code, 2)
        self.assertIn("no tailored CV v9 (versions 1 to 2)", err)
        with mock.patch.object(cvpdf, "write_pdf", side_effect=cvpdf.PdfUnavailable("x")):
            code, _, err = self.run_cli("tailor", "pdf", str(self.app))
        self.assertEqual((code, "error: no PDF: x" in err), (2, True), "here it is an error")


class DashboardTest(WebTestCase):
    MDE = 8  # the fixtures' to_apply application

    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        self.v1 = write(research.tailored_file(self.private, "Massive Dynamic Example", "AISE", TODAY, 1), TAILORED)
        self.v2 = write(research.tailored_file(self.private, "Massive Dynamic Example", "AISE", TODAY, 2), TAILORED)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.v1), "v1")
            db.save_tailored_cv(conn, self.MDE, str(self.v2), "v2")

    def raw(self, path: str) -> tuple[int, dict, bytes]:
        status, headers, body = web.handle(self.settings, "GET", path, HOST, today=TODAY)
        return status, dict(headers), body

    def test_panel_offers_the_latest_pdf_once_made(self) -> None:
        self.assertNotIn("/research/pdf", self.get(f"/?app={self.MDE}")[2])
        self.v1.with_suffix(".pdf").write_bytes(b"%PDF-1.7 v1")
        self.assertNotIn("/research/pdf", self.get(f"/?app={self.MDE}")[2], "only the latest version's")
        self.v2.with_suffix(".pdf").write_bytes(b"%PDF-1.7 v2")
        self.assertIn(f"/research/pdf?app={self.MDE}&amp;v=2", self.get(f"/?app={self.MDE}")[2])

    def test_pdf_route(self) -> None:
        self.assertEqual(self.raw(f"/research/pdf?app={self.MDE}&v=2")[0], 404, "not made yet")
        self.v2.with_suffix(".pdf").write_bytes(b"%PDF-1.7 v2")
        status, headers, body = self.raw(f"/research/pdf?app={self.MDE}&v=2")
        self.assertEqual((status, body), (200, b"%PDF-1.7 v2"))
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(headers["Content-Disposition"], f'inline; filename="{self.v2.stem}.pdf"')
        for bad in (f"app={self.MDE}", f"app={self.MDE}&v=x", f"app={self.MDE}&v=9", "app=999&v=1", "app=x&v=1"):
            self.assertEqual(self.raw(f"/research/pdf?{bad}")[0], 404, bad)
        outside = write(self.tmp / "outside.md", TAILORED)
        outside.with_suffix(".pdf").write_bytes(b"%PDF outside")
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(outside), "v3")
        self.assertEqual(self.raw(f"/research/pdf?app={self.MDE}&v=3")[0], 404, "never outside private_dir")


class RenderTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not WEASYPRINT:
            self.skipTest("WeasyPrint is not installed (pip install -r requirements.txt; brew install pango)")

    def test_example_cv_is_one_page(self) -> None:
        md = write(self.tmp / "CV_Example_Eng_200126_1.md", TAILORED)
        out, pages = cvpdf.write_pdf(md)
        self.assertEqual((out, pages), (md.with_suffix(".pdf"), 1))
        self.assertTrue(out.read_bytes().startswith(b"%PDF"))
        self.assertFalse(list(self.tmp.glob(".*.tmp")), "no temporary file left")

    def test_a_cv_at_the_word_limit_fits_two_pages(self) -> None:
        self.assertGreaterEqual(research.word_count(LONG_CV), research.TAILOR_MAX_WORDS)
        _, pages = cvpdf.write_pdf(write(self.tmp / "long.md", LONG_CV))
        self.assertLessEqual(pages, cvpdf.MAX_PAGES)
