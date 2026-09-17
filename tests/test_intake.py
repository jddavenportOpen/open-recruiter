"""Invariants for the experience-bank intake.

Three things are being guarded here, and each one has already failed somewhere:

  1. A file we read BADLY is flagged loudly. The specific bug: a ghostscript-
     distilled resume extracted at 4% recall -- the name survived, the email and
     every section header did not -- while the reader reported a clean byte
     count. `ByteCountIsNotAShortfall` builds exactly that file, at the SAME size
     as a healthy one, and asserts the two get opposite verdicts.
  2. A field that cannot be derived honestly is left ABSENT, with the reason
     recorded. A plausible guess in an experience bank becomes a false claim on
     every resume built from it.
  3. Every headline number traces back to the file it came from, and two
     generations that disagree are surfaced rather than silently merged.
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile

from openrecruiter import intake
from openrecruiter.engine import validate_bank


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def mini_pdf(body: bytes, pad_lines: int = 0) -> bytes:
    """Smallest valid PDF carrying one uncompressed content stream.

    `pad_lines` inflates the FILE without adding a single character of text,
    which is what embedded fonts and images do to a real resume. It is the knob
    that lets a gutted document weigh as much as a healthy one.
    """
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
    if pad_lines:
        out += b"\n" + b"\n".join(b"%" + b"x" * 90 for _ in range(pad_lines))
    return out


def text_pdf(lines: list[bytes], pad_lines: int = 0) -> bytes:
    """Ghostscript's shape: one Tj, then ' for every line after a TL."""
    body = (b"BT\n/F1 11 Tf\n1 0 0 1 72 720 Tm\n14 TL\n"
            + b"\n".join(lines) + b"\nET")
    return mini_pdf(body, pad_lines)


PAD = 2000          # ~185KB of non-text bytes, the weight of embedded fonts

HEALTHY_LINES = [
    b"(Dana Reyes)Tj",
    b"(dana@example.org | \\(415\\) 555-0142 | Portland, OR)'",
    b"(SUMMARY)'",
    b"(Product leader who ships infrastructure people rely on every day of the week.)'",
    b"(EXPERIENCE)'",
    b"(Acme Corp | Principal Product Manager | Remote | 2020 - 2024)'",
    b"(- Grew the services line from $2M to $20M in annual recurring revenue.)'",
    b"(- Shipped a platform used by 3,000 people monthly across four regions.)'",
    b"(- Cut onboarding time by 40% by rebuilding the whole first-run experience.)'",
    b"(- Hired and ran a team of nine across product, design and analytics.)'",
    b"(Northstar Labs | Senior Analyst | Boise, ID | Jan 2017 - Dec 2019)'",
    b"(- Built the forecasting model the finance team still runs every quarter.)'",
    b"(- Reduced the monthly close from nine days to four days of real work.)'",
    b"(- Wrote the reporting layer three departments now depend on daily.)'",
    b"(EDUCATION)'",
    b"(Brigham Young University, MBA, 2027)'",
    b"(SKILLS)'",
    b"(Python, SQL, Product strategy, Forecasting, Stakeholder management)'",
]

# The 4%-recall failure: the name comes back, and nothing that identifies the
# person or structures the document does. The words that survive are real words,
# so an "is there text?" check and an alpha-ratio check both pass.
_PROSE = ("led the team through a transition that took most of a year and changed how "
          "the group worked together day to day while keeping the lights on for "
          "everyone involved and the partners who depended on that work").split()
GUTTED_LINES = [b"(Dana Reyes)Tj"] + [
    b"(%s)'" % " ".join(_PROSE[i:i + 8]).encode() for i in range(0, len(_PROSE), 8)]


RESUME_2024 = """Dana Reyes
dana@example.org | (415) 555-0142 | Portland, OR

SUMMARY
Product leader who ships infrastructure people rely on every day.

EXPERIENCE
Acme Corp | Principal Product Manager | Remote | 2020 - 2024
- Grew the services line from $2M to $20M in annual recurring revenue.
- Shipped a platform used by 3,000 people monthly across four regions.
- Cut onboarding time by 40% by rebuilding the first-run experience.

Northstar Labs | Senior Analyst | Boise, ID | Jan 2017 - Dec 2019
- Built the forecasting model that the finance team still runs quarterly.
- Reduced the monthly close from nine days to four days.

EDUCATION
Brigham Young University, MBA, 2027

SKILLS
Python, SQL, Product strategy, Forecasting, Stakeholder management
"""

# Same career, three years earlier. The employer and title match, so this MERGES
# into the same job -- and carries a different number for the same achievement.
RESUME_2021 = """Dana Reyes
dana.reyes@oldmail.example | (415) 555-0142

SUMMARY
Product leader who ships.

EXPERIENCE
Acme Corp | Principal Product Manager | 2020 - 2022
- Grew the services line from $2M to $18M in annual recurring revenue.
- Shipped a platform used by 3,000 people monthly across four regions.

