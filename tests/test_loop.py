"""Invariants for the work loop and for quota pacing.

Every test here is written to fail if the behaviour it names is REMOVED, not
merely if it is renamed. The suite this project was forked from stayed green
while its central invariant was deleted, so a test that would pass against an
empty implementation is treated here as a defect.

Three of these exist specifically because their absence was expensive:

  * confusing a server blip with a plan wall (opposite correct responses),
  * a kill switch a self-healer could clear (it happened, within 11 hours),
  * a crash mid-submit that gets retried (the one unrecoverable mistake in this
    system is applying twice).
"""
from __future__ import annotations

import inspect
import os
import tempfile
import time
import unittest

from openrecruiter import loop, quota
from openrecruiter.channels.base import Decision
from openrecruiter.store import State, Store


# -- fixtures ------------------------------------------------------------------

def _personas(score, vote=True):
    dims = {k: {"score": score} for k in
            ("quantified_impact_credibility", "keyword_requirement_coverage",
             "experience_domain_relevance", "target_employer_convention_fit",
             "structure_clarity_execution")}
    return {p: {"dimensions": dict(dims), "would_interview": vote,
                "reason": "the summary buries the platform work"}
            for p in ("hiring_manager", "recruiter", "ai_systems_rep")}


def build_result(score=88, passed=True, vote=True, **over):
    out = {"passed": passed,
           "panel": {"personas": _personas(score, vote)},
           "claims": ["$20M annualized revenue"],
           "weakest_reason": "the summary buries the platform work",
           "resume_path": "/tmp/r.pdf"}
    out.update(over)
    return out


CANONICAL_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "rateLimitType": "seven_day",
        "utilization": 0.84,
        "resetsAt": 1789736400,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.13, "resetsAt": 1789700000},
            "seven_day": {"utilization": 0.84, "resetsAt": 1789736400},
        },
    },
}

HEALTHY = {"five_hour": {"utilization": 0.10, "resets_at": None},
           "seven_day": {"utilization": 0.20, "resets_at": None}}


class Recorder:
    """Records every call so a test can assert what did NOT happen."""

    def __init__(self, build=None, ask=Decision.APPROVE, submit=None, windows=HEALTHY):
        self._build = build if build is not None else (lambda app: build_result())
        self._ask = ask
        self._submit = submit if submit is not None else (lambda app: {"verified": True})
        self._windows = windows
        self.builds, self.asks, self.submits, self.notes = [], [], [], []

    def build(self, app):
        self.builds.append(app.id)
        return self._build(app) if callable(self._build) else self._build

    def ask(self, app):
        self.asks.append(app.id)
        return self._ask(app) if callable(self._ask) else self._ask

    def submit(self, app):
        self.submits.append(app.id)
        return self._submit(app) if callable(self._submit) else self._submit

    def quota_reader(self):
        return self._windows() if callable(self._windows) else self._windows

    def notify(self, text):
        self.notes.append(text)


class Clock:
    def __init__(self, t=None):
        self.t = time.time() if t is None else t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class CycleSpy:
    """A per-cycle snapshot of "did anything actually happen".

    `run_once` reads the quota exactly once, before it picks anything, so this
    doubles as a hook at the top of every cycle. Each mark records how many
    events the store has written and how many times the loop has slept, both as
    of that moment -- so consecutive marks say whether the cycle between them
    moved a row or cost any time.
    """

    def __init__(self, store, clock, windows=HEALTHY):
        self.store, self.clock, self.windows = store, clock, windows
        self.marks = []

    def events(self):
        return self.store.db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]

    def quota_reader(self):
        self.marks.append((self.events(), len(self.clock.slept)))
        return self.windows


class LoopCase(unittest.TestCase):
    def assert_no_hot_spin(self, spy):
        """Every cycle either moved a row or cost real time.

        A cycle that does neither is a fixed point: the next one reads the same
        rows and reaches the same conclusion, immediately, forever. The cost is
        not the wasted CPU -- it is that each lap calls `deps.quota_reader()`,
        which in production shells a real CLI run and burns the very budget the
        pacer exists to protect.
        """
        self.assertGreater(len(spy.marks), 1, "the spy saw fewer than two cycles")
        for i in range(1, len(spy.marks)):
            moved = spy.marks[i][0] != spy.marks[i - 1][0]
            slept = spy.marks[i][1] != spy.marks[i - 1][1]
            self.assertTrue(
                moved or slept,
                f"cycle {i} changed nothing in the store and slept for nothing -- "
                "a fixed point spinning on deps.quota_reader()")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orloop-")
        self.store = Store(os.path.join(self.tmp, "s.db"))
        self.addCleanup(self.store.close)
        self.ks = loop.KillSwitch(os.path.join(self.tmp, "STOP"))

    def seed(self, n=1, tier="standard", company="Acme"):
        for i in range(n):
            self.store.upsert_discovered(f"a{i}", company, "Principal PM",
                                         f"https://example.com/{i}", tier=tier)

    def deps(self, rec=None, **over):
        rec = rec or Recorder()
        self.rec = rec
        kw = dict(build=rec.build, ask=rec.ask, submit=rec.submit,
                  quota_reader=rec.quota_reader, killswitch=self.ks,
                  notify=rec.notify, lock_path=os.path.join(self.tmp, "lock"))
        kw.update(over)
        return loop.Deps(**kw)


