"""The conversational surface: the thing that makes this an agent you text.

Every command here works identically on every transport, because nothing in this
module knows what a transport is. `route()` is a pure function of (text, context)
and returns an `Intent` describing what the caller should do. It performs no I/O,
opens no database, and sends no message.

Three design positions worth reading before extending:

* **One item per request, structurally.** `Intent` carries a singular `item`, and
  `__post_init__` refuses a list of them. There is deliberately no constant
  naming how many items a turn may present -- a number would be a knob, and a
  knob is how "one at a time" becomes "twenty at a time" at 1am. `status` answers
  with counts and the single item on deck, never a menu, because a menu is a
  firehose and quietly re-invents deciding many things in one breath.
  This is also why the surface can never page anyone at 3am: it has nothing to
  say unless a human asked it something.

* **Consent is parsed in exactly ONE place in this system.** That place is
  `openrecruiter.channels.base.parse_reply`. This module does not keep its own
  copy of "what counts as yes" -- two definitions of consent that drift apart is
  precisely the bug class this project exists to not repeat. The import is
  deferred into the call so the module still has no transport dependency at
  import time, and a context may inject its own reader for tests.

* **A check that cannot run is a refusal.** Every context call goes through
  `_ask`, which turns a missing or throwing accessor into `ContextUnavailable`
  and then into an `Intent` with `refusal` set and `ok` False. An unreadable
  queue never renders as an empty one.

* **A refusal must name an exit that exists.** The ambiguity refusal below used
  to fire on EVERY input while a card and a goal change were both outstanding --
  `queue`, `help`, `goals` and any free-text question included -- and then told
  the user to "answer the card itself", which hit the same refusal. A surface
  whose only escape from a state is a verb it refuses is a deadlock, and the
  whole "you can just talk to it" claim is dead in that state. So the ambiguity
  gate refuses ONLY a reading that is genuinely a yes or a no, and the text it
  returns names exits (`skip <id>`, `pause`, a fresh `set ...`) that are checked
  ahead of the gate and therefore actually work.
"""
from __future__ import annotations

import dataclasses
import enum
import re
from typing import Any, Mapping, Protocol, runtime_checkable


class Verb(str, enum.Enum):
    GO = "go"                        # present the ONE item on deck
    QUEUE = "queue"                  # counts, plus what is on deck
    WHY = "why"                      # explain a pass, a low score, a screen-out
    SKIP = "skip"                    # decline one named item
    PAUSE = "pause"                  # halt now
    GOALS = "goals"
    SET = "set"                      # a PROPOSED refinement, awaiting confirmation
    SET_CONFIRMED = "set_confirmed"  # the human confirmed the diff
    SET_CANCELLED = "set_cancelled"
    HELP = "help"
    DECISION = "decision"            # y/n answering the outstanding card
    FALLBACK = "fallback"            # anything else: hand it to the agent, with context


class ContextUnavailable(RuntimeError):
    """A thing the surface needed to read could not be read. Never a silent pass."""


class NotConfirmed(RuntimeError):
    """A refinement was asked to apply without the human confirming the diff."""


@runtime_checkable
class Context(Protocol):
    """What `route` needs from the caller. Every member is optional at runtime --
    an absent one produces a refusal naming it, rather than a wrong answer.

    Attributes:
        outstanding_card_id: the app_id of the card currently awaiting a y/n, or None
        pending_refinement:  a dict from an earlier SET intent's payload, or None
        paused:              whether the loop is currently halted
    Methods:
        stats()            -> {state_name: count}
        next_item()        -> one item mapping, or None when the queue is empty
        get(app_id)        -> one item mapping, or None when unknown
        goals()            -> the goals mapping, or None when none are set
        refine_goals(goals, instruction) -> the PROPOSED goals, in the argument
            order and return shape of `openrecruiter.interview.refine_goals`:
            `(proposed_goals, Diff)`. A bare mapping is also accepted, so a
            context may inject something simpler, but the ARGUMENT ORDER is the
            production one -- this docstring previously said `(phrase, current)`,
            which is reversed, and every test injected its own refiner, so the
            verb had never once been exercised against the real function.
        parse_reply(text)  -> optional; defaults to the channel layer's one reader
    """
    outstanding_card_id: str | None
    pending_refinement: Mapping[str, Any] | None
    paused: bool