SKILLS
Python, Excel
"""


def write(folder: str, name: str, data) -> str:
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return path


def write_docx(path: str, paragraphs: list[str]) -> str:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
                   + body + "</w:body></w:document>")
    return path


class Tmp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="intake-")
        cls.addClassCleanup(shutil.rmtree, cls.dir, True)


# --------------------------------------------------------------------------- #
class DiscoveryNeverConfusesEmptyWithBlind(Tmp):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        write(cls.dir, "current.txt", RESUME_2024)
        write(cls.dir, "old.md", RESUME_2021)
        write(cls.dir, "printed.pdf", text_pdf(HEALTHY_LINES))
        write_docx(os.path.join(cls.dir, "word-version.docx"), ["Dana Reyes"])
        write(cls.dir, ".hidden-resume.txt", "x")
        write(cls.dir, "headshot.png", b"\x89PNG")
        write(cls.dir, "notes.json", "{}")
        write(cls.dir, "archive/older.txt", RESUME_2021)

    def test_finds_every_supported_type_including_nested(self):
        names = {os.path.basename(p) for p in intake.discover_resumes(self.dir)}
        self.assertEqual(names, {"current.txt", "old.md", "printed.pdf",
                                 "word-version.docx", "older.txt"})

    def test_skips_dotfiles_and_types_that_are_not_resumes(self):
        names = {os.path.basename(p) for p in intake.discover_resumes(self.dir)}
        for skipped in (".hidden-resume.txt", "headshot.png", "notes.json"):
            self.assertNotIn(skipped, names)

    def test_non_recursive_stays_put(self):
        names = {os.path.basename(p)
                 for p in intake.discover_resumes(self.dir, recursive=False)}
        self.assertNotIn("older.txt", names)

    def test_order_is_deterministic(self):
        self.assertEqual(intake.discover_resumes(self.dir),
                         sorted(intake.discover_resumes(self.dir)))

    def test_a_folder_we_cannot_read_raises_instead_of_returning_empty(self):
        """'Nothing is here' and 'I could not look' are different answers. A
        caller that cannot tell them apart builds a bank out of nothing."""
        with self.assertRaises(intake.IntakeError):
            intake.discover_resumes(os.path.join(self.dir, "does-not-exist"))

    def test_a_file_is_not_a_folder(self):
        """Asserting on the MESSAGE, not merely on the raise. A .txt is not
        executable, so the permissions guard below rejects it too -- this test
        passed for years with the isdir check deleted, telling the caller to
        chmod a file that was never the problem."""
        with self.assertRaises(intake.IntakeError) as raised:
            intake.discover_resumes(os.path.join(self.dir, "current.txt"))
        self.assertIn("is a file, not a folder", str(raised.exception))

    def test_a_folder_we_cannot_enter_says_permissions(self):
        """The other half of the pair: the permissions refusal has to stay
        reachable and has to say something different from the one above."""
        locked = tempfile.mkdtemp(dir=self.dir)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.R_OK | os.X_OK):
            self.skipTest("running as a user chmod cannot lock out (root?)")
        with self.assertRaises(intake.IntakeError) as raised:
            intake.discover_resumes(locked)
        self.assertIn("permissions", str(raised.exception))

    def test_an_empty_folder_is_a_real_empty_answer(self):
        empty = tempfile.mkdtemp(dir=self.dir)
        self.assertEqual(intake.discover_resumes(empty), [])


# --------------------------------------------------------------------------- #
class ByteCountIsNotAShortfall(Tmp):
    """The load-bearing test. Two PDFs of the SAME size: one reads completely,
    one comes back with the name and some prose and nothing else. A reader that
    grades on bytes calls them both fine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.healthy = write(cls.dir, "healthy.pdf", text_pdf(HEALTHY_LINES, PAD))
        cls.gutted = write(cls.dir, "gutted.pdf", text_pdf(GUTTED_LINES, PAD))

    def test_the_two_files_really_are_the_same_weight(self):
        a, b = os.path.getsize(self.healthy), os.path.getsize(self.gutted)
        self.assertLess(abs(a - b) / max(a, b), 0.05,
                        "fixture broken: the comparison is only honest at equal size")

    def test_the_healthy_read_is_high_confidence(self):
        rec = intake.extract(self.healthy)
        self.assertEqual(rec["level"], intake.HIGH,
                         f"clean resume graded {rec['level']}: {rec['warnings']}")
        self.assertEqual(rec["warnings"], [])

    def test_the_gutted_read_is_flagged(self):
        rec = intake.extract(self.gutted)
        self.assertEqual(rec["level"], intake.LOW)
        self.assertLess(rec["confidence"], 0.5)

    def test_it_produced_enough_text_to_fool_a_naive_check(self):
        """The bug's signature: it returned SOMETHING, so 'did we get text?'
        passed while the document was gutted."""
        rec = intake.extract(self.gutted)
        self.assertGreater(rec["chars"], intake.MIN_USABLE_CHARS)
        self.assertNotEqual(rec["level"], intake.UNREADABLE)

    def test_the_warnings_name_what_is_missing(self):
        warnings = " ".join(intake.extract(self.gutted)["warnings"]).lower()
        self.assertIn("email", warnings)
        self.assertIn("section header", warnings)
        self.assertIn("characters out of", warnings)

    def test_the_gap_between_them_is_not_cosmetic(self):
        healthy = intake.extract(self.healthy)["confidence"]
        gutted = intake.extract(self.gutted)["confidence"]
        self.assertGreater(healthy - gutted, 0.4,
                           f"healthy {healthy} vs gutted {gutted}: the grade is not discriminating")


