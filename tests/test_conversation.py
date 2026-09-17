"""Invariants for the conversational surface.

The surface is where a careful system becomes a careless one, because it is the
only place a human types free text at it. Three of these classes exist to fail if
that erodes:

  * a one-word answer must never be captured by a command (and vice versa),
  * nothing gets applied without the human seeing and confirming the diff,
  * no turn may hand back more than one application.

Each test asserts BEHAVIOUR, not shape: remove the precedence rule, the
confirmation gate, or the one-item guard and the corresponding test goes red.
"""
import inspect
import subprocess
import sys
import unittest

from openrecruiter import conversation as conv
from openrecruiter.conversation import Intent, NotConfirmed, Verb, apply_refinement, route


ITEM = {"app_id": "a1", "id": "a1", "company": "Acme", "role": "Principal PM",
        "tier": "standard", "state": "awaiting_approval", "score": 84.0, "raw_score": 79.0,
        "weakest": "recruiter",
        "weakest_reason": "the summary buries the platform work under tooling",
        "claims": ["$20M annualized revenue"]}
OTHER = dict(ITEM, app_id="a2", id="a2", company="Globex", role="Group PM")


def _boom(*_a, **_k):
    raise RuntimeError("the database is locked")


_UNSET = object()          # so goals=None and items=[] mean what they say


class Ctx:
    """A context that answers from memory. No store, no channel, no I/O."""

    def __init__(self, items=_UNSET, counts=_UNSET, goals=_UNSET, card=None,
                 pending=None, paused=False, refiner=None):
        items = [ITEM, OTHER] if items is _UNSET else items
        self.items = {i["app_id"]: i for i in items}
        self.counts = {"discovered": 3, "awaiting_approval": 1} if counts is _UNSET else counts
        self._goals = ({"search": {"comp_min": 0, "locations": ["remote"]}}
                       if goals is _UNSET else goals)
        self.outstanding_card_id = card
        self.pending_refinement = pending
        self.paused = paused
        self.refined = []
        self._refiner = refiner

    def stats(self):
        return dict(self.counts)

    def next_item(self):
        return next(iter(self.items.values()), None)

    def get(self, app_id):
        return self.items.get(app_id)

    def goals(self):
        return self._goals

    def refine_goals(self, goals, instruction):
        # (goals, instruction) -- deliberately the SAME argument order as
        # openrecruiter.interview.refine_goals. A fake whose signature disagrees
        # with the real one is a fake that certifies a call site nobody can use;
        # that is exactly how the reversed call in _set survived a full suite.
        self.refined.append((goals, instruction))
        if self._refiner:
            return self._refiner(goals, instruction)
        return {"search": {"comp_min": 180000, "locations": ["remote"]}}


def _items_in(intent):
    """Every application-shaped mapping the intent hands back, anywhere."""
    found = []
    if intent.item is not None:
        found.append(intent.item)
    for val in (intent.payload or {}).values():
        if conv._is_item_like(val):
            found.append(val)
        elif isinstance(val, (list, tuple, set)):
            found += [v for v in val if conv._is_item_like(v)]
    return found


class EveryVerbRoutes(unittest.TestCase):
    def test_go_and_next_present_the_one_on_deck(self):
        for word in ("go", "next", "/go", "Next please"):
            i = route(word, Ctx())
            self.assertIs(i.verb, Verb.GO, word)
            self.assertEqual(i.app_id, "a1")
            self.assertIn("Principal PM", i.reply)

    def test_go_carries_the_weakest_reason_not_just_a_score(self):
        i = route("go", Ctx())
        self.assertIn("buries the platform work", i.reply)

    def test_queue_and_status_report_counts_and_what_is_on_deck(self):
        for word in ("queue", "status", "pending"):
            i = route(word, Ctx())
            self.assertIs(i.verb, Verb.QUEUE, word)
            self.assertEqual(i.payload["counts"], {"discovered": 3, "awaiting_approval": 1})
            self.assertIn("discovered: 3", i.reply)
            self.assertIn("on deck", i.reply)

    def test_why_names_the_weakest_judge_and_its_actual_reason(self):
        i = route("why a1", Ctx())
        self.assertIs(i.verb, Verb.WHY)
        self.assertTrue(i.ok)
        self.assertIn("recruiter", i.reply)
        self.assertIn("buries the platform work under tooling", i.reply,
                      "why must give the REASON, not a number")

    def test_skip_declines_exactly_the_named_item(self):
        c = Ctx()
        i = route("skip a2", c)
        self.assertIs(i.verb, Verb.SKIP)
        self.assertEqual(i.app_id, "a2")
        self.assertEqual(i.decision, "reject")

    def test_goals_shows_the_current_search(self):
        i = route("goals", Ctx())
        self.assertIs(i.verb, Verb.GOALS)
        self.assertIn("locations", i.reply)

    def test_help_lists_the_verbs(self):
        i = route("help", Ctx())
        self.assertIs(i.verb, Verb.HELP)
        for v in ("go", "queue", "why", "skip", "pause", "goals", "set"):
            self.assertIn(v, i.reply)

    def test_unset_goals_say_so_rather_than_printing_nothing(self):
        i = route("goals", Ctx(goals=None))
        self.assertIs(i.verb, Verb.GOALS)
        self.assertTrue(i.payload["unset"])
        self.assertIn("not told me", i.reply)


