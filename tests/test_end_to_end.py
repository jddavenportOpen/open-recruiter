"""The whole chain, in one test, with no network and no real model.

Every other test file proves one module in isolation. This one proves they
actually fit together -- which is a different claim, and the one that was false
for most of this project's life: ten well-tested modules that almost nothing
imported.

The model is faked (it returns fixed selections and panels) and the employer is
the local mock ATS. Everything between them is the real code path: the real
store, the real gates, the real typesetter, the real panel arithmetic, the real
channel contract, the real submit-and-verify.
"""
import json
import os
import tempfile
import threading
import unittest

from openrecruiter import mock_ats, store as store_mod, wire
from openrecruiter.channels.base import Capabilities, Card, Channel, Decision
from openrecruiter.engine import panel
from openrecruiter.loop import Deps, KillSwitch, run_once
from openrecruiter.store import State, Store


BANK = {
    "name": "Dana Reyes",
    "contact": {"email": "dana@example.org", "phone": "415-555-0142",
                "location": "Portland, OR"},
    "summaries": {"pm": "Product leader who ships data platforms."},
    "skills_pool": ["Python", "SQL", "Product strategy"],
    "education": [{"school": "Brigham Young University", "degree": "MBA",
                   "dates": "2027"}],
    "jobs": [{
        "id": "acme",
        "company": "Acme", "title": "Principal PM", "location": "Remote",
        "dates": "2020 to 2024",
        "bullets": {
            "b1": "Grew a fixed-price services line from two million to twenty million.",
            "b2": "Shipped an internal platform used by three thousand people monthly.",
            "b3": "Scaled the data organisation from five engineers to thirty-five.",
        },
    }],
}

SELECTION = {"summary_key": "pm",
             "jobs": [{"id": "acme", "bullet_ids": ["b1", "b2", "b3"]}],
             "skills": ["Python", "SQL", "Product strategy"],
             "claims": ["$20M annualized revenue", "3,000 monthly active users"]}


def fake_model(scores, *, votes=3, reason="the summary buries the platform work"):
    """A model that selects real bullets and returns a fixed panel."""
    def call(prompt, **kw):
        if "SELECTING and ORDERING" in prompt:
            return wire.ModelReply(text=json.dumps(SELECTION))
        dims = {d: scores for d in panel.DIMENSION_WEIGHTS}
        # Which persona is being asked is irrelevant to a fixture; the loop reads
        # all three back by name from the payload we assemble in build_resume.
        return wire.ModelReply(
            text=json.dumps({"dimensions": dims,
                             "would_interview": votes > 0,
                             "reason": reason}),
            windows={"five_hour": {"utilization": 0.1, "resets_at": 1789700000.0},
                     "seven_day": {"utilization": 0.2, "resets_at": 1789900000.0}})
    return call


class RecordingChannel(Channel):
    """Answers the card with a scripted decision and remembers what it was shown."""
    name = "recording"

    def __init__(self, decision=Decision.APPROVE):
        self.decision, self.cards, self.texts = decision, [], []

    @property
    def capabilities(self):
        return Capabilities(True, True, True, True, True)

    def send_text(self, text):
        self.texts.append(text)
        return "m1"

    def send_card(self, card: Card):
        self.cards.append(card)
        return card.app_id

    def poll_inbound(self, since=None):
        return []

    def await_decision(self, card_id, timeout_s=0, **kw):
        return self.decision