# -- quota: reading the event --------------------------------------------------

class ReadingTheRateLimitEvent(unittest.TestCase):
    def test_the_canonical_event_parses(self):
        ev = quota.parse_rate_limit_event(CANONICAL_EVENT)
        self.assertEqual(ev["kind"], "seven_day")
        self.assertAlmostEqual(ev["utilization"], 0.84)
        self.assertEqual(ev["resets_at"], 1789736400)
        self.assertAlmostEqual(ev["windows"]["five_hour"]["utilization"], 0.13)

    def test_it_parses_from_a_json_line(self):
        import json
        ev = quota.parse_rate_limit_event(json.dumps(CANONICAL_EVENT))
        self.assertEqual(ev["kind"], "seven_day")

    def test_the_opus_weekly_cap_is_a_window_of_its_own(self):
        """seven_day_opus is a SEPARATE cap. Reading only five_hour/seven_day
        walks into it at full speed."""
        ev = quota.parse_rate_limit_event({
            "type": "rate_limit_event",
            "rate_limit_info": {"rateLimitType": "five_hour", "utilization": 0.1,
                                "resetsAt": 100,
                                "unifiedWindows": {
                                    "five_hour": {"utilization": 0.1, "resetsAt": 100},
                                    "seven_day": {"utilization": 0.2, "resetsAt": 200},
                                    "seven_day_opus": {"utilization": 0.97, "resetsAt": 900}}}})
        self.assertIn("seven_day_opus", ev["windows"])
        pause, resume = quota.should_pause(ev)
        self.assertTrue(pause, "the opus weekly cap was not read at all")
        self.assertEqual(resume, 900)

    def test_an_ordinary_line_is_not_an_event(self):
        for line in ("", "hello", "{}", '{"type":"assistant","message":{}}', 42, None):
            self.assertIsNone(quota.parse_rate_limit_event(line), repr(line))

    def test_a_truncated_event_raises_rather_than_looking_like_an_ordinary_line(self):
        """Returning None here is the failure this module exists to prevent: a
        corrupt quota event would be indistinguishable from prose output."""
        with self.assertRaises(quota.MalformedRateLimitEvent):
            quota.parse_rate_limit_event('{"type":"rate_limit_event","rate_limit_in')

    def test_an_event_missing_its_numbers_raises(self):
        for info in ({}, {"rateLimitType": "five_hour"},
                     {"rateLimitType": "five_hour", "utilization": None}):
            with self.assertRaises(quota.MalformedRateLimitEvent):
                quota.parse_rate_limit_event({"type": "rate_limit_event",
                                              "rate_limit_info": info})

    def test_an_event_with_no_info_object_raises(self):
        """Not None: an event whose body we cannot find is a failed reading, and
        the caller must not mistake it for an ordinary line of output."""
        for body in ({}, {"rate_limit_info": None}, {"rate_limit_info": "seven_day"},
                     {"rate_limit_info": []}):
            with self.assertRaises(quota.MalformedRateLimitEvent):
                quota.parse_rate_limit_event(dict(body, type="rate_limit_event"))

    def test_an_event_that_names_no_window_raises(self):
        with self.assertRaises(quota.MalformedRateLimitEvent):
            quota.parse_rate_limit_event({
                "type": "rate_limit_event",
                "rate_limit_info": {"unifiedWindows": {
                    "five_hour": {"utilization": 0.1, "resetsAt": 1},
                    "seven_day": {"utilization": 0.2, "resetsAt": 2}}}})

    def test_a_boolean_is_not_a_utilization(self):
        with self.assertRaises(quota.MalformedRateLimitEvent):
            quota.parse_rate_limit_event({"type": "rate_limit_event",
                                          "rate_limit_info": {"rateLimitType": "five_hour",
                                                              "utilization": True}})

    def test_percent_and_millisecond_producers_are_normalized(self):
        ev = quota.parse_rate_limit_event({
            "type": "rate_limit_event",
            "rate_limit_info": {"rateLimitType": "seven_day", "utilization": 84,
                                "resetsAt": 1789736400000}})
        self.assertAlmostEqual(ev["utilization"], 0.84)
        self.assertEqual(ev["resets_at"], 1789736400)

    def test_a_stream_with_no_event_is_a_refusal_not_a_green_light(self):
        with self.assertRaises(quota.QuotaUnknown):
            quota.windows_from_stream(["hello", '{"type":"assistant"}'])

    def test_a_stream_with_an_event_yields_its_windows(self):
        import json
        ws = quota.windows_from_stream(["noise", json.dumps(CANONICAL_EVENT)])
        self.assertEqual(set(ws), {"five_hour", "seven_day"})


