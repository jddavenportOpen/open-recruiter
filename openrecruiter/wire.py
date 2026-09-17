"""Wiring: turn the modules into a running system.

Every other module is deliberately free of its neighbours -- the loop takes
injected deps, the channels take a Card, the engine takes plain data. That makes
each one testable in isolation, and it means nothing runs until something puts
them together. This is that something, and it is the only module allowed to know
about all of them.

The one external dependency of the whole system lives here too: a `claude -p`
subprocess. It is used for exactly two things -- choosing which of the user's own
bullets to include, and running the three grading personas. It never invents a
fact; `approved_facts` and the claims ledger exist to catch it if it tries.

Reading the rate-limit event off that same subprocess is why the quota reader is
free: a healthy run emits its own usage before it does any work, so pacing costs
no extra call.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time

from . import apply as apply_mod
from . import boards, quota, store as store_mod
from .channels import Card, Decision, load_channels
from .engine import format_qa, panel, parse_check, render_resume

CLAUDE = os.environ.get("OPENRECRUITER_CLAUDE", "claude")
CALL_TIMEOUT = float(os.environ.get("OPENRECRUITER_CALL_TIMEOUT", "300"))


class ModelUnavailable(RuntimeError):
    """The model could not be reached, or answered with something unusable.

    Deliberately distinct from "the resume was bad": one is a fact about the
    candidate, the other is a fact about our plumbing, and a build that cannot
    run must never be recorded as a build that failed the bar.
    """


@dataclasses.dataclass
class ModelReply:
    text: str
    windows: object | None = None   # quota windows seen on this call, if any


def claude_call(prompt: str, *, timeout: float = CALL_TIMEOUT,
                runner=None) -> ModelReply:
    """One `claude -p` turn. Returns its text and any usage it reported.

    `--output-format stream-json` is not for the text -- it is so the
    rate_limit_event rides along. Asking the plan how full it is costs a whole
    extra run; reading what the run already told us costs nothing.
    """
    if runner is None:
        if not shutil.which(CLAUDE):
            raise ModelUnavailable(
                f"{CLAUDE!r} is not on PATH. OpenRecruiter runs on YOUR Claude "
                f"subscription through the official binary -- install it, sign in, "
                f"and re-run `openrecruiter doctor`.")
        runner = _subprocess_runner
    try:
        raw = runner(prompt, timeout)
    except subprocess.TimeoutExpired:
        raise ModelUnavailable(f"the model did not answer within {timeout:.0f}s") from None
    except OSError as e:
        raise ModelUnavailable(f"could not start {CLAUDE!r}: {e}") from None

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    text_parts, windows = [], None
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            text_parts.append(ln)          # not a stream: treat as plain output
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "rate_limit_event":
            try:
                windows = quota.parse_rate_limit_event(ev)
            except quota.QuotaUnknown:
                windows = None             # unreadable usage is not a build failure
        elif ev.get("type") == "result":
            if isinstance(ev.get("result"), str):
                text_parts.append(ev["result"])
        elif ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

    text = "\n".join(p for p in text_parts if p).strip()
    if not text:
        # An empty answer and a refused one look identical downstream, and one of
        # them would be recorded as "this resume did not clear the bar".
        raise ModelUnavailable("the model returned nothing usable")
    return ModelReply(text=text, windows=windows)


def _subprocess_runner(prompt: str, timeout: float) -> str:
    proc = subprocess.run(
        [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose"],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL)          # never inherit stdin: it hangs headless
    if proc.returncode != 0 and not proc.stdout.strip():
        raise ModelUnavailable(
            f"{CLAUDE} exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def _json_from(text: str) -> object:
    """Pull one JSON object out of a model reply, tolerating prose around it.

    A parse failure raises rather than returning a default. The grader's whole
    value is that a score it could not read becomes a refusal instead of a
    guessed number.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.strip("`")
        t = t.split("\n", 1)[1] if t.lower().startswith("json") else t
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        raise ModelUnavailable(f"no JSON object in the reply: {text[:160]!r}")
    try:
        return json.loads(t[start:end + 1])
    except ValueError as e:
        raise ModelUnavailable(f"unparseable JSON in the reply: {e}") from None


# --------------------------------------------------------------------------- #
# build: bank + posting -> a tailored, gated, graded resume                    #
# --------------------------------------------------------------------------- #
TAILOR_PROMPT = """You are selecting from a candidate's OWN experience bank for one posting.

RULES, and they are absolute:
- Use ONLY bullets that appear verbatim in the bank below. Do not rewrite them,
  do not merge them, do not adjust a number, do not add a word.
- You are SELECTING and ORDERING, not writing. If nothing fits, select fewer.
- Never invent an employer, a date, a title, a metric, or a skill.

Return ONLY a JSON object:
{{"summary_key": "<one key from summaries>",
  "jobs": [{{"id": "<job id from the bank>", "bullet_ids": ["<bullet id>", ...]}}],
  "skills": ["<skill from skills_pool>", ...],
  "claims": ["<each headline number this selection asserts>", ...]}}

POSTING
{posting}

BANK
{bank}
"""