class ConfidenceHeuristics(Tmp):
    def _txt(self, name, body):
        return write(self.dir, name, body)

    def test_no_email_can_never_be_high(self):
        """An email is not one signal among four. Without it the contact line did
        not survive, and a resume whose contact line is gone is unusable however
        good the rest of the read was -- the round-trip gate hard-fails on it."""
        body = RESUME_2024.replace("dana@example.org | ", "")
        rec = intake.extract(self._txt("no-email.txt", body))
        self.assertFalse(rec["signals"]["has_email"])
        self.assertEqual(rec["level"], intake.LOW)
        self.assertGreaterEqual(rec["confidence"], 0.6,
                                "fixture is meant to be strong everywhere EXCEPT the email")

    def test_no_section_headers_is_flagged(self):
        body = "Dana Reyes\ndana@example.org\n" + ("some prose about work. " * 30)
        rec = intake.extract(self._txt("no-headers.txt", body))
        self.assertFalse(rec["signals"]["has_section_headers"])
        self.assertEqual(rec["level"], intake.LOW)

    def test_glyph_soup_is_flagged(self):
        body = "Dana Reyes dana@example.org\nSUMMARY\nEXPERIENCE\n" + ("3 8 1 9 " * 120)
        rec = intake.extract(self._txt("glyphs.txt", body))
        self.assertFalse(rec["signals"]["alpha_ratio_ok"])
        self.assertEqual(rec["level"], intake.LOW)

    def test_almost_nothing_is_unreadable_not_merely_low(self):
        rec = intake.extract(self._txt("scan.txt", "Dana Reyes"))
        self.assertEqual(rec["level"], intake.UNREADABLE)
        self.assertEqual(rec["confidence"], 0.0)
        self.assertFalse(rec["usable"])
        self.assertTrue(rec["error"])

    def test_a_clean_text_resume_is_high(self):
        rec = intake.extract(self._txt("clean.txt", RESUME_2024))
        self.assertEqual(rec["level"], intake.HIGH)
        self.assertEqual(rec["warnings"], [])

    def test_a_docx_reads(self):
        path = write_docx(os.path.join(self.dir, "d.docx"), RESUME_2024.splitlines())
        rec = intake.extract(path)
        self.assertEqual(rec["engine"], "docx-ooxml")
        self.assertIn("dana@example.org", rec["text"])

    def test_the_engine_that_answered_is_reported(self):
        rec = intake.extract(write(self.dir, "e.pdf", text_pdf(HEALTHY_LINES)))
        self.assertIn(rec["engine"], ("stdlib", "pymupdf"))

    def test_a_missing_file_raises(self):
        with self.assertRaises(intake.IntakeError):
            intake.extract(os.path.join(self.dir, "nope.pdf"))

    def test_an_unsupported_type_raises(self):
        with self.assertRaises(intake.IntakeError):
            intake.extract(write(self.dir, "x.rtf", "Dana"))


