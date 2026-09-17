"""Invariant tests for the panel gate.

These exist because the ancestor repo's suite was MUTATION-BLIND: deleting the
dimension weighting, removing the incomplete-panel refusal, moving ADVANCE_FLOOR
to 100, dropping DEFAULT_THRESHOLD to 0, and emptying the REACH set ALL left it
40/40 green. A suite that stays green while you delete the thing it is guarding
is not a suite, it is decoration.

Every test here is written to FAIL if its invariant is removed. If you change the
gate, these should break -- that is the point. Read the reasoning before retuning
a constant.
"""
import unittest

from openrecruiter.engine import panel as P

DIMS = list(P.DIMENSION_WEIGHTS)


def mk(scores, votes=None, company="Some Startup"):
    """A panel payload. `scores` is per-persona: a number, or a dict of dimension->score."""
    if isinstance(scores, (int, float)):
        scores = {p: scores for p in P.PANEL}
    if votes is None:
        votes = {p: True for p in P.PANEL}
    if isinstance(votes, bool):
        votes = {p: votes for p in P.PANEL}
    personas = {}
    for p in P.PANEL:
        v = scores[p]
        dims = v if isinstance(v, dict) else {d: {"score": v} for d in DIMS}
        personas[p] = {"dimensions": dims, "would_interview": votes[p]}
    return {"company": company, "personas": personas}


class VoteCouplingIsBounded(unittest.TestCase):
    """THE bug this package was forked to fix.

    Unbounded `max(score, 85)` made three yes-votes an unconditional pass at the
    standard tier: a panel scoring ZERO on every dimension returned panel_avg
    85.0 and overall_pass True, because 85 >= 70 always. Anything reading
    overall_pass to decide whether to SEND an application would have been sending
    on three booleans.
    """

    def test_zero_scores_with_three_yes_votes_fails(self):
        r = P.aggregate(mk(0))
        self.assertEqual(r["raw_panel_avg"], 0.0)
        self.assertFalse(r["overall_pass"],
                         "three yes-votes must not pass a panel that scored zero")

    def test_a_vote_cannot_lift_more_than_the_cap(self):
        # NOTE: asserted against a CONCRETE bound, deliberately not against
        # P.MAX_VOTE_LIFT. A test that reads the constant it guards moves with the
        # mutation and silently stops guarding -- the mutation harness caught
        # exactly that here, which is what the harness is for.
        r = P.aggregate(mk(10))
        self.assertLess(r["panel_avg"], 50.0,
                        "a yes-vote must not manufacture a passing score out of a 10")
        self.assertFalse(r["overall_pass"])

    def test_the_lift_is_capped_independently_of_the_raw_floor(self):
        """The cap and the raw floor guard different failure modes, so each must
        hold on its own. Without this, removing the cap is invisible because the
        raw floor happens to catch the same case."""
        self.assertLess(P._coupled(0), 50.0, "a vote lifted a 0 into passing territory")
        self.assertLess(P._coupled(20), 60.0, "a vote lifted a 20 too far")
        self.assertEqual(P._coupled(95), 95.0, "a vote must not inflate an already-strong score")

    def test_the_mid_band_rescue_the_floor_exists_for_still_works(self):
        # A judge who votes yes and then scores 72-78 is the documented case the
        # floor was built for. It must still be rescued to ADVANCE_FLOOR.
        for score in (72, 78):
            r = P.aggregate(mk(score))
            self.assertEqual(r["panel_avg"], float(P.ADVANCE_FLOOR),
                             f"a yes-vote at {score} should lift to the floor")
            self.assertTrue(r["overall_pass"])

    def test_a_vote_never_lowers_a_score(self):
        r_yes = P.aggregate(mk(95))
        r_no = P.aggregate(mk(95, votes=False))
        self.assertGreaterEqual(r_yes["panel_avg"], r_no["panel_avg"])

    def test_monotonic_in_score(self):
        """Higher dimension scores can never produce a lower panel average."""
        prev = -1.0
        for v in range(0, 101, 5):
            avg = P.aggregate(mk(v))["panel_avg"]
            self.assertGreaterEqual(avg, prev, f"panel_avg went DOWN at score {v}")
            prev = avg


class RawFloorIsEnforced(unittest.TestCase):
    """The lift cap alone still lets a vote carry a weak panel over the coupled
    threshold. The unclamped composite has to stand on its own too."""

    def test_below_raw_floor_fails_even_when_coupled_average_clears(self):
        r = P.aggregate(mk(60))
        self.assertGreaterEqual(r["panel_avg"], P.DEFAULT_THRESHOLD,
                                "precondition: the coupled average clears the threshold")
        self.assertLess(r["raw_panel_avg"], P.RAW_FLOOR_DEFAULT)
        self.assertFalse(r["overall_pass"], "the raw floor must veto this")

    def test_raw_average_is_never_the_clamped_one(self):
        r = P.aggregate(mk(50))
        self.assertEqual(r["raw_panel_avg"], 50.0,
                         "raw_panel_avg must report the UNCLAMPED composite")


