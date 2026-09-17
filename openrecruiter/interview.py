"""The intake interview, the proposed pipeline, and plain-language refinement.

This module answers the question the ancestor system could not: *whose* job hunt
is this? There, the search lived in compiled regexes in a source file -- one
person's target titles, one person's comp floor, one person's idea of senior.
Installing it meant inheriting his search. That is a feed, not a recruiter.

Three pieces, in the order a user meets them:

  QUESTIONS / build_goals   ask, then turn the answers into a goals dict
  propose_pipeline          propose where to look, with a reason per entry
  refine_goals              change the goals by typing a sentence

Two rules shape all of it.

**The agent proposes; the user authors.** `recruit-copilot`, which this forks,
refuses to configure the search at all, on the grounds that authorship must stay
with the user. That is right about the danger and wrong about the remedy: a blank
`goals.json` is not authorship, it is a blank page, and the observed outcome of a
blank page is a user who edits nothing and runs the example. So we propose, every
proposal carries a short reason naming the goal that produced it, and nothing is
a pipeline until `confirm_pipeline`. An entry the user adds by hand survives
every later re-proposal -- if the agent could quietly drop it, the agent would be
the author again.

**Nothing here is guessed.** `refine_goals` reports a clause it cannot read
rather than picking the nearest rule, and withholds the whole instruction when
any part of it is unreadable. Same reason `parse_reply` refuses "yes but change
the summary": a half-understood instruction applied silently is worse than a
re-ask, because the user believes it landed.

Deterministic and offline by construction -- no LLM call, no clock, no network,
no disk. The same answers always build the same goals, which is what makes any of
this testable.

Note on scope: a *pipeline* here is where to look for jobs. It is never a
decision to send anything. Approval is per application, in the channel layer,
and this module has no approval surface at all -- a test asserts that.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from typing import Any, Callable

VERSION = 1

# Remote posture. Stored as a token rather than a bool because "I will take a
# hybrid role" is a real third answer, and folding it into either bool loses it.
REMOTE_ONLY = "remote_only"
HYBRID_OK = "hybrid_ok"
ONSITE_OK = "onsite_ok"
UNSTATED = "unstated"

DEFAULT_MIN_MATCH = 55

# The ladder used to turn "senior or above" into an explicit avoid-list. Order is
# load-bearing: everything strictly below the lowest level you named is something
# you have told us not to send.
SENIORITY_LADDER = ("intern", "new grad", "junior", "associate", "mid",
                    "senior", "lead", "staff", "principal", "director",
                    "vp", "head", "chief")
_SENIORITY_ALIASES = {
    "sr": "senior", "snr": "senior", "jr": "junior", "entry": "junior",
    "entry level": "junior", "early career": "junior", "graduate": "new grad",
    "newgrad": "new grad", "mid level": "mid", "midlevel": "mid", "ic": "senior",
    "vice president": "vp", "head of": "head", "c level": "chief", "cxo": "chief",
}
# Stripped from a title to derive its base form ("senior product manager" ->
# "product manager"), which becomes the `medium` match band.
_TITLE_PREFIXES = ("senior", "sr.", "sr", "staff", "principal", "lead", "junior",
                   "jr.", "jr", "associate", "head of", "director of", "vp of",
                   "chief")

COMPANY_SIZES = ("seed", "early", "growth", "late", "public", "any")
_SIZE_WORDS = {
    "seed": "seed", "pre seed": "seed", "preseed": "seed", "garage": "seed",
    "startup": "early", "startups": "early", "early": "early", "early stage": "early",
    "series a": "early", "series b": "growth", "series c": "growth",
    "scaleup": "growth", "scale up": "growth", "growth": "growth", "midsize": "growth",
    "mid size": "growth", "series d": "late", "late stage": "late", "pre ipo": "late",
    "public": "public", "big tech": "public", "faang": "public", "enterprise": "public",
    "large": "public", "fortune 500": "public",
    "any": "any", "no preference": "any", "doesn't matter": "any",
    "does not matter": "any", "dont care": "any", "don't care": "any", "whatever": "any",
}

# Domain aliases -> catalog slug. A domain with no slug is still a real goal (it
# shapes keywords and reasons); it just has no seeded companies, and the proposal
# says so out loud rather than returning a short list that looks complete.
_DOMAIN_SLUGS = {
    "ai infra": "ai-infra", "ai infrastructure": "ai-infra", "ml infra": "ai-infra",
    "ml infrastructure": "ai-infra", "llm infra": "ai-infra", "inference": "ai-infra",
    "ai": "ai", "artificial intelligence": "ai", "machine learning": "ai", "ml": "ai",
    "llm": "ai", "llms": "ai", "genai": "ai",
    "fintech": "fintech", "payments": "fintech", "banking": "fintech", "finance": "fintech",
    "healthtech": "health", "health": "health", "healthcare": "health", "biotech": "health",
    "devtools": "devtools", "developer tools": "devtools", "dev tools": "devtools",
    "infrastructure": "devtools", "platform": "devtools",
    "security": "security", "infosec": "security", "cybersecurity": "security",
    "cyber": "security", "appsec": "security",
    "data": "data", "analytics": "data", "data infra": "data", "data platform": "data",
    "climate": "climate", "climate tech": "climate", "energy": "climate",
    "cleantech": "climate",
    "saas": "saas", "b2b saas": "saas", "b2b": "saas", "productivity": "saas",
    "crypto": "crypto", "web3": "crypto", "blockchain": "crypto", "defi": "crypto",
}

# A STARTING POINT, not a search. Every entry is proposed with a reason naming
# the domain the user asked for, and the user deletes what they do not want. The
# failure this repo exists to avoid is a catalog that runs *instead* of the
# user's judgement; a catalog they edit before anything happens is the opposite.
SEED_COMPANIES: dict[str, tuple[str, ...]] = {
    "ai-infra": ("Anthropic", "OpenAI", "Databricks", "Modal", "Together AI",
                 "Weights & Biases"),
    "ai": ("Anthropic", "OpenAI", "Google DeepMind", "Scale AI", "Hugging Face", "Cohere"),
    "fintech": ("Stripe", "Plaid", "Block", "Brex", "Ramp", "Chime"),
    "health": ("Oscar Health", "Ro", "Komodo Health", "Tempus", "Zocdoc", "Hims & Hers"),
    "devtools": ("GitHub", "GitLab", "Vercel", "Sentry", "JetBrains", "Postman"),
    "security": ("CrowdStrike", "Okta", "Cloudflare", "Snyk", "Wiz", "1Password"),
    "data": ("Snowflake", "dbt Labs", "Fivetran", "Confluent", "Databricks", "Airbyte"),
    "climate": ("Watershed", "Arcadia", "Form Energy", "Crusoe", "Redwood Materials", "Tesla"),
    "saas": ("Atlassian", "HubSpot", "Notion", "Figma", "Asana", "Airtable"),
    "crypto": ("Coinbase", "Kraken", "Circle", "Chainalysis", "Anchorage Digital",
               "Uniswap Labs"),
}


class InterviewError(Exception):
    """Base for every refusal in this module."""


class IncompleteIntake(InterviewError):
    """A required answer is missing or unreadable. Never silently defaulted:
    a goals dict with no titles scores every job identically, which looks like a
    working search and is not one."""


class UnknownAnswer(InterviewError):
    """An answer key no question asked for. Raised rather than dropped -- a
    typo'd key that silently discards the comp floor is exactly the bug class
    this project exists to not repeat."""


class UnknownEdit(InterviewError):
    """An edit naming something that does not exist. Also raised rather than
    ignored: a removal that quietly does nothing leaves the entry in the
    pipeline while the user believes it is gone."""


class ProposalError(InterviewError):
    """A proposal is malformed -- almost always an entry with no reason."""


# --------------------------------------------------------------------------
# the interview
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Question:
    """One interview question, and how its answer becomes structure.

    `parse` is attached to the question rather than living in `build_goals` so
    that a surface which asks the questions one at a time can validate an answer
    the moment it arrives, using the same code that will later build the goals.
    """
    key: str
    prompt: str
    why: str          # shown with the prompt: nobody answers well in the dark
    example: str
    kind: str         # "list" | "text" | "money" | "choice" -- a UI hint only
    required: bool
    parse: Callable[[str], Any]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _split_list(text: str) -> list[str]:
    parts = re.split(r"[,;\n]| and | or |/", (text or ""), flags=re.I)
    out, seen = [], set()
    for p in parts:
        p = _norm(p).strip(".")
        if not p:
            continue
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
    return out


_NONE_WORDS = {"none", "no", "n/a", "na", "nope", "nothing", "no floor",
               "any", "doesn't matter", "does not matter", "dont care",
               "don't care", "unsure", "not sure", "skip"}

_MONEY = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m)?\b", re.I)


def parse_money(text: str) -> int | None:
    """Read a compensation figure. None when there is no number at all.

    A bare number under 1000 is read as thousands, because "180" in answer to
    "what is your floor" is never one hundred and eighty dollars. Stated as a
    rule here so it is one decision in one place, rather than each caller
    guessing differently -- the drift that made the ancestor print one salary and
    filter on another.
    """
    t = _norm(text).lower()
    if not t:
        return None
    if t in _NONE_WORDS:
        return 0
    m = _MONEY.search(t)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    suf = (m.group(2) or "").lower()
    if suf == "k":
        val *= 1_000
    elif suf == "m":
        val *= 1_000_000
    elif val < 1000:
        val *= 1_000
    return int(val)


def _canon_level(word: str) -> str | None:
    w = _norm(word).lower()
    w = _SENIORITY_ALIASES.get(w, w)
    return w if w in SENIORITY_LADDER else None


def parse_titles(text: str) -> list[str]:
    """Target titles, in the user's own words and their own order. Order is
    authorship, so it is preserved; duplicates are folded case-insensitively.

    Fragments of one character are dropped: the list separators include "/" so
    that "pm / product manager" works, and the cost of that is that "n/a" comes
    apart into two letters. A one-character job title does not exist, so throwing
    them away is safe -- and it lets an answer of "n/a" reach the refusal below
    instead of becoming two titles nobody wants.
    """
    return [t for t in _split_list(text)
            if len(t) > 1 and t.lower() not in _NONE_WORDS]


def parse_seniority(text: str) -> dict:
    """Levels you want, and -- derived -- the levels below them.

    The avoid-list is computed, not asked. Someone who says "senior or above" has
    already told us not to send them junior postings; making them list every
    level they do not want is a worse interview.
    """
    t = " " + _norm(text).lower() + " "
    found = []
    for level in SENIORITY_LADDER:
        if re.search(rf"(?<![a-z]){re.escape(level)}(?![a-z])", t):
            found.append(level)
    for alias, level in _SENIORITY_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t) and level not in found:
            found.append(level)
    if not found:
        return {"prefer": [], "avoid": []}
    prefer = sorted(set(found), key=SENIORITY_LADDER.index)
    floor = SENIORITY_LADDER.index(prefer[0])
    return {"prefer": prefer, "avoid": list(SENIORITY_LADDER[:floor])}


def parse_domains(text: str) -> dict:
    """Industries, split into wanted and refused.

    An entry the user negates in place ("ai, not crypto") lands in `avoid`,
    because making them answer the dealbreaker question again for something they
    already said is how an interview gets abandoned halfway.
    """
    prefer, avoid = [], []
    for item in _split_list(text):
        low = item.lower()
        if low in _NONE_WORDS:
            continue
        m = re.match(r"^(?:no|not|never|avoid|anything but|except)\s+(.+)$", low)
        if m:
            avoid.append(_norm(m.group(1)))
        else:
            prefer.append(item)
    return {"prefer": prefer, "avoid": avoid, "deprioritize": []}


def parse_locations(text: str) -> list[str]:
    return [x for x in _split_list(text) if x.lower() not in _NONE_WORDS]


_RELO_NO = re.compile(r"\b(no|not|won'?t|cannot|can'?t|never)\s+(relocat\w*|relo|moving|move)\b"
                      r"|\bnot relocating\b|\bno relo\b", re.I)
_RELO_YES = re.compile(r"\b(open to|willing to|happy to|will|would|can)\s+relocat\w*"
                       r"|\brelocation is fine\b|\bopen to relocation\b", re.I)


def parse_remote(text: str) -> dict:
    """Remote posture plus relocation, from one answer.

    Returns `remote: "unstated"` rather than defaulting to remote-only. A default
    here is a search the user never asked for, run on their behalf."""
    t = _norm(text).lower()
    mode = UNSTATED
    if re.search(r"\b(remote only|only remote|fully remote|remote-first|remote first)\b", t):
        mode = REMOTE_ONLY
    elif re.search(r"\bhybrid\b", t):
        mode = HYBRID_OK
    elif re.search(r"\b(onsite|on-site|in office|in-office|in person|in-person)\b", t):
        mode = ONSITE_OK
    elif re.search(r"\bno remote\b", t):
        mode = ONSITE_OK
    elif re.search(r"\bremote\b", t):
        mode = REMOTE_ONLY
    relo = None
    if _RELO_NO.search(t):
        relo = False
    elif _RELO_YES.search(t):
        relo = True
    return {"remote": mode, "relocation": relo}


def parse_company_size(text: str) -> list[str]:
    t = _norm(text).lower()
    out = []
    for word, slug in _SIZE_WORDS.items():
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", t) and slug not in out:
            out.append(slug)
    return sorted(set(out), key=COMPANY_SIZES.index)


def parse_dealbreakers(text: str) -> list[str]:
    return [x for x in _split_list(text) if x.lower() not in _NONE_WORDS]


QUESTIONS: tuple[Question, ...] = (
    Question(
        key="titles",
        prompt="What job titles are you actually going after?",
        why="Every posting is scored against these, so this is the one answer "
            "that decides what you see at all.",
        example="senior product manager, principal product manager",
        kind="list", required=True, parse=parse_titles),
    Question(
        key="seniority",
        prompt="What level? (e.g. senior or above, staff, director)",
        why="Levels below the lowest one you name are filtered out, so you stop "
            "seeing the same title three rungs down.",
        example="senior or above",
        kind="text", required=False, parse=parse_seniority),
    Question(
        key="domains",
        prompt="Which industries or problem spaces? Say 'not X' for one you refuse.",
        why="This seeds which companies get proposed to you, and boosts matching "
            "postings.",
        example="ai infra, devtools, not crypto",
        kind="list", required=False, parse=parse_domains),
    Question(
        key="locations",
        prompt="Where? (cities, states, countries -- or 'remote')",
        why="A posting outside these is scored down rather than hidden, so a "
            "great job in the wrong city still reaches you.",
        example="remote, san francisco, new york",
        kind="list", required=False, parse=parse_locations),
    Question(
        key="remote",
        prompt="Remote, hybrid or onsite -- and would you relocate?",
        why="Relocation is the question that most often kills an application "
            "late, so it is asked up front.",
        example="remote only, no relocation",
        kind="choice", required=False, parse=parse_remote),
    Question(
        key="comp_min",
        prompt="What is your compensation floor? ('none' is a real answer.)",
        why="A posting that states a band under this is filtered out. A posting "
            "that states nothing is never penalised for it.",
        example="180k",
        kind="money", required=False, parse=parse_money),
    Question(
        key="company_size",
        prompt="Company size or stage? (seed, startup, growth, public, any)",
        why="This picks which job boards are worth watching -- they are not "
            "interchangeable by stage.",
        example="early, growth",
        kind="list", required=False, parse=parse_company_size),
    Question(
        key="dealbreakers",
        prompt="Anything that is an automatic no?",
        why="These become hard filters, and they are printed on every approval "
            "card so you can see what was already ruled out.",
        example="no relocation, no crypto, no on-call",
        kind="list", required=False, parse=parse_dealbreakers),
)

_KEYS = tuple(q.key for q in QUESTIONS)


def question(key: str) -> Question:
    for q in QUESTIONS:
        if q.key == key:
            return q
    raise UnknownAnswer(f"no question named {key!r}; known: {', '.join(_KEYS)}")


def build_goals(answers: dict) -> dict:
    """Turn interview answers into the goals dict everything else reads.

    Pure and deterministic: no clock, no disk, no network, no randomness, and the
    input is never mutated.

    The result is deliberately shaped so `openrecruiter.engine.goals.score` can
    read it directly -- `titles`, `seniority`, `comp_min`, `locations`,
    `keywords_bonus`, `min_match` are top-level. There is no second copy of the
    search under another key, because two copies of a number is how the ancestor
    came to display one salary while filtering on a different one.
    """
    if not isinstance(answers, dict):
        raise IncompleteIntake(f"answers should be a dict of question key -> reply, "
                               f"got {type(answers).__name__}")
    unknown = [k for k in answers if k not in _KEYS]
    if unknown:
        raise UnknownAnswer(
            f"no question asked for {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(_KEYS)}")

    parsed: dict[str, Any] = {}
    unanswered: list[str] = []
    unparsed: dict[str, str] = {}
    for q in QUESTIONS:
        raw = answers.get(q.key)
        if raw is None or not _norm(str(raw)):
            if q.required:
                raise IncompleteIntake(
                    f"{q.key!r} is required and was not answered: {q.prompt}")
            unanswered.append(q.key)
            parsed[q.key] = None
            continue
        parsed[q.key] = q.parse(str(raw))

    titles = parsed["titles"] or []
    if not titles:
        raise IncompleteIntake(
            "no target titles could be read from your answer, and without them "
            "every posting scores the same -- which looks like a working search "
            "and is not one. Re-answer: " + question("titles").prompt)

    seniority = parsed["seniority"] or {"prefer": [], "avoid": []}
    if answers.get("seniority") and not seniority["prefer"]:
        unparsed["seniority"] = _norm(str(answers["seniority"]))
    domains = parsed["domains"] or {"prefer": [], "avoid": [], "deprioritize": []}
    remote = parsed["remote"] or {"remote": UNSTATED, "relocation": None}
    if answers.get("remote") and remote["remote"] == UNSTATED and remote["relocation"] is None:
        unparsed["remote"] = _norm(str(answers["remote"]))
    comp = parsed["comp_min"]
    if answers.get("comp_min") and comp is None:
        unparsed["comp_min"] = _norm(str(answers["comp_min"]))
        comp = 0
    sizes = parsed["company_size"] or []
    if answers.get("company_size") and not sizes:
        unparsed["company_size"] = _norm(str(answers["company_size"]))

    goals = {
        "version": VERSION,
        "titles": {"strong": titles, "medium": []},
        "seniority": {"prefer": list(seniority["prefer"]), "avoid": list(seniority["avoid"])},
        "domains": {"prefer": list(domains["prefer"]),
                    "avoid": list(domains["avoid"]),
                    "deprioritize": list(domains.get("deprioritize", []))},
        "locations": list(parsed["locations"] or []),
        "remote": remote["remote"],
        "relocation": remote["relocation"],
        "comp_min": int(comp or 0),
        "company_sizes": sizes,
        "dealbreakers": list(parsed["dealbreakers"] or []),
        "keywords_bonus": [],
        "min_match": DEFAULT_MIN_MATCH,
        "unanswered": unanswered,
        "unparsed_answers": unparsed,
    }

    # Dealbreakers are run through the same reader `refine_goals` uses, so
    # "no relocation" typed here means what it means when typed later. Without
    # this the phrase would be stored as decoration while the relocation flag
    # stayed unset.
    for phrase in goals["dealbreakers"]:
        for op in _read_clause(phrase.lower()) or ():
            _apply(goals, op)

    _derive(goals)
    return goals


# Recomputed from the authored fields every time anything changes, so a derived
# value can never drift away from the answer it came from.
def _derive(goals: dict) -> None:
    strong = goals["titles"]["strong"]
    medium = []
    for t in strong:
        base = _strip_level(t)
        if base and base.lower() not in {x.lower() for x in strong} \
                and base.lower() not in {x.lower() for x in medium}:
            medium.append(base)
    goals["titles"]["medium"] = medium

    # engine.goals.score only knows `keywords_bonus`; it is the wanted domains
    # under the name the scorer reads. One derivation, one direction.
    goals["keywords_bonus"] = list(goals["domains"]["prefer"])

    if goals.get("remote") in (REMOTE_ONLY, HYBRID_OK):
        if not any(l.lower() == "remote" for l in goals["locations"]):
            goals["locations"] = ["remote"] + goals["locations"]


def _strip_level(title: str) -> str:
    t = _norm(title)
    low = t.lower()
    for p in sorted(_TITLE_PREFIXES, key=len, reverse=True):
        if low.startswith(p + " "):
            return _norm(t[len(p):])
    return ""


# --------------------------------------------------------------------------
# refinement
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Diff:
    """What an instruction would do, for a human to confirm.

    `applied` is False whenever any clause was unreadable, and in that case the
    goals handed back are the ORIGINAL ones. Applying the half we understood and
    reporting the rest would leave the user believing the whole sentence landed
    -- the same failure mode as treating "yes but change the summary" as consent.
    """
    changes: tuple[str, ...]
    noop: tuple[str, ...]      # understood, but nothing to change
    unparsed: tuple[str, ...]  # could not read -- never guessed at
    applied: bool

    @property
    def ok(self) -> bool:
        return self.applied and not self.unparsed

    def as_text(self) -> str:
        lines = []
        if not self.applied:
            lines.append("NOT APPLIED -- part of that instruction was unreadable, "
                         "so nothing was changed.")
        for c in self.changes:
            lines.append(("would change: " if not self.applied else "") + c)
        for n in self.noop:
            lines.append(f"no change: {n}")
        for u in self.unparsed:
            lines.append(f"could not read: {u!r} -- say it another way?")
        if not lines:
            lines.append("nothing in that instruction changed anything.")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.as_text()


_VAGUE = {"that", "this", "it", "them", "those", "these", "stuff", "things",
          "thing", "something", "anything", "everything", "whatever", "some",
          "such", "like", "you", "know", "i", "me", "my", "we", "much", "kind",
          "sort", "etc", "the", "a", "an"}
_TRAILING = ("roles", "role", "jobs", "job", "companies", "company", "work",
             "positions", "position", "stuff", "gigs")

_MONEY_CUE = re.compile(
    r"\b(under|below|less than|lower than|beneath|at least|minimum|min|floor|"
    r"over|above|more than|starting at|salary|comp|compensation|pay|base|tc|"
    r"skip|drop|no|nothing|not|raise|bump)\b", re.I)


# A comma between two digits is a thousands separator, not a clause break.
# Splitting on it turned "nothing under $180,000" into "nothing under $180" plus
# an orphan "000", which then read as a refusal of a domain called '000'.
_CLAUSE_SPLIT = re.compile(r"(?<!\d),(?!\d)|[;\n]|\band\b|\bbut\b|\balso\b|\bplus\b",
                           re.I)


def _split_clauses(instruction: str) -> list[str]:
    parts = _CLAUSE_SPLIT.split(_norm(instruction))
    return [_norm(p).strip(".").lower() for p in parts if _norm(p).strip(".")]


def _clean_phrase(raw: str) -> str | None:
    """A domain phrase we are confident about, or None.

    Confidence is the whole point: a fallback that accepts anything turns
    "more of that stuff you know" into a domain called 'that stuff'.
    """
    p = _norm(raw).strip(".").lower()
    p = re.sub(r"^(?:of|on|about|into|any|anything|more|less)\s+", "", p)
    words = p.split()
    while words and words[-1] in _TRAILING:
        words.pop()
    if not words or len(words) > 4:
        return None
    if any(w in _VAGUE for w in words):
        return None
    if any(ch.isdigit() for ch in p):
        return None
    return " ".join(words)


def _read_clause(clause: str) -> list[tuple] | None:
    """One clause -> a list of ops, or None when we cannot read it.

    Ordered: the specific readers run before the domain fallback, so
    "no relocation" is a relocation instruction and not a domain named
    'relocation'.
    """
    c = _norm(clause).lower()
    if not c:
        return None

    if _RELO_NO.search(c):
        return [("set", "relocation", False)]
    if _RELO_YES.search(c):
        return [("set", "relocation", True)]

    if re.search(r"\b(remote only|only remote|fully remote|remote-only)\b", c):
        return [("set", "remote", REMOTE_ONLY)]
    if re.search(r"\b(hybrid)\b", c) and not re.search(r"\bno hybrid\b", c):
        return [("set", "remote", HYBRID_OK)]
    if re.search(r"\b(no remote|onsite only|only onsite|in office only)\b", c):
        return [("set", "remote", ONSITE_OK)]

    money = parse_money(c) if _MONEY.search(c) else None
    if money is not None and _MONEY_CUE.search(c):
        return [("set", "comp_min", money)]

    m = re.match(r"^(?:no|not|never|avoid|exclude|rule out|nothing|skip|drop|"
                 r"no more|stop)\s+(.+)$", c)
    if m:
        p = _clean_phrase(m.group(1))
        return [("add", "domains.avoid", p)] if p else None

    m = re.match(r"^(?:less|fewer|de-?prioriti[sz]e|dial back|tone down|"
                 r"not as much|ease off)\s+(.+)$", c)
    if m:
        p = _clean_phrase(m.group(1))
        return [("add", "domains.deprioritize", p)] if p else None

    m = re.match(r"^(?:more|much more|prioriti[sz]e|focus on|lean into|"
                 r"add|want more|i want more|emphasi[sz]e)\s+(.+)$", c)
    if m:
        p = _clean_phrase(m.group(1))
        return [("add", "domains.prefer", p)] if p else None

    return None


_DOMAIN_LISTS = ("domains.prefer", "domains.avoid", "domains.deprioritize")


def _fmt(path: str, value) -> str:
    if path == "comp_min":
        return f"${int(value):,}" if value else "no floor"
    if path == "relocation":
        return {True: "yes", False: "no", None: "not stated"}[value]
    return str(value)


def _apply(goals: dict, op: tuple) -> tuple[str, str]:
    """Apply one op. Returns ("change"|"noop", human-readable line)."""
    kind, path, value = op
    if kind == "set":
        old = goals.get(path)
        if old == value:
            return ("noop", f"{path} is already {_fmt(path, value)}")
        goals[path] = value
        return ("change", f"{path}: {_fmt(path, old)} -> {_fmt(path, value)}")

    if kind == "add":
        group, name = path.split(".")
        low = value.lower()
        was = next((other for other in _DOMAIN_LISTS
                    if any(x.lower() == low for x in goals[group][other.split(".")[1]])), None)
        if was == path:
            return ("noop", f"{value!r} is already in {path}")
        for other in _DOMAIN_LISTS:
            lst = goals[group][other.split(".")[1]]
            goals[group][other.split(".")[1]] = [x for x in lst if x.lower() != low]
        goals[group][name].append(value)
        if was:
            return ("change", f"{value!r}: {was} -> {path}")
        return ("change", f"{path}: + {value!r}")

    raise InterviewError(f"unknown op kind {kind!r}")


def _require_goals(goals) -> None:
    """A malformed goals dict is refused here rather than half-edited.

    A refinement that silently no-ops on a dict missing `domains` would report
    'nothing changed' -- indistinguishable from an instruction that genuinely
    changed nothing."""
    if not isinstance(goals, dict):
        raise InterviewError(f"goals should be a dict from build_goals, "
                             f"got {type(goals).__name__}")
    for key in ("titles", "domains", "seniority"):
        if not isinstance(goals.get(key), dict):
            raise InterviewError(
                f"goals is missing {key!r} -- this is not a build_goals() result, "
                f"and refining it would produce a search nobody authored")


def refine_goals(goals: dict, instruction: str) -> tuple[dict, Diff]:
    """Apply a plain-language instruction, and report what it did.

    Returns (candidate_goals, diff). The caller shows the diff and confirms --
    this function never writes anything.

    Deliberate refusals:
      * a clause it cannot read confidently is reported, never guessed at
      * if ANY clause is unreadable, NOTHING is applied and the original goals
        come back, with the diff naming both halves
      * an instruction that changes nothing says so explicitly, so "understood,
        already true" is never mistaken for "ignored"
    """
    _require_goals(goals)
    base = copy.deepcopy(goals)
    candidate = copy.deepcopy(goals)
    clauses = _split_clauses(instruction)
    if not clauses:
        return base, Diff((), (), ("<empty instruction>",), False)

    changes: list[str] = []
    noop: list[str] = []
    unparsed: list[str] = []
    for clause in clauses:
        ops = _read_clause(clause)
        if not ops:
            unparsed.append(clause)
            continue
        for op in ops:
            what, line = _apply(candidate, op)
            (changes if what == "change" else noop).append(line)

    _derive(candidate)
    if candidate.get("keywords_bonus") != base.get("keywords_bonus"):
        changes.append(f"(derived) keywords_bonus: {base.get('keywords_bonus')} "
                       f"-> {candidate['keywords_bonus']}")

    if unparsed:
        return base, Diff(tuple(changes), tuple(noop), tuple(unparsed), False)
    return candidate, Diff(tuple(changes), tuple(noop), (), True)


# --------------------------------------------------------------------------
# the proposed pipeline
# --------------------------------------------------------------------------

_KINDS = ("company", "board", "title")


def _rk(kind: str, value: str) -> str:
    """Reason key. Namespaced by kind so a company and a board sharing a name
    cannot overwrite each other's reason."""
    return f"{kind}:{value}"