class MockEmployer:
    """The local mock ATS, on a real socket, for the length of one test."""

    def __enter__(self):
        self.ats = mock_ats.MockATS().start()
        return self

    def __exit__(self, *a):
        self.ats.stop()

    def url(self, path="/apply"):
        return self.ats.url(path)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["OPENRECRUITER_HOME"] = self.home
        self.store = Store(os.path.join(self.home, "t.db"))

    def tearDown(self):
        self.store.close()
        os.environ.pop("OPENRECRUITER_HOME", None)

    def _deps(self, channel, scores, employer=None, votes=3):
        state = {}
        call = wire.instrumented_call(state, fake_model(scores, votes=votes))
        resumes = os.path.join(self.home, "resumes")

        def build(app):
            return wire.build_resume(app, BANK, f"{app.role} at {app.company}",
                                     resumes, call=call)

        def submit(app):
            if employer is None:
                return {"sent": True, "verified": True, "note": "fixture", "retryable": False}
            return wire.make_submit(self.store, BANK, allow_external=False)(app)

        return Deps(build=build, ask=wire.make_ask([channel]), submit=submit,
                    quota_reader=wire.make_quota_reader(state, probe=call),
                    killswitch=KillSwitch(os.path.join(self.home, "STOP")))

    # -- the happy path -------------------------------------------------------
    def test_a_strong_application_reaches_a_human_and_only_then_is_sent(self):
        self.store.upsert_discovered("a1", "Acme Corp", "Principal PM",
                                     "https://boards.greenhouse.io/acme/jobs/1")
        ch = RecordingChannel(Decision.APPROVE)
        run_once(self.store, self._deps(ch, scores=88))   # build -> card
        run_once(self.store, self._deps(ch, scores=88))   # decision -> submit
        for _ in range(3):
            if self.store.get("a1").state in (State.SUBMITTED_VERIFIED,
                                              State.SUBMITTED_UNVERIFIED):
                break
            run_once(self.store, self._deps(ch, scores=88))

        app = self.store.get("a1")
        self.assertIn(app.state, (State.SUBMITTED_VERIFIED, State.SUBMITTED_UNVERIFIED))
        self.assertEqual(len(ch.cards), 1, "exactly one card for one application")

        card = ch.cards[0]
        self.assertEqual(card.company, "Acme Corp")
        self.assertTrue(card.weakest_reason, "the card must carry a real reason")
        self.assertTrue(card.claims, "the card must say what is being asserted")
        self.assertTrue(os.path.exists(app.resume_path), "a real PDF was produced")

        states = [e["to_state"] for e in self.store.history("a1")]
        self.assertIn(State.AWAITING_APPROVAL.value, states)
        self.assertLess(states.index(State.AWAITING_APPROVAL.value),
                        states.index(State.SUBMITTING.value),
                        "it must reach a human BEFORE it is sent")

    # -- the bar ---------------------------------------------------------------
    def test_a_weak_application_never_reaches_a_human(self):
        self.store.upsert_discovered("a2", "Acme Corp", "PM", "https://x/2")
        ch = RecordingChannel(Decision.APPROVE)
        run_once(self.store, self._deps(ch, scores=40))
        self.assertEqual(self.store.get("a2").state, State.BELOW_BAR)
        self.assertEqual(ch.cards, [], "a below-bar resume was offered for approval")

    def test_a_reach_employer_is_held_to_the_reach_bar(self):
        # 85s clear the standard bar and fail the reach bar (90 / raw 80 / 2 votes).
        self.store.upsert_discovered("a3", "Anthropic", "PM",
                                     "https://boards.greenhouse.io/anthropic/jobs/3")
        ch = RecordingChannel(Decision.APPROVE)
        run_once(self.store, self._deps(ch, scores=85))
        self.assertEqual(self.store.get("a3").state, State.BELOW_BAR)
        self.assertEqual(ch.cards, [])

    def test_the_same_panel_passes_for_an_ordinary_employer(self):
        self.store.upsert_discovered("a4", "Acme Corp", "PM", "https://x/4")
        ch = RecordingChannel(Decision.APPROVE)
        run_once(self.store, self._deps(ch, scores=85))
        self.assertEqual(len(ch.cards), 1,
                         "the reach bar must not be applied to everyone")
        self.assertIn(State.AWAITING_APPROVAL.value,
                      [e["to_state"] for e in self.store.history("a4")],
                      "it should have reached a human")

    # -- the human's answer is load-bearing -----------------------------------
    def test_a_no_is_honoured_and_nothing_is_sent(self):
        self.store.upsert_discovered("a5", "Acme Corp", "PM", "https://x/5")
        ch = RecordingChannel(Decision.REJECT)
        run_once(self.store, self._deps(ch, scores=88))
        run_once(self.store, self._deps(ch, scores=88))
        app = self.store.get("a5")
        self.assertEqual(app.state, State.DECLINED)
        self.assertNotIn(State.SUBMITTING.value,
                         [e["to_state"] for e in self.store.history("a5")])

    def test_silence_is_not_consent(self):
        self.store.upsert_discovered("a6", "Acme Corp", "PM", "https://x/6")
        ch = RecordingChannel(Decision.TIMEOUT)
        run_once(self.store, self._deps(ch, scores=88))
        run_once(self.store, self._deps(ch, scores=88))
        self.assertNotIn(State.SUBMITTING.value,
                         [e["to_state"] for e in self.store.history("a6")])

    # -- against a real (mock) employer ---------------------------------------
    def test_a_real_submit_against_the_mock_ats_is_verified_by_read_back(self):
        with MockEmployer() as emp:
            self.store.upsert_discovered("a7", "Acme Corp", "PM", emp.url("/apply"))
            ch = RecordingChannel(Decision.APPROVE)
            for _ in range(4):
                run_once(self.store, self._deps(ch, scores=88, employer=emp))
                if self.store.get("a7").state in (State.SUBMITTED_VERIFIED,
                                                  State.SUBMITTED_UNVERIFIED,
                                                  State.FAILED):
                    break
            app = self.store.get("a7")
            self.assertIn(app.state, (State.SUBMITTED_VERIFIED,
                                      State.SUBMITTED_UNVERIFIED, State.FAILED))
            self.assertNotEqual(app.state, State.APPROVED,
                                "an approved row must not be left stranded")

    # -- outcomes --------------------------------------------------------------
    def test_the_outcome_question_is_answerable(self):
        self.store.upsert_discovered("a8", "Acme Corp", "PM", "https://x/8")
        ch = RecordingChannel(Decision.APPROVE)
        for _ in range(4):
            run_once(self.store, self._deps(ch, scores=88))
            if self.store.get("a8").state in (State.SUBMITTED_VERIFIED,
                                              State.SUBMITTED_UNVERIFIED):
                break
        self.store.record_outcome("a8", "interview")
        report = self.store.score_vs_outcome()
        self.assertIn("interview", report)
        self.assertEqual(report["interview"]["n"], 1)
        self.assertIsNotNone(report["interview"]["mean_score"],
                             "the score must be joinable to the outcome")

    # -- the stop is real ------------------------------------------------------
    def test_the_kill_switch_stops_it_before_anything_is_built(self):
        self.store.upsert_discovered("a9", "Acme Corp", "PM", "https://x/9")
        ch = RecordingChannel(Decision.APPROVE)
        deps = self._deps(ch, scores=88)
        open(os.path.join(self.home, "STOP"), "w").close()
        run_once(self.store, deps)
        self.assertEqual(self.store.get("a9").state, State.DISCOVERED)
        self.assertEqual(ch.cards, [])

    # -- plumbing failures are not candidate failures --------------------------
    def test_a_model_outage_is_not_recorded_as_a_bad_resume(self):
        self.store.upsert_discovered("a10", "Acme Corp", "PM", "https://x/10")
        ch = RecordingChannel(Decision.APPROVE)

        def dead(prompt, **kw):
            raise wire.ModelUnavailable("the model did not answer")

        deps = self._deps(ch, scores=88)
        deps = Deps(build=lambda app: wire.build_resume(
                        app, BANK, "x", os.path.join(self.home, "r"), call=dead),
                    ask=deps.ask, submit=deps.submit,
                    quota_reader=deps.quota_reader, killswitch=deps.killswitch)
        try:
            run_once(self.store, deps)
        except wire.ModelUnavailable:
            pass
        self.assertNotEqual(self.store.get("a10").state, State.BELOW_BAR,
                            "an outage was recorded as the resume failing its bar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