def _is_item_like(v: Any) -> bool:
    return isinstance(v, Mapping) and ("app_id" in v or ("id" in v and "company" in v))


@dataclasses.dataclass(frozen=True)
class Intent:
    """What the caller should do about one message.

    `ok` is False when something could not be read or carried out; `reply` then
    carries the refusal verbatim, so a caller that only prints `reply` still
    tells the truth.
    """
    verb: Verb
    text: str
    reply: str = ""
    item: Mapping[str, Any] | None = None
    app_id: str | None = None
    decision: str | None = None          # "approve" | "reject"; same strings as channels.Decision
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    needs_confirmation: bool = False
    refusal: str | None = None

    def __post_init__(self):
        # The one-item rule enforced in the type, not in each call site. A future
        # verb that wanted to hand back a page of applications would have to
        # delete this to do it, and deleting it is what the tests watch for.
        if self.item is not None and not isinstance(self.item, Mapping):
            raise TypeError(
                "Intent.item must be ONE item mapping; the surface presents one "
                f"application per request (got {type(self.item).__name__})")
        for key, val in (self.payload or {}).items():
            if isinstance(val, (list, tuple, set)) and sum(
                    1 for v in val if _is_item_like(v)) > 1:
                raise TypeError(
                    f"Intent.payload[{key!r}] carries more than one application. "
                    "This surface presents one at a time; send counts, not a menu.")

    @property
    def ok(self) -> bool:
        return self.refusal is None


# -- context access -----------------------------------------------------------

def _ask(ctx: Any, name: str, *args):
    """Read one thing from the context, or raise loudly.

    A missing accessor and a throwing accessor are the same kind of blindness,
    and neither may be reported as "nothing found"."""
    fn = getattr(ctx, name, None)
    if fn is None:
        raise ContextUnavailable(f"this context has no {name}(), so I cannot answer that")
    try:
        return fn(*args) if callable(fn) else fn
    except ContextUnavailable:
        raise
    except Exception as e:                                   # noqa: BLE001 - reported, never swallowed
        raise ContextUnavailable(f"{name}() failed: {e.__class__.__name__}: {e}") from e


def _attr(ctx: Any, name: str, default=None):
    try:
        return getattr(ctx, name, default)
    except Exception:                                        # noqa: BLE001
        return default


def _consent(ctx: Any, text: str) -> str:
    """'approve' | 'reject' | 'ambiguous', using the system's single definition."""
    fn = _attr(ctx, "parse_reply")
    if not callable(fn):
        try:
            from openrecruiter.channels.base import parse_reply as fn  # deferred on purpose
        except Exception as e:                               # noqa: BLE001
            raise ContextUnavailable(
                f"cannot read yes/no: the consent parser is unavailable ({e}). "
                "Nothing was decided.") from e
    d = fn(text)
    return str(getattr(d, "value", d)).lower()


# -- lexicon ------------------------------------------------------------------

_GO = {"go", "next", "start", "begin", "resume", "continue"}
_QUEUE = {"queue", "status", "pending", "q"}
_PAUSE = {"pause", "halt", "stop"}
_GOALS = {"goals", "goal"}
_SET = {"set", "refine"}
_HELP = {"help", "commands", "?"}
_WHY = {"why", "explain"}
_SKIP = {"skip", "decline", "drop"}
# "pause please" must halt just as surely as "pause". Anything longer than filler
# ("stop applying to startups") is a sentence, not the halt verb, and falls through.
# Words, not phrases: the set is matched one word at a time by `_all_filler`.
_PAUSE_FILLER = {"please", "now", "everything", "all", "it", "this", "that", "for",
                 "right", "immediately", "just", "already", "pls", "thanks", "thank",
                 "you", "ok", "okay"}

# A state a human may be asked to approve. An allowlist, not a denylist: a state
# this module has never heard of is one it cannot vouch for, and presenting an
# unvouched-for row is exactly the failure the tier gate exists to prevent.
# `expired` is here because re-asking is what expiry MEANS (store.LEGAL routes
# EXPIRED -> AWAITING_APPROVAL); `below_bar` and `screened_out` never are.
_OFFERABLE = {"awaiting_approval", "expired"}