# --------------------------------------------------------------------------- #
class MergeRefusesToInvent(Tmp):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current = write(cls.dir, "dana-2024.txt", RESUME_2024)
        cls.old = write(cls.dir, "dana-2021.txt", RESUME_2021)

    def bank(self, *paths):
        return intake.merge_to_bank([intake.extract(p) for p in paths])

    def test_a_complete_resume_produces_a_bank_the_validator_accepts(self):
        result = validate_bank.validate(self.bank(self.current))
        self.assertTrue(result["passed"], result["errors"])

    def test_the_bank_carries_the_real_bullets(self):
        job = self.bank(self.current)["jobs"][0]
        self.assertEqual(job["company"], "Acme Corp")
        self.assertEqual(job["title"], "Principal Product Manager")
        self.assertIn("Grew the services line from $2M to $20M in annual recurring revenue.",
                      job["bullets"].values())

    def test_a_missing_email_is_a_gap_and_never_a_placeholder(self):
        path = write(self.dir, "no-contact.txt",
                     RESUME_2024.replace("dana@example.org | ", ""))
        bank = self.bank(path)
        self.assertNotIn("email", bank["contact"])
        self.assertTrue(any(g["field"] == "contact.email" for g in bank["_gaps"]),
                        f"the hole was not recorded: {bank['_gaps']}")
        # The bank's CONTENT, without the meta keys: those are commentary about
        # the holes, and the point is that the holes were not filled with prose.
        content = {k: v for k, v in bank.items() if not k.startswith("_")}
        blob = json.dumps(content).lower()
        for invented in ("unknown", "n/a", "tbd", "example.com", "your.email",
                         "placeholder", "not found", "@"):
            self.assertNotIn(invented, blob,
                             f"the merge invented {invented!r} rather than leaving a hole")

    def test_an_undecidable_header_leaves_both_fields_absent(self):
        """'Meridian | Northstar' could be company-then-title or the reverse.
        Deciding it by coin flip puts a wrong employer on every resume built from
        this bank, so neither field is filled and both holes are named."""
        path = write(self.dir, "ambiguous.txt",
                     "Dana Reyes\ndana@example.org | (415) 555-0142\n\nSUMMARY\n"
                     "Product leader who ships infrastructure people rely on.\n\n"
                     "EXPERIENCE\n"
                     "Meridian | Northstar | 2015 - 2016\n"
                     "- Did work that mattered to the people who paid for it.\n"
                     "- Ran the weekly review that kept three teams pointed the same way.\n"
                     "- Rebuilt the reporting stack the company still runs today.\n\n"
                     "SKILLS\nPython, SQL\n")
        bank = self.bank(path)
        job = bank["jobs"][0]
        self.assertIsNone(job.get("company"))
        self.assertIsNone(job.get("title"))
        self.assertIn("Meridian | Northstar", job["_raw_header"])
        fields = {g["field"] for g in bank["_gaps"]}
        self.assertTrue(any(f.endswith(".company") for f in fields), fields)
        self.assertTrue(any(f.endswith(".title") for f in fields), fields)

    def test_a_decidable_header_is_still_decided(self):
        """The refusal above must be a judgement, not a blanket give-up: one
        piece naming a role is enough to tell the two apart."""
        job = self.bank(self.current)["jobs"][1]
        self.assertEqual(job["company"], "Northstar Labs")
        self.assertEqual(job["title"], "Senior Analyst")

    HEAD = ("Dana Reyes\ndana@example.org | (415) 555-0142\n\nEXPERIENCE\n"
            "Acme Corp | Principal Product Manager | 2020 - 2024\n"
            "- A real bullet about the work that was done here.\n"
            "- A second bullet, so the job is not thin on its own.\n")

    def _stray_report(self, name, body):
        gaps = self.bank(write(self.dir, name, body))["_gaps"]
        loose = [g for g in gaps if g["field"] == "experience.lines"]
        self.assertTrue(loose, f"lines were dropped silently: {gaps}")
        return loose[0]["why"]

    def test_strays_in_the_middle_are_reported_not_dropped(self):
        """A line that is neither a bullet nor a job header may be a bullet the
        extractor lost its marker for. Dropping it quietly loses real experience,
        so it is named instead."""
        why = self._stray_report("loose-mid.txt", self.HEAD
                                 + "unmarked alpha line that fits no rule here\n"
                                   "unmarked beta line that fits no rule here\n"
                                   "unmarked gamma line that fits no rule here\n"
                                   "unmarked delta line that fits no rule here\n")
        self.assertIn("alpha", why)

    def test_strays_at_the_end_of_the_file_are_reported(self):
        why = self._stray_report("loose-tail.txt",
                                 self.HEAD + "unmarked omega line at the very end\n")
        self.assertIn("omega", why)

    def test_strays_before_the_next_section_are_reported(self):
        why = self._stray_report("loose-boundary.txt",
                                 self.HEAD + "unmarked sigma line before education\n"
                                 "\nEDUCATION\nBrigham Young University, MBA, 2027\n")
        self.assertIn("sigma", why)

    def test_the_same_job_in_two_generations_merges_once(self):
        bank = self.bank(self.current, self.old)
        acme = [j for j in bank["jobs"] if j["id"].startswith("acme-corp")]
        self.assertEqual(len(acme), 1, "the same job was banked twice")
        self.assertEqual(len(acme[0]["_sources"]), 2)
        # the shared bullet is stored once, the drifted one separately
        texts = list(acme[0]["bullets"].values())
        self.assertEqual(len(texts), len(set(texts)))
        self.assertTrue(any("$20M" in t for t in texts))
        self.assertTrue(any("$18M" in t for t in texts))

    def test_sources_that_disagree_are_recorded_as_conflicts(self):
        conflicts = self.bank(self.current, self.old)["_conflicts"]
        fields = {c["field"] for c in conflicts}
        self.assertIn("contact.email", fields,
                      "two different emails were merged without saying so")
        self.assertTrue(any(f.endswith(".dates") for f in fields), fields)

    def test_a_low_confidence_source_is_named_in_the_bank(self):
        path = write(self.dir, "weak.txt",
                     "Dana Reyes\n" + ("prose about the work that was done. " * 20)
                     + "\nEXPERIENCE\nAcme Corp | Principal Product Manager | 2020 - 2024\n"
                     "- A real bullet about the work that was done here.\n")
        bank = self.bank(path)
        self.assertEqual(bank["_low_confidence_sources"], [path])
        self.assertTrue(any(g["field"] == "source.confidence" for g in bank["_gaps"]))

    def test_everything_unreadable_raises_rather_than_returning_an_empty_bank(self):
        """An empty bank is indistinguishable from a person with no career."""
        blank = intake.extract(write(self.dir, "blank.txt", "Dana"))
        with self.assertRaises(intake.IntakeError):
            intake.merge_to_bank([blank])

    def test_no_extractions_raises(self):
        with self.assertRaises(intake.IntakeError):
            intake.merge_to_bank([])

    def test_something_that_is_not_an_extraction_raises(self):
        with self.assertRaises(intake.IntakeError):
            intake.merge_to_bank([{"text": "Dana Reyes"}])

    def test_the_merge_is_deterministic(self):
        a = self.bank(self.current, self.old)
        b = self.bank(self.current, self.old)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# --------------------------------------------------------------------------- #
