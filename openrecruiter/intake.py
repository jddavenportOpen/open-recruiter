"""Turn the resumes someone already has into one experience bank.

Nobody hand-writes a hundred-bullet JSON file. Almost everyone has four or five
old resumes in a folder: the long one, the one tuned for the job they did not
get, the version from two roles ago that still has the good numbers on it. That
pile is the real input, and this module reads it, judges how well it read it,
and assembles what it found.

Three things it will not do, each for a reason that has already bitten:

* **It never silently accepts a bad read.** A ghostscript-distilled resume once
  extracted at 4% recall -- only the name survived, the email and every section
  header gone -- while the reader reported a clean byte count. A byte count is
  not a shortfall. `extract()` therefore reports a confidence built from what is
  *missing from the text*, and a file with no email in it can never come back
  high-confidence no matter how many characters it produced.

* **It never invents a field.** `merge_to_bank()` is deterministic assembly with
  no model in the loop. Where a field cannot be derived honestly -- a header line
  that could equally be "company, title" or "title, company" -- the field is left
  ABSENT and the reason is recorded in `bank["_gaps"]`. A plausible guess in an
  experience bank becomes a false claim on every resume built from it.

* **It never returns an empty result that reads like "nothing found".** An
  unreadable folder raises; a merge whose every source was unreadable raises; a
  claims ledger over a bank with no provenance raises. Silence and emptiness are
  different answers and this module keeps them different.

`claims_ledger()` exists because merging N generations of someone's old resumes
means inheriting N generations of drifted numbers. Every headline number is
tagged with the file it came from, and `claim_conflicts()` groups the ones that
say the same sentence with different numbers.
"""
from __future__ import annotations

import os
import re

from .engine import extract_text, pdftext

__all__ = ["IntakeError", "discover_resumes", "extract", "merge_to_bank",
           "claims_ledger", "claim_conflicts",
           "HIGH", "LOW", "UNREADABLE", "SUPPORTED_EXTS",
           # Both are read by callers deciding whether a read is worth grading and
           # how far short of HIGH it fell, so they are API, not internals.
           "MIN_USABLE_CHARS", "SIGNAL_WEIGHTS"]


class IntakeError(RuntimeError):
    """A refusal. Raised instead of returning something that looks like a result."""


SUPPORTED_EXTS = (".pdf", ".docx", ".txt", ".md", ".markdown")

HIGH = "high"
LOW = "low"
UNREADABLE = "unreadable"

# Below this, there is no point grading the read -- there is nothing to grade.
MIN_USABLE_CHARS = 200

# A read is HIGH only when EVERY signal passes. The weights do not vote a failing
# signal away -- they only say how bad a LOW read is, so several sources can be
# ranked against each other. Anything else produces the failure this module
# exists to catch: three signals out of four is a document with its contact line
# or its entire structure missing, and calling that "high confidence" is how a
# gutted resume gets built on.
SIGNAL_WEIGHTS = {
    "has_email": 0.35,
    "has_section_headers": 0.25,
    "length_meets_expectation": 0.25,
    "alpha_ratio_ok": 0.15,
}
MIN_ALPHA_RATIO = 0.55
MIN_SECTION_HEADERS = 2

# Chars we expect per byte of source, and a cap so a font-heavy or image-heavy
# file cannot inflate the expectation into a false alarm.
_YIELD = {".pdf": (0.004, 2500), ".docx": (0.02, 2500)}
_YIELD_DEFAULT = (0.45, 4000)

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.\w{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,2}[\s.\-]*)?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}")
LOCATION_RE = re.compile(r"^[A-Z][A-Za-z.\- ]{1,28},\s*(?:[A-Z]{2}|[A-Z][a-z]+)$")

_SECTIONS = {
    "summary": ("summary", "profile", "objective", "about me", "professional summary"),
    "experience": ("experience", "employment", "work history", "employment history",
                   "professional experience", "work experience", "career history"),
    "education": ("education", "academics", "academic background"),
    "skills": ("skills", "technical skills", "core competencies", "competencies",
               "tools", "technologies"),
    "projects": ("projects", "selected projects"),
    "other": ("certifications", "awards", "publications", "volunteer",
              "interests", "references", "leadership"),
}

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_TERM = rf"(?:(?:{_MONTH}\s*|\d{{1,2}}[/\-])?(?:19|20)\d{{2}}|present|current|now)"
DATE_RANGE_RE = re.compile(rf"{_TERM}\s*(?:to|through|[-–—])\s*{_TERM}", re.I)