# An id is an opaque primary key, so this only rejects shapes that cannot be one.
# Determiners and quantifiers are in here for the same reason the pronouns are:
# "skip the acme one" split to head "skip" + rest "the", and "the" matched the id
# pattern, so the surface reported skipping an application called "the".
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")
_NOT_IDS = {"it", "this", "that", "one", "them", "him", "her", "us", "me", "yes", "no",
            "the", "a", "an", "all", "any", "some", "both", "each", "every", "other",
            "another", "ones", "these", "those", "my", "your", "first", "last", "next",
            "everything", "anything", "please", "now", "again", "there", "they"}

_WORDS = re.compile(r"[a-z0-9]+")


def _looks_like_id(tok: str) -> bool:
    return bool(_ID.match(tok)) and tok.lower() not in _NOT_IDS


def _all_filler(rest: str) -> bool:
    """Is everything after the halt word mere politeness or deixis?

    Punctuation is stripped per WORD here, not just off the head word as `_norm`
    does. Without that, "stop it." fell out of the halt branch and into the
    consent reader, which reads it as a plain NO -- so the halt silently DECLINED
    the open card and left the loop running. The one verb that must not be
    fragile was the fragile one, and it failed toward keeping going."""
    return all(w in _PAUSE_FILLER for w in _WORDS.findall(rest.lower()))


def _norm(text: str) -> tuple[str, str]:
    """(head-word, rest). Tolerates /slash commands and trailing punctuation."""
    s = (text or "").strip()
    if s.startswith("/"):
        s = s[1:]
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "", ""
    parts = s.split(" ", 1)
    head = parts[0].strip(".,!?:;").lower()
    return head, (parts[1].strip() if len(parts) > 1 else "")


# -- rendering helpers --------------------------------------------------------

def _label(item: Mapping[str, Any]) -> str:
    role = item.get("role") or "role unknown"
    company = item.get("company") or "company unknown"
    return f"{role} — {company}"


def _describe(item: Mapping[str, Any]) -> str:
    bits = [_label(item)]
    if item.get("tier"):
        bits.append(f"tier {item['tier']}")
    if item.get("score") is not None:
        bits.append(f"score {item['score']}")
    return "  ·  ".join(bits)


def help_text() -> str:
    return "\n".join([
        "go / next      — show me the one on deck",
        "queue          — what is pending, what is in flight",
        "why <id>       — why that score, that pass, or that screen-out",
        "skip <id>      — decline that one",
        "pause / stop   — halt now",
        "goals          — what I am searching for",
        "set <phrase>   — refine that search (I show you the diff first)",
        "help           — this",
        "",
        "Anything else, just say it — I will answer.",
    ])


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, Mapping):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix or "(root)"] = d
    return out


def diff_goals(current: Any, proposed: Any) -> list[str]:
    """A human-readable diff of two goal mappings.

    The diff, not the phrase, is what gets confirmed: "more senior roles" is not
    reviewable, but `search.seniority.prefer: [senior] -> [staff, principal]` is."""
    a, b = _flatten(current or {}), _flatten(proposed or {})
    lines = []
    for key in sorted(set(a) | set(b)):
        if key not in b:
            lines.append(f"- {key}: {a[key]!r} (removed)")
        elif key not in a:
            lines.append(f"+ {key}: {b[key]!r}")
        elif a[key] != b[key]:
            lines.append(f"~ {key}: {a[key]!r} -> {b[key]!r}")
    return lines


# -- the router ---------------------------------------------------------------