class StoppingAtTheCeilingNotTheWall(unittest.TestCase):
    def test_the_ceiling_is_below_the_wall(self):
        self.assertTrue(quota.should_pause({"seven_day": 0.91})[0])
        self.assertFalse(quota.should_pause({"seven_day": 0.89})[0])
        self.assertTrue(quota.should_pause({"seven_day": 0.80}, ceiling=0.75)[0])

    def test_resume_is_the_latest_breaching_reset(self):
        """Waking when the five-hour window clears would walk straight back into
        the weekly one."""
        pause, resume = quota.should_pause(
            {"five_hour": {"utilization": 0.95, "resets_at": 100},
             "seven_day": {"utilization": 0.99, "resets_at": 9000}})
        self.assertTrue(pause)
        self.assertEqual(resume, 9000)

    def test_a_window_below_the_ceiling_does_not_set_the_resume_time(self):
        pause, resume = quota.should_pause(
            {"five_hour": {"utilization": 0.95, "resets_at": 100},
             "seven_day": {"utilization": 0.10, "resets_at": 9_000_000}})
        self.assertEqual((pause, resume), (True, 100))

    def test_no_windows_is_a_refusal_not_headroom(self):
        """An empty read and a healthy read must never be the same answer."""
        for blind in ({}, None, []):
            with self.assertRaises(quota.QuotaUnknown):
                quota.should_pause(blind)

    def test_an_unknown_window_kind_still_counts(self):
        self.assertTrue(quota.should_pause({"thirty_day": 0.99})[0],
                        "a cap we have not heard of is still a cap")

    def test_a_nonsense_ceiling_is_refused(self):
        for bad in (0, -1, 1.5, "0.9", True, None):
            with self.assertRaises(ValueError):
                quota.should_pause({"seven_day": 0.5}, ceiling=bad)


class BlipAndWallAreOpposites(unittest.TestCase):
    def test_a_wall_is_a_wall(self):
        for t in ("Claude usage limit reached. Your limit will reset at 5pm.",
                  "You've reached your 5-hour limit",
                  "weekly limit reached for this plan",
                  "Opus limit reached — switch models or wait"):
            self.assertEqual(quota.classify_failure(t), quota.PLAN_WALL, t)

    def test_the_overload_disclaimer_is_a_blip_even_though_it_says_usage_limit(self):
        """THE bug this function exists for. The blip copy contains the wall's
        own keywords; substring order decides whether we sleep for six days."""
        for t in ("Overloaded (this is not your usage limit)",
                  "API Error: this error is unrelated to your usage limit",
                  "529 overloaded_error — does not count against your usage limit"):
            self.assertEqual(quota.classify_failure(t), quota.SERVER_BLIP, t)

    def test_the_disclaimer_is_checked_BEFORE_the_wall_keywords(self):
        """Presence is not order, and only order is load-bearing here.

        Deleting the disclaimer tier is caught. DEMOTING it -- one swap, nothing
        removed, `_WALL` tested first -- passed all 490 tests, because every
        other fixture in this file carries exactly one signal and so classifies
        the same in either order. A message needs BOTH to tell the orders apart,
        and the real one does: Anthropic's overload copy names the limit it is
        disclaiming. Under the inversion this transient 529 becomes a plan wall
        and parks the runner until a reset that was never coming.
        """
        both = ("API Error 529 overloaded_error: this error is unrelated to your "
                "usage limit and does not affect your weekly limit")
        self.assertTrue(any(w in both for w in quota._WALL),
                        "the fixture lost its wall keyword -- it would now pass in "
                        "either order and guard nothing")
        self.assertTrue(any(d in both for d in quota._BLIP_DISCLAIMERS),
                        "the fixture lost its disclaimer")
        self.assertEqual(quota.classify_failure(both), quota.SERVER_BLIP,
                         "the wall keywords were checked before the disclaimer")

    def test_a_wall_delivered_as_429_is_still_a_wall(self):
        """429 is also a blip code. If the code wins, every wall gets hammered."""
        self.assertEqual(
            quota.classify_failure("HTTP 429: usage limit reached, resets at 18:00"),
            quota.PLAN_WALL)

    def test_transient_http_failures_are_blips(self):
        for t in ("API error 503", "http 502 bad gateway", "status 500",
                  "Overloaded", "connection reset by peer", "read timeout"):
            self.assertEqual(quota.classify_failure(t), quota.SERVER_BLIP, t)

    def test_unclassified_is_not_transient(self):
        """'other' must not be a synonym for 'retry'. An empty message lands here
        on purpose -- a check that could not run is a refusal."""
        for t in ("", "   ", None, 7, "TypeError: NoneType is not subscriptable",
                  "compensation: $500,000 base"):
            self.assertEqual(quota.classify_failure(t), quota.OTHER, repr(t))

    def test_the_wall_message_names_its_window(self):
        self.assertEqual(quota.wall_window_kind("your 5-hour limit is reached"), "five_hour")
        self.assertEqual(quota.wall_window_kind("weekly limit reached"), "seven_day")
        self.assertEqual(quota.wall_window_kind("Opus limit reached"), "seven_day_opus")
        self.assertIsNone(quota.wall_window_kind("something else entirely"))

    def test_sleeping_is_bounded(self):
        self.assertEqual(quota.sleep_for(1000, 400, 60), 600)
        self.assertEqual(quota.sleep_for(None, 400, 60), 60)
        self.assertEqual(quota.sleep_for(10 ** 12, 0, 60), 3600, "a bogus reset must cost an hour, not a decade")
        self.assertEqual(quota.sleep_for(100, 400, 60), 1.0)