class ClaimsLedgerTracesEveryNumber(Tmp):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current = write(cls.dir, "dana-2024.txt", RESUME_2024)
        cls.old = write(cls.dir, "dana-2021.txt", RESUME_2021)
        cls.bank = intake.merge_to_bank(
            [intake.extract(cls.current), intake.extract(cls.old)])
        cls.ledger = intake.claims_ledger(cls.bank)

    def _values(self):
        return {(c["value"], os.path.basename(c["source_file"] or "")) for c in self.ledger}

    def test_every_claim_names_the_file_it_came_from(self):
        self.assertTrue(self.ledger)
        for c in self.ledger:
            self.assertTrue(c["traced"], c)
            self.assertIn(c["source_file"], (self.current, self.old), c)

    def test_a_number_traces_to_the_generation_that_wrote_it(self):
        pairs = self._values()
        self.assertIn(("$20M", "dana-2024.txt"), pairs)
        self.assertIn(("$18M", "dana-2021.txt"), pairs)
        self.assertNotIn(("$18M", "dana-2024.txt"), pairs)

    def test_percentages_and_counts_are_claims_too(self):
        values = {c["value"] for c in self.ledger}
        self.assertIn("40%", values)
        self.assertIn("3,000", values)

    def test_a_year_is_not_reported_as_a_headline_claim(self):
        for c in self.ledger:
            if c["value"] in ("2020", "2024", "2027"):
                self.assertEqual(c["kind"], "year", c)

    def test_drift_between_two_generations_is_surfaced(self):
        """The reason this function exists: merging five years of resumes merges
        five years of drifted numbers, and nothing else in the pipeline can see
        that the same sentence used to say a smaller number."""
        groups = intake.claim_conflicts(self.ledger)
        drift = [g for g in groups if any("$18M" in v for _s, v in g["values"])]
        self.assertEqual(len(drift), 1, f"drift not surfaced: {groups}")
        values = {v for _s, v in drift[0]["values"]}
        self.assertIn("$18M", values)
        self.assertIn("$20M", values)
        self.assertEqual(len(drift[0]["sources"]), 2)

    def test_agreement_between_generations_is_not_a_conflict(self):
        """The platform bullet is identical in both files. Reporting that as a
        conflict would bury the one that matters."""
        for g in intake.claim_conflicts(self.ledger):
            self.assertNotIn("platform", g["skeleton"], g)

    def test_the_conflict_group_does_not_pick_a_winner(self):
        """Which number is true is a question only the person can answer, and
        auto-picking the larger one is how a resume acquires a claim its owner
        cannot defend in an interview."""
        for g in intake.claim_conflicts(self.ledger):
            for key in ("winner", "chosen", "resolved", "correct"):
                self.assertNotIn(key, g)

    def test_a_bank_with_no_provenance_refuses(self):
        """A ledger whose every row says 'source: unknown' answers the wrong
        question convincingly."""
        handwritten = {"jobs": [{"id": "j1", "company": "Acme", "title": "PM",
                                 "bullets": {"b1": "Grew revenue to $20M."}}]}
        with self.assertRaises(intake.IntakeError):
            intake.claims_ledger(handwritten)

    def test_the_ledger_covers_summaries_as_well_as_bullets(self):
        path = write(self.dir, "summary-claim.txt",
                     "Dana Reyes\ndana@example.org\n\nSUMMARY\n"
                     "Product leader who has shipped to 3,000 customers.\n\n"
                     "EXPERIENCE\nAcme Corp | Principal Product Manager | 2020 - 2024\n"
                     "- A real bullet about the work that was done here.\n")
        bank = intake.merge_to_bank([intake.extract(path)])
        locations = {c["location"] for c in intake.claims_ledger(bank)}
        self.assertTrue(any(loc.startswith("summaries[") for loc in locations), locations)