GRADE_PROMPT = """You are {persona_desc}

Grade this resume against the posting. Be honest; a generous score helps nobody.

Return ONLY a JSON object:
{{"dimensions": {{{dims}}},
  "would_interview": true|false,
  "reason": "<one sentence: the single biggest thing holding it back>"}}

Each dimension is an integer 0-100. `reason` is required even when you would
interview.

POSTING
{posting}

RESUME
{resume}
"""

PERSONAS = {
    "recruiter": "a recruiter giving this six seconds before moving on.",
    "hiring_manager": "the hiring manager who owns this role and this budget.",
    "ai_systems_rep": "an ATS parser plus a technical screener checking every claim "
                      "is concrete, dated and checkable.",
}


def build_resume(app: store_mod.Application, bank: dict, posting_text: str,
                 out_dir: str, *, call=claude_call) -> dict:
    """Tailor, typeset, gate, and grade ONE application.

    Returns the result mapping `loop.Deps.build` promises. Raises
    ModelUnavailable rather than returning a failure: "we could not build it" and
    "it did not clear the bar" are different facts, and the store has different
    states for them.
    """
    sel = _json_from(call(TAILOR_PROMPT.format(
        posting=posting_text[:6000], bank=json.dumps(bank)[:40000])).text)
    resume = _assemble(bank, sel)

    os.makedirs(out_dir, exist_ok=True)
    pdf = os.path.join(out_dir, f"{app.id}.pdf")
    pages = _decide_pages(bank)
    layout = render_resume.render(resume, pdf, pages=pages) \
        if _accepts_pages() else render_resume.render(resume, pdf)

    qa = format_qa.analyze(layout, pages)
    if not qa["passed"]:
        codes = sorted({i["code"] for i in qa["issues"]})
        return {"passed": False, "score": 0.0, "raw_score": 0.0,
                "weakest": "layout", "weakest_reason": f"page gate: {', '.join(codes)}",
                "claims": list(sel.get("claims") or []), "resume_path": pdf}

    rt = parse_check.check(pdf, resume)
    if not (rt.get("passed") if isinstance(rt, dict) else bool(rt)):
        detail = rt.get("detail", "the rendered PDF did not read back") if isinstance(rt, dict) else ""
        return {"passed": False, "score": 0.0, "raw_score": 0.0,
                "weakest": "machine_readability", "weakest_reason": detail,
                "claims": list(sel.get("claims") or []), "resume_path": pdf}

    resume_text = _plain(resume)
    dims = ", ".join(f'"{d}": <0-100>' for d in panel.DIMENSION_WEIGHTS)
    personas = {}
    for name, desc in PERSONAS.items():
        got = _json_from(call(GRADE_PROMPT.format(
            persona_desc=desc, dims=dims,
            posting=posting_text[:6000], resume=resume_text[:12000])).text)
        personas[name] = got

    # company is NOT passed here: the loop reads it from the store row, because a
    # payload that names its own employer is a build choosing its own pass mark.
    return {"passed": True, "panel": {"personas": personas},
            "claims": list(sel.get("claims") or []), "resume_path": pdf}


def _accepts_pages() -> bool:
    import inspect
    return "pages" in inspect.signature(render_resume.render).parameters


def _decide_pages(bank: dict) -> int:
    """One page unless the history genuinely needs two.

    A reasoned default, not a measured one -- the evidence people cite for
    two pages measures recruiter preference in a simulated panel, not callbacks.
    Labelled as judgement wherever it is surfaced.
    """
    jobs = bank.get("jobs") or []
    bullets = sum(len(j.get("bullets") or {}) for j in jobs)
    return 2 if (len(jobs) >= 6 or bullets >= 28) else 1


def _assemble(bank: dict, sel: dict) -> dict:
    """Build the resume mapping from the bank using ONLY the selected ids.

    A selection naming something the bank does not contain is dropped, not
    fabricated: the model's job was to choose, and anything it returns that is
    not in the bank is by definition something it made up.
    """
    by_id = {j.get("id"): j for j in (bank.get("jobs") or [])}
    jobs = []
    for want in sel.get("jobs") or []:
        src = by_id.get(want.get("id"))
        if not src:
            continue
        pool = src.get("bullets") or {}
        chosen = [pool[b] for b in (want.get("bullet_ids") or []) if b in pool]
        if not chosen:
            continue
        jobs.append({"company": src.get("company", ""), "title": src.get("title", ""),
                     "location": src.get("location", ""), "dates": src.get("dates", ""),
                     "bullets": chosen})
    summaries = bank.get("summaries") or {}
    contact = bank.get("contact") or {}
    pool = bank.get("skills_pool") or []
    allowed = set(pool if isinstance(pool, list) else [])
    return {
        "name": bank.get("name", ""),
        "contact": " | ".join(x for x in [contact.get("email"), contact.get("phone"),
                                          contact.get("location")] if x),
        "summary": summaries.get(sel.get("summary_key")) or next(iter(summaries.values()), ""),
        "jobs": jobs,
        "education": bank.get("education") or [],
        "skills": [s for s in (sel.get("skills") or []) if not allowed or s in allowed] or pool,
    }