def reason_for(pipeline: dict, kind: str, value: str) -> str:
    return pipeline.get("reasons", {}).get(_rk(kind, value), "")


def validate_proposal(pipeline: dict) -> dict:
    """Every proposed entry must carry a reason. Raises if one does not.

    Enforced in code, not left to each producer: a pipeline entry with no reason
    is an instruction the user cannot evaluate, and a list of those is exactly
    the feed this module exists to replace.
    """
    reasons = pipeline.get("reasons")
    if not isinstance(reasons, dict):
        raise ProposalError("pipeline has no reasons map")
    for kind, key in (("company", "companies"), ("board", "boards"), ("title", "titles")):
        for entry in pipeline.get(key, []):
            if not reasons.get(_rk(kind, entry)):
                raise ProposalError(f"{kind} {entry!r} was proposed with no reason")
    for fkey in pipeline.get("filters", {}):
        if not reasons.get(_rk("filter", fkey)):
            raise ProposalError(f"filter {fkey!r} was proposed with no reason")
    return pipeline


def propose_pipeline(goals: dict, bank: dict | None, *, previous: dict | None = None) -> dict:
    """Propose where to look. The user edits this; `confirm_pipeline` settles it.

    `bank` is the experience bank (what the user can actually claim). It is used
    to ground proposals in their own history, and its absence is reported in
    `warnings` rather than silently producing a thinner list that looks complete.

    `previous` is a confirmed pipeline. Anything the user added by hand there is
    carried forward with their reason intact, and anything they removed is not
    proposed again -- otherwise every re-proposal would quietly overwrite their
    judgement with ours.
    """
    if not isinstance(goals, dict) or not goals.get("titles", {}).get("strong"):
        raise IncompleteIntake(
            "cannot propose a pipeline without target titles -- run the interview "
            "first (build_goals)")

    warnings: list[str] = []
    reasons: dict[str, str] = {}
    bank = bank if isinstance(bank, dict) else None
    if bank is None:
        warnings.append("no experience bank was supplied, so nothing below is grounded "
                        "in your actual history -- these come from your stated goals only")
        bank = {}
    elif not bank.get("companies") and not bank.get("domains"):
        warnings.append("the experience bank is empty (no companies, no domains), so "
                        "proposals come from your stated goals only")

    pinned = (previous or {}).get("pinned", {})
    removed = (previous or {}).get("removed", {})

    companies = _propose_companies(goals, bank, reasons, warnings, removed)
    boards = _propose_boards(goals, reasons, removed)
    titles = _propose_titles(goals, reasons, removed)
    filters = _propose_filters(goals, reasons)

    for kind, key, lst in (("company", "companies", companies),
                           ("board", "boards", boards),
                           ("title", "titles", titles)):
        for entry in pinned.get(key, []):
            if any(e.lower() == entry.lower() for e in lst):
                # the user's reason wins over ours: they authored this entry
                reasons[_rk(kind, entry)] = (previous or {}).get("reasons", {}).get(
                    _rk(kind, entry), reasons.get(_rk(kind, entry), "you added this by hand"))
                continue
            lst.append(entry)
            reasons[_rk(kind, entry)] = (previous or {}).get("reasons", {}).get(
                _rk(kind, entry), "you added this by hand")

    return validate_proposal({
        "companies": companies,
        "boards": boards,
        "titles": titles,
        "filters": filters,
        "reasons": reasons,
        "warnings": warnings,
        "pinned": {k: list(v) for k, v in pinned.items()},
        "removed": {k: list(v) for k, v in removed.items()},
        "confirmed": False,
    })