# -- the loop: one at a time ---------------------------------------------------

class ExactlyOneApplicationPerCycle(LoopCase):
    def test_run_once_processes_one_and_leaves_the_rest_alone(self):
        self.seed(3)
        d = self.deps()
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.SUBMITTED)
        self.assertEqual(len(self.rec.builds), 1)
        self.assertEqual(len(self.rec.asks), 1)
        self.assertEqual(len(self.rec.submits), 1)
        self.assertEqual(self.store.stats().get("discovered"), 2,
                         "run_once touched more than one application")

    def test_run_forever_advances_one_per_cycle(self):
        self.seed(3)
        d = self.deps()
        c = Clock()
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=2)
        self.assertEqual(rep.cycles, 2)
        self.assertEqual(len(self.rec.submits), 2)
        self.assertEqual(self.store.stats().get("discovered"), 1)

    def test_a_cycle_bound_of_zero_does_nothing(self):
        self.seed(3)
        d = self.deps()
        c = Clock()
        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=0)
        self.assertEqual(self.rec.builds, [])

    def test_no_entry_point_takes_a_quantity(self):
        """There is no parameter that would mean 'more than one this cycle'."""
        forbidden = {"limit", "count", "batch", "n", "how_many", "max_applications",
                     "apps", "applications", "items", "queue", "size", "per_cycle"}
        for fn in (loop.run_once, loop.run_forever, loop.recover):
            params = set(inspect.signature(fn).parameters)
            self.assertEqual(params & forbidden, set(),
                             f"{fn.__name__} grew a quantity parameter")

    def test_the_package_defines_no_decision_verb_that_means_many(self):
        FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                     "auto_approve", "approve_above", "batch_approve", "approve_batch")
        for mod in (loop, quota):
            src = inspect.getsource(mod).lower()
            for bad in FORBIDDEN:
                self.assertNotIn(bad, src, f"{mod.__name__} mentions {bad}")


# -- the loop: the gate --------------------------------------------------------