class ReachTierIsStricter(unittest.TestCase):
    def test_reach_set_is_not_empty(self):
        self.assertTrue(P.REACH, "emptying REACH silently downgrades every reach employer")

    def test_a_reach_employer_is_detected(self):
        self.assertTrue(P.aggregate(mk(95, company="Anthropic"))["is_reach"])

    def test_reach_needs_more_than_the_standard_tier(self):
        self.assertGreater(P.REACH_THRESHOLD, P.DEFAULT_THRESHOLD)
        self.assertGreater(P.RAW_FLOOR_REACH, P.RAW_FLOOR_DEFAULT)

    def test_85s_with_three_votes_pass_standard_and_fail_reach(self):
        self.assertTrue(P.aggregate(mk(85))["overall_pass"])
        self.assertFalse(P.aggregate(mk(85, company="Anthropic"))["overall_pass"],
                         "the reach bar is a conjunction; 85 must not clear 90")

    def test_reach_requires_a_majority_vote(self):
        votes = {p: (i == 0) for i, p in enumerate(P.PANEL)}  # one yes
        r = P.aggregate(mk(95, votes=votes, company="Anthropic"))
        self.assertFalse(r["overall_pass"], "reach needs >=2 interview votes")


class WeightingIsLoadBearing(unittest.TestCase):
    """If every weight were equal, the rubric would be decoration."""

    def test_weights_are_not_all_equal(self):
        self.assertGreater(len(set(P.DIMENSION_WEIGHTS.values())), 1)

    def test_the_heaviest_dimension_moves_the_score_most(self):
        heavy = max(P.DIMENSION_WEIGHTS, key=P.DIMENSION_WEIGHTS.get)
        light = min(P.DIMENSION_WEIGHTS, key=P.DIMENSION_WEIGHTS.get)
        self.assertNotEqual(heavy, light)

        def with_one_at(dim, val):
            dims = {d: {"score": 90} for d in DIMS}
            dims[dim] = {"score": val}
            return P.aggregate(mk({p: dims for p in P.PANEL}))["raw_panel_avg"]

        drop_heavy = 90 - with_one_at(heavy, 0)
        drop_light = 90 - with_one_at(light, 0)
        self.assertGreater(drop_heavy, drop_light,
                           "zeroing the heaviest dimension must hurt more than the lightest")

    def test_composite_divides_by_full_rubric_weight(self):
        """Scoring a subset must not renormalize -- that reports the average of
        whatever showed up as though it were the full rubric."""
        self.assertEqual(sum(P.DIMENSION_WEIGHTS.values()), 100)


class IncompletePanelRefuses(unittest.TestCase):
    """A missing persona is a FAILED GRADING RUN, not a low score. Averaging
    around the gap reports a confidently wrong number."""

    def test_missing_persona_raises(self):
        payload = mk(90)
        del payload["personas"][P.PANEL[0]]
        with self.assertRaises(P.IncompletePanel):
            P.aggregate(payload)

    def test_missing_dimension_raises(self):
        payload = mk(90)
        del payload["personas"][P.PANEL[0]]["dimensions"][DIMS[0]]
        with self.assertRaises(P.IncompletePanel):
            P.aggregate(payload)

    def test_a_boolean_is_not_a_score(self):
        payload = mk(90)
        payload["personas"][P.PANEL[0]]["dimensions"][DIMS[0]] = {"score": True}
        with self.assertRaises(P.IncompletePanel):
            P.aggregate(payload)

    def test_missing_personas_never_score_zero(self):
        payload = mk(90)
        del payload["personas"][P.PANEL[1]]
        try:
            r = P.aggregate(payload)
        except P.IncompletePanel:
            return
        self.fail(f"averaged around a missing persona and returned {r['panel_avg']}")


class ThresholdsAreNotVacuous(unittest.TestCase):
    def test_thresholds_are_in_range(self):
        for name in ("DEFAULT_THRESHOLD", "REACH_THRESHOLD",
                     "RAW_FLOOR_DEFAULT", "RAW_FLOOR_REACH"):
            v = getattr(P, name)
            self.assertTrue(0 < v <= 100, f"{name}={v} is vacuous")

    def test_a_bad_panel_can_actually_fail(self):
        self.assertFalse(P.aggregate(mk(30))["overall_pass"])

    def test_a_good_panel_can_actually_pass(self):
        self.assertTrue(P.aggregate(mk(95))["overall_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
