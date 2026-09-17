"""Invariants for the intake interview, the proposed pipeline, and refinement.

These tests are written to FAIL if the behaviour they describe is removed. Each
one names a specific failure it is guarding against, and most are regressions
against a real failure in the ancestor system rather than hypotheticals:

  * a goals dict nobody authored (the hardcoded-regex search)
  * a proposal entry with no reason (a feed, not a search)
  * a hand-added company quietly dropped on the next re-proposal
  * an instruction half-applied because half of it was understood
  * an empty result that cannot be told apart from "nothing found"
"""
import copy
import inspect
import unittest

from openrecruiter import interview as iv
from openrecruiter.engine import goals as engine_goals


FULL_ANSWERS = {
    "titles": "senior product manager, principal product manager",
    "seniority": "senior or above",
    "domains": "ai infra, devtools, not crypto",
    "locations": "san francisco, new york",
    "remote": "remote only, no relocation",
    "comp_min": "180k",
    "company_size": "early, growth",
    "dealbreakers": "no on-call",
}


def goals_for(**over):
    answers = dict(FULL_ANSWERS)
    answers.update(over)
    return iv.build_goals(answers)


class QuestionSet(unittest.TestCase):

    def test_the_interview_covers_every_topic_the_search_needs(self):
        """A missing question is a field the user never gets to author, which is
        the exact defect this module exists to fix."""
        keys = [q.key for q in iv.QUESTIONS]
        for needed in ("titles", "seniority", "domains", "locations", "remote",
                       "comp_min", "company_size", "dealbreakers"):
            self.assertIn(needed, keys)

    def test_questions_are_ordered_unique_and_self_describing(self):
        keys = [q.key for q in iv.QUESTIONS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIsInstance(iv.QUESTIONS, tuple)   # ordered, not a dict
        for q in iv.QUESTIONS:
            self.assertTrue(q.prompt.strip(), f"{q.key} has no prompt")
            self.assertTrue(q.why.strip(), f"{q.key} does not say why it is asked")
            self.assertTrue(callable(q.parse), f"{q.key} declares no parser")

    def test_every_question_is_actually_consumed_by_build_goals(self):
        """A question whose answer is never read is a lie told to the user.

        Asserted by answering everything, then re-building with ONE answer
        dropped and requiring the result to differ."""
        full = iv.build_goals(FULL_ANSWERS)
        for q in iv.QUESTIONS:
            partial = {k: v for k, v in FULL_ANSWERS.items() if k != q.key}
            if q.required:
                with self.assertRaises(iv.IncompleteIntake):
                    iv.build_goals(partial)
                continue
            self.assertNotEqual(full, iv.build_goals(partial),
                                f"dropping {q.key!r} changed nothing -- that answer "
                                f"is collected and never used")

    def test_each_parser_is_reachable_by_name(self):
        self.assertIs(iv.question("comp_min").parse, iv.parse_money)
        with self.assertRaises(iv.UnknownAnswer):
            iv.question("favourite_colour")


class BuildGoals(unittest.TestCase):

    def test_it_builds_the_answers_into_structure(self):
        g = goals_for()
        self.assertEqual(g["titles"]["strong"],
                         ["senior product manager", "principal product manager"])
        self.assertEqual(g["seniority"]["prefer"], ["senior"])
        self.assertEqual(g["domains"]["prefer"], ["ai infra", "devtools"])
        self.assertIn("crypto", g["domains"]["avoid"])
        self.assertEqual(g["remote"], iv.REMOTE_ONLY)
        self.assertIs(g["relocation"], False)
        self.assertEqual(g["comp_min"], 180000)
        self.assertEqual(g["company_sizes"], ["early", "growth"])

    def test_levels_below_the_lowest_one_named_are_derived_not_asked(self):
        g = goals_for(seniority="senior or above")
        self.assertEqual(g["seniority"]["avoid"],
                         ["intern", "new grad", "junior", "associate", "mid"])
        self.assertNotIn("staff", g["seniority"]["avoid"])

    def test_level_matching_respects_word_boundaries(self):
        """'somewhere in the middle' must not be read as the 'mid' level -- a
        substring match here silently filters out half the user's search."""
        g = goals_for(seniority="somewhere in the middle, ish")
        self.assertEqual(g["seniority"]["prefer"], [])
        self.assertIn("seniority", g["unparsed_answers"])

    def test_an_unreadable_optional_answer_is_reported_not_discarded(self):
        g = goals_for(company_size="smallish, but not too small")
        self.assertEqual(g["company_sizes"], [])
        self.assertIn("company_size", g["unparsed_answers"])

    def test_a_skipped_question_is_named(self):
        answers = dict(FULL_ANSWERS)
        answers.pop("locations")
        g = iv.build_goals(answers)
        self.assertIn("locations", g["unanswered"])

    def test_missing_titles_refuses_instead_of_defaulting(self):
        """A goals dict with no titles scores every posting identically. That
        looks like a working search and is not one, so it must never be built."""
        with self.assertRaises(iv.IncompleteIntake):
            iv.build_goals({k: v for k, v in FULL_ANSWERS.items() if k != "titles"})
        with self.assertRaises(iv.IncompleteIntake):
            iv.build_goals(dict(FULL_ANSWERS, titles="   "))
        # answered, but nothing usable came out of it -- the case a
        # required-field check alone does not catch
        with self.assertRaises(iv.IncompleteIntake):
            iv.build_goals(dict(FULL_ANSWERS, titles="none"))
        with self.assertRaises(iv.IncompleteIntake):
            iv.build_goals(dict(FULL_ANSWERS, titles="n/a, unsure"))

    def test_an_unknown_answer_key_raises_rather_than_being_dropped(self):
        with self.assertRaises(iv.UnknownAnswer):
            iv.build_goals(dict(FULL_ANSWERS, comp_floor="180k"))

    def test_it_is_pure_and_deterministic(self):
        answers = dict(FULL_ANSWERS)
        snapshot = copy.deepcopy(answers)
        first = iv.build_goals(answers)
        second = iv.build_goals(answers)
        self.assertEqual(first, second)
        self.assertEqual(answers, snapshot, "build_goals mutated its input")

    def test_money_is_read_the_same_way_everywhere(self):
        self.assertEqual(iv.parse_money("180k"), 180000)
        self.assertEqual(iv.parse_money("$180,000"), 180000)
        self.assertEqual(iv.parse_money("at least 180"), 180000)
        self.assertEqual(iv.parse_money("1.2m"), 1200000)
        self.assertEqual(iv.parse_money("none"), 0)
        self.assertIsNone(iv.parse_money("as much as possible"))

    def test_a_dealbreaker_is_structured_not_just_stored(self):
        """'no relocation' typed as a dealbreaker must mean what it means when
        typed at refine time. Otherwise the phrase is decoration while the flag
        it describes stays unset."""
        g = goals_for(remote="hybrid", dealbreakers="no relocation, no crypto")
        self.assertIs(g["relocation"], False)
        self.assertIn("crypto", g["domains"]["avoid"])
        self.assertIn("no relocation", g["dealbreakers"])

    def test_derived_fields_follow_the_authored_ones(self):
        g = goals_for(titles="senior product manager", domains="ai infra")
        self.assertEqual(g["titles"]["medium"], ["product manager"])
        self.assertEqual(g["keywords_bonus"], ["ai infra"])

    def test_remote_is_never_defaulted_on_the_users_behalf(self):
        answers = dict(FULL_ANSWERS)
        answers.pop("remote")
        g = iv.build_goals(answers)
        self.assertEqual(g["remote"], iv.UNSTATED)
        self.assertIsNone(g["relocation"])


class ScorerCompatibility(unittest.TestCase):
    """The goals dict is the search block the engine scores against. If these two
    ever need a translation layer, one of them will drift -- which is how the
    ancestor came to print one salary and filter on another."""

    def test_the_engine_scores_directly_against_build_goals_output(self):
        g = goals_for()
        good, _ = engine_goals.score("Senior Product Manager", "San Francisco", g,
                                     "$200,000")
        bad, _ = engine_goals.score("Warehouse Associate", "Boise", g)
        self.assertGreater(good, g["min_match"])
        self.assertLess(bad, g["min_match"])

    def test_a_posting_under_the_floor_is_scored_down(self):
        g = goals_for()
        high, _ = engine_goals.score("Senior Product Manager", "remote", g, "$200,000")
        low, _ = engine_goals.score("Senior Product Manager", "remote", g, "$120,000")
        self.assertGreater(high, low)


class ProposedPipeline(unittest.TestCase):

    def test_every_proposed_entry_carries_a_reason(self):
        p = iv.propose_pipeline(goals_for(), {"companies": [], "domains": ["ai infra"]})
        self.assertTrue(p["companies"] and p["boards"] and p["titles"])
        for kind, key in (("company", "companies"), ("board", "boards"),
                          ("title", "titles")):
            for entry in p[key]:
                self.assertTrue(iv.reason_for(p, kind, entry),
                                f"{kind} {entry!r} proposed with no reason")
        for fkey in p["filters"]:
            self.assertTrue(p["reasons"].get(f"filter:{fkey}"))

    def test_a_reasonless_entry_is_refused(self):
        """The check is in code, not only in this test: a future producer that
        appends without a reason must fail loudly."""
        p = iv.propose_pipeline(goals_for(), {})
        p["companies"].append("Mystery Corp")
        with self.assertRaises(iv.ProposalError):
            iv.validate_proposal(p)

    def test_proposals_are_grounded_in_the_stated_domains(self):
        p = iv.propose_pipeline(goals_for(domains="fintech"), {})
        self.assertIn("Stripe", p["companies"])
        self.assertNotIn("Coinbase", p["companies"])
        self.assertIn("fintech", iv.reason_for(p, "company", "Stripe"))

    def test_a_refused_domain_never_seeds_companies(self):
        p = iv.propose_pipeline(goals_for(domains="ai infra, not crypto"), {})
        for banned in ("Coinbase", "Kraken", "Circle"):
            self.assertNotIn(banned, p["companies"])

    def test_the_bank_grounds_the_proposal_in_real_history(self):
        p = iv.propose_pipeline(goals_for(), {"companies": ["Acme Corp"]})
        self.assertEqual(p["companies"][0], "Acme Corp")
        self.assertIn("worked here", iv.reason_for(p, "company", "Acme Corp"))

    def test_boards_follow_the_remote_and_stage_answers(self):
        remote = iv.propose_pipeline(goals_for(remote="remote only"), {})
        onsite = iv.propose_pipeline(
            goals_for(remote="onsite", locations="new york", company_size="public"), {})
        self.assertIn("remoteok", remote["boards"])
        self.assertNotIn("remoteok", onsite["boards"])
        self.assertIn("builtin-new-york", onsite["boards"])

    def test_titles_are_expanded_to_the_level_you_asked_for(self):
        p = iv.propose_pipeline(goals_for(titles="product manager"), {})
        self.assertIn("senior product manager", p["titles"])

    def test_a_title_that_already_has_a_level_is_not_double_prefixed(self):
        p = iv.propose_pipeline(
            goals_for(titles="staff engineer", seniority="staff or principal"), {})
        for t in p["titles"]:
            self.assertNotIn("staff staff", t)
            self.assertNotIn("principal staff", t)

    def test_dealbreaker_phrases_are_not_used_as_match_terms(self):
        """Excluding on the literal phrase 'no on-call' would filter out exactly
        the postings that promise no on-call."""
        p = iv.propose_pipeline(goals_for(dealbreakers="no on-call"), {})
        self.assertNotIn("no on-call", p["filters"]["exclude_keywords"])
        self.assertIn("on-call", p["filters"]["exclude_keywords"])
        self.assertIn("no on-call", p["filters"]["dealbreakers"])

    def test_the_comp_floor_reaches_the_filters(self):
        p = iv.propose_pipeline(goals_for(comp_min="200k"), {})
        self.assertEqual(p["filters"]["comp_min"], 200000)

    def test_a_missing_bank_is_reported_not_silently_tolerated(self):
        p = iv.propose_pipeline(goals_for(), None)
        self.assertTrue(any("no experience bank" in w for w in p["warnings"]))

    def test_an_empty_company_list_says_why_rather_than_looking_complete(self):
        """Rule: never return an empty result indistinguishable from
        'nothing found'."""
        p = iv.propose_pipeline(goals_for(domains="underwater basket weaving"), {})
        self.assertEqual(p["companies"], [])
        self.assertTrue(any("no seeded companies" in w for w in p["warnings"]))
        self.assertTrue(any("Add your own" in w for w in p["warnings"]))

    def test_it_refuses_to_propose_without_goals(self):
        with self.assertRaises(iv.IncompleteIntake):
            iv.propose_pipeline({"titles": {"strong": []}}, {})


class ConfirmAndRePropose(unittest.TestCase):

    def test_a_hand_added_company_survives_a_later_re_proposal(self):
        """The single most important test in this module.

        If a re-proposal can drop what the user added, the agent is the author
        again and the whole propose-then-confirm design is theatre."""
        g = goals_for()
        first = iv.propose_pipeline(g, {})
        final = iv.confirm_pipeline(first, {
            "add": {"companies": ["Tiny Local Co"]},
            "reasons": {"company:Tiny Local Co": "my neighbour works there"}})
        self.assertIn("Tiny Local Co", final["companies"])

        again = iv.propose_pipeline(g, {}, previous=final)
        self.assertIn("Tiny Local Co", again["companies"])
        self.assertEqual(iv.reason_for(again, "company", "Tiny Local Co"),
                         "my neighbour works there")

    def test_a_hand_added_entry_survives_a_goals_change_too(self):
        g = goals_for(domains="fintech")
        final = iv.confirm_pipeline(iv.propose_pipeline(g, {}),
                                    {"add": {"titles": ["head of product"]}})
        moved, diff = iv.refine_goals(g, "more ai infra")
        self.assertTrue(diff.applied)
        again = iv.propose_pipeline(moved, {}, previous=final)
        self.assertIn("head of product", again["titles"])

    def test_a_removed_entry_is_not_resurrected(self):
        g = goals_for(domains="fintech")
        first = iv.propose_pipeline(g, {})
        self.assertIn("Stripe", first["companies"])
        final = iv.confirm_pipeline(first, {"remove": {"companies": ["Stripe"]}})
        self.assertNotIn("Stripe", final["companies"])
        again = iv.propose_pipeline(g, {}, previous=final)
        self.assertNotIn("Stripe", again["companies"])

    def test_re_adding_something_previously_removed_works(self):
        g = goals_for(domains="fintech")
        final = iv.confirm_pipeline(iv.propose_pipeline(g, {}),
                                    {"remove": {"companies": ["Stripe"]}})
        final = iv.confirm_pipeline(final, {"add": {"companies": ["Stripe"]}})
        again = iv.propose_pipeline(g, {}, previous=final)
        self.assertIn("Stripe", again["companies"])

    def test_confirming_does_not_mutate_the_proposal(self):
        p = iv.propose_pipeline(goals_for(), {})
        snapshot = copy.deepcopy(p)
        iv.confirm_pipeline(p, {"add": {"companies": ["Somewhere Else"]}})
        self.assertEqual(p, snapshot)

    def test_an_unknown_edit_key_raises(self):
        p = iv.propose_pipeline(goals_for(), {})
        with self.assertRaises(iv.UnknownEdit):
            iv.confirm_pipeline(p, {"delete": {"companies": ["Anthropic"]}})
        with self.assertRaises(iv.UnknownEdit):
            iv.confirm_pipeline(p, {"add": {"recruiters": ["someone"]}})
        with self.assertRaises(iv.UnknownEdit):
            iv.confirm_pipeline(p, {"filters": {"vibes": "good"}})

    def test_removing_something_absent_raises_instead_of_no_opping(self):
        """A removal that quietly does nothing leaves the user believing the
        entry is gone while it is still in the pipeline."""
        p = iv.propose_pipeline(goals_for(), {})
        with self.assertRaises(iv.UnknownEdit):
            iv.confirm_pipeline(p, {"remove": {"companies": ["Never Proposed Inc"]}})

    def test_a_confirmed_pipeline_still_has_a_reason_for_everything(self):
        p = iv.propose_pipeline(goals_for(), {})
        final = iv.confirm_pipeline(p, {"add": {"boards": ["some-niche-board"]}})
        iv.validate_proposal(final)
        self.assertTrue(iv.reason_for(final, "board", "some-niche-board"))
        self.assertTrue(final["confirmed"])


class RefineGoals(unittest.TestCase):

    def base(self):
        return goals_for(comp_min="150k", remote="hybrid",
                         domains="fintech", dealbreakers="none")

    def test_a_comp_floor_instruction(self):
        g, diff = iv.refine_goals(self.base(), "skip anything under 180k")
        self.assertTrue(diff.applied)
        self.assertEqual(g["comp_min"], 180000)
        self.assertTrue(any("180,000" in c for c in diff.changes))

    def test_comp_floor_phrasings_agree(self):
        for phrasing in ("nothing under $180,000", "at least 180k",
                         "raise my floor to 180k", "no roles below 180k"):
            g, diff = iv.refine_goals(self.base(), phrasing)
            self.assertTrue(diff.applied, phrasing)
            self.assertEqual(g["comp_min"], 180000, phrasing)

    def test_a_relocation_instruction(self):
        start = goals_for(remote="hybrid, open to relocation")
        self.assertIs(start["relocation"], True)
        g, diff = iv.refine_goals(start, "no relocation")
        self.assertTrue(diff.applied)
        self.assertIs(g["relocation"], False)

    def test_a_remote_instruction(self):
        g, diff = iv.refine_goals(self.base(), "remote only")
        self.assertTrue(diff.applied)
        self.assertEqual(g["remote"], iv.REMOTE_ONLY)

    def test_domain_weighting_in_one_sentence(self):
        g, diff = iv.refine_goals(self.base(), "more ai infra, less fintech")
        self.assertTrue(diff.applied)
        self.assertIn("ai infra", g["domains"]["prefer"])
        self.assertIn("fintech", g["domains"]["deprioritize"])
        self.assertNotIn("fintech", g["domains"]["prefer"])

    def test_less_is_not_the_same_as_never(self):
        """Collapsing 'less fintech' into 'no fintech' deletes options the user
        did not refuse. The two land in different lists and behave differently."""
        softer, _ = iv.refine_goals(self.base(), "less fintech")
        harder, _ = iv.refine_goals(self.base(), "no fintech")
        self.assertIn("fintech", softer["domains"]["deprioritize"])
        self.assertNotIn("fintech", softer["domains"]["avoid"])
        self.assertIn("fintech", harder["domains"]["avoid"])

        soft_p = iv.propose_pipeline(softer, {})
        hard_p = iv.propose_pipeline(harder, {})
        self.assertNotIn("Stripe", soft_p["companies"])
        self.assertNotIn("fintech", soft_p["filters"]["exclude_keywords"])
        self.assertIn("fintech", hard_p["filters"]["exclude_keywords"])

    def test_a_domain_moves_between_lists_rather_than_appearing_twice(self):
        g, _ = iv.refine_goals(self.base(), "no fintech")
        g2, diff = iv.refine_goals(g, "more fintech")
        self.assertIn("fintech", g2["domains"]["prefer"])
        self.assertNotIn("fintech", g2["domains"]["avoid"])
        self.assertTrue(diff.applied)

    def test_derived_fields_are_recomputed_after_a_refinement(self):
        """keywords_bonus is what the scorer actually reads. If it can lag the
        domains it is derived from, the search the user sees and the search that
        runs are different searches."""
        g, _ = iv.refine_goals(self.base(), "more ai infra")
        self.assertIn("ai infra", g["keywords_bonus"])
        self.assertEqual(g["keywords_bonus"], g["domains"]["prefer"])

    def test_an_unparseable_instruction_refuses_rather_than_guessing(self):
        base = self.base()
        g, diff = iv.refine_goals(base, "make it sparkle")
        self.assertFalse(diff.applied)
        self.assertFalse(diff.ok)
        self.assertEqual(diff.unparsed, ("make it sparkle",))
        self.assertEqual(g, base, "goals changed on an instruction we could not read")

    def test_a_vague_phrase_is_not_accepted_as_a_domain(self):
        for vague in ("more of that stuff you know", "less of the same",
                      "more things like the last one"):
            g, diff = iv.refine_goals(self.base(), vague)
            self.assertFalse(diff.applied, vague)
            self.assertEqual(g, self.base(), vague)

    def test_a_partly_understood_instruction_applies_nothing(self):
        """Half-applying is worse than refusing: the user believes the whole
        sentence landed. Same doctrine as 'yes but change the summary'."""
        base = self.base()
        g, diff = iv.refine_goals(base, "skip anything under 200k and make it sparkle")
        self.assertFalse(diff.applied)
        self.assertEqual(g["comp_min"], base["comp_min"])
        self.assertTrue(any("200,000" in c for c in diff.changes),
                        "the readable half must still be reported back")
        self.assertEqual(diff.unparsed, ("make it sparkle",))
        self.assertIn("NOT APPLIED", diff.as_text())

    def test_an_empty_instruction_is_a_refusal_not_a_silent_no_op(self):
        base = self.base()
        g, diff = iv.refine_goals(base, "")
        self.assertFalse(diff.applied)
        self.assertTrue(diff.unparsed)
        self.assertEqual(g, base)

    def test_an_instruction_that_changes_nothing_says_so(self):
        """'Understood, already true' must be distinguishable from 'ignored'."""
        base = self.base()
        g, diff = iv.refine_goals(base, "skip anything under 150k")
        self.assertTrue(diff.applied)
        self.assertEqual(diff.changes, ())
        self.assertTrue(diff.noop)
        self.assertIn("already", diff.as_text())
        self.assertEqual(g, base)

    def test_refining_does_not_mutate_the_input(self):
        base = self.base()
        snapshot = copy.deepcopy(base)
        iv.refine_goals(base, "skip anything under 300k")
        self.assertEqual(base, snapshot)

    def test_a_malformed_goals_dict_is_refused(self):
        with self.assertRaises(iv.InterviewError):
            iv.refine_goals({"titles": {"strong": ["pm"]}}, "no relocation")
        with self.assertRaises(iv.InterviewError):
            iv.refine_goals("not a dict", "no relocation")

    def test_the_diff_is_human_readable(self):
        _, diff = iv.refine_goals(self.base(), "skip anything under 180k")
        text = diff.as_text()
        self.assertIn("$150,000", text)
        self.assertIn("$180,000", text)
        self.assertEqual(text, str(diff))


class NoApprovalSurface(unittest.TestCase):
    """This module configures a SEARCH. It must never grow a way to decide about
    applications -- least of all many at once. A pipeline that could also approve
    is a volume knob wearing a different name."""

    FORBIDDEN = ("approve", "auto_send", "submit_all", "apply_all", "bulk", "batch")

    def test_the_module_has_no_approval_or_bulk_verb(self):
        src = inspect.getsource(iv).lower()
        # the docstring says the word once, explaining why there is none
        body = src.split('"""', 2)[-1]
        for bad in self.FORBIDDEN:
            self.assertNotIn(f"def {bad}", body, f"interview grew {bad}")
            self.assertNotIn(f"def _{bad}", body, f"interview grew {bad}")

    def test_nothing_here_touches_application_state(self):
        names = [n for n in dir(iv) if not n.startswith("__")]
        for n in names:
            self.assertNotIn("applicat", n.lower())
        self.assertNotIn("from .store", inspect.getsource(iv))
        self.assertNotIn("import store", inspect.getsource(iv))


if __name__ == "__main__":
    unittest.main()