class PauseAlwaysHalts(unittest.TestCase):
    """A halt outranks every other reading, and can never send anything."""

    def test_pause_and_stop_halt(self):
        for word in ("pause", "stop", "halt", "pause please", "/stop"):
            i = route(word, Ctx())
            self.assertIs(i.verb, Verb.PAUSE, word)

    def test_stop_with_a_card_open_declines_it_and_never_applies(self):
        i = route("stop", Ctx(card="a1"))
        self.assertIs(i.verb, Verb.PAUSE)
        self.assertTrue(i.payload["declines_outstanding"])
        self.assertNotEqual(i.decision, "approve",
                            "halting must never be readable as consent to send")

    def test_a_sentence_beginning_with_stop_is_not_the_halt_verb(self):
        i = route("stop applying to startups", Ctx())
        self.assertIs(i.verb, Verb.FALLBACK,
                      "a sentence is for the agent to answer, not a command to obey")


class ApprovalKeywordsAreNotCommands(unittest.TestCase):
    """The boundary this surface exists to get right.

    While a card is outstanding, a one-word answer belongs to the CARD. "go" and
    "skip" live in both vocabularies; if the command table won, a bare "go" would
    start a new session instead of answering the application already on screen --
    and a bare "skip" would be read as a command with no target instead of the
    decline the user plainly meant.
    """

    def test_bare_yes_is_a_decision_on_the_outstanding_card(self):
        for word in ("y", "Y", "yes", "yeah", "ok", "\U0001F44D", "1"):
            i = route(word, Ctx(card="a1"))
            self.assertIs(i.verb, Verb.DECISION, word)
            self.assertEqual(i.decision, "approve", word)
            self.assertEqual(i.app_id, "a1")

    def test_bare_no_is_a_decision_on_the_outstanding_card(self):
        for word in ("n", "no", "nope", "\U0001F44E", "0"):
            i = route(word, Ctx(card="a1"))
            self.assertIs(i.verb, Verb.DECISION, word)
            self.assertEqual(i.decision, "reject", word)

    def test_go_while_a_card_is_open_answers_the_card_not_the_queue(self):
        i = route("go", Ctx(card="a1"))
        self.assertIs(i.verb, Verb.DECISION)
        self.assertEqual(i.decision, "approve")
        self.assertIsNone(i.item, "answering a card must not also present a new one")

    def test_bare_skip_while_a_card_is_open_declines_that_card(self):
        i = route("skip", Ctx(card="a1"))
        self.assertIs(i.verb, Verb.DECISION)
        self.assertEqual(i.decision, "reject")
        self.assertEqual(i.app_id, "a1")

    def test_skip_with_an_id_stays_a_command_even_with_a_card_open(self):
        """'skip a2' must not be read as a bare rejection of the open card a1."""
        i = route("skip a2", Ctx(card="a1"))
        self.assertIs(i.verb, Verb.SKIP)
        self.assertEqual(i.app_id, "a2")

    def test_go_with_no_card_open_is_the_command(self):
        i = route("go", Ctx())
        self.assertIs(i.verb, Verb.GO)
        self.assertEqual(i.app_id, "a1")

    def test_a_stray_yes_with_nothing_outstanding_decides_nothing(self):
        i = route("y", Ctx())
        self.assertIs(i.verb, Verb.DECISION)
        self.assertFalse(i.ok, "a yes with nothing pending must not be carried forward")
        self.assertIsNone(i.decision)
        self.assertIsNone(i.item, "a stray yes must not start a session")

    def test_a_qualified_yes_is_not_a_decision(self):
        """Consent is read by the channel layer's one parser, so 'yes but...'
        re-asks here exactly as it does there."""
        i = route("yes but change the summary", Ctx(card="a1"))
        self.assertIsNot(i.verb, Verb.DECISION)
        self.assertIsNone(i.decision)

    def test_decision_strings_match_the_channel_layer(self):
        from openrecruiter.channels.base import Decision
        vals = {d.value for d in Decision}
        for word, expect in (("y", "approve"), ("n", "reject")):
            self.assertIn(route(word, Ctx(card="a1")).decision, vals)
            self.assertEqual(route(word, Ctx(card="a1")).decision, expect)