def _dropped(removed: dict, key: str, value: str) -> bool:
    return any(x.lower() == value.lower() for x in removed.get(key, []))


def _propose_companies(goals, bank, reasons, warnings, removed) -> list[str]:
    out: list[str] = []

    def add(name, reason):
        if _dropped(removed, "companies", name):
            return
        if any(o.lower() == name.lower() for o in out):
            return
        out.append(name)
        reasons[_rk("company", name)] = reason

    # Your own history first. A return application to a place that already knows
    # you is usually the strongest one on the list, and it is the one proposal
    # here that is not us guessing.
    for c in bank.get("companies", []) or []:
        name = _norm(str(c))
        if name:
            add(name, "your experience bank says you worked here -- a return "
                      "application is the strongest one you can send")

    avoided = {_canon_domain(d) for d in goals["domains"]["avoid"]}
    deprio = {_canon_domain(d) for d in goals["domains"]["deprioritize"]}
    bank_domains = {_canon_domain(d) for d in (bank.get("domains") or [])}
    unseeded = []
    for want in goals["domains"]["prefer"]:
        slug = _canon_domain(want)
        if slug is None or slug not in SEED_COMPANIES:
            unseeded.append(want)
            continue
        if slug in avoided or slug in deprio:
            continue
        for name in SEED_COMPANIES[slug]:
            reason = f"works in {want}, which you named as a target"
            if slug in bank_domains:
                reason += f"; your bank shows {want} experience to point at"
            add(name, reason)

    if unseeded:
        warnings.append(
            "no seeded companies for " + ", ".join(sorted(unseeded)) +
            " -- this catalog is a starting point, not a search. Add the companies "
            "you actually want and they will stick.")
    if not out:
        warnings.append("no companies proposed at all. That is not 'none exist' -- it "
                        "means nothing you told us matched the seed catalog. Add your own.")
    return out