class TheGateStandsBetweenTheResumeAndTheHuman(LoopCase):
    def test_a_build_below_the_bar_is_never_offered(self):
        self.seed(1)
        d = self.deps(Recorder(build=lambda app: build_result(score=40, passed=False)))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.BELOW_BAR)
        self.assertEqual(self.rec.asks, [], "a resume that failed its gate reached a human")
        self.assertEqual(self.store.get("a0").state, State.BELOW_BAR)

    def test_a_builder_claiming_a_pass_on_a_failing_panel_is_still_refused(self):
        """The builder's own verdict is never the gate. It can withhold a card,
        never produce one."""
        self.seed(1)
        d = self.deps(Recorder(build=lambda app: build_result(score=30, passed=True)))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.BELOW_BAR)
        self.assertEqual(self.rec.asks, [])

    def test_a_pass_that_cannot_be_checked_is_a_refusal(self):
        self.seed(1)
        d = self.deps(Recorder(build=lambda app: {"passed": True,
                                                  "claims": ["x"],
                                                  "weakest_reason": "y"}))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.FAILED)
        self.assertEqual(self.rec.asks, [], "an unverifiable gate was treated as a pass")

    def test_a_reach_employer_needs_the_full_panel(self):
        """Its bar includes a majority interview vote, which a bare score cannot
        show."""
        self.seed(1, tier="reach")
        d = self.deps(Recorder(build=lambda app: {"passed": True, "score": 95,
                                                  "raw_score": 95, "claims": ["x"],
                                                  "weakest_reason": "y"}))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.FAILED)
        self.assertEqual(self.rec.asks, [])

    def test_a_declared_reach_row_is_held_to_the_reach_bar(self):
        """Score 80 clears the standard bar and not the reach one. The row says
        reach; the looser reading must not win."""
        self.seed(1, tier="reach", company="Not In The List Inc")
        d = self.deps(Recorder(build=lambda app: build_result(score=80)))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.BELOW_BAR)
        self.assertEqual(self.rec.asks, [])

    def test_an_incomplete_panel_is_not_a_low_score(self):
        bad = build_result()
        bad["panel"]["personas"].pop("recruiter")
        self.seed(1)
        d = self.deps(Recorder(build=lambda app: bad))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.FAILED)
        self.assertEqual(self.rec.asks, [])

    def test_the_build_result_does_not_get_to_say_who_the_employer_is(self):
        """panel.is_reach() keys the ENTIRE bar off the company string, so a
        builder that names its own employer picks its own pass mark.

        The row is Anthropic at panel 85 / raw 72: it clears the standard bar
        (70/68) and fails the reach one (90/80 plus two votes). Filling company
        with setdefault made this depend on whether the builder stayed silent --
        omitting it gave below_bar, sending "" or "Acme" gave a card and a
        submitted application. The company is read from the row now, so all
        three behave the same.
        """
        for sent in (None, "", "Acme", "a0", "Definitely Not Anthropic"):
            with self.subTest(company_sent_by_builder=sent):
                store = Store(os.path.join(self.tmp, f"gate-{sent!r}.db"))
                self.addCleanup(store.close)
                store.upsert_discovered("a0", "Anthropic", "Principal PM",
                                        "https://example.com/0", tier="standard")

                def build(app, sent=sent):
                    out = build_result(score=72)
                    if sent is not None:
                        out["panel"]["company"] = sent
                    return out

                d = self.deps(Recorder(build=build))
                r = loop.run_once(store, d, now=time.time())
                self.assertIs(r.outcome, loop.Outcome.BELOW_BAR)
                self.assertEqual(self.rec.asks, [],
                                 "the build result named its own employer and got a card")
                self.assertEqual(self.rec.submits, [])
                self.assertEqual(store.get("a0").state, State.BELOW_BAR)

    def test_the_build_result_cannot_declare_its_way_down_to_the_standard_bar(self):
        """Two independent reads of the tier, and the stricter wins.

        The row says reach; the build result says standard. Score 80 clears the
        standard bar and not the reach one, so letting the build result's own
        label win is the same hole as naming its own employer -- a mislabelled
        tier must not be a way past this.
        """
        self.seed(1, tier="reach", company="Not In The List Inc")
        d = self.deps(Recorder(build=lambda app: build_result(score=80, tier="standard")))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.BELOW_BAR)
        self.assertEqual(self.rec.asks, [], "a reach row was judged at the standard bar")

    def test_a_card_with_no_reason_and_no_claims_is_refused(self):
        """A card that is a filename and a number turns reading into clicking."""
        self.seed(1)
        d = self.deps(Recorder(build=lambda app: {"passed": True, "score": 88,
                                                  "raw_score": 88}))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.FAILED)
        self.assertEqual(self.rec.asks, [])


# -- the loop: decisions -------------------------------------------------------

class OneDecisionPerApplication(LoopCase):
    def test_a_decline_submits_nothing(self):
        self.seed(1)
        d = self.deps(Recorder(ask=Decision.REJECT))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.DECLINED)
        self.assertEqual(self.rec.submits, [])
        self.assertEqual(self.store.get("a0").state, State.DECLINED)

    def test_silence_expires_and_is_re_asked_later_not_immediately(self):
        self.seed(1)
        rec = Recorder(ask=Decision.TIMEOUT)
        d = self.deps(rec, reask_after_s=3600)
        t = time.time()
        self.assertIs(loop.run_once(self.store, d, now=t).outcome, loop.Outcome.EXPIRED)
        self.assertEqual(self.store.get("a0").state, State.EXPIRED)
        self.assertIs(loop.run_once(self.store, d, now=t + 60).outcome, loop.Outcome.IDLE)
        self.assertEqual(len(rec.asks), 1, "an expired card was re-asked immediately")
        loop.run_once(self.store, d, now=t + 7200)
        self.assertEqual(len(rec.asks), 2, "an expired card was never re-asked")

    def test_an_unclear_answer_leaves_the_card_standing_and_submits_nothing(self):
        self.seed(1)
        d = self.deps(Recorder(ask=Decision.AMBIGUOUS))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.RE_ASK)
        self.assertEqual(self.rec.submits, [])
        self.assertEqual(self.store.get("a0").state, State.AWAITING_APPROVAL)

    def test_an_unverified_submission_is_held_for_a_human(self):
        self.seed(1)
        d = self.deps(Recorder(submit=lambda app: {"verified": False}))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.HELD)
        self.assertEqual(self.store.get("a0").state, State.SUBMITTED_UNVERIFIED)

    def test_a_submission_that_never_left_is_a_failure_not_a_submission(self):
        self.seed(1)
        d = self.deps(Recorder(submit=lambda app: {"sent": False, "note": "form 500ed"}))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.FAILED)
        self.assertEqual(self.store.get("a0").state, State.FAILED)


# -- the loop: the stop --------------------------------------------------------