class FallbackAnswersInsteadOfErroring(unittest.TestCase):
    def test_unrecognized_input_falls_through_with_context(self):
        i = route("what happened with the Globex one, did they ever reply?", Ctx())
        self.assertIs(i.verb, Verb.FALLBACK)
        self.assertEqual(i.payload["counts"], {"discovered": 3, "awaiting_approval": 1})
        self.assertIsNotNone(i.payload["goals"])

    def test_fallback_is_not_an_error_listing_commands(self):
        i = route("how's it going", Ctx())
        self.assertTrue(i.ok)
        self.assertEqual(i.reply, "", "the agent answers; the surface must not scold")
        self.assertNotIn("go / next", i.reply)

    def test_empty_message_falls_back_rather_than_raising(self):
        for t in ("", "   ", None):
            i = route(t, Ctx())
            self.assertIs(i.verb, Verb.FALLBACK, repr(t))

    def test_fallback_marks_unreadable_context_instead_of_reporting_empty(self):
        c = Ctx()
        c.stats = _boom
        i = route("anything at all here", c)
        self.assertIs(i.verb, Verb.FALLBACK)
        self.assertIsNone(i.payload["counts"])
        self.assertIn("counts_unavailable", i.payload,
                      "an unreadable queue must not look like an empty one")


class RefinementNeedsExplicitConfirmationOfTheDiff(unittest.TestCase):
    def test_set_proposes_a_diff_and_applies_nothing(self):
        c = Ctx()
        i = route("set only remote roles above 180k", c)
        self.assertIs(i.verb, Verb.SET)
        self.assertTrue(i.needs_confirmation)
        self.assertFalse(i.payload["confirmed"])
        self.assertTrue(any("comp_min" in line for line in i.payload["diff"]))
        self.assertIn("180000", i.reply)
        self.assertEqual(c.refined[0][1], "only remote roles above 180k")
        self.assertEqual(c._goals["search"]["comp_min"], 0, "nothing may be applied yet")

    def test_the_proposal_cannot_be_applied(self):
        i = route("set remote only", Ctx())
        with self.assertRaises(NotConfirmed):
            apply_refinement(i)

    def test_a_yes_on_the_pending_diff_confirms_it(self):
        c = Ctx()
        proposal = route("set remote only", c)
        c.pending_refinement = proposal.payload
        done = route("y", c)
        self.assertIs(done.verb, Verb.SET_CONFIRMED)
        self.assertEqual(apply_refinement(done)["search"]["comp_min"], 180000)

    def test_a_no_cancels_and_still_cannot_be_applied(self):
        c = Ctx()
        c.pending_refinement = route("set remote only", c).payload
        done = route("n", c)
        self.assertIs(done.verb, Verb.SET_CANCELLED)
        with self.assertRaises(NotConfirmed):
            apply_refinement(done)

    def test_an_unclear_reply_to_the_diff_applies_nothing(self):
        c = Ctx()
        c.pending_refinement = route("set remote only", c).payload
        i = route("hmm maybe, what would that drop?", c)
        self.assertIsNot(i.verb, Verb.SET_CONFIRMED)
        with self.assertRaises(NotConfirmed):
            apply_refinement(i)

    def test_an_outstanding_card_and_a_pending_diff_make_y_ambiguous(self):
        c = Ctx(card="a1")
        c.pending_refinement = route("set remote only", c).payload
        i = route("y", c)
        self.assertFalse(i.ok, "two things waiting on one word is not consent to either")
        self.assertIsNone(i.decision)
        with self.assertRaises(NotConfirmed):
            apply_refinement(i)

    def test_a_refinement_that_changes_nothing_is_refused(self):
        c = Ctx(refiner=lambda goals, instruction: dict(goals))
        i = route("set remote only", c)
        self.assertFalse(i.ok)
        self.assertIn("would not change anything", i.reply)

    def test_a_failing_refiner_refuses_loudly_and_changes_nothing(self):
        c = Ctx(refiner=_boom)
        i = route("set remote only", c)
        self.assertFalse(i.ok)
        self.assertIn("refine_goals failed", i.reply)
        self.assertFalse(i.needs_confirmation)

    def test_a_missing_refiner_refuses_rather_than_guessing(self):
        c = Ctx()
        c.refine_goals = None                   # no injected refiner, no interview module
        i = route("set remote only", c)
        self.assertFalse(i.ok)
        self.assertIn("Nothing was changed", i.reply)

    def test_set_with_no_phrase_asks_what(self):
        i = route("set", Ctx())
        self.assertIs(i.verb, Verb.SET)
        self.assertFalse(i.ok)

    def test_diff_shows_removals_additions_and_changes(self):
        lines = conv.diff_goals({"a": 1, "b": 2}, {"a": 9, "c": 3})
        self.assertIn("~ a: 1 -> 9", lines)
        self.assertTrue(any(line.startswith("- b") for line in lines))
        self.assertTrue(any(line.startswith("+ c") for line in lines))