def _plain(resume: dict) -> str:
    out = [resume.get("name", ""), resume.get("contact", ""), "",
           resume.get("summary", ""), ""]
    for j in resume.get("jobs") or []:
        out.append(f"{j.get('title','')} — {j.get('company','')} ({j.get('dates','')})")
        out += [f"  - {b}" for b in j.get("bullets") or []]
    skills = resume.get("skills") or []
    if skills:
        out += ["", "Skills: " + ", ".join(skills)]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# ask: one card, one decision, on whichever rails are configured               #
# --------------------------------------------------------------------------- #
def make_ask(channels, *, timeout_s: float = 86400.0):
    """One application, one message, one answer.

    Sends on every configured rail so a user with both gets the card wherever
    they are, and takes the FIRST decision that arrives. There is no path here
    that decides more than one application, and there is deliberately no
    parameter that would let a caller pass a list.
    """
    def ask(app: store_mod.Application):
        card = Card(app_id=app.id, company=app.company, role=app.role,
                    tier=app.tier, panel_avg=app.score or 0.0,
                    raw_panel_avg=app.raw_score or 0.0,
                    weakest_persona=app.weakest or "",
                    weakest_reason=app.weakest_reason or "",
                    claims=list(app.claims or []),
                    resume_path=app.resume_path, url=app.url)
        card_ids = []
        for ch in channels:
            try:
                card_ids.append((ch, ch.send_card(card)))
            except Exception:
                continue                   # one dead rail must not lose the other
        if not card_ids:
            raise RuntimeError("no messaging rail accepted the card")
        ch, cid = card_ids[0]
        return ch.await_decision(cid, timeout_s=timeout_s)
    return ask


# --------------------------------------------------------------------------- #
# submit                                                                       #
# --------------------------------------------------------------------------- #
def make_submit(store: store_mod.Store, bank: dict, *, allow_external: bool | None = None):
    """Apply, then try hard to disprove that it worked.

    Real employers are OFF unless OPENRECRUITER_ALLOW_REAL_SUBMIT=1 is set by a
    human who has read what this does. The default target is the local mock ATS,
    so the whole path can be exercised without touching anyone's careers page.
    """
    if allow_external is None:
        allow_external = os.environ.get("OPENRECRUITER_ALLOW_REAL_SUBMIT") == "1"

    def submit(app: store_mod.Application) -> dict:
        contact = bank.get("contact") or {}
        packet = apply_mod.ApplyPacket(
            fields={"full_name": bank.get("name", ""),
                    "email": contact.get("email", ""),
                    "phone": contact.get("phone", "")},
            applicant_name=bank.get("name", ""),
            applicant_email=contact.get("email", ""))
        res = apply_mod.submit(store, app.id, packet, allow_external=allow_external)
        return {"sent": getattr(res, "sent", None),
                "verified": bool(getattr(res, "verified", False)),
                "note": getattr(res, "note", "") or "",
                "retryable": bool(getattr(res, "retryable", False))}
    return submit


# --------------------------------------------------------------------------- #
# quota                                                                        #
# --------------------------------------------------------------------------- #
def make_quota_reader(state: dict, probe=None):
    """Report the usage the last real call already told us.

    Probing the plan with its own extra run would spend the thing we are
    measuring, so the normal path is free: a healthy `claude -p` reports its own
    utilization before it does any work, and `instrumented_call` keeps the last
    reading here.

    COLD START is the interesting case, and it was a deadlock the unit tests
    could not see. The loop reads quota BEFORE it builds, so on the very first
    cycle nothing has reported anything -- and a reader that simply raises
    QuotaUnknown means the runner is blind forever and never starts. Refusing to
    proceed is right when we cannot see; refusing to ever look is not.

    So on a cold start we take ONE cheap reading from the model itself. It costs
    a single small turn, once per run, and it is the only honest way to learn a
    number that belongs to the plan rather than to us. If that probe cannot
    produce a reading either, THEN we are genuinely blind and say so.
    """
    def read():
        w = state.get("windows")
        if w is not None:
            return w
        if probe is None:
            raise quota.QuotaUnknown(
                "no usage reported yet on this run, and no probe was configured")
        try:
            reply = probe("Reply with the single word: ok")
        except Exception as e:
            raise quota.QuotaUnknown(
                f"cold-start usage probe failed: {type(e).__name__}: {e}") from None
        if reply is not None and getattr(reply, "windows", None) is not None:
            state["windows"] = reply.windows
            return reply.windows
        raise quota.QuotaUnknown(
            "the model answered but reported no usage, so the plan's headroom is "
            "unknown -- backing off rather than guessing it is fine")
    return read