class AStopThatStaysStopped(LoopCase):
    def test_it_is_checked_before_every_application(self):
        self.seed(3)
        rec = Recorder()
        d = self.deps(rec)
        c = Clock()
        # engage on the way out of the first application
        original = rec.submit

        def submit_then_stop(app):
            out = original(app)
            self.ks.engage("JD pressed stop")
            return out
        d.submit = submit_then_stop
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=5)
        self.assertEqual(len(rec.builds), 1, "the loop kept applying after the stop")
        self.assertIn("kill switch", rep.stopped)

    def test_a_stop_pressed_while_the_card_is_outstanding_prevents_the_submit(self):
        self.seed(1)

        def ask(app):
            self.ks.engage("stopped while the card was out")
            return Decision.APPROVE
        d = self.deps(Recorder(ask=ask))
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.HALTED)
        self.assertEqual(self.rec.submits, [], "a stop was pressed and it submitted anyway")

    def test_a_stop_pressed_between_the_yes_and_the_submit_still_stops_it(self):
        """The second check, on the resume path where nobody was asked anything:
        run_once enters with the row already APPROVED, and the human presses stop
        while it is picking."""
        now = time.time()
        self.seed(1)
        self.store.transition("a0", State.BUILDING, "build")
        self.store.transition("a0", State.AWAITING_APPROVAL, "gate", score=88, raw_score=88)
        self.store.transition("a0", State.APPROVED, "yes", approved_at=now)

        real = self.ks

        class PressedWhilePicking(loop.KillSwitch):
            """Clear on the first look, engaged by the time we would submit."""

            def __init__(self):
                super().__init__(real.path)
                self.looks = 0

            def engaged(self):
                self.looks += 1
                return None if self.looks == 1 else "JD pressed stop"

        ks = PressedWhilePicking()
        d = self.deps(killswitch=ks)
        r = loop.run_once(self.store, d, now=now)
        self.assertIs(r.outcome, loop.Outcome.HALTED)
        self.assertEqual(self.rec.submits, [], "it submitted after the stop was pressed")
        self.assertEqual(self.store.get("a0").state, State.APPROVED)

    def test_the_loop_has_no_way_to_release_it(self):
        verbs = {"release", "clear", "reset", "resume", "disable", "unset",
                 "disengage", "off", "unlock", "delete", "remove"}
        names = {n.lower() for n, _ in inspect.getmembers(loop.KillSwitch)}
        self.assertEqual(names & verbs, set(), "the kill switch grew a release verb")
        src = inspect.getsource(loop)
        for call in ("os.remove", "os.unlink(self.path)", "shutil.rmtree"):
            self.assertNotIn(call, src.replace("os.unlink(self.path)     # the LOCK, never the stop file", ""),
                             f"the loop can delete files: {call}")

    def test_the_stop_survives_the_run(self):
        self.seed(1)
        self.ks.engage("stay stopped")
        d = self.deps()
        c = Clock()
        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=3)
        self.assertTrue(os.path.exists(self.ks.path), "the loop removed its own stop file")
        self.assertEqual(self.rec.builds, [])

    def test_deleting_the_file_under_a_running_loop_does_not_restart_it(self):
        """A self-healer tidying the file away must not un-stop a live runner."""
        self.ks.engage("stopped")
        self.assertTrue(self.ks.engaged())
        os.unlink(self.ks.path)
        self.assertTrue(self.ks.engaged(), "the stop was cleared out from under the loop")

    def test_an_unreadable_stop_reads_as_engaged(self):
        ks = loop.KillSwitch(self.tmp)          # a directory: open() raises OSError
        self.assertTrue(ks.engaged(), "a switch we cannot read must fail closed")

    def test_a_second_runner_is_refused(self):
        lock = loop.RunnerLock(os.path.join(self.tmp, "lock")).acquire()
        self.addCleanup(lock.__exit__, None, None, None)
        self.seed(1)
        d = self.deps()
        with self.assertRaises(loop.AlreadyRunning):
            loop.run_forever(self.store, d, clock=Clock(), sleeper=lambda s: None,
                             max_cycles=1)


# -- the loop: pacing ----------------------------------------------------------