# --------------------------------------------------------------------------- #
class ContactLineSurvivesTheMerge(Tmp):
    """The contact block is the one part of a bank that is on EVERY resume built
    from it. A wrong line here is not a degraded bullet, it is mail that never
    arrives -- so which source wins, and whether each field was captured at all,
    are asserted rather than assumed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current = write(cls.dir, "dana-2024.txt", RESUME_2024)
        cls.old = write(cls.dir, "dana-2021.txt", RESUME_2021)

    def bank(self, *paths):
        return intake.merge_to_bank([intake.extract(p) for p in paths])

    def _conflict(self, bank, field):
        hits = [c for c in bank["_conflicts"] if c["field"] == field]
        self.assertEqual(len(hits), 1, f"{field} conflict missing: {bank['_conflicts']}")
        return hits[0]

    def test_the_live_email_wins_and_the_dead_one_does_not(self):
        """Recording the disagreement is only half the job. The 2021 file carries
        an address that stopped working years ago; picking it silently puts a dead
        contact line on every resume built from this bank."""
        bank = self.bank(self.current, self.old)
        self.assertEqual(bank["contact"]["email"], "dana@example.org")
        self.assertNotEqual(bank["contact"]["email"], "dana.reyes@oldmail.example")
        conflict = self._conflict(bank, "contact.email")
        self.assertEqual(conflict["kept"], bank["contact"]["email"],
                         "the bank kept one value and reported keeping another")
        self.assertEqual({v["value"] for v in conflict["values"]},
                         {"dana@example.org", "dana.reyes@oldmail.example"})

    def test_the_highest_confidence_source_wins_not_the_first_one(self):
        """The documented rule is 'highest confidence wins, ties break on input
        order'. Both fixtures above grade 1.0, so the ranking itself is never
        exercised by them -- here the weak source is deliberately FIRST."""
        weak = write(self.dir, "weak-contact.txt", "Dana Reyes\nweak@example.org\n"
                     + ("prose about the work that was done here. " * 12))
        strong = write(self.dir, "strong-contact.txt",
                       RESUME_2024.replace("dana@example.org", "strong@example.org"))
        weak_rec, strong_rec = intake.extract(weak), intake.extract(strong)
        self.assertLess(weak_rec["confidence"], strong_rec["confidence"],
                        "fixture broken: the two sources must not grade the same")
        bank = intake.merge_to_bank([weak_rec, strong_rec])
        self.assertEqual(bank["contact"]["email"], "strong@example.org",
                         "input order beat confidence")
        self.assertEqual(self._conflict(bank, "contact.email")["kept"],
                         "strong@example.org")

    def test_the_name_is_captured(self):
        self.assertEqual(self.bank(self.current)["contact"].get("name"), "Dana Reyes")

    def test_the_phone_is_captured(self):
        self.assertEqual(self.bank(self.current)["contact"].get("phone"), "(415) 555-0142")

    def test_the_location_is_captured(self):
        self.assertEqual(self.bank(self.current)["contact"].get("location"), "Portland, OR")


# --------------------------------------------------------------------------- #
class TheBankKeepsWhatItRead(Tmp):
    """A gap that is reported is a feature; a section that silently evaporates is
    the bug. These assert the CONTENT arrived, not just that nothing complained."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current = write(cls.dir, "dana-2024.txt", RESUME_2024)

    def bank(self, *paths):
        return intake.merge_to_bank([intake.extract(p) for p in paths])

    def test_education_lines_are_kept_verbatim(self):
        bank = self.bank(self.current)
        self.assertEqual(bank.get("education_raw"), ["Brigham Young University, MBA, 2027"])
        self.assertTrue(any(g["field"] == "education" and "verbatim" in g["why"]
                            for g in bank["_gaps"]), bank["_gaps"])

    def test_skills_reach_the_pool(self):
        pool = self.bank(self.current)["skills_pool"].get("general", [])
        for skill in ("Python", "SQL", "Product strategy", "Stakeholder management"):
            self.assertIn(skill, pool)
        self.assertFalse(any(g["field"] == "skills_pool" for g in self.bank(self.current)["_gaps"]))

    def test_a_job_header_with_nothing_under_it_is_named_as_a_gap(self):
        """An empty job is the shape a mis-detected header leaves behind, and it
        is also what a resume whose bullets did not survive extraction looks
        like. Either way it has to say so."""
        path = write(self.dir, "no-bullets.txt",
                     "Dana Reyes\ndana@example.org | (415) 555-0142 | Portland, OR\n\n"
                     "SUMMARY\nProduct leader who ships infrastructure people rely on.\n\n"
                     "EXPERIENCE\nAcme Corp | Principal Product Manager | 2020 - 2024\n\n"
                     "EDUCATION\nBrigham Young University, MBA, 2027\n\n"
                     "SKILLS\nPython, SQL, Forecasting\n")
        bank = self.bank(path)
        self.assertEqual(bank["jobs"][0]["bullets"], {})
        holes = [g for g in bank["_gaps"] if g["field"].endswith("].bullets")]
        self.assertTrue(holes, f"an empty job was banked in silence: {bank['_gaps']}")
        self.assertIn("no bullet lines", holes[0]["why"])

    def test_a_bank_with_no_jobs_at_all_says_so(self):
        """The whole point of _gaps: a career-shaped hole has to be louder than an
        empty list, and it has to say what a job header looks like so the person
        can see why theirs was not one."""
        path = write(self.dir, "no-jobs.txt",
                     "Dana Reyes\ndana@example.org | (415) 555-0142 | Portland, OR\n\n"
                     "SUMMARY\nProduct leader who ships infrastructure people rely on "
                     "every single day of the week.\n\n"
                     "EDUCATION\nBrigham Young University, MBA, 2027\n\n"
                     "SKILLS\nPython, SQL, Forecasting, Stakeholder management\n")
        bank = self.bank(path)
        self.assertEqual(bank["jobs"], [])
        holes = [g for g in bank["_gaps"] if g["field"] == "jobs"]
        self.assertTrue(holes, f"a bank with no career said nothing: {bank['_gaps']}")
        self.assertIn("job header", holes[0]["why"])

    def test_prose_that_mentions_a_date_range_is_not_a_job(self):
        """The failure the length cap exists for: a sentence carrying '2019 to
        2021' becomes a header, the job under it gets no bullets, and a real line
        of experience is converted into an empty employer nobody worked for."""
        prose = ("Between 2019 to 2021 the entire organisation moved onto a new platform "
                 "and this sentence is deliberately long prose that merely happens to "
                 "mention a date range.")
        path = write(self.dir, "prose-dates.txt",
                     "Dana Reyes\ndana@example.org | (415) 555-0142 | Portland, OR\n\n"
                     "SUMMARY\nProduct leader who ships infrastructure people rely on.\n\n"
                     "EXPERIENCE\nAcme Corp | Principal Product Manager | 2020 - 2024\n"
                     "- Grew the services line and kept the lights on for everyone.\n"
                     "- Shipped the platform three departments still depend on daily.\n"
                     + prose + "\n\nSKILLS\nPython, SQL\n")
        bank = self.bank(path)
        self.assertEqual([j["id"] for j in bank["jobs"]],
                         ["acme-corp__principal-product-manager"],
                         "a prose sentence was banked as an employer")
        self.assertEqual(len(bank["jobs"][0]["bullets"]), 2)
        loose = [g for g in bank["_gaps"] if g["field"] == "experience.lines"]
        self.assertTrue(loose, "the line was neither used nor reported")
        self.assertIn("moved onto a new platform", loose[0]["why"])