def route(text: str, ctx: Any) -> Intent:
    """Map one inbound message to an Intent. Pure: no I/O, no sends, no writes."""
    raw = text if isinstance(text, str) else ""
    head, rest = _norm(raw)
    card = _attr(ctx, "outstanding_card_id")
    pending = _attr(ctx, "pending_refinement")

    if not head:
        return _fallback(raw, ctx, note="empty message")

    # 1. Halting outranks everything, including being read as an answer to a card.
    #    A halt never sends anything, so routing it here cannot manufacture consent
    #    -- the outstanding card is declined, never approved.
    if head in _PAUSE and _all_filler(rest):
        return _pause(raw, card)

    # 2. Targeted commands are checked BEFORE the y/n reader, because "skip a1"
    #    would otherwise parse as a bare rejection of whatever card is open and
    #    silently decline the wrong application.
    if head in _WHY:
        return _why(raw, rest, ctx)
    if head in _SKIP and rest and _looks_like_id(rest.split(" ")[0]):
        return _skip(raw, rest.split(" ")[0], ctx)
    if head in _SET:
        return _set(raw, rest, ctx)

    # 3. A bare yes/no belongs to whatever is outstanding -- it is a DECISION, not
    #    a command. "go" and "skip" live in both vocabularies; while a card is open
    #    the card wins, which is what keeps a one-word answer from starting a new
    #    session instead of answering the question actually on screen.
    try:
        verdict = _consent(ctx, raw) if (card or pending) else "ambiguous"
    except ContextUnavailable as e:
        return _refuse(Verb.DECISION, raw, str(e), app_id=card)

    if card and pending and verdict in ("approve", "reject"):
        # Two things want the same "y". Ambiguity is never consent.
        #
        # This gate is deliberately AFTER the consent read and conditional on it.
        # Refusing unconditionally (which is what it used to do) refused `queue`,
        # `help`, `goals`, `go` and every free-text question too, and pointed the
        # user at "answer the card itself" -- which lands right back here. The
        # three exits named below are all checked ABOVE this line, so each one is
        # reachable from inside this state.
        return _refuse(Verb.DECISION, raw,
                       "there is both an application and a goal change waiting on a yes, "
                       "so a bare yes or no is ambiguous and I will not guess which you "
                       f"meant. Clear one of them first: 'skip {card}' declines the "
                       "application, a fresh 'set ...' replaces the goal change, and "
                       "'pause' halts everything. Then answer the other. "
                       "'why <id>', 'queue' and plain questions all still work.",
                       app_id=card)

    if card and verdict in ("approve", "reject"):
        return Intent(verb=Verb.DECISION, text=raw, app_id=card, decision=verdict,
                      reply=("Applying." if verdict == "approve" else "Skipped."),
                      payload={"card_id": card})
    if pending and verdict in ("approve", "reject"):
        return _confirm_refinement(raw, pending, verdict)

    # 4. Plain commands.
    if head in _GO:
        # "the card wins" has to be enforced here, not inherited from the consent
        # reader. Only "go" and "ok" happen to be in that reader's _YES set, so
        # "next", "start", "begin", "resume" and "continue" -- and "go ahead and
        # send it", which is too long for the reader to call consent -- all fell
        # straight through to _go and presented a SECOND application while the
        # first still had nobody's answer on it.
        if card:
            return _refuse(
                Verb.GO, raw,
                f"{card} is still waiting on you"
                + (" — and so is a goal change. Clear them with "
                   f"'skip {card}' and a fresh 'set ...', or 'pause'."
                   if pending else
                   f" — answer it Y / N, or 'skip {card}'.")
                + " I will bring you the next one after that.",
                app_id=card)
        return _go(raw, ctx)
    if head in _QUEUE:
        return _queue(raw, ctx)
    if head in _GOALS:
        return _goals(raw, ctx)
    if head in _HELP:
        return Intent(verb=Verb.HELP, text=raw, reply=help_text())
    if head in _SKIP:
        # Reachability, stated honestly because it reads the other way: with a
        # card open, "skip" and "skip it" are read as a plain NO by the consent
        # parser two steps above and DECLINE that card -- they never arrive here.
        # This branch is for a skip the parser could not read as either answer
        # ("skip the acme one"), where there is nothing to do but ask which.
        return _refuse(Verb.SKIP, raw,
                       ("Which one? Reply N to skip the one I just sent, or "
                        "'skip <id>'." if card else "Which one? Use 'skip <id>'."),
                       app_id=card)

    # 5. A lone yes/no with nothing outstanding decides nothing. Saying so is the
    #    honest answer; treating it as "go" would make a stray "y" start work.
    try:
        if _consent(ctx, raw) in ("approve", "reject"):
            return _refuse(Verb.DECISION, raw,
                           "Nothing is waiting on a yes or no right now. "
                           "Say 'go' when you want the next one.")
    except ContextUnavailable:
        pass                                  # fall through; the fallback still answers

    return _fallback(raw, ctx)


# -- verb handlers ------------------------------------------------------------

def _refuse(verb: Verb, raw: str, why: str, **kw) -> Intent:
    return Intent(verb=verb, text=raw, reply=why, refusal=why, **kw)


def _pause(raw: str, card: str | None) -> Intent:
    reply = "Paused. Nothing goes out until you say go."
    if card:
        reply = ("Paused. Nothing goes out until you say go — including the one "
                 "I just sent, which I have left as declined.")
    return Intent(verb=Verb.PAUSE, text=raw, reply=reply, app_id=card,
                  payload={"declines_outstanding": bool(card)})


