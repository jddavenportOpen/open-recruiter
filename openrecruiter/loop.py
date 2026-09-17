"""The work loop: one application, start to finish, then stop and look around.

Three properties are load-bearing, and each one is here because its absence was
expensive somewhere else:

**One at a time, structurally.** `run_once` takes no count, no limit, and no
collection. It picks a single application, carries it as far as it can go, and
returns. `run_forever` is a `while` around `run_once` with a pause in it. There
is nowhere to put a number that would make it do more per cycle, because the
human decision sits inside the cycle. That is the volume control, and it is not
a setting.

**A stop that stays stopped.** The estate this was extracted from had a deliberate
shutdown reversed by a self-healer inside eleven hours. So the kill switch here
is a file the loop can create but has no verb to remove, it is LATCHED in memory
the moment it is seen (deleting the file under a running loop does not restart
it), and an unreadable switch reads as ENGAGED. It is checked before every
application and again after the human says yes, so a stop pressed while a card
is outstanding still prevents the submit.

**A crash cannot invent a receipt.** `recover` runs before every pick. A row left
in BUILDING is retryable -- nothing left the machine. A row left in SUBMITTING is
moved to SUBMITTED_UNVERIFIED and held for a human, and is never picked again,
because after a crash mid-submit we genuinely do not know whether the form went
through, and the only unrecoverable mistake available here is applying twice.

Single runner. Two of these against one store would both "recover" each other's
in-flight rows, so `run_forever` takes an advisory lock and refuses to start
beside a live sibling.
"""
from __future__ import annotations

import dataclasses
import enum
import os
import sys
import time
from typing import Callable

from openrecruiter import quota
from openrecruiter.channels.base import Decision
from openrecruiter.engine import panel
from openrecruiter.store import Application, State, Store


# -- the stop ------------------------------------------------------------------

def _home() -> str:
    return os.environ.get("OPENRECRUITER_HOME") or os.path.expanduser("~/.openrecruiter")


def default_stop_path() -> str:
    return os.environ.get("OPENRECRUITER_STOP") or os.path.join(_home(), "STOP")


def default_lock_path() -> str:
    return os.environ.get("OPENRECRUITER_LOCK") or os.path.join(_home(), "runner.lock")


class KillSwitch:
    """A stop the loop can press and cannot release.

    There is deliberately no `release`, `clear`, `reset` or `resume` method: the
    only way back is a human deleting the file. A test asserts that absence, and
    asserts the loop source contains no call that would remove the file anyway.

    Latching matters as much as the file does. Once this process has seen the
    switch engaged it stays engaged for the life of the process, so a watchdog or
    a "self-healer" that tidies the file away cannot quietly un-stop a runner
    that is already mid-flight.
    """

    def __init__(self, path: str | None = None):
        self.path = path or default_stop_path()
        self._latched: str | None = None

    def engaged(self) -> str | None:
        """The reason, or None. Unreadable counts as engaged -- fail closed."""
        if self._latched:
            return self._latched
        try:
            with open(self.path) as fh:
                reason = fh.read().strip() or "stopped (no reason given)"
        except FileNotFoundError:
            return None
        except OSError as e:
            reason = f"stop file unreadable ({e}) -- treating as ENGAGED"
        self._latched = reason
        return reason

    def engage(self, reason: str = "") -> str:
        """Press stop. Safe for anyone to call, including the loop itself:
        engaging can only ever reduce what happens next."""
        reason = (reason or "stopped").strip()
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "w") as fh:
            fh.write(reason + "\n")
        self._latched = reason
        return reason


class AlreadyRunning(RuntimeError):
    pass


