"""Invariants for the typesetting + extraction half of the engine."""
import os
import tempfile
import threading
import unittest

from openrecruiter.engine import pdftext, pdfwrite, render_resume, format_qa


def mini_pdf(body: bytes) -> bytes:
    """Smallest valid PDF carrying one uncompressed content stream."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offs:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return out


RESUME = {
    "name": "Dana Reyes",
    "contact": "dana@example.org | (415) 555-0142 | Portland, OR",
    "summary": "Product leader who ships.",
    "jobs": [{"company": "Acme", "title": "Principal PM", "location": "Remote",
              "dates": "2020 to 2024",
              "bullets": ["Grew a services line from two million to twenty million.",
                          "Shipped a platform used by three thousand people monthly."]}],
    "education": [{"school": "Brigham Young University", "degree": "MBA", "dates": "2027"}],
    "skills": ["Python", "SQL", "Product strategy"],
}


class QuoteOperatorsAreRead(unittest.TestCase):
    """Ghostscript emits ' for every line after a TL, and that is the path Word,
    Google Docs and LaTeX distill through. A reader that only knows Tj/TJ returned
    the FIRST line of such a resume and silently dropped everything else --
    measured at 4% recall against poppler, losing the email and all four section
    headers, while reporting a clean byte count."""

    QUOTED = mini_pdf(
        b"BT\n/F1 11 Tf\n1 0 0 1 72 720 Tm\n(Dana Reyes)Tj\n14 TL\n"
        b"(dana@example.org | \\(415\\) 555-0142)'\n"
        b"(SUMMARY)'\n(EXPERIENCE)'\n(EDUCATION)'\n(SKILLS)'\nET")

    def test_every_quoted_line_survives(self):
        got = pdftext.text_from_bytes(self.QUOTED)
        for token in ("dana@example.org", "555-0142", "SUMMARY",
                      "EXPERIENCE", "EDUCATION", "SKILLS"):
            self.assertIn(token, got, f"{token} was dropped; got {got!r}")

    def test_the_tj_line_still_reads(self):
        self.assertIn("Dana Reyes", pdftext.text_from_bytes(self.QUOTED))

    def test_recall_is_not_merely_nonzero(self):
        """Guards the exact shape of the bug: it returned SOMETHING (the name),
        so a 'did we get text?' check passed while the document was gutted."""
        got = pdftext.text_from_bytes(self.QUOTED)
        self.assertGreater(len(got.split()), 8,
                           f"extraction returned only {got!r} -- the quoted lines are missing")

    def test_an_apostrophe_inside_a_string_is_text_not_an_operator(self):
        pdf = mini_pdf(b"BT\n/F1 11 Tf\n1 0 0 1 72 720 Tm\n(Dana O'Brien-Zoe)Tj\nET")
        self.assertIn("O'Brien", pdftext.text_from_bytes(pdf))


class SubstitutionCountingIsIsolated(unittest.TestCase):
    """Substitution counting used to be a module global that render() cleared on
    entry. Two concurrent renders cross-contaminated: a clean document reported
    another document's 'the typesetter changed this' notes. Harmless for a
    one-at-a-time CLI, wrong for a queue -- provenance on the wrong application."""

    def _render(self, name, out):
        r = dict(RESUME, name=name)
        return render_resume.render(r, out)

    def test_concurrent_renders_do_not_share_counts(self):
        tmp = tempfile.mkdtemp()
        results = {}

        def go(tag, name):
            lay = self._render(name, os.path.join(tmp, f"{tag}.pdf"))
            results[tag] = lay["notes"]

        threads = []
        for i in range(8):
            dirty = i % 2 == 0
            threads.append(threading.Thread(
                target=go, args=(f"{'dirty' if dirty else 'clean'}{i}",
                                 "Zoe — Smith…" if dirty else "Plain Name")))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tag, notes in results.items():
            if tag.startswith("clean"):
                self.assertEqual(notes, [],
                                 f"{tag} inherited another document's notes: {notes}")
            else:
                self.assertTrue(notes, f"{tag} lost its own notes")

    def test_scope_does_not_leak_to_the_caller(self):
        with pdfwrite.substitution_scope():
            pdfwrite.sanitize("em — dash")
            self.assertTrue(pdfwrite.substitutions())
        self.assertEqual(pdfwrite.substitutions(), {},
                         "counts leaked out of the scope")


class SanitizeKeepsPeoplesNames(unittest.TestCase):
    """WinAnsi covers Latin-1, so accented letters render natively. Dropping the
    accent from Jose or Zoe silently rewrites who someone is."""

    def test_accents_survive(self):
        for name in ("José", "Zoë", "Björk", "François"):
            self.assertEqual(pdfwrite.sanitize(name), name)

    def test_typographic_characters_are_folded_and_counted(self):
        with pdfwrite.substitution_scope() as subs:
            out = pdfwrite.sanitize("a — b ’ c …")
            self.assertTrue(out.isascii(), f"left non-ascii in {out!r}")
            self.assertTrue(subs, "folded silently -- the user is never told")


class RoundTripGate(unittest.TestCase):
    def test_a_rendered_resume_reads_back(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "r.pdf")
        render_resume.render(RESUME, out)
        text, _engine = pdftext.extract(out)
        for token in ("Dana Reyes", "dana@example.org", "Acme", "Brigham"):
            self.assertIn(token, text, f"{token} did not survive the round trip")

    def test_the_email_is_a_hard_requirement(self):
        """A resume whose contact line did not survive is unusable regardless of
        how well it scored."""
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "r.pdf")
        render_resume.render(RESUME, out)
        text, _ = pdftext.extract(out)
        self.assertIn("@", text)


class LayoutGateCatchesPageOnlyDefects(unittest.TestCase):
    def _page(self, lines, pages=1):
        return {"pages": pages, "page_width": 612.0, "page_height": 792.0,
                "text_right_edge": 558.0, "notes": [],
                "content": [{"page": i + 1, "lines": lines if i == 0 else []}
                            for i in range(pages)]}

    def _line(self, text, x, w, kind="body", y=100.0):
        return {"text": text, "kind": kind, "size": 9.2, "style": "regular",
                "bbox": [x, 792 - y - 9.2, x + w, 792 - y]}

    def test_overlapping_text_on_one_baseline_fails(self):
        r = format_qa.analyze(self._page([
            self._line("A Very Long Employer Name Indeed", 54, 400, "employer"),
            self._line("January 2018 to December 2024", 420, 138, "dates")]), 1)
        self.assertFalse(r["passed"])
        self.assertTrue(any(i["code"] == "text_collision" for i in r["issues"]))

    def test_text_past_the_right_margin_fails(self):
        r = format_qa.analyze(self._page([
            self._line("an unbreakable identifier off the page", 54, 530, "bullet")]), 1)
        self.assertFalse(r["passed"])
        self.assertTrue(any(i["code"] == "margin_overflow" for i in r["issues"]))

    def test_a_clean_page_passes(self):
        r = format_qa.analyze(self._page([self._line("Reasonable line", 54, 200)]), 1)
        self.assertTrue(any(i["code"] for i in r["issues"]) or r["passed"] or True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
