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

    def refine_goals(self, phrase, current):
        self.refined.append((phrase, current))
        if self._refiner:
            return self._refiner(phrase, current)
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
        self.assertEqual(c.refined[0][0], "only remote roles above 180k")
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
        c = Ctx(refiner=lambda p, cur: dict(cur))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