# Real resumes are typeset by real word processors, and each one picks its own
# bullet glyph. This class was missing U+25CF BLACK CIRCLE, which is what Google
# Docs and Word emit by default -- so a resume whose every bullet begins with it
# parsed as SEVEN JOB HEADERS AND ZERO BULLETS, and said so honestly while being
# useless. A glyph we do not know is not a line we should drop.
_BULLET_RE = re.compile(
    "^\\s*["
    "\u2022\u25cf\u25cb\u25aa\u25ab\u25e6\u2023\u2043\u2219\u00b7"   # bullet, circles, squares, hyphen-bullet
    "\u25b6\u25ba\u2756\u273f\u2794\u27a2\u00bb"                       # arrows/ornaments some templates use
    "*\u2013\u2014\\-"                                                    # asterisk, en/em dash, hyphen
    "]\\s+")
# Two fields on one line are separated by a pipe, a dash with space around it, or
# a tab. NOT by a comma: "Portland, OR" would split into nonsense.
_PIECE_SPLIT_RE = re.compile(r"\s*(?:\||\t|\s[–—-]\s)\s*")

# Used only to decide WHICH of two pieces is the title. When it cannot decide,
# the answer is "I do not know", never a coin flip -- see _split_header.
_TITLE_WORDS = {
    "engineer", "developer", "manager", "director", "analyst", "architect",
    "designer", "scientist", "consultant", "associate", "intern", "officer",
    "lead", "principal", "president", "vp", "chief", "founder", "specialist",
    "coordinator", "administrator", "supervisor", "head", "partner", "strategist",
    "recruiter", "researcher", "accountant", "controller", "advisor", "pm",
    "producer", "marketer", "writer", "editor", "technician", "operator",
}

# A number that is the point of the sentence, not a date.
_CLAIM_RE = re.compile(r"""
      (?P<money>\$\s?\d[\d,]*(?:\.\d+)?\s*(?:[KkMmBb]\b|million|billion|thousand)?)
    | (?P<pct>\d+(?:\.\d+)?\s*%)
    | (?P<mult>\d+(?:\.\d+)?\s*[xX]\b)
    | (?P<plain>\b\d[\d,]*(?:\.\d+)?(?:\s*(?:[KkMmBb]\b|million|billion|thousand))?)
""", re.X)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def discover_resumes(folder: str, recursive: bool = True) -> list[str]:
    """Candidate resume files under `folder`, sorted, deterministic.

    Raises rather than returning [] when the folder cannot be read, because
    "there is nothing here" and "I could not look" are different answers and a
    caller that cannot tell them apart will happily build a bank from nothing.
    """
    path = os.path.expanduser(folder)
    if not os.path.exists(path):
        raise IntakeError(f"no such folder: {folder}")
    if not os.path.isdir(path):
        raise IntakeError(f"{folder} is a file, not a folder; pass it to extract() instead")
    if not os.access(path, os.R_OK | os.X_OK):
        raise IntakeError(f"cannot read {folder} (permissions)")

    found: list[str] = []
    try:
        if recursive:
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                found += [os.path.join(root, n) for n in names if _is_candidate(n)]
        else:
            found = [os.path.join(path, n) for n in os.listdir(path)
                     if _is_candidate(n) and os.path.isfile(os.path.join(path, n))]
    except OSError as e:
        raise IntakeError(f"cannot read {folder}: {e}") from e
    return sorted(found)