class PacingAgainstRealQuota(LoopCase):
    def test_it_pauses_at_the_ceiling_and_sleeps_to_the_reset(self):
        self.seed(1)
        c = Clock()
        windows = {"seven_day": {"utilization": 0.93, "resets_at": c.t + 1800}}
        d = self.deps(Recorder(windows=windows))
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=1)
        self.assertIs(rep.results[0].outcome, loop.Outcome.PAUSED)
        self.assertEqual(self.rec.builds, [], "it kept working past the ceiling")
        self.assertEqual(len(c.slept), 1)
        self.assertAlmostEqual(c.slept[0], 1800, delta=1)

    def test_below_the_ceiling_it_works(self):
        self.seed(1)
        d = self.deps(Recorder(windows={"seven_day": {"utilization": 0.89, "resets_at": 1}}))
        self.assertIs(loop.run_once(self.store, d, now=time.time()).outcome,
                      loop.Outcome.SUBMITTED)

    def test_a_blind_quota_read_halts_rather_than_applying_blind(self):
        self.seed(1)

        def blind():
            raise quota.QuotaUnknown("no rate_limit_event in the stream")
        d = self.deps(Recorder(windows=blind), max_blind_quota_reads=3, blind_backoff_s=1)
        c = Clock()
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=20)
        self.assertEqual(self.rec.builds, [], "it applied while blind to its own budget")
        self.assertIn("blind", rep.stopped)
        self.assertEqual(rep.cycles, 3)

    def test_a_plan_wall_sleeps_to_the_reset_instead_of_retrying(self):
        self.seed(1)
        c = Clock()
        # Utilization still reads healthy -- reporting lags, and the wall arrives
        # anyway. The reset time is the only thing worth acting on.
        windows = {"five_hour": {"utilization": 0.50, "resets_at": c.t + 2400}}

        def walled(app):
            raise RuntimeError("Claude usage limit reached — your 5-hour limit resets at 6pm")
        rec = Recorder(build=walled, windows=windows)
        d = self.deps(rec)
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=1)
        self.assertIs(rep.results[0].outcome, loop.Outcome.PAUSED)
        self.assertAlmostEqual(rep.results[0].resume_at, windows["five_hour"]["resets_at"])
        self.assertEqual(len(rec.builds), 1, "it retried into the wall")
        self.assertAlmostEqual(c.slept[0], 2400, delta=1)

    def test_a_server_blip_is_retried_promptly_and_not_slept_off(self):
        """The opposite response to the wall above, from a message that also
        contains the words 'usage limit'."""
        self.seed(1)

        def blip(app):
            raise RuntimeError("Overloaded (this is not your usage limit)")
        rec = Recorder(build=blip)
        d = self.deps(rec, min_gap_s=0)
        c = Clock()
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=2)
        self.assertEqual([r.outcome for r in rep.results],
                         [loop.Outcome.FAILED, loop.Outcome.FAILED])
        self.assertEqual(len(rec.builds), 2, "a blip was not retried")
        self.assertEqual(c.slept, [], "a blip was slept off like a wall")

    def test_retries_are_bounded(self):
        self.seed(1)

        def blip(app):
            raise RuntimeError("503 service unavailable")
        rec = Recorder(build=blip)
        d = self.deps(rec, max_retries=2)
        c = Clock()
        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=6)
        self.assertEqual(len(rec.builds), 2, "a failing build was retried forever")


class ACycleEitherMovesARowOrCostsTime(LoopCase):
    """Fixed points. `run_forever` paced HALTED, PAUSED and IDLE and nothing
    else, so every other non-advancing outcome fell through to a `min_gap_s`
    that is 0.0 by default -- an unpaced `while True` around the quota reader."""

    def test_an_unanswered_card_is_bounded_and_does_not_block_the_queue(self):
        """AMBIGUOUS moves nothing, and the row it does not move sits at the
        head of the queue.

        Measured before the fix: 40 cycles, 40 asks of the same card, 40 quota
        reads, zero sleeps, and the second application never built at all.
        """
        self.seed(2)
        c = Clock()
        spy = CycleSpy(self.store, c)
        rec = Recorder(ask=Decision.AMBIGUOUS)
        d = self.deps(rec, quota_reader=spy.quota_reader, max_reasks=3)
        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=40)

        self.assertEqual(len(rec.asks), 6,
                         "the same unanswered card was re-asked every cycle")
        self.assertEqual(sorted(set(rec.builds)), ["a0", "a1"],
                         "an unanswered card blocked every other application")
        self.assertEqual(rec.submits, [], "an unclear answer was acted on")
        self.assertEqual(self.store.stats().get("expired"), 2,
                         "the parked cards did not end somewhere they can be re-asked")
        self.assertNotIn(State.APPROVED.value, self.store.stats())
        self.assert_no_hot_spin(spy)

    def test_a_stranded_yes_is_parked_rather_than_spun_on(self):
        """A yes too old to act on cannot be submitted and cannot be re-asked
        while it sits in APPROVED, so it is a permanent fixed point.

        Measured before the fix: 50 cycles, 50 STALE_APPROVAL results, 50 quota
        reads, zero sleeps, the row still APPROVED at the end.
        """
        now = time.time()
        self.seed(1)
        self.store.transition("a0", State.BUILDING, "build")
        self.store.transition("a0", State.AWAITING_APPROVAL, "gate", score=88, raw_score=88)
        self.store.transition("a0", State.APPROVED, "yes", approved_at=now - 3 * 86400)

        c = Clock(now)
        spy = CycleSpy(self.store, c)
        rec = Recorder()
        d = self.deps(rec, quota_reader=spy.quota_reader, consent_max_age_s=86400)
        rep = loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=50)

        outcomes = [r.outcome for r in rep.results]
        self.assertEqual(outcomes.count(loop.Outcome.STALE_APPROVAL), 1,
                         "the same stranded yes was reported on every cycle")
        self.assertEqual(self.store.get("a0").state, State.EXPIRED,
                         "the stale approval stayed a fixed point in APPROVED")
        self.assertEqual(rec.submits, [], "a stale yes was submitted")
        self.assert_no_hot_spin(spy)

    def test_parking_a_card_leaves_the_question_open_and_never_answers_it(self):
        """Both fixes park a row in EXPIRED rather than leaving it to spin.

        Parking has to be a pause on the ASKING, never a verdict: the card comes
        back to a human, and only a real yes moves it. If parking could close the
        question, the bound above would have turned "no clear answer" into a
        silent decline -- or worse, into consent.
        """
        self.seed(1)
        unclear = [Decision.AMBIGUOUS] * 3
        rec = Recorder(ask=lambda app: unclear.pop(0) if unclear else Decision.APPROVE)
        c = Clock()
        d = self.deps(rec, reask_after_s=300, max_reasks=3)

        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=3)
        self.assertEqual(self.store.get("a0").state, State.EXPIRED)
        self.assertEqual(len(rec.asks), 3)
        self.assertEqual(rec.submits, [], "a parked card was submitted without an answer")

        # Time passes. The card is put back in front of a human, and this time
        # the human says yes.
        loop.run_forever(self.store, d, clock=c, sleeper=c.sleep, max_cycles=1)
        self.assertEqual(len(rec.asks), 4, "a parked card was never re-asked")
        self.assertEqual(rec.submits, ["a0"])
        self.assertEqual(self.store.get("a0").state, State.SUBMITTED_VERIFIED)