def instrumented_call(state: dict, call=claude_call):
    """Wrap the model call so every reply updates what we know about usage."""
    def inner(prompt, **kw):
        reply = call(prompt, **kw)
        if reply.windows is not None:
            state["windows"] = reply.windows
        return reply
    return inner


# --------------------------------------------------------------------------- #
# the assembled system                                                         #
# --------------------------------------------------------------------------- #
def home() -> str:
    h = os.environ.get("OPENRECRUITER_HOME") or os.path.expanduser("~/.openrecruiter")
    os.makedirs(h, exist_ok=True)
    return h


def load_bank() -> dict:
    p = os.path.join(home(), "bank.json")
    if not os.path.exists(p):
        raise FileNotFoundError(
            "no experience bank yet — run `openrecruiter intake <folder>` first")
    with open(p) as fh:
        return json.load(fh)


def save_bank(bank: dict) -> str:
    p = os.path.join(home(), "bank.json")
    with open(p, "w") as fh:
        json.dump(bank, fh, indent=1)
    return p


def build_deps(store: store_mod.Store, bank: dict, *, channels=None,
               call=claude_call, killswitch=None) -> "object":
    """Assemble the real Deps the loop runs on."""
    from .loop import Deps, KillSwitch
    state: dict = {}
    wrapped = instrumented_call(state, call)
    chans = channels if channels is not None else load_channels()
    if not chans:
        raise RuntimeError(
            "no messaging rail configured — you would have no way to approve "
            "anything. Run `openrecruiter doctor`.")
    resumes = os.path.join(home(), "resumes")

    def build(app):
        posting = (app.meta or {}).get("description") or f"{app.role} at {app.company}"
        return build_resume(app, bank, posting, resumes, call=wrapped)

    return Deps(build=build,
                ask=make_ask(chans),
                submit=make_submit(store, bank),
                quota_reader=make_quota_reader(state, probe=wrapped),
                killswitch=killswitch or KillSwitch(os.path.join(home(), "STOP")))


def scan(store: store_mod.Store, pipeline: dict, *, client=None) -> dict:
    """Pull the configured boards and record anything genuinely new.

    `allow_partial=True` on purpose: one dead board must not throw away the
    postings already collected from healthy ones. The failures come back in the
    result and are surfaced, never folded into "nothing new today" -- a board
    that is down and a market with no jobs in it are different facts.
    """
    client = client or boards.BoardClient(cache_dir=boards.default_cache_dir())
    sources = [boards.Source(ats=r["ats"], token=r["token"], company=r.get("name"))
               for r in (pipeline.get("boards") or [])
               if r.get("ats") and r.get("token")]
    if not sources:
        return {"found": 0, "added": 0, "failures": [],
                "note": "no boards configured — run `openrecruiter setup`"}

    res = client.sweep(sources, allow_partial=True)
    added = 0
    for p in res.postings:
        url = p.get("url")
        if not url:
            continue
        if store.upsert_discovered(
                boards.stable_id(url), p.get("company") or "", p.get("role") or "",
                url, ats=p.get("ats"),
                meta={"description": p.get("description") or ""}):
            added += 1
    return {"found": len(res.postings), "added": added,
            "failures": [str(f) for f in (res.failures or [])]}

# --------------------------------------------------------------------------- #
# proposal -> a pipeline `scan` can actually run                               #
# --------------------------------------------------------------------------- #
def _slug(company: str) -> str:
    keep = [c.lower() for c in company if c.isalnum() or c == " "]
    return "".join(keep).replace(" ", "")


def boards_from_proposal(proposal: dict) -> list:
    """Turn proposed COMPANY NAMES into board rows `scan` can fetch.

    The interview proposes employers; the board APIs want a token. For
    Greenhouse and Ashby that token is usually the company slug, and usually is
    not always: it is a GUESS, and it is labelled as one on every row rather
    than quietly presented as configuration. A wrong token fails loudly at scan
    time as a named failure, never as an empty market.

    This is the seam the user is meant to edit. It is deliberately a flat, dull
    JSON list for exactly that reason.
    """
    out = []
    for name in proposal.get("companies") or []:
        out.append({"name": name, "ats": "greenhouse", "token": _slug(name),
                    "guessed": True})
    return out