class OneItemPerTurn(unittest.TestCase):
    """Session framing. There is no path that hands back a second application."""

    def test_intent_refuses_a_list_of_items(self):
        with self.assertRaises(TypeError):
            Intent(verb=Verb.GO, text="go", item=[ITEM, OTHER])

    def test_intent_refuses_a_payload_carrying_several_applications(self):
        with self.assertRaises(TypeError):
            Intent(verb=Verb.QUEUE, text="queue", payload={"items": [ITEM, OTHER]})

    def test_no_verb_yields_two_items_even_with_a_full_queue(self):
        many = [dict(ITEM, app_id=f"a{n}", id=f"a{n}") for n in range(50)]
        for text in ("go", "next", "queue", "status", "why a1", "skip a2", "pause",
                     "goals", "set remote only", "help", "y", "n",
                     "tell me about the pipeline"):
            for card in (None, "a1"):
                i = route(text, Ctx(items=many, card=card))
                self.assertLessEqual(len(_items_in(i)), 1,
                                     f"{text!r} (card={card}) handed back several applications")

    def test_status_reports_counts_not_a_menu(self):
        many = [dict(ITEM, app_id=f"a{n}", id=f"a{n}") for n in range(50)]
        i = route("queue", Ctx(items=many))
        self.assertIn("counts", i.payload)
        for val in i.payload.values():
            self.assertFalse(isinstance(val, (list, tuple, set)) and len(val) > 1,
                             "status must not become a list to pick from")


class FailClosedAndLoud(unittest.TestCase):
    """A check that cannot run is a refusal. An unreadable queue is never an
    empty one -- that equivalence is how a blind system reports 'all clear'."""

    def test_an_unreadable_queue_refuses_instead_of_saying_empty(self):
        c = Ctx()
        c.next_item = _boom
        i = route("go", c)
        self.assertFalse(i.ok)
        self.assertNotIn("queue_empty", i.payload)
        self.assertNotIn("Nothing is queued", i.reply)

    def test_a_genuinely_empty_queue_says_so_distinguishably(self):
        i = route("go", Ctx(items=[]))
        self.assertTrue(i.ok)
        self.assertTrue(i.payload["queue_empty"])

    def test_unreadable_stats_refuse(self):
        c = Ctx()
        c.stats = _boom
        i = route("queue", c)
        self.assertFalse(i.ok)
        self.assertIn("locked", i.reply)

    def test_unreadable_lookup_refuses_and_skips_nothing(self):
        c = Ctx()
        c.get = _boom
        for text, verb in (("why a1", Verb.WHY), ("skip a1", Verb.SKIP)):
            i = route(text, c)
            self.assertIs(i.verb, verb)
            self.assertFalse(i.ok)
            self.assertIsNone(i.decision, "a failed lookup must not decline anything")

    def test_unreadable_goals_refuse(self):
        c = Ctx()
        c.goals = _boom
        i = route("goals", c)
        self.assertFalse(i.ok)

    def test_a_context_missing_an_accessor_refuses_by_name(self):
        class Bare:
            outstanding_card_id = None
            pending_refinement = None
            paused = False
        i = route("queue", Bare())
        self.assertFalse(i.ok)
        self.assertIn("stats", i.reply)