def _go(raw: str, ctx: Any) -> Intent:
    try:
        item = _ask(ctx, "next_item")
    except ContextUnavailable as e:
        return _refuse(Verb.GO, raw, f"I cannot see the queue right now, so I will not "
                                     f"claim it is empty: {e}")
    if item is None:
        # An empty queue and an unreadable one must never render the same.
        return Intent(verb=Verb.GO, text=raw, payload={"queue_empty": True},
                      reply="Nothing is queued. The scout has not found anything new.")
    if not isinstance(item, Mapping):
        return _refuse(Verb.GO, raw, f"next_item() returned a {type(item).__name__}, not one "
                                     f"application. Refusing to present it.")
    # Defense in depth. Presenting an item IS asking a human to approve sending
    # it, and the bar that decides what may be asked about lived entirely in the
    # caller's next_item(). This module already knows what below_bar means (see
    # `_why`: "never offered for approval"), so a caller bug that put one on deck
    # was being laundered into an approval prompt by the surface that knew better.
    state = str(item.get("state") or "").strip().lower()
    if state not in _OFFERABLE:
        return _refuse(
            Verb.GO, raw,
            f"next_item() put {_label(item)} on deck in state "
            f"{state or '(none recorded)'}, which is not awaiting your approval. I will "
            "not present it: a resume that failed its tier's gate is never offered, and "
            "routing one to you through here would be the gate's only loophole. "
            "'why " + str(item.get("app_id") or item.get("id") or "<id>") + "' explains it.",
            app_id=item.get("app_id") or item.get("id"))
    was_paused = bool(_attr(ctx, "paused", False))
    reply = _describe(item)
    if item.get("weakest_reason"):
        reply += f"\nweakest — {item.get('weakest') or 'judge'}: {item['weakest_reason']}"
    if was_paused:
        reply = "Resuming.\n" + reply
    return Intent(verb=Verb.GO, text=raw, item=item, app_id=item.get("app_id") or item.get("id"),
                  reply=reply, payload={"resumed": was_paused})


def _queue(raw: str, ctx: Any) -> Intent:
    try:
        counts = _ask(ctx, "stats")
    except ContextUnavailable as e:
        return _refuse(Verb.QUEUE, raw, f"I cannot read the queue right now: {e}")
    if not isinstance(counts, Mapping):
        return _refuse(Verb.QUEUE, raw,
                       f"stats() returned a {type(counts).__name__}; I will not guess at it.")
    try:
        nxt = _ask(ctx, "next_item")
        next_err = None
    except ContextUnavailable as e:
        nxt, next_err = None, str(e)

    total = sum(v for v in counts.values() if isinstance(v, int))
    if not counts or total == 0:
        body = "Nothing in the pipeline yet."
    else:
        body = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    if next_err:
        body += f"\n(on deck: unknown — {next_err})"
    elif isinstance(nxt, Mapping):
        body += f"\non deck: {_describe(nxt)}"
    else:
        body += "\non deck: nothing"
    # Counts and the ONE on deck. Not a list -- a list is a menu, and a menu is
    # how deciding-one-at-a-time quietly becomes deciding-several.
    return Intent(verb=Verb.QUEUE, text=raw, reply=body,
                  item=nxt if isinstance(nxt, Mapping) else None,
                  payload={"counts": dict(counts), "next_unavailable": next_err})