class RunnerLock:
    """Advisory single-runner lock. Steals a lock whose owner is gone, refuses one
    whose owner is alive -- two runners would double-apply."""

    def __init__(self, path: str | None = None):
        self.path = path or default_lock_path()
        self._held = False

    def acquire(self, notify=None) -> "RunnerLock":
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = self._owner()
            if owner is not None and _pid_alive(owner):
                raise AlreadyRunning(
                    f"another runner (pid {owner}) holds {self.path}; "
                    "two runners against one store would apply twice")
            if notify:
                notify(f"stale runner lock from pid {owner} -- taking it over")
            fd = os.open(self.path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        self._held = True
        return self

    def _owner(self) -> int | None:
        try:
            with open(self.path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        if self._held:
            try:
                os.unlink(self.path)     # the LOCK, never the stop file
            except OSError:
                pass
            self._held = False
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# -- results -------------------------------------------------------------------

class Outcome(str, enum.Enum):
    HALTED = "halted"                  # the kill switch, or a blind quota read
    IDLE = "idle"                      # nothing to work on
    PAUSED = "paused"                  # at the quota ceiling, or walled
    BELOW_BAR = "below_bar"            # built, failed its gate, never offered
    RE_ASK = "re_ask"                  # no clear answer; the card stands
    DECLINED = "declined"
    EXPIRED = "expired"                # the consent window closed
    SUBMITTED = "submitted"            # and the confirmation was read back
    HELD = "held"                      # may have sent; a human has to look
    FAILED = "failed"
    STALE_APPROVAL = "stale_approval"  # a yes too old to act on


@dataclasses.dataclass(frozen=True)
class Result:
    outcome: Outcome
    app_id: str | None = None
    state: State | None = None
    detail: str = ""
    resume_at: float | None = None


@dataclasses.dataclass(frozen=True)
class Report:
    cycles: int
    stopped: str
    results: list


@dataclasses.dataclass(frozen=True)
class Asked:
    """What `deps.ask` returns. A bare `Decision` is accepted too."""
    decision: Decision
    card_id: str | None = None
    channel: str | None = None


def _stderr_notify(text: str) -> None:
    print(f"[openrecruiter] {text}", file=sys.stderr)


@dataclasses.dataclass
class Deps:
    """Everything the loop needs from the outside, injected so tests are instant.

    build(app)  -> {"passed": bool, "panel": <panel payload>, "score", "raw_score",
                    "weakest", "weakest_reason", "claims", "resume_path", "tier"}
    ask(app)    -> Decision | Asked
    submit(app) -> {"sent": bool|None, "verified": bool, "note": str,
                    "retryable": bool}
    quota_reader() -> quota windows (raises quota.QuotaUnknown when blind)
    """
    build: Callable[[Application], dict]
    ask: Callable[[Application], object]
    submit: Callable[[Application], dict]
    quota_reader: Callable[[], object]
    killswitch: KillSwitch
    notify: Callable[[str], None] = _stderr_notify
    ceiling: float = quota.DEFAULT_CEILING
    # A yes older than this is not acted on. Consent to send an application is
    # perishable; a crash on Friday must not post it on Monday.
    consent_max_age_s: float | None = 86400.0
    reask_after_s: float = 21600.0
    max_retries: int = 3
    # An unclear answer moves nothing, so asking again in the next cycle gets the
    # same non-answer -- and the row sits at the HEAD of the queue while it does,
    # so one unanswered card stalls every other application indefinitely. After
    # this many unclear answers the card is parked to be re-asked later. It is a
    # bound on re-asking, not on applying: nothing here lets a cycle do more.
    max_reasks: int = 3
    wall_sleep_s: float = 900.0
    idle_sleep_s: float = 300.0
    blind_backoff_s: float = 60.0
    max_blind_quota_reads: int = 3
    # Can only ever slow the runner down. There is no knob in the other direction.
    min_gap_s: float = 0.0
    lock_path: str | None = None


_RESULT_TAIL = 200

# Outcomes that leave the store exactly as they found it. Running the next cycle
# immediately would read the same rows and reach the same conclusion, so each one
# has to cost real time: an unpaced fixed point is a hot loop around
# deps.quota_reader(), which in production shells a real CLI run and burns the
# quota this module exists to pace. The other outcomes all move a row, and the
# ones that can repeat (a retryable FAILED) are bounded by deps.max_retries.
#
# RE_ASK and STALE_APPROVAL are also each cut off at the source -- bounded
# re-asking in _ask, and parking in _pick -- because pacing a fixed point only
# makes it a slow one. This is the backstop, and it holds for any future outcome
# added to the set.
_NON_ADVANCING = frozenset({Outcome.IDLE, Outcome.RE_ASK, Outcome.STALE_APPROVAL})


class GateUnverifiable(RuntimeError):
    """The pass/fail of a build could not be checked. Never offered to a human."""


# -- crash recovery ------------------------------------------------------------

_RETRYABLE = "retryable: "
_HELD_AFTER_CRASH = ("interrupted mid-submit -- we do not know whether the form "
                     "went through, so it is held for a human rather than retried")


def recover(store: Store, notify=_stderr_notify) -> list:
    """Bring transient rows back to a legal resting state. Idempotent.

    BUILDING  -> FAILED, marked retryable. Nothing left the machine.
    SUBMITTING-> SUBMITTED_UNVERIFIED. Something may have. The asymmetry is the
                 point: the cheap mistake is rebuilding a resume, the expensive
                 one is a second application to an employer who counts them.
    """
    moved = []
    for app in store.by_state(State.BUILDING):
        store.transition(app.id, State.FAILED,
                         _RETRYABLE + "interrupted while building")
        moved.append((app.id, State.FAILED))
    for app in store.by_state(State.SUBMITTING):
        store.transition(app.id, State.SUBMITTED_UNVERIFIED, _HELD_AFTER_CRASH)
        notify(f"{app.id}: {_HELD_AFTER_CRASH}")
        moved.append((app.id, State.SUBMITTED_UNVERIFIED))
    return moved


# -- picking -------------------------------------------------------------------

def _last_change(store: Store, app_id: str) -> float:
    hist = store.history(app_id)
    return hist[-1]["at"] if hist else 0.0


def _retry_count(store: Store, app_id: str) -> int:
    return sum(1 for e in store.history(app_id)
               if e["to_state"] == State.FAILED.value and (e["note"] or "").startswith(_RETRYABLE))


def _consent_age(app: Application, now: float) -> float | None:
    return None if app.approved_at is None else now - app.approved_at


def _pick(store: Store, deps: Deps, now: float, reported: set, skipped: list):
    """The next single application, or None.

    Ordered by obligation: finish a yes the human already gave, then re-ask a
    card that went cold, then answer one that is outstanding, then retry a build
    that died for a reason that was not the resume's fault, then build a new one.
    """
    for app in store.by_state(State.APPROVED):
        age = _consent_age(app, now)
        if deps.consent_max_age_s is None or age is None or age <= deps.consent_max_age_s:
            return app
        # A stale yes cannot be submitted and cannot be re-asked while it sits in
        # APPROVED, which makes the row a FIXED POINT: with nothing else queued
        # the runner returned STALE_APPROVAL every cycle forever, calling
        # deps.quota_reader() each time -- which in production shells a real CLI
        # run, so the spin burns the very budget this module paces. Measured at
        # 50 cycles, 50 quota reads, zero sleeps. Park it in EXPIRED, the exit
        # the store keeps for exactly this, and the card is re-asked instead.
        store.transition(app.id, State.EXPIRED,
                         f"approved {age / 3600:.1f}h ago, outside the consent window")
        skipped.append(app.id)
        if app.id not in reported:
            reported.add(app.id)
            deps.notify(
                f"{app.id}: approved {age / 3600:.1f}h ago, older than the "
                f"{deps.consent_max_age_s / 3600:.1f}h consent window -- NOT submitting. "
                "The card will be re-asked; a yes this old is not acted on.")
    for app in store.by_state(State.EXPIRED):
        if now - _last_change(store, app.id) >= deps.reask_after_s:
            return app
    for app in store.by_state(State.AWAITING_APPROVAL):
        return app
    for app in store.by_state(State.FAILED):
        hist = store.history(app.id)
        if hist and (hist[-1]["note"] or "").startswith(_RETRYABLE) \
                and _retry_count(store, app.id) < deps.max_retries:
            return app
    return store.next_to_build()


# -- the gate ------------------------------------------------------------------

def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _gate(app: Application, result) -> tuple:
    """Decide, here, whether this resume may be shown to a human.

    The builder's own `passed` flag is never trusted on its own: it is ANDed with
    a pass this module derives itself, so a builder bug can only ever withhold a
    card, never produce one. A gate that cannot be evaluated raises -- BELOW_BAR
    is terminal by design, and "we could not check" must not become "it passed".
    """
    if not isinstance(result, dict):
        raise GateUnverifiable(f"build returned {type(result).__name__}, not a result mapping")

    claimed = result.get("passed")
    if claimed is not None and not isinstance(claimed, bool):
        raise GateUnverifiable(f"`passed` is {claimed!r}, which is not a verdict")

    fields = {}
    for key in ("weakest", "weakest_reason", "resume_path"):
        if result.get(key) is not None:
            fields[key] = result[key]
    if result.get("claims") is not None:
        fields["claims"] = list(result["claims"])

    payload = result.get("panel")
    # Which bar this is judged against takes TWO INDEPENDENT READS -- what the
    # row says and what the build result claims -- and the STRICTER one wins.
    # Same shape, and the same reason, as apply.is_reach_target: a mislabelled
    # tier must not be a way past this. The build result is a claim by the thing
    # being judged, so it may raise its own bar and never lower the row's.
    declared = str(result.get("tier") or "").strip().lower()
    on_row = str(app.tier or "").strip().lower()
    tier = "reach" if "reach" in (declared, on_row) else (declared or on_row or "standard")

    if isinstance(payload, dict) and payload.get("personas"):
        payload = dict(payload)
        # The EMPLOYER is a fact about the row, never a field of the build
        # result. panel.is_reach() keys the whole bar off this one string --
        # threshold, raw floor, and the majority interview vote -- so a builder
        # that sent its own `company` was choosing its own pass mark. setdefault
        # let it: it filled the gap only when the builder stayed silent, so a
        # builder that spoke always won. Measured on an Anthropic row at panel
        # 85 / raw 72: omitting company gave below_bar, sending "" or "Acme"
        # gave a card and a submitted application.
        payload["company"] = app.company
        agg = panel.aggregate(payload)                      # IncompletePanel is a refusal
        derived = bool(agg["overall_pass"])
        if tier == "reach" and not agg["is_reach"]:
            # The row declares a reach employer the panel's list has never heard
            # of. Honour the stricter of the two, never the looser.
            derived = (derived and agg["interview_votes"] >= 2
                       and agg["panel_avg"] >= panel.REACH_THRESHOLD
                       and agg["raw_panel_avg"] >= panel.RAW_FLOOR_REACH)
        fields["score"] = agg["panel_avg"]
        fields["raw_score"] = agg["raw_panel_avg"]
        fields.setdefault("weakest", agg["weakest_persona"])
        fields.setdefault("weakest_reason",
                          (agg["panel"].get(agg["weakest_persona"]) or {}).get("reason", ""))
        if agg["is_reach"]:
            tier = "reach"
    else:
        if tier == "reach":
            raise GateUnverifiable(
                "a reach employer needs the full panel payload: its bar includes a "
                "majority interview vote, which cannot be checked from a score alone")
        score, raw = _num(result.get("score")), _num(result.get("raw_score"))
        if score is None or raw is None:
            raise GateUnverifiable(
                "no panel payload and no numeric score/raw_score -- the gate cannot "
                "be evaluated, so this is a refusal, not a pass")
        derived = score >= panel.DEFAULT_THRESHOLD and raw >= panel.RAW_FLOOR_DEFAULT
        fields["score"], fields["raw_score"] = score, raw

    fields["tier"] = tier
    passed = derived and (claimed is not False)
    if claimed is not None and claimed != derived:
        fields["_disagreement"] = ("builder said pass, gate says fail" if claimed
                                   else "builder said fail, gate says pass")

    if passed and not (fields.get("weakest_reason") or "").strip():
        raise GateUnverifiable(
            "a passing build with no weakest-judge reason would make a card that is "
            "a filename and a number -- which is a click, not a decision")
    if passed and not fields.get("claims"):
        raise GateUnverifiable(
            "a passing build that lists no claims gives the human nothing to check "
            "before the resume asserts it on their behalf")
    return passed, fields


# -- one application, end to end -----------------------------------------------

def _paused_by_wall(deps: Deps, text: str, now: float, app_id=None) -> Result:
    """A wall is a time, not an error. Find the reset rather than retrying."""
    resume_at = None
    try:
        windows = quota.normalize_windows(deps.quota_reader())
    except Exception:
        windows = {}
    kind = quota.wall_window_kind(text)
    if kind and kind in windows and windows[kind]["resets_at"]:
        resume_at = windows[kind]["resets_at"]
    else:
        resets = [w["resets_at"] for w in windows.values()
                  if w["resets_at"] and w["utilization"] >= deps.ceiling]
        if resets:
            resume_at = max(resets)
    deps.notify(f"plan wall: {text.strip()[:200]}")
    return Result(Outcome.PAUSED, app_id=app_id, detail="plan wall", resume_at=resume_at)


def _build(store: Store, deps: Deps, app: Application, now: float, reasks: dict) -> Result:
    store.transition(app.id, State.BUILDING, "build")
    try:
        result = deps.build(app)
    except Exception as e:                       # noqa: BLE001 -- classified below
        kind = quota.classify_failure(f"{type(e).__name__}: {e}")
        note = (_RETRYABLE if kind in (quota.PLAN_WALL, quota.SERVER_BLIP) else "") + \
            f"build raised ({kind}): {e}"
        store.transition(app.id, State.FAILED, note[:500])
        if kind == quota.PLAN_WALL:
            return _paused_by_wall(deps, str(e), now, app.id)
        deps.notify(f"{app.id}: build failed ({kind}): {e}")
        return Result(Outcome.FAILED, app.id, State.FAILED, f"build raised ({kind}): {e}")

    try:
        passed, fields = _gate(app, result)
    except GateUnverifiable as e:
        store.transition(app.id, State.FAILED, f"gate unverifiable: {e}"[:500])
        deps.notify(f"{app.id}: gate could not be evaluated -- not offered. {e}")
        return Result(Outcome.FAILED, app.id, State.FAILED, f"gate unverifiable: {e}")
    except panel.IncompletePanel as e:
        store.transition(app.id, State.FAILED, f"incomplete panel: {e}"[:500])
        deps.notify(f"{app.id}: incomplete panel -- not offered. {e}")
        return Result(Outcome.FAILED, app.id, State.FAILED, f"incomplete panel: {e}")

    disagreement = fields.pop("_disagreement", None)
    if disagreement:
        deps.notify(f"{app.id}: {disagreement} -- taking the cautious reading")
    if not passed:
        store.transition(app.id, State.BELOW_BAR, "failed its tier's gate", **fields)
        return Result(Outcome.BELOW_BAR, app.id, State.BELOW_BAR,
                      "below the bar for its tier; never offered")
    app = store.transition(app.id, State.AWAITING_APPROVAL, "passed the gate", **fields)
    return _ask(store, deps, app, now, reasks)


def _as_asked(value) -> Asked:
    if isinstance(value, Asked):
        return value
    if isinstance(value, Decision):
        return Asked(value)
    if isinstance(value, tuple) and value and isinstance(value[0], Decision):
        return Asked(*value)
    raise TypeError(f"ask returned {value!r}, which is not a Decision")


def _ask(store: Store, deps: Deps, app: Application, now: float, reasks: dict) -> Result:
    a = _as_asked(deps.ask(app))
    marks = {k: v for k, v in (("card_id", a.card_id), ("channel", a.channel)) if v}
    if a.decision is Decision.APPROVE:
        stop = deps.killswitch.engaged()
        if stop:
            # Stopped while the card was outstanding. The yes is real and the row
            # stays where it is; what must not happen is the submit.
            return Result(Outcome.HALTED, app.id, app.state, f"stopped before submit: {stop}")
        app = store.transition(app.id, State.APPROVED, "approved by the human",
                               approved_at=now, **marks)
        return _submit(store, deps, app, now)
    if a.decision is Decision.REJECT:
        store.transition(app.id, State.DECLINED, "declined by the human", **marks)
        return Result(Outcome.DECLINED, app.id, State.DECLINED, "declined")
    if a.decision is Decision.TIMEOUT:
        store.transition(app.id, State.EXPIRED, "no answer inside the consent window", **marks)
        return Result(Outcome.EXPIRED, app.id, State.EXPIRED, "consent window closed; will re-ask")
    # AMBIGUOUS: the channel has already re-asked. Nothing moves, and silence is
    # never read as a yes.
    #
    # "Nothing moves" is also what makes this a fixed point. The row stays in
    # AWAITING_APPROVAL, which _pick takes ahead of any new build, so the next
    # cycle asks the same question about the same row -- forever, at full speed,
    # and no other application is ever built. Measured at 40 cycles: 40 asks,
    # zero sleeps, the second application never touched. So re-asking is bounded
    # here, and past the bound the card is PARKED rather than abandoned: EXPIRED
    # is re-asked after `reask_after_s`, so the question stays open while the
    # queue moves. Parking is not an answer and cannot become one -- the only
    # way out of EXPIRED is being asked again.
    asked = reasks.get(app.id, 0) + 1
    reasks[app.id] = asked
    if asked >= deps.max_reasks:
        store.transition(app.id, State.EXPIRED,
                         f"no clear answer after {asked} asks; parked to re-ask later")
        deps.notify(f"{app.id}: no clear yes or no after {asked} asks -- parking the card. "
                    "It will be re-asked; nothing is submitted in the meantime.")
        return Result(Outcome.EXPIRED, app.id, State.EXPIRED,
                      f"no clear answer after {asked} asks; parked, will re-ask")
    return Result(Outcome.RE_ASK, app.id, app.state, "no clear yes or no; card stands")


def _submit(store: Store, deps: Deps, app: Application, now: float) -> Result:
    stop = deps.killswitch.engaged()
    if stop:
        return Result(Outcome.HALTED, app.id, app.state, f"stopped before submit: {stop}")
    store.transition(app.id, State.SUBMITTING, "submitting")
    try:
        res = deps.submit(app) or {}
    except Exception as e:                       # noqa: BLE001
        note = f"submit raised: {e} -- {_HELD_AFTER_CRASH}"
        store.transition(app.id, State.SUBMITTED_UNVERIFIED, note[:500])
        deps.notify(f"{app.id}: {note}")
        return Result(Outcome.HELD, app.id, State.SUBMITTED_UNVERIFIED, note)

    if not isinstance(res, dict):
        note = f"submit returned {type(res).__name__} -- {_HELD_AFTER_CRASH}"
        store.transition(app.id, State.SUBMITTED_UNVERIFIED, note[:500])
        deps.notify(f"{app.id}: {note}")
        return Result(Outcome.HELD, app.id, State.SUBMITTED_UNVERIFIED, note)

    sent = res.get("sent", True)
    note = str(res.get("note") or "")
    if sent is False:
        # Only the submitter can assert nothing was sent, and only it can say
        # whether trying again is safe. The loop never decides that for it.
        prefix = _RETRYABLE if res.get("retryable") else ""
        store.transition(app.id, State.FAILED, (prefix + f"not sent: {note}")[:500])
        deps.notify(f"{app.id}: not sent -- {note}")
        return Result(Outcome.FAILED, app.id, State.FAILED, f"not sent: {note}")
    if res.get("verified") is True:
        store.transition(app.id, State.SUBMITTED_VERIFIED, note or "confirmation read back")
        return Result(Outcome.SUBMITTED, app.id, State.SUBMITTED_VERIFIED,
                      note or "confirmation read back")
    held = note or "submitted, but no confirmation was read back -- 'we clicked submit' is not 'we sent it'"
    store.transition(app.id, State.SUBMITTED_UNVERIFIED, held[:500])
    deps.notify(f"{app.id}: {held}")
    return Result(Outcome.HELD, app.id, State.SUBMITTED_UNVERIFIED, held)


def run_once(store: Store, deps: Deps, now: float | None = None,
             _reported: set | None = None, _reasks: dict | None = None) -> Result:
    """Process EXACTLY ONE application, as far as it can legally go, then return.

    There is no parameter here that means "more than one", and adding one would
    require moving the human decision out of the cycle.
    """
    now = time.time() if now is None else now
    reported = _reported if _reported is not None else set()
    # How many unclear answers each card has given THIS process. Carried across
    # cycles by run_forever; a bare run_once starts fresh, which is the cautious
    # direction -- it can only ask once more, never park sooner.
    reasks = _reasks if _reasks is not None else {}

    stop = deps.killswitch.engaged()
    if stop:
        return Result(Outcome.HALTED, detail=stop)

    recover(store, deps.notify)

    windows = deps.quota_reader()                # QuotaUnknown propagates: blind is a refusal
    paused, resume_at = quota.should_pause(windows, deps.ceiling)
    if paused:
        kind, util = quota.headroom(windows)
        return Result(Outcome.PAUSED, detail=f"{kind} at {util:.0%} (ceiling {deps.ceiling:.0%})",
                      resume_at=resume_at)

    skipped: list = []
    app = _pick(store, deps, now, reported, skipped)
    if app is None:
        if skipped:
            # Visible, not silent: a yes we will not act on is a row a human has
            # to decide about, and IDLE would bury it. _pick has already parked
            # the row in EXPIRED, so this reports once and then the loop idles
            # rather than reporting the same stranded yes forever.
            return Result(Outcome.STALE_APPROVAL, skipped[0], State.EXPIRED,
                          "approved outside the consent window; not submitting, will re-ask")
        return Result(Outcome.IDLE, detail="nothing to work on")

    if app.state in (State.DISCOVERED, State.FAILED):
        return _build(store, deps, app, now, reasks)
    if app.state is State.EXPIRED:
        app = store.transition(app.id, State.AWAITING_APPROVAL, "re-asking after expiry")
        return _ask(store, deps, app, now, reasks)
    if app.state is State.AWAITING_APPROVAL:
        return _ask(store, deps, app, now, reasks)
    if app.state is State.APPROVED:
        return _submit(store, deps, app, now)
    # _pick returns nothing else; if it ever does, refuse loudly rather than
    # inventing a transition.
    raise RuntimeError(f"{app.id}: picked in state {app.state.value}, which the loop does not handle")


def run_forever(store: Store, deps: Deps, clock=time.time, sleeper=time.sleep,
                max_cycles: int | None = None) -> Report:
    """`run_once` in a `while`, paced against real quota.

    `max_cycles` bounds a test run; it can only stop the loop sooner. Sleeping is
    to `resetsAt` rather than a blind retry, and a quota read we cannot make is
    retried a few times and then halts -- a runner that cannot see its own budget
    does not get to keep applying.
    """
    results, blind, cycles = [], 0, 0
    stopped = "cycle limit reached"
    lock = RunnerLock(deps.lock_path) if deps.lock_path != "" else None
    if lock:
        lock.acquire(deps.notify)
    try:
        reported: set = set()
        reasks: dict = {}
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            stop = deps.killswitch.engaged()
            if stop:
                results.append(Result(Outcome.HALTED, detail=stop))
                stopped = f"kill switch: {stop}"
                break
            try:
                r = run_once(store, deps, now=clock(), _reported=reported, _reasks=reasks)
            except quota.QuotaUnknown as e:
                blind += 1
                deps.notify(f"quota unreadable ({blind}/{deps.max_blind_quota_reads}): {e}")
                if blind >= deps.max_blind_quota_reads:
                    results.append(Result(Outcome.HALTED, detail=f"quota unreadable: {e}"))
                    stopped = f"quota unreadable {blind}x -- halted rather than applying blind"
                    break
                sleeper(deps.blind_backoff_s * blind)
                continue
            blind = 0
            results.append(r)
            if len(results) > _RESULT_TAIL:      # a forever loop must not grow forever
                del results[0]
            if r.outcome is Outcome.HALTED:
                stopped = f"halted: {r.detail}"
                break
            if r.outcome is Outcome.PAUSED:
                sleeper(quota.sleep_for(r.resume_at, clock(), deps.wall_sleep_s))
                continue
            if r.outcome in _NON_ADVANCING:
                sleeper(max(deps.idle_sleep_s, deps.min_gap_s))
                continue
            if deps.min_gap_s:
                sleeper(deps.min_gap_s)
    finally:
        if lock:
            lock.__exit__(None, None, None)
    return Report(cycles=cycles, stopped=stopped, results=results)