class WhyExplainsRatherThanScores(unittest.TestCase):
    def test_a_score_with_no_recorded_reason_is_refused_not_rendered(self):
        item = dict(ITEM, weakest_reason=None, weakest=None)
        i = route("why a1", Ctx(items=[item]))
        self.assertFalse(i.ok, "a number is not an explanation")
        self.assertIn("re-run the grading step", i.reply)

    def test_a_screen_out_explains_the_screen(self):
        item = dict(ITEM, state="screened_out", weakest_reason=None,
                    screen_reason="title is not on your list")
        i = route("why a1", Ctx(items=[item]))
        self.assertTrue(i.ok)
        self.assertIn("title is not on your list", i.reply)

    def test_a_screen_out_with_no_reason_is_refused(self):
        item = dict(ITEM, state="screened_out", weakest_reason=None)
        i = route("why a1", Ctx(items=[item]))
        self.assertFalse(i.ok)

    def test_below_bar_says_it_was_never_offered_and_needs_a_rebuild(self):
        item = dict(ITEM, state="below_bar")
        i = route("why a1", Ctx(items=[item]))
        self.assertIn("never offered", i.reply)
        self.assertIn("Rebuild", i.reply)

    def test_an_unknown_id_is_refused(self):
        i = route("why nosuchid", Ctx())
        self.assertFalse(i.ok)
        self.assertIn("nosuchid", i.reply)

    def test_why_with_no_id_asks_which(self):
        i = route("why", Ctx())
        self.assertIs(i.verb, Verb.WHY)
        self.assertFalse(i.ok)

    def test_skip_with_an_unknown_id_skips_nothing(self):
        i = route("skip nosuchid", Ctx())
        self.assertFalse(i.ok)
        self.assertIsNone(i.decision)


class NoBulkDecisionHere(unittest.TestCase):
    FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                 "auto_approve", "approve_above", "batch_approve", "approve_batch")

    def test_the_surface_defines_no_bulk_verb(self):
        src = inspect.getsource(conv).lower()
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, src, f"conversation defines {bad}")

    def test_every_decision_names_exactly_one_application(self):
        for text, card in (("y", "a1"), ("n", "a1"), ("skip a2", None)):
            i = route(text, Ctx(card=card))
            if i.decision:
                self.assertIsInstance(i.app_id, str)


class TheSurfaceIsPure(unittest.TestCase):
    def test_importing_it_pulls_in_no_transport(self):
        """No channel import at module scope: the surface must be usable (and
        testable) with no messaging rail configured at all."""
        code = ("import sys, openrecruiter.conversation as c;"
                "print([m for m in sys.modules if 'channels' in m])")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=conv.__file__.rsplit("/openrecruiter/", 1)[0])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]", "conversation.py imported a transport")

    def test_route_writes_nothing_to_the_context(self):
        c = Ctx()
        before = (dict(c._goals), c.outstanding_card_id, c.pending_refinement, c.paused)
        for text in ("go", "queue", "why a1", "skip a1", "pause", "goals",
                     "set remote only", "help", "anything"):
            route(text, c)
        self.assertEqual((dict(c._goals), c.outstanding_card_id,
                          c.pending_refinement, c.paused), before,
                         "route must decide, not act")


