"""The seam between what `intake` emits and what `build_resume` consumes.

This file exists because every other test in the repo was green while that seam
was broken. `test_end_to_end` fed the builder a hand-written bank shaped to the
BUILDER's expectations; real intake emits a different shape (the name under
`contact.name`, education as `education_raw`, `skills_pool` as a dict). The
result was a tailored resume with NO NAME AND NO EDUCATION, produced by code
that passed 585 tests.

So the rule here: **these tests may not contain a bank literal.** Every bank is
produced by running the real intake path over real files on disk. A fixture
cannot catch a fixture-shaped bug.
"""
import os
import tempfile
import unittest

from openrecruiter import intake, wire


RESUME_TEXT = """Dana Reyes
dana@example.org | (415) 555-0142 | Portland, OR

SUMMARY
Product leader who ships data platforms.

EXPERIENCE
Acme - Principal Product Manager - 2020 to 2024
- Grew a fixed-price services line from two million to twenty million.
- Shipped an internal platform used by three thousand people monthly.
- Scaled the data organisation from five engineers to thirty-five.

Globex - Senior Product Manager - 2017 to 2020
- Launched a billing system handling four hundred thousand invoices a year.

EDUCATION
Brigham Young University - MBA - 2027

SKILLS
Python, SQL, Product strategy
"""


def real_bank(text=RESUME_TEXT, name="dana.txt"):
    """A bank built by the REAL intake path. Never a literal."""
    folder = tempfile.mkdtemp()
    with open(os.path.join(folder, name), "w") as fh:
        fh.write(text)
    got = [intake.extract(p) for p in intake.discover_resumes(folder)]
    assert got, "intake found nothing to read -- the fixture is wrong, not the code"
    return intake.merge_to_bank(got)


def select_everything(bank):
    """The selection a model would return for this bank, built from the bank's
    own ids so the test cannot drift from intake's id scheme."""
    jobs = []
    for j in bank.get("jobs") or []:
        bullets = j.get("bullets") or {}
        ids = list(bullets) if isinstance(bullets, dict) else list(range(len(bullets)))
        jobs.append({"id": j.get("id"), "bullet_ids": ids})
    return {"summary_key": next(iter(bank.get("summaries") or {}), None),
            "jobs": jobs,
            "skills": ["Python"],
            "claims": ["$20M annualized revenue"]}


class TheResumeCarriesWhatTheBankKnows(unittest.TestCase):
    def setUp(self):
        self.bank = real_bank()
        self.resume = wire._assemble(self.bank, select_everything(self.bank))

    def test_the_resume_has_a_name(self):
        """The bug that started this file: intake stores the name under
        `contact.name`, the builder read a top-level `name`, and the resume came
        out anonymous."""
        self.assertTrue(self.resume["name"].strip(),
                        "the resume has NO NAME -- the intake/builder seam is broken")
        self.assertIn("Dana", self.resume["name"])

    def test_the_resume_has_education(self):
        """intake emits `education_raw`; the builder read `education`."""
        self.assertTrue(self.resume["education"],
                        "education vanished between intake and the builder")

    def test_the_resume_has_the_contact_line(self):
        for token in ("dana@example.org", "555-0142", "Portland"):
            self.assertIn(token, self.resume["contact"])

    def test_the_resume_has_skills(self):
        """`skills_pool` arrives as a dict from intake and a list by hand."""
        self.assertTrue(self.resume["skills"],
                        "skills vanished between intake and the builder")

    def test_every_job_in_the_bank_can_be_selected(self):
        self.assertEqual(len(self.resume["jobs"]), len(self.bank["jobs"]),
                         "a job the bank holds could not be selected by its own id")
        for job in self.resume["jobs"]:
            self.assertTrue(job["company"], "a job lost its employer")
            self.assertTrue(job["bullets"], "a job lost every bullet")

    def test_bullets_are_verbatim_from_the_bank(self):
        """Nothing may be rewritten on the way through."""
        pool = set()
        for j in self.bank["jobs"]:
            b = j.get("bullets") or {}
            pool |= set(b.values() if isinstance(b, dict) else b)
        for job in self.resume["jobs"]:
            for line in job["bullets"]:
                self.assertIn(line, pool, f"bullet was not verbatim from the bank: {line!r}")


class NothingIsInvented(unittest.TestCase):
    def test_a_selection_naming_an_unknown_job_is_dropped_not_faked(self):
        bank = real_bank()
        sel = select_everything(bank)
        sel["jobs"].append({"id": "a-job-that-does-not-exist", "bullet_ids": ["x"]})
        resume = wire._assemble(bank, sel)
        self.assertEqual(len(resume["jobs"]), len(bank["jobs"]),
                         "a job the bank never held appeared in the resume")

    def test_a_selection_naming_an_unknown_bullet_is_dropped(self):
        bank = real_bank()
        sel = select_everything(bank)
        sel["jobs"][0]["bullet_ids"] = ["not-a-real-bullet-id"]
        resume = wire._assemble(bank, sel)
        companies = [j["company"] for j in resume["jobs"]]
        self.assertNotIn(bank["jobs"][0].get("company"), companies,
                         "a job with no real bullets should be dropped, not invented into")

    def test_an_unknown_skill_is_not_carried_through(self):
        bank = real_bank()
        sel = select_everything(bank)
        sel["skills"] = ["Rust"]          # never appears in the source resume
        resume = wire._assemble(bank, sel)
        self.assertNotIn("Rust", resume["skills"],
                         "a skill the bank never held reached the resume")


class TheRenderedResumeSurvivesTheGates(unittest.TestCase):
    """Beyond shape: a bank from real intake must actually typeset and read back."""

    def test_a_real_bank_renders_and_reads_back_with_its_name_and_email(self):
        from openrecruiter.engine import pdftext, render_resume
        bank = real_bank()
        resume = wire._assemble(bank, select_everything(bank))
        out = os.path.join(tempfile.mkdtemp(), "r.pdf")
        render_resume.render(resume, out)
        text, _engine = pdftext.extract(out)
        for token in ("Dana", "dana@example.org", "Acme", "Brigham"):
            self.assertIn(token, text,
                          f"{token!r} did not survive into the rendered PDF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