def _why(raw: str, rest: str, ctx: Any) -> Intent:
    app_id = rest.split(" ")[0] if rest else ""
    if not app_id or not _looks_like_id(app_id):
        return _refuse(Verb.WHY, raw, "Which one? Use 'why <id>'.")
    try:
        item = _ask(ctx, "get", app_id)
    except ContextUnavailable as e:
        return _refuse(Verb.WHY, raw, f"I cannot look that up right now: {e}", app_id=app_id)
    if item is None:
        return _refuse(Verb.WHY, raw, f"I have no application with id {app_id!r}.", app_id=app_id)
    if not isinstance(item, Mapping):
        return _refuse(Verb.WHY, raw, f"get({app_id!r}) returned a {type(item).__name__}.",
                       app_id=app_id)

    state = str(item.get("state") or "")
    reason = item.get("weakest_reason")
    judge = item.get("weakest")
    if state == "screened_out":
        why = item.get("screen_reason") or (item.get("meta") or {}).get("screen_reason")
        if not why:
            return _refuse(Verb.WHY, raw,
                           f"{_label(item)} was screened out and no reason was recorded. "
                           "That is a bug in the scout, not a verdict I can explain.",
                           app_id=app_id, item=item)
        return Intent(verb=Verb.WHY, text=raw, item=item, app_id=app_id,
                      reply=f"{_label(item)}\nscreened out before building: {why}")

    if not reason:
        # A number is not an explanation. If the grading step recorded a score but
        # no reason, that is a hole in the grading step, and reporting the score
        # as though it were the reason is the failure this refuses to commit.
        return _refuse(Verb.WHY, raw,
                       f"{_label(item)} has no recorded reason from the panel"
                       + (f" (it scored {item['score']})." if item.get("score") is not None else ".")
                       + " I will not hand you a number and call it an explanation — "
                         "re-run the grading step for this one.",
                       app_id=app_id, item=item)

    lines = [_describe(item), f"weakest — {judge or 'judge'}: {reason}"]
    if item.get("raw_score") is not None:
        lines.append(f"raw panel {item['raw_score']} (before the interview-vote lift)")
    if state == "below_bar":
        lines.append("It did not clear its tier's bar, so it was never offered for "
                     "approval. Rebuild it if you want another pass at it.")
    if item.get("claims"):
        lines += ["asserting:"] + [f"  • {c}" for c in item["claims"]]
    return Intent(verb=Verb.WHY, text=raw, item=item, app_id=app_id, reply="\n".join(lines))


def _skip(raw: str, app_id: str, ctx: Any) -> Intent:
    try:
        item = _ask(ctx, "get", app_id)
    except ContextUnavailable as e:
        return _refuse(Verb.SKIP, raw, f"I cannot look that up right now, so I have skipped "
                                       f"nothing: {e}", app_id=app_id)
    if item is None:
        return _refuse(Verb.SKIP, raw, f"I have no application with id {app_id!r}, so I "
                                       f"skipped nothing.", app_id=app_id)
    return Intent(verb=Verb.SKIP, text=raw, app_id=app_id,
                  item=item if isinstance(item, Mapping) else None,
                  decision="reject",
                  reply=f"Skipped {_label(item) if isinstance(item, Mapping) else app_id}.")


def _goals(raw: str, ctx: Any) -> Intent:
    try:
        goals = _ask(ctx, "goals")
    except ContextUnavailable as e:
        return _refuse(Verb.GOALS, raw, f"I cannot read your goals right now: {e}")
    if not goals:
        return Intent(verb=Verb.GOALS, text=raw, payload={"goals": None, "unset": True},
                      reply="You have not told me what you are looking for yet. "
                            "Say 'set ...' and I will propose something.")
    return Intent(verb=Verb.GOALS, text=raw, payload={"goals": goals},
                  reply="\n".join(f"{k}: {v}" for k, v in _flatten(goals).items()))


def _unpack_refinement(result: Any) -> tuple[Any, Any]:
    """Split `refine_goals`'s return into (proposed, diff).

    The production function returns `(goals, Diff)`; a context may inject a
    plainer refiner that returns the mapping alone. Anything else is handed
    through untouched so the Mapping check below refuses it by name rather than
    this helper guessing at a shape."""
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1]
    return result, None