class BothOutstandingIsNotADeadlock(unittest.TestCase):
    """C1. Ambiguity refuses the AMBIGUOUS THING, not the whole surface.

    The gate used to fire on every input while a card and a goal change were both
    open: `queue`, `help`, `goals`, `go` and any free-text question all came back
    as the same refusal, whose own text told the user to "answer the card itself"
    -- which landed straight back on it. That is a self-contradicting deadlock,
    and it kills the one claim this module makes: that you can just talk to it.
    """

    def _both(self):
        c = Ctx(card="a1")
        c.pending_refinement = route("set remote only", c).payload
        return c

    def test_every_non_yes_no_verb_still_works_with_both_outstanding(self):
        for text, verb in (("queue", Verb.QUEUE), ("status", Verb.QUEUE),
                           ("help", Verb.HELP), ("goals", Verb.GOALS),
                           ("why a1", Verb.WHY),
                           ("did Globex ever reply?", Verb.FALLBACK)):
            i = route(text, self._both())
            self.assertIs(i.verb, verb, text)
            self.assertTrue(i.ok, f"{text!r} was refused as 'ambiguous'")

    def test_only_a_reading_that_is_a_yes_or_a_no_is_refused(self):
        for word in ("y", "n", "yes", "no", "ok"):
            i = route(word, self._both())
            self.assertFalse(i.ok, word)
            self.assertIsNone(i.decision, word)
            with self.assertRaises(NotConfirmed):
                apply_refinement(i)

    def test_the_refusal_names_exits_that_actually_work(self):
        c = self._both()
        reply = route("y", c).reply
        self.assertIn("skip a1", reply)
        self.assertIn("pause", reply)
        # Not a text assertion: each named exit is EXECUTED from inside the state
        # the refusal describes. A refusal that names an exit it also refuses is
        # the bug, so asserting on wording alone would re-admit it.
        self.assertIs(route("skip a1", self._both()).verb, Verb.SKIP)
        self.assertIs(route("pause", self._both()).verb, Verb.PAUSE)
        again = route("set only remote roles above 180k", self._both())
        self.assertIs(again.verb, Verb.SET)
        self.assertTrue(again.ok, "a fresh 'set' must be able to replace the pending one")


class SetMatchesTheRealRefiner(unittest.TestCase):
    """C2. The verb is exercised against interview.refine_goals ITSELF.

    Every other test in this file injects Ctx.refine_goals, so the call site was
    free to disagree with the real function -- and it did, in both directions:
    reversed arguments AND an unhandled tuple return. A fake that agrees with a
    call site nobody can use certifies nothing.
    """

    ANSWERS = {"titles": "senior product manager, principal product manager",
               "seniority": "senior or above", "domains": "ai infra, devtools, not crypto",
               "locations": "san francisco, new york", "remote": "remote only, no relocation",
               "comp_min": "180k", "company_size": "early, growth",
               "dealbreakers": "no on-call"}

    class RealCtx:
        """No refine_goals of its own: `_set` must reach for the real module."""

        def __init__(self, goals):
            self._goals = goals
            self.outstanding_card_id = None
            self.pending_refinement = None
            self.paused = False

        def stats(self):
            return {}

        def next_item(self):
            return None

        def get(self, app_id):
            return None

        def goals(self):
            return self._goals

    def _ctx(self):
        from openrecruiter.interview import build_goals
        return self.RealCtx(build_goals(self.ANSWERS))

    def test_set_proposes_a_real_diff_with_no_injected_refiner(self):
        i = route("set more fintech", self._ctx())
        self.assertIs(i.verb, Verb.SET)
        self.assertTrue(i.ok, i.reply)
        self.assertTrue(i.needs_confirmation)
        self.assertTrue(any("fintech" in line for line in i.payload["diff"]), i.payload["diff"])
        self.assertIn("fintech", i.payload["proposed"]["domains"]["prefer"])

    def test_the_confirmed_change_is_what_the_real_refiner_produced(self):
        c = self._ctx()
        c.pending_refinement = route("set comp floor 250k", c).payload
        done = route("y", c)
        self.assertIs(done.verb, Verb.SET_CONFIRMED)
        self.assertEqual(apply_refinement(done)["comp_min"], 250000)

    def test_an_unreadable_instruction_refuses_instead_of_reporting_no_change(self):
        """refine_goals returns the ORIGINAL goals when a clause is unreadable, so
        a zero-line diff here means 'I did not understand you', never 'that is
        already true'. Reporting the second would wave a half-read sentence past."""
        i = route("set xyzzy blorp", self._ctx())
        self.assertFalse(i.ok)
        self.assertIn("could not read", i.reply)
        self.assertIn("xyzzy blorp", i.reply)
        self.assertNotIn("would not change anything", i.reply)
        self.assertFalse(i.needs_confirmation)