def _canon_domain(text: str) -> str | None:
    t = _norm(text).lower()
    if t in _DOMAIN_SLUGS:
        return _DOMAIN_SLUGS[t]
    for alias, slug in _DOMAIN_SLUGS.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
            return slug
    return None


def _propose_boards(goals, reasons, removed) -> list[str]:
    out: list[str] = []

    def add(name, reason):
        if _dropped(removed, "boards", name) or name in out:
            return
        out.append(name)
        reasons[_rk("board", name)] = reason

    add("linkedin", "broadest coverage for your titles; nearly every employer posts there")
    if goals.get("remote") in (REMOTE_ONLY, HYBRID_OK):
        add("remoteok", f"you answered remote ({goals['remote']}) and this board is "
                        f"remote-only postings")
        add("weworkremotely", "same reason: remote-only listings, so less to filter")
    sizes = goals.get("company_sizes") or []
    if "seed" in sizes or "early" in sizes or "any" in sizes:
        add("wellfound", "you said early-stage; this is where seed and Series A roles post")
        add("hn-who-is-hiring", "early-stage roles that never reach a job board")
    if "growth" in sizes or "late" in sizes:
        add("otta", "growth-stage coverage, which the startup boards miss")
    if "public" in sizes:
        add("company-careers-pages", "large employers post to their own site first and "
                                     "syndicate late")
    for loc in goals.get("locations", []):
        if loc.lower() in ("remote", "united states", "usa", "us", "anywhere"):
            continue
        add(f"builtin-{_slug(loc)}", f"local board for {loc}, which you named as a location")
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(text).lower()).strip("-")