def _set(raw: str, phrase: str, ctx: Any) -> Intent:
    if not phrase:
        return _refuse(Verb.SET, raw, "Set what? e.g. 'set only remote roles above $180k'.")
    try:
        current = _ask(ctx, "goals")
    except ContextUnavailable as e:
        return _refuse(Verb.SET, raw, f"I cannot read your current goals, so I will not "
                                      f"propose a change to them: {e}")
    refiner = _attr(ctx, "refine_goals")
    if not callable(refiner):
        try:
            from openrecruiter.interview import refine_goals as refiner  # deferred
        except Exception as e:                               # noqa: BLE001
            return _refuse(Verb.SET, raw,
                           f"I cannot refine goals right now: refine_goals is unavailable "
                           f"({e}). Nothing was changed.")
    try:
        # (goals, instruction) -- the production order. This call used to be
        # refiner(phrase, current), which raised "goals should be a dict from
        # build_goals, got str" against the real function every single time. It
        # failed loud rather than wrong, which is the right direction to fail,
        # but the verb had never worked outside a test that injected its own.
        result = refiner(current, phrase)
    except Exception as e:                                   # noqa: BLE001
        return _refuse(Verb.SET, raw, f"refine_goals failed on {phrase!r}: "
                                      f"{e.__class__.__name__}: {e}. Nothing was changed.")
    proposed, diff = _unpack_refinement(result)
    # interview.refine_goals hands back the ORIGINAL goals whenever any clause was
    # unreadable, so an unparsed instruction arrives here as a zero-line diff and
    # would be reported as "that would not change anything" -- a plain misreading
    # of "I could not understand you", and the one place a half-understood
    # sentence could be waved through as understood.
    if diff is not None and not getattr(diff, "ok", True):
        unread = ", ".join(repr(u) for u in (getattr(diff, "unparsed", ()) or ()))
        return _refuse(Verb.SET, raw,
                       f"I could not read {unread or 'part of that'} in {phrase!r}, so I "
                       f"have changed nothing and am not proposing a diff. Say it another "
                       f"way and I will show you what it would do.")
    if not isinstance(proposed, Mapping) or not proposed:
        return _refuse(Verb.SET, raw, f"I could not turn {phrase!r} into a goal change. "
                                      f"Nothing was changed.")
    lines = diff_goals(current, proposed)
    if not lines:
        return _refuse(Verb.SET, raw, f"{phrase!r} would not change anything about your "
                                      f"current search, so I have left it alone.")
    return Intent(verb=Verb.SET, text=raw, needs_confirmation=True,
                  payload={"phrase": phrase, "current": current, "proposed": dict(proposed),
                           "diff": lines, "confirmed": False},
                  reply="\n".join(["This is what would change:", *lines, "",
                                   "Apply it? Y / N"]))


def _confirm_refinement(raw: str, pending: Mapping[str, Any], verdict: str) -> Intent:
    proposed = pending.get("proposed") if isinstance(pending, Mapping) else None
    if verdict == "reject":
        return Intent(verb=Verb.SET_CANCELLED, text=raw, payload={"proposed": proposed},
                      reply="Left your goals as they were.")
    if not isinstance(proposed, Mapping) or not proposed:
        return _refuse(Verb.SET_CONFIRMED, raw,
                       "I no longer have the change you are confirming, so I have not "
                       "applied anything. Send the 'set ...' again.")
    return Intent(verb=Verb.SET_CONFIRMED, text=raw,
                  payload={"proposed": dict(proposed), "diff": pending.get("diff") or [],
                           "confirmed": True},
                  reply="Updated. New postings are scored against that from now on.")


def _fallback(raw: str, ctx: Any, note: str | None = None) -> Intent:
    """Not a command. Hand the agent the context it needs to just answer.

    Never an error listing commands: a recruiter you can only address in verbs is
    a form, and the whole point of this surface is that it is not one."""
    payload: dict[str, Any] = {"note": note}
    try:
        payload["counts"] = dict(_ask(ctx, "stats") or {})
    except (ContextUnavailable, TypeError, ValueError) as e:
        payload["counts"] = None
        payload["counts_unavailable"] = str(e)
    try:
        payload["goals"] = _ask(ctx, "goals")
    except ContextUnavailable as e:
        payload["goals"] = None
        payload["goals_unavailable"] = str(e)
    payload["outstanding_card_id"] = _attr(ctx, "outstanding_card_id")
    payload["paused"] = bool(_attr(ctx, "paused", False))
    return Intent(verb=Verb.FALLBACK, text=raw, payload=payload, reply="")


# -- the hard guarantee -------------------------------------------------------

def apply_refinement(intent: Intent) -> dict:
    """Return the goals to persist -- only from a CONFIRMED refinement.

    A field on a dataclass is advisory; a caller can ignore it. This raises, so
    the only way to persist a goal change is through the diff the human saw."""
    if intent.verb is not Verb.SET_CONFIRMED or not intent.payload.get("confirmed"):
        raise NotConfirmed(
            f"refusing to apply a goal change that was not confirmed "
            f"(verb={intent.verb.value}, confirmed={bool(intent.payload.get('confirmed'))})")
    proposed = intent.payload.get("proposed")
    if not isinstance(proposed, Mapping) or not proposed:
        raise NotConfirmed("confirmed intent carries no proposed goals")
    return dict(proposed)


__all__ = ["Verb", "Intent", "Context", "ContextUnavailable", "NotConfirmed",
           "route", "apply_refinement", "diff_goals", "help_text"]