class ACardOutranksEveryGoSynonym(unittest.TestCase):
    """C3. "the card wins" is enforced here, not inherited from the y/n reader.

    Only "go" and "ok" are in that reader's yes-set, so four of the six synonyms
    -- and any longer phrasing -- sailed past an unanswered card and presented a
    SECOND application, leaving the first with nobody's answer on it.
    """

    SYNONYMS = ("go", "next", "start", "begin", "resume", "continue",
                "go ahead and send it", "/next")

    def test_no_go_synonym_presents_a_second_card(self):
        for word in self.SYNONYMS:
            i = route(word, Ctx(card="a1"))
            self.assertIsNone(i.item, f"{word!r} presented a new application over an open card")
            if i.verb is Verb.GO:
                self.assertFalse(i.ok, f"{word!r} ran the go command with a card open")

    def test_every_go_synonym_covered_here(self):
        """If someone adds a synonym to _GO, this test must grow with it."""
        self.assertTrue(conv._GO.issubset(set(self.SYNONYMS)),
                        f"untested go synonyms: {conv._GO - set(self.SYNONYMS)}")

    def test_the_refusal_says_which_card_is_waiting(self):
        i = route("next", Ctx(card="a1"))
        self.assertFalse(i.ok)
        self.assertEqual(i.app_id, "a1")
        self.assertIn("a1", i.reply)

    def test_a_go_synonym_with_no_card_still_presents_the_one_on_deck(self):
        for word in self.SYNONYMS[:6]:
            i = route(word, Ctx())
            self.assertIs(i.verb, Verb.GO, word)
            self.assertEqual(i.app_id, "a1", word)


class HaltIsNotPunctuationSensitive(unittest.TestCase):
    """C4. The one verb that must not be fragile was the fragile one.

    `_norm` stripped punctuation off the head word only, so "stop it." fell out
    of the halt branch into the consent reader, which reads it as a plain NO --
    the halt silently DECLINED the open card and the loop kept running.
    """

    def test_punctuated_and_padded_halts_still_halt(self):
        for word in ("stop it.", "stop it!", "halt it now", "stop, please.",
                     "stop right now!", "pause for now.", "stop everything.",
                     "pause — please"):
            i = route(word, Ctx(card="a1"))
            self.assertIs(i.verb, Verb.PAUSE, word)
            self.assertTrue(i.payload["declines_outstanding"], word)
            self.assertNotEqual(i.decision, "approve", word)

    def test_a_halt_never_degrades_into_a_decision(self):
        i = route("stop it.", Ctx(card="a1"))
        self.assertIsNot(i.verb, Verb.DECISION,
                         "a halt read as a decline leaves the loop running")

    def test_a_real_sentence_is_still_not_the_halt_verb(self):
        for word in ("stop applying to startups", "stop sending resumes to Globex",
                     "pause the Acme application until Monday"):
            self.assertIs(route(word, Ctx()).verb, Verb.FALLBACK, word)


class GoRefusesWhatWasNeverOffered(unittest.TestCase):
    """C5. Defense in depth: the tier bar lived entirely in the caller.

    This module already knows below_bar means "never offered for approval" (`_why`
    says so), so presenting one anyway made the surface the gate's only loophole.
    """

    def test_a_below_bar_or_terminal_item_is_never_presented(self):
        for state in ("below_bar", "screened_out", "declined", "submitted_verified",
                      "submitting", "discovered", "building", "failed", "approved"):
            i = route("go", Ctx(items=[dict(ITEM, state=state)]))
            self.assertFalse(i.ok, f"{state} was presented for approval")
            self.assertIsNone(i.item, state)
            self.assertIn(state, i.reply, state)

    def test_an_item_with_no_state_is_refused_not_assumed_fine(self):
        item = dict(ITEM)
        item.pop("state")
        i = route("go", Ctx(items=[item]))
        self.assertFalse(i.ok, "a check that cannot run is a refusal, never a pass")

    def test_awaiting_approval_and_expired_are_still_presented(self):
        # EXPIRED is offerable because re-asking is exactly what expiry means;
        # store.LEGAL routes EXPIRED -> AWAITING_APPROVAL for that reason.
        for state in ("awaiting_approval", "expired"):
            i = route("go", Ctx(items=[dict(ITEM, state=state)]))
            self.assertTrue(i.ok, state)
            self.assertIsNotNone(i.item, state)

    def test_a_determiner_is_not_an_application_id(self):
        for text in ("skip the acme one", "skip that one", "skip all of them",
                     "skip my first one"):
            i = route(text, Ctx())
            self.assertNotEqual(i.app_id, text.split(" ")[1], text)
            self.assertIsNone(i.decision, f"{text!r} declined something by name-guess")

    def test_a_real_id_still_skips(self):
        i = route("skip a2", Ctx())
        self.assertIs(i.verb, Verb.SKIP)
        self.assertEqual(i.app_id, "a2")
        self.assertEqual(i.decision, "reject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