def _propose_titles(goals, reasons, removed) -> list[str]:
    out: list[str] = []

    def add(name, reason):
        if _dropped(removed, "titles", name) or any(o.lower() == name.lower() for o in out):
            return
        out.append(name)
        reasons[_rk("title", name)] = reason

    prefer = goals["seniority"]["prefer"]
    for t in goals["titles"]["strong"]:
        add(t, "a title you named")
        # A title that already carries a level is left alone. Prefixing another
        # one produces "staff senior product manager", which no employer posts.
        if _strip_level(t):
            continue
        for level in prefer[:2]:
            if level in t.lower() or level in ("mid", "new grad"):
                continue
            add(f"{level} {t}", f"your title at {level}, the level you asked for")
    for t in goals["titles"]["medium"]:
        add(t, "the base form of a title you named, so a differently-worded "
               "posting still reaches you")
    return out


def _propose_filters(goals, reasons) -> dict:
    f: dict[str, Any] = {}

    def add(key, value, reason):
        f[key] = value
        reasons[_rk("filter", key)] = reason

    add("comp_min", goals.get("comp_min", 0),
        f"your stated floor ({_fmt('comp_min', goals.get('comp_min', 0))}); a posting "
        f"that states no band is never penalised for it")
    add("locations", list(goals.get("locations", [])),
        "where you said you would work; elsewhere is scored down, not hidden")
    add("remote", goals.get("remote", UNSTATED), "your remote answer")
    add("relocation", goals.get("relocation"),
        f"you said relocation: {_fmt('relocation', goals.get('relocation'))}")
    add("seniority_avoid", list(goals["seniority"]["avoid"]),
        "levels below the lowest one you named")
    # Match terms and stated dealbreakers are deliberately SEPARATE keys.
    # A dealbreaker is a sentence, not a keyword: matching on the literal phrase
    # "no on-call" would exclude precisely the postings that promise no on-call.
    # The negated noun ("on-call") is already in domains.avoid, put there by the
    # same reader `refine_goals` uses; the raw phrase stays for the card to show.
    add("exclude_keywords", list(goals["domains"]["avoid"]),
        "the things you refused, as match terms")
    add("dealbreakers", list(goals.get("dealbreakers", [])),
        "your own words, printed on every approval card so you can see what was "
        "already ruled out -- not used as match terms")
    add("min_match", goals.get("min_match", DEFAULT_MIN_MATCH),
        "the score a posting must reach before it is worth building a resume for")
    return f