# --------------------------------------------------------------------------- #
class GradingThresholdsAreNotDecoration(Tmp):
    def _txt(self, name, body):
        return write(self.dir, name, body)

    def test_one_section_header_is_not_enough_structure(self):
        """A resume has several sections. One header means the read found SUMMARY
        and lost EXPERIENCE, EDUCATION and SKILLS -- which is a gutted document,
        not a thin one, and the threshold is what says so."""
        body = "Dana Reyes\ndana@example.org\nSUMMARY\n" + ("some prose about the work here. " * 20)
        rec = intake.extract(self._txt("one-header.txt", body))
        self.assertEqual(rec["signals"]["header_count"], 1)
        self.assertFalse(rec["signals"]["has_section_headers"])
        self.assertEqual(rec["level"], intake.LOW)

    def test_a_plain_file_still_has_to_meet_a_length_expectation(self):
        """The yield expectation is not just a PDF concern. A .txt whose bytes are
        mostly padding extracts far short of its weight, and a zero expectation
        would hand that signal out free to every .txt and .md ever read."""
        rec = intake.extract(self._txt("padded.txt", RESUME_2024 + "\n" + (" " * 6000)))
        self.assertGreater(rec["signals"]["expected_chars"], rec["chars"],
                           "the expectation is free: every plain file passes it")
        self.assertFalse(rec["signals"]["length_meets_expectation"])
        self.assertEqual(rec["level"], intake.LOW)
        self.assertTrue(any("characters out of" in w for w in rec["warnings"]))

    def test_a_file_too_big_to_read_is_refused_and_says_why(self):
        """Skipping it quietly would leave a source contributing nothing with no
        record of why -- the same 'empty is not silence' rule the folder guard
        keeps, one level down."""
        path = os.path.join(self.dir, "huge.txt")
        with open(path, "wb") as fh:
            # Sparse: getsize() reports the full length without writing 12MB.
            fh.seek(intake.extract_text.MAX_BYTES + 1)
            fh.write(b"\0")
        self.assertGreater(os.path.getsize(path), intake.extract_text.MAX_BYTES)
        rec = intake.extract(path)
        self.assertEqual(rec["level"], intake.UNREADABLE)
        self.assertFalse(rec["usable"])
        self.assertIsNone(rec["engine"])
        self.assertEqual(rec["chars"], 0)
        self.assertIn("larger than", rec["error"])
        self.assertIn(rec["error"], rec["warnings"])