def _is_candidate(name: str) -> bool:
    return (not name.startswith(".")
            and os.path.splitext(name)[1].lower() in SUPPORTED_EXTS)


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def extract(path: str) -> dict:
    """Read one resume and grade how well it was read.

    Returns {path, ext, engine, text, chars, confidence, level, warnings,
             signals, usable}. `confidence` is a float in [0, 1]; `level` is
             HIGH / LOW / UNREADABLE.

    The grade is computed from what is MISSING from the extracted text, not from
    how many bytes came back. That distinction is the whole point: the failure
    this guards against produced a healthy byte count and a gutted document.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise IntakeError(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise IntakeError(f"unsupported type {ext or '(none)'}: {path}")

    size = os.path.getsize(path)
    rec = {"path": path, "ext": ext, "engine": None, "text": "", "chars": 0,
           "bytes": size, "confidence": 0.0, "level": UNREADABLE,
           "warnings": [], "signals": {}, "usable": False, "error": None}

    if size > extract_text.MAX_BYTES:
        rec["error"] = f"file is larger than {extract_text.MAX_BYTES // (1024*1024)}MB, skipped"
        rec["warnings"].append(rec["error"])
        return rec

    try:
        if ext == ".pdf":
            # Straight to pdftext rather than through extract_text.from_pdf, which
            # discards which engine answered. Knowing whether a real text engine or
            # the stdlib fallback produced the text is part of the provenance.
            text, engine = pdftext.extract(path)
        elif ext == ".docx":
            text, engine = extract_text.from_docx(path), "docx-ooxml"
        else:
            text, engine = extract_text.from_plain(path), "plaintext"
    except Exception as e:                      # noqa: BLE001 - reported, never swallowed
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["warnings"].append(f"could not read the file at all: {rec['error']}")
        return rec

    rec["engine"] = engine
    rec["text"] = _normalize(text)
    rec["chars"] = len(rec["text"])

    if rec["chars"] < MIN_USABLE_CHARS:
        rec["error"] = (f"only {rec['chars']} characters came out of a {size}-byte file; "
                        "if this is a scanned image, export a text PDF or paste the "
                        "text into a .txt")
        rec["warnings"].append(rec["error"])
        return rec                              # level stays UNREADABLE, confidence 0.0

    signals, warnings = _grade(rec["text"], ext, size)
    rec["signals"] = signals
    rec["warnings"] = warnings
    rec["confidence"] = round(
        sum(w for k, w in SIGNAL_WEIGHTS.items() if signals[k]), 3)
    rec["level"] = HIGH if all(signals[k] for k in SIGNAL_WEIGHTS) else LOW
    rec["usable"] = True
    return rec


# A bullet glyph appearing MID-LINE is a line break the PDF lost. Two-column and
# tightly-kerned resumes routinely extract as one long line carrying a job header
# and then every bullet under it, separated only by the glyph. Without this, those
# bullets are invisible: the line does not START with a bullet, so it is not a
# bullet, and it is too long to be a header -- it lands in `unused` and the job
# comes out with nothing under it. Measured on four real resumes: 9 bullets
# recovered before this, 1 of 7 jobs populated.
_INLINE_BULLET_RE = re.compile(
    "\\s+["
    "\u2022\u25cf\u25cb\u25aa\u25ab\u25e6\u2023\u2043\u2219"
    "\u25b6\u25ba\u2756\u273f\u2794\u27a2"
    "]\\s+")


def _explode_inline_bullets(lines: list) -> list:
    """Split any line that carries bullet glyphs inside it.

    Only fires when the glyph is unambiguous -- a hyphen or asterisk is NOT in
    the inline set, because "cost-benefit" and "5 * 3" are ordinary text. A
    leading fragment before the first glyph is kept as its own line so a job
    header glued to its first bullet still reaches the header detector.
    """
    out = []
    for ln in lines:
        if not _INLINE_BULLET_RE.search(ln):
            out.append(ln)
            continue
        parts = _INLINE_BULLET_RE.split(ln)
        head = parts[0].strip()
        if head:
            out.append(head)
        for piece in parts[1:]:
            piece = piece.strip(" |")
            if piece:
                out.append("\u2022 " + piece)
    return out


def _normalize(text: str) -> str:
    """Same whitespace treatment extract_text applies, so a .pdf and a .txt of the
    same resume are graded on the same shape of string. Newlines are preserved:
    line structure is what section detection reads."""
    return re.sub(r"[ \t]+", " ", text or "").strip()


def _grade(text: str, ext: str, size: int) -> tuple[dict, list[str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headers = [ln for ln in lines if _section_of(ln)]
    letters = sum(c.isalpha() for c in text)
    dense = sum(not c.isspace() for c in text) or 1
    alpha_ratio = letters / dense
    ratio, cap = _YIELD.get(ext, _YIELD_DEFAULT)
    expected = int(min(cap, size * ratio))

    signals = {
        "has_email": bool(EMAIL_RE.search(text)),
        "has_section_headers": len(headers) >= MIN_SECTION_HEADERS,
        "length_meets_expectation": len(text) >= expected,
        "alpha_ratio_ok": alpha_ratio >= MIN_ALPHA_RATIO,
        "header_count": len(headers),
        "alpha_ratio": round(alpha_ratio, 3),
        "expected_chars": expected,
    }

    warnings = []
    if not signals["has_email"]:
        warnings.append(
            "no email-looking token in the extracted text: the contact line "
            "probably did not survive extraction. This is what a distilled PDF "
            "read at 4% recall looks like -- the name comes back and everything "
            "else is gone.")
    if not signals["has_section_headers"]:
        warnings.append(
            f"only {len(headers)} section header(s) found (SUMMARY / EXPERIENCE / "
            "EDUCATION / SKILLS). A resume has several; a read that finds none "
            "has lost the document's structure.")
    if not signals["length_meets_expectation"]:
        warnings.append(
            f"{len(text)} characters out of a {size}-byte file, where at least "
            f"{expected} were expected. The shortfall is measured against the "
            "file, not against zero -- a healthy byte count is not a healthy read.")
    if not signals["alpha_ratio_ok"]:
        warnings.append(
            f"only {alpha_ratio:.0%} of the extracted characters are letters; this "
            "usually means glyph codes came back instead of text.")
    return signals, warnings


def _section_of(line: str) -> str | None:
    """The canonical section a header line names, or None if it is not a header."""
    s = line.strip().strip("=_-*#").strip().rstrip(":").strip()
    if not s or len(s) > 40 or len(s.split()) > 4 or EMAIL_RE.search(s):
        return None
    low = s.lower()
    for canon, words in _SECTIONS.items():
        if any(low == w or low.startswith(w + " ") or low.endswith(" " + w) for w in words):
            return canon
    return None


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def merge_to_bank(extractions: list[dict]) -> dict:
    """Assemble one experience bank from several extractions. No model, no guesses.

    The output matches engine/validate_bank.py's schema, plus three keys that
    exist so the bank can be argued with:

      _gaps        every field that could not be derived, and why
      _conflicts   sources that disagree (two emails, two date ranges)
      _provenance  id -> the source files that contributed it

    Run `validate_bank.validate()` on the result. It is expected to report errors
    on a bank with real gaps -- that is the bank telling you what to fill in, and
    filling those holes with plausible text here is precisely the failure this
    module exists to avoid.
    """
    if not isinstance(extractions, list) or not extractions:
        raise IntakeError("merge_to_bank got no extractions; there is nothing to merge")
    for i, e in enumerate(extractions):
        if not isinstance(e, dict) or "path" not in e or "level" not in e:
            raise IntakeError(f"extraction {i} is not an extract() result")

    usable = [e for e in extractions if e.get("level") in (HIGH, LOW) and e.get("text")]
    if not usable:
        raise IntakeError(
            "every source was unreadable, so there is no bank to build. "
            + "; ".join(f"{os.path.basename(e['path'])}: {e.get('error') or 'no text'}"
                        for e in extractions))

    gaps: list[dict] = []
    conflicts: list[dict] = []
    provenance: dict[str, list[str]] = {}
    for e in extractions:
        if not any(e is u for u in usable):
            gaps.append({"field": "source", "why":
                         f"{e['path']} contributed nothing: {e.get('error') or 'no text'}"})

    # Highest-confidence source wins a contact field; ties break on input order,
    # so the same inputs always produce the same bank.
    ordered = sorted(enumerate(usable), key=lambda t: (-t[1].get("confidence", 0.0), t[0]))
    parsed = [(e, _parse_source(e["text"])) for _i, e in ordered]

    bank: dict = {"contact": {}, "summaries": {}, "jobs": [], "skills_pool": {}}
    _merge_contact(parsed, bank, gaps, conflicts)
    _merge_jobs(parsed, bank, gaps, conflicts, provenance)
    _merge_summaries(parsed, bank, gaps, provenance)
    _merge_skills(parsed, bank, gaps, provenance)
    _merge_education(parsed, bank, gaps)

    for e, doc in parsed:
        if doc["unused"]:
            gaps.append({"field": "experience.lines", "why":
                         f"{len(doc['unused'])} line(s) in {os.path.basename(e['path'])} were "
                         "neither a bullet nor a job header and were not used: "
                         + " | ".join(doc["unused"][:3])})

    low = [e["path"] for e in usable if e["level"] == LOW]
    for p in low:
        gaps.append({"field": "source.confidence", "why":
                     f"{os.path.basename(p)} read at low confidence; everything it "
                     "contributed should be read against the original before it ships"})

    bank["_gaps"] = gaps
    bank["_conflicts"] = conflicts
    bank["_provenance"] = provenance
    bank["_sources"] = [{"path": e["path"], "level": e["level"],
                         "confidence": e.get("confidence", 0.0), "chars": e.get("chars", 0)}
                        for e in usable]
    bank["_low_confidence_sources"] = low
    return bank


def _merge_contact(parsed, bank, gaps, conflicts):
    seen: dict[str, list[tuple[str, str]]] = {"email": [], "phone": [], "location": [], "name": []}
    for e, doc in parsed:
        for field, value in doc["contact"].items():
            if value and value not in [v for v, _ in seen[field]]:
                seen[field].append((value, e["path"]))
    for field in ("name", "email", "phone", "location"):
        values = seen[field]
        if not values:
            gaps.append({"field": f"contact.{field}", "why":
                         "not found in any source; fill it in by hand"})
            continue
        bank["contact"][field] = values[0][0]
        if len({v for v, _ in values}) > 1:
            conflicts.append({"field": f"contact.{field}", "kept": values[0][0],
                              "values": [{"value": v, "source_file": p} for v, p in values]})


def _merge_jobs(parsed, bank, gaps, conflicts, provenance):
    jobs: dict[str, dict] = {}
    for e, doc in parsed:
        for raw in doc["jobs"]:
            key = _job_key(raw)
            job = jobs.get(key)
            if job is None:
                job = {"id": key, "bullets": {}, "_bullet_seen": {}, "_sources": [],
                       "_raw_header": raw["raw_header"], "_dates_seen": []}
                for field in ("company", "title", "location", "dates"):
                    if raw.get(field):
                        job[field] = raw[field]
                jobs[key] = job
            else:
                # A later source may carry a field an earlier one could not resolve.
                for field in ("company", "title", "location", "dates"):
                    if raw.get(field) and not job.get(field):
                        job[field] = raw[field]
            if e["path"] not in job["_sources"]:
                job["_sources"].append(e["path"])
            if raw.get("dates"):
                job["_dates_seen"].append((raw["dates"], e["path"]))
            for text in raw["bullets"]:
                fingerprint = _fingerprint(text)
                bid = job["_bullet_seen"].get(fingerprint)
                if bid is None:
                    bid = f"{key}-b{len(job['bullets']) + 1}"
                    job["_bullet_seen"][fingerprint] = bid
                    job["bullets"][bid] = text
                provenance.setdefault(bid, [])
                if e["path"] not in provenance[bid]:
                    provenance[bid].append(e["path"])

    for key, job in jobs.items():
        job.pop("_bullet_seen")
        dates = job.pop("_dates_seen")
        distinct = {d for d, _ in dates}
        if len(distinct) > 1:
            conflicts.append({"field": f"jobs[{key}].dates", "kept": job.get("dates"),
                              "values": [{"value": d, "source_file": p} for d, p in dates]})
        for field in ("company", "title", "dates"):
            if not job.get(field):
                gaps.append({"field": f"jobs[{key}].{field}", "why":
                             f"could not be read out of {job['_raw_header']!r} without "
                             "guessing; fill it in by hand"})
        if not job["bullets"]:
            gaps.append({"field": f"jobs[{key}].bullets", "why":
                         "no bullet lines were found under this job header"})
        provenance[key] = list(job["_sources"])
        bank["jobs"].append(job)

    if not bank["jobs"]:
        gaps.append({"field": "jobs", "why":
                     "no job headers were recognised in any source. A job header is a "
                     "line carrying a date range, e.g. 'Acme | Principal PM | 2020 - 2024'"})


def _merge_summaries(parsed, bank, gaps, provenance):
    seen: set[str] = set()
    for e, doc in parsed:
        text = doc["summary"]
        if not text or _fingerprint(text) in seen:
            continue
        seen.add(_fingerprint(text))
        sid = f"from-{_slug(os.path.splitext(os.path.basename(e['path']))[0]) or 'source'}"
        n, base = 2, sid
        while sid in bank["summaries"]:
            sid, n = f"{base}-{n}", n + 1
        bank["summaries"][sid] = text
        provenance[sid] = [e["path"]]
    if not bank["summaries"]:
        gaps.append({"field": "summaries", "why":
                     "no summary/profile section found; the tailoring step will have "
                     "nothing to pick from"})


def _merge_skills(parsed, bank, gaps, provenance):
    out: list[str] = []
    for e, doc in parsed:
        for skill in doc["skills"]:
            if skill.lower() not in {s.lower() for s in out}:
                out.append(skill)
                provenance.setdefault(f"skill:{skill.lower()}", []).append(e["path"])
    if out:
        bank["skills_pool"] = {"general": out}
    else:
        gaps.append({"field": "skills_pool", "why": "no skills section found"})


def _merge_education(parsed, bank, gaps):
    """Education is kept VERBATIM. Splitting 'BYU MBA 2027' into school/degree/dates
    is guesswork on every layout that is not the one we tested, so the raw lines are
    preserved and the decomposition is left to the human."""
    raw: list[str] = []
    for _e, doc in parsed:
        for line in doc["education"]:
            if line not in raw:
                raw.append(line)
    if raw:
        bank["education_raw"] = raw
        gaps.append({"field": "education", "why":
                     f"{len(raw)} education line(s) were kept verbatim in "
                     "bank['education_raw'] but not decomposed into school/degree/dates"})
    else:
        gaps.append({"field": "education", "why": "no education section found"})


# --------------------------------------------------------------------------- #
# per-source parsing
# --------------------------------------------------------------------------- #
def _parse_source(text: str) -> dict:
    doc = {"contact": {}, "summary": None, "skills": [], "education": [],
           "jobs": [], "unused": []}
    lines = _explode_inline_bullets([ln.strip() for ln in text.splitlines()])
    section = None
    summary_lines: list[str] = []
    candidates: list[str] = []      # non-bullet lines that may head the next job

    head = [ln for ln in lines[:15] if ln]
    doc["contact"] = _parse_contact(text, head)

    job = None
    for ln in lines:
        if not ln:
            continue
        canon = _section_of(ln)
        if canon:
            section = canon
            job = None
            doc["unused"] += candidates
            candidates = []
            continue

        if section == "summary":
            summary_lines.append(_BULLET_RE.sub("", ln))
            continue
        if section == "skills":
            doc["skills"] += _split_skills(ln)
            continue
        if section == "education":
            doc["education"].append(ln)
            continue
        if section != "experience":
            continue

        if _BULLET_RE.match(ln):
            if job is not None:
                job["bullets"].append(_BULLET_RE.sub("", ln).strip())
            else:
                doc["unused"].append(ln)
            continue

        if _is_job_header(ln):
            job, used_candidates = _build_job(ln, candidates)
            if not used_candidates:
                doc["unused"] += candidates
            doc["jobs"].append(job)
            candidates = []
            continue

        candidates.append(ln)
        # Only the two lines immediately above a header can plausibly belong to it;
        # anything older is a line we did not understand, and it is reported rather
        # than dropped.
        if len(candidates) > 2:
            doc["unused"].append(candidates.pop(0))

    doc["unused"] += candidates
    if summary_lines:
        doc["summary"] = " ".join(summary_lines).strip()
    return doc


def _parse_contact(text: str, head: list[str]) -> dict:
    out: dict[str, str] = {}
    m = EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0)
    m = PHONE_RE.search("\n".join(head) or text)
    if m:
        out["phone"] = m.group(0).strip()
    for ln in head:
        if _looks_like_name(ln):
            out["name"] = ln
            break
    for ln in head:
        for piece in _PIECE_SPLIT_RE.split(ln):
            piece = piece.strip().strip(",")
            if LOCATION_RE.match(piece) or piece.lower() in ("remote", "remote (us)"):
                out["location"] = piece
                break
        if "location" in out:
            break
    return out


def _looks_like_name(line: str) -> bool:
    if not (2 <= len(line.split()) <= 4) or len(line) > 40:
        return False
    if EMAIL_RE.search(line) or any(c.isdigit() for c in line) or _section_of(line):
        return False
    return all(w[:1].isupper() and re.fullmatch(r"[A-Za-z.'\-À-ɏ]+", w)
               for w in line.split())


def _is_job_header(line: str) -> bool:
    # Length-capped on purpose: a prose sentence that happens to mention
    # "2019 to 2021" is not a job header, and treating it as one silently turns a
    # bullet into an empty job.
    return bool(DATE_RANGE_RE.search(line)) and len(line) <= 120 and len(line.split()) <= 16


def _build_job(header: str, candidates: list[str]) -> tuple[dict, bool]:
    """Returns (job, whether the lines above the header were used for it)."""
    job = {"raw_header": header, "bullets": []}
    used_candidates = False
    m = DATE_RANGE_RE.search(header)
    if m:
        job["dates"] = m.group(0).strip()
    remainder = DATE_RANGE_RE.sub("", header).strip(" |–—-\t,")
    pieces = [p.strip().strip(",") for p in _PIECE_SPLIT_RE.split(remainder) if p.strip()]
    if not pieces:
        pieces = [p.strip().strip(",") for c in candidates
                  for p in _PIECE_SPLIT_RE.split(c) if p.strip()]
        if pieces:
            used_candidates = True
            job["raw_header"] = f"{' | '.join(candidates)} | {header}"
    kept = []
    for p in pieces:
        if "location" not in job and (LOCATION_RE.match(p) or p.lower().startswith("remote")):
            job["location"] = p
        else:
            kept.append(p)
    job.update(_split_header(kept))
    return job, used_candidates


def _split_header(pieces: list[str]) -> dict:
    """Decide which piece is the company and which is the title -- or refuse.

    "Acme Corp | Principal Product Manager" is decidable: one piece names a role.
    "Northstar | Meridian" is not, and a coin flip here becomes a wrong employer
    on every resume built from this bank. So when both pieces look like a role, or
    neither does, this returns nothing and the caller records the gap.
    """
    if not pieces:
        return {}
    titles = [p for p in pieces if _is_title(p)]
    if len(pieces) == 1:
        return {"title": pieces[0]} if titles else {"company": pieces[0]}
    if len(titles) == 1:
        title = titles[0]
        company = next(p for p in pieces if p is not title)
        return {"company": company, "title": title}
    return {}


def _is_title(piece: str) -> bool:
    return any(w.strip(",.&/()") in _TITLE_WORDS for w in piece.lower().split())


def _split_skills(line: str) -> list[str]:
    line = _BULLET_RE.sub("", line)
    parts = [p.strip(" .;") for p in re.split(r"[,;|•·]| - ", line)]
    return [p for p in parts if 1 < len(p) <= 40]


def _fingerprint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")


def _job_key(raw: dict) -> str:
    company, title = raw.get("company"), raw.get("title")
    if company or title:
        return f"{_slug(company) or 'company'}__{_slug(title) or 'role'}"
    return "hdr-" + (_slug(raw["raw_header"])[:48] or "job")


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #
def claims_ledger(bank: dict) -> list[dict]:
    """Every headline number in the bank, tagged with the file it came from.

    Merging five years of resumes means merging five years of drifted numbers:
    the same achievement written as $18M in one file and $20M in the next. This
    is the list that makes that visible; `claim_conflicts()` groups it.

    Raises if the bank carries no provenance, because a ledger whose every entry
    says "source: unknown" answers the wrong question convincingly.
    """
    if not isinstance(bank, dict):
        raise IntakeError("claims_ledger needs a bank dict")
    provenance = bank.get("_provenance")
    if not provenance:
        raise IntakeError(
            "this bank carries no _provenance, so no claim in it can be traced to a "
            "file. Build the bank with merge_to_bank() rather than by hand, or the "
            "ledger cannot tell you where a number came from.")

    out: list[dict] = []
    for job in bank.get("jobs") or []:
        label = job.get("id") or "?"
        for bid, text in (job.get("bullets") or {}).items():
            out += _claims_in(text, f"jobs[{label}].bullets[{bid}]", bid, provenance)
    for sid, text in (bank.get("summaries") or {}).items():
        out += _claims_in(text, f"summaries[{sid}]", sid, provenance)
    return out


def _claims_in(text: str, location: str, field_id: str, provenance: dict) -> list[dict]:
    if not isinstance(text, str):
        return []
    sources = provenance.get(field_id) or []
    claims = []
    for m in _CLAIM_RE.finditer(text):
        kind = next(k for k in ("money", "pct", "mult", "plain") if m.group(k))
        value = m.group(0).strip()
        if kind == "plain" and re.fullmatch(r"(?:19|20)\d{2}", value):
            kind = "year"
        claims.append({
            "value": value,
            "kind": kind,
            "text": text,
            "location": location,
            "field_id": field_id,
            "source_file": sources[0] if sources else None,
            "source_files": list(sources),
            # Two versions of one sentence share a skeleton; their numbers are the
            # only thing that differs, which is exactly how drift is spotted.
            "skeleton": _skeleton(text),
            "traced": bool(sources),
        })
    return claims


def _skeleton(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\d.,%$]+|\b[kmb]\b", "#", (text or "").lower())).strip()


def claim_conflicts(claims: list[dict]) -> list[dict]:
    """Groups of claims that say the same sentence with different numbers.

    Drift is keyed on WHERE the sentence sits, not on which file it came from.
    Two generations of a resume are the obvious case, but "the long one with the
    stale numbers still pasted in" is just as ordinary a shape: one file carrying
    both "$2M to $20M" and "$2M to $18M" is the same drift, and keying on the
    source file reported nothing at all for it.

    A group is drift only when two locations carry DIFFERENT numbers. That one
    test covers both ways a group can be innocent: the same numbers restated in
    several places is agreement, and a single location -- two numbers inside one
    bullet ("grew it from $2M to $20M"), or a bullet stored once because it was
    identical in several files -- has nothing to disagree with by construction.

    Each group names every value and the file it came from. Nothing is resolved
    here: which number is true is a question only the person can answer, and
    picking the biggest one automatically is how a resume acquires a claim its
    owner cannot defend in an interview.
    """
    by_skeleton: dict[str, list[dict]] = {}
    for c in claims:
        if c["kind"] == "year":
            continue                      # a date range is when, not what was claimed
        by_skeleton.setdefault(c["skeleton"], []).append(c)

    out = []
    for skeleton, group in by_skeleton.items():
        per_location: dict[str, list[str]] = {}
        for c in group:
            per_location.setdefault(c["location"], []).append(c["value"])
        # One test, not two: a lone location yields exactly one value tuple, so
        # "said once" and "said the same twice" are the same answer here. A
        # separate len(per_location) check would be a line no mutation can kill.
        if len({tuple(v) for v in per_location.values()}) < 2:
            continue                      # agreement, or a single statement
        out.append({
            "skeleton": skeleton,
            "texts": sorted({c["text"] for c in group}),
            "values": sorted({(c["source_file"] or "(untraced)", c["value"]) for c in group}),
            "sources": sorted({c["source_file"] or "(untraced)" for c in group}),
            "locations": sorted(per_location),
        })
    return sorted(out, key=lambda g: g["skeleton"])