# -- the loop: crash safety ----------------------------------------------------

class ACrashCannotInventAReceipt(LoopCase):
    def _park(self, state):
        self.seed(1)
        self.store.transition("a0", State.BUILDING, "build")
        if state is State.BUILDING:
            return
        self.store.transition("a0", State.AWAITING_APPROVAL, "gate", score=88, raw_score=88)
        self.store.transition("a0", State.APPROVED, "yes", approved_at=time.time())
        self.store.transition("a0", State.SUBMITTING, "submitting")

    def test_a_crash_while_building_is_retryable(self):
        self._park(State.BUILDING)
        loop.recover(self.store, notify=lambda t: None)
        self.assertEqual(self.store.get("a0").state, State.FAILED)
        d = self.deps()
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.SUBMITTED, "a crashed build was never retried")

    def test_a_crash_mid_submit_is_held_and_never_re_submitted(self):
        """We do not know whether the form went through. Applying twice is the
        one unrecoverable mistake available here."""
        self._park(State.SUBMITTING)
        loop.recover(self.store, notify=lambda t: None)
        self.assertEqual(self.store.get("a0").state, State.SUBMITTED_UNVERIFIED)
        d = self.deps()
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.IDLE)
        self.assertEqual(self.rec.submits, [], "a crashed submit was sent a second time")

    def test_recovery_runs_before_work_and_leaves_no_transient_row(self):
        self.seed(2)
        self.store.transition("a0", State.BUILDING, "build")
        d = self.deps()
        loop.run_once(self.store, d, now=time.time())
        states = self.store.stats()
        self.assertNotIn(State.BUILDING.value, states)
        self.assertNotIn(State.SUBMITTING.value, states)

    def test_recovery_is_idempotent(self):
        self._park(State.SUBMITTING)
        first = loop.recover(self.store, notify=lambda t: None)
        second = loop.recover(self.store, notify=lambda t: None)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_a_submit_that_raises_is_held_not_retried(self):
        self.seed(1)

        def boom(app):
            raise RuntimeError("the browser died after clicking submit")
        rec = Recorder(submit=boom)
        d = self.deps(rec)
        r = loop.run_once(self.store, d, now=time.time())
        self.assertIs(r.outcome, loop.Outcome.HELD)
        self.assertEqual(self.store.get("a0").state, State.SUBMITTED_UNVERIFIED)
        loop.run_once(self.store, d, now=time.time())
        self.assertEqual(len(rec.submits), 1, "an unknown submit outcome was retried")


class ConsentIsPerishable(LoopCase):
    def _approve_at(self, when):
        self.seed(1)
        self.store.transition("a0", State.BUILDING, "build")
        self.store.transition("a0", State.AWAITING_APPROVAL, "gate", score=88, raw_score=88)
        self.store.transition("a0", State.APPROVED, "yes", approved_at=when)

    def test_a_yes_from_two_days_ago_is_not_acted_on(self):
        now = time.time()
        self._approve_at(now - 2 * 86400)
        d = self.deps(consent_max_age_s=86400)
        r = loop.run_once(self.store, d, now=now)
        self.assertIs(r.outcome, loop.Outcome.STALE_APPROVAL)
        self.assertEqual(self.rec.submits, [], "a stale yes was submitted")
        self.assertTrue(any("consent window" in n for n in self.rec.notes),
                        "a stranded approval was skipped silently")

    def test_a_fresh_yes_is_finished_after_a_restart(self):
        now = time.time()
        self._approve_at(now - 60)
        d = self.deps(consent_max_age_s=86400)
        r = loop.run_once(self.store, d, now=now)
        self.assertIs(r.outcome, loop.Outcome.SUBMITTED)
        self.assertEqual(self.rec.builds, [], "it rebuilt an application already approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