# --------------------------------------------------------------------------- #
class ConflictDetectionIsNarrowOnPurpose(Tmp):
    """Every false conflict buries a true one. These pin the two things that are
    deliberately NOT drift, and the one shape that was being missed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.head = ("Dana Reyes\n%s | (415) 555-0142\n\nSUMMARY\nProduct leader who ships "
                    "infrastructure people rely on every single day of the week.\n\n"
                    "EXPERIENCE\n")

    def bank(self, *paths):
        return intake.merge_to_bank([intake.extract(p) for p in paths])

    def conflicts(self, *paths):
        return intake.claim_conflicts(intake.claims_ledger(self.bank(*paths)))

    def test_a_year_that_moved_between_generations_is_not_drift(self):
        """'finished in 2019' vs 'finished in 2021' is a corrected date, not a
        restated number. Reporting it drowns the ledger in noise, since every
        generation of a resume re-dates something."""
        a = write(self.dir, "yr-2019.txt", self.head % "a@example.org"
                  + "Acme Corp | Principal Product Manager | 2020 - 2024\n"
                    "- Led the migration that finished in 2019 on schedule.\n\nSKILLS\nPython\n")
        b = write(self.dir, "yr-2021.txt", self.head % "b@example.org"
                  + "Acme Corp | Principal Product Manager | 2020 - 2024\n"
                    "- Led the migration that finished in 2021 on schedule.\n\nSKILLS\nPython\n")
        ledger = intake.claims_ledger(self.bank(a, b))
        self.assertEqual({c["kind"] for c in ledger}, {"year"}, "fixture broken")
        self.assertEqual(self.conflicts(a, b), [],
                         "a date range was reported as a disagreement about a number")

    def test_the_same_number_in_two_places_is_agreement_not_drift(self):
        """Two files, two separate bullets, identical numbers. The sentence
        appears twice; nothing about it disagrees."""
        a = write(self.dir, "same-a.txt", self.head % "a@example.org"
                  + "Acme Corp | Principal Product Manager | 2020 - 2024\n"
                    "- Grew the services line to $20M in annual recurring revenue.\n"
                    "\nSKILLS\nPython\n")
        b = write(self.dir, "same-b.txt", self.head % "b@example.org"
                  + "Northstar Labs | Senior Analyst | 2015 - 2018\n"
                    "- Grew the services line to $20M in annual recurring revenue.\n"
                    "\nSKILLS\nPython\n")
        ledger = intake.claims_ledger(self.bank(a, b))
        self.assertEqual(len({c["location"] for c in ledger}), 2, "fixture broken")
        self.assertEqual({c["value"] for c in ledger}, {"$20M"}, "fixture broken")
        self.assertEqual(self.conflicts(a, b), [],
                         "two files agreeing were reported as disagreeing")

    def test_drift_inside_one_file_is_surfaced_too(self):
        """'The long one with the stale numbers still pasted in' is an ordinary
        resume shape. Keying drift on the source FILE reported nothing here."""
        one = write(self.dir, "long-one.txt", self.head % "a@example.org"
                    + "Acme Corp | Principal Product Manager | 2020 - 2024\n"
                      "- Grew the services line from $2M to $20M in annual recurring revenue.\n"
                      "- Grew the services line from $2M to $18M in annual recurring revenue.\n"
                      "\nSKILLS\nPython\n")
        groups = self.conflicts(one)
        self.assertEqual(len(groups), 1, f"stale numbers in one file went unseen: {groups}")
        self.assertEqual({v for _s, v in groups[0]["values"]}, {"$2M", "$20M", "$18M"})
        self.assertEqual(len(groups[0]["locations"]), 2)

    def test_two_numbers_in_one_sentence_do_not_argue_with_each_other(self):
        """'from $2M to $20M' is one statement with two numbers in it. A group
        needs two distinct locations, or every growth bullet is a conflict."""
        one = write(self.dir, "single.txt", self.head % "a@example.org"
                    + "Acme Corp | Principal Product Manager | 2020 - 2024\n"
                      "- Grew the services line from $2M to $20M in annual recurring revenue.\n"
                      "- Shipped a platform used by 3,000 people every month.\n"
                      "\nSKILLS\nPython\n")
        self.assertEqual(self.conflicts(one), [])


class PublicApiIsWhatTheModuleDeclares(unittest.TestCase):
    def test_every_exported_name_exists(self):
        for name in intake.__all__:
            self.assertTrue(hasattr(intake, name), f"__all__ exports a missing {name}")

    def test_the_grading_constants_callers_read_are_exported(self):
        """Both are read outside this module to decide whether a read is worth
        grading and how far short of HIGH it fell. Leaving them out of __all__
        makes `from intake import *` a different module than the docs describe."""
        for name in ("MIN_USABLE_CHARS", "SIGNAL_WEIGHTS"):
            self.assertIn(name, intake.__all__)


class TheIntakeHasNoBulkVerb(unittest.TestCase):
    """Same absence the channel layer asserts, asserted at this end too: the
    intake reads files, it does not decide anything about applications."""

    FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                 "auto_approve", "approve_above", "batch_approve", "approve_batch")

    def test_no_approval_verb_lives_here(self):
        import inspect
        src = inspect.getsource(intake).lower()
        for bad in self.FORBIDDEN:
            self.assertNotIn(f"def {bad}", src)

    def test_the_module_does_not_reach_for_a_third_party_import(self):
        import inspect
        import re as _re
        imports = _re.findall(r"^\s*(?:from|import)\s+([\w.]+)",
                              inspect.getsource(intake), _re.M)
        allowed = {"os", "re", "__future__", ".engine", "json", "sys"}
        for name in imports:
            self.assertIn(name.split(".")[0] or name, {a.split(".")[0] or a for a in allowed},
                          f"intake grew a dependency on {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