_EDIT_KEYS = ("add", "remove", "reasons", "filters")
_LIST_KEYS = ("companies", "boards", "titles")
_KIND_OF = {"companies": "company", "boards": "board", "titles": "title"}


def confirm_pipeline(proposal: dict, edits: dict | None = None) -> dict:
    """Settle a proposal into the pipeline the user authored.

    `edits` is {"add": {"companies": [...]}, "remove": {...},
                "reasons": {"company:Foo": "..."}, "filters": {...}}

    Everything added here is recorded in `pinned` and everything removed in
    `removed`, and `propose_pipeline(..., previous=<this>)` honours both. That is
    the whole point: a re-proposal must not be able to quietly undo the user.

    An edit naming something unknown raises. A removal that silently does nothing
    is worse than an error, because the user walks away believing the entry is
    gone.
    """
    if not isinstance(proposal, dict):
        raise UnknownEdit(f"proposal should be a dict, got {type(proposal).__name__}")
    edits = edits or {}
    unknown = [k for k in edits if k not in _EDIT_KEYS]
    if unknown:
        raise UnknownEdit(f"unknown edit key(s): {', '.join(sorted(unknown))}. "
                          f"Known: {', '.join(_EDIT_KEYS)}")

    final = copy.deepcopy(proposal)
    final.setdefault("pinned", {})
    final.setdefault("removed", {})
    reasons = final.setdefault("reasons", {})

    for section in ("add", "remove"):
        for key in (edits.get(section) or {}):
            if key not in _LIST_KEYS:
                raise UnknownEdit(f"cannot {section} {key!r}: a pipeline has "
                                  f"{', '.join(_LIST_KEYS)}")

    for key, values in (edits.get("remove") or {}).items():
        current = final.get(key, [])
        for v in values:
            hit = next((c for c in current if c.lower() == str(v).lower()), None)
            if hit is None:
                raise UnknownEdit(
                    f"cannot remove {v!r} from {key}: it is not in the proposal. "
                    f"(Nothing was changed -- a removal that quietly does nothing "
                    f"is how you end up applying to it anyway.)")
            current.remove(hit)
            reasons.pop(_rk(_KIND_OF[key], hit), None)
            final["removed"].setdefault(key, []).append(hit)
            pins = final["pinned"].get(key, [])
            final["pinned"][key] = [p for p in pins if p.lower() != hit.lower()]

    for key, values in (edits.get("add") or {}).items():
        current = final.setdefault(key, [])
        for v in values:
            name = _norm(str(v))
            if not name:
                raise UnknownEdit(f"cannot add an empty entry to {key}")
            given = (edits.get("reasons") or {}).get(_rk(_KIND_OF[key], name))
            if not any(c.lower() == name.lower() for c in current):
                current.append(name)
            reasons[_rk(_KIND_OF[key], name)] = given or "you added this by hand"
            pins = final["pinned"].setdefault(key, [])
            if not any(p.lower() == name.lower() for p in pins):
                pins.append(name)
            final["removed"][key] = [r for r in final["removed"].get(key, [])
                                     if r.lower() != name.lower()]

    for fkey, fval in (edits.get("filters") or {}).items():
        if fkey not in final.get("filters", {}):
            raise UnknownEdit(
                f"unknown filter {fkey!r}. Known: "
                f"{', '.join(sorted(final.get('filters', {})))}")
        final["filters"][fkey] = fval
        reasons[_rk("filter", fkey)] = (edits.get("reasons") or {}).get(
            _rk("filter", fkey), "you set this by hand")

    for rk, text in (edits.get("reasons") or {}).items():
        reasons[rk] = text

    final["confirmed"] = True
    return validate_proposal(final)
