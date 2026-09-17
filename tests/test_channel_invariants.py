"""Invariants for the approval rail.

The approval gate is the only thing standing between this tool and the category
it does not want to join. Three of these tests exist specifically to fail if a
future change erodes it -- including one that asserts a feature does NOT exist.
"""
import inspect
import unittest

from openrecruiter.channels import base
from openrecruiter.channels.base import Capabilities, Card, Channel, Decision, parse_reply
from openrecruiter.channels.telegram import TelegramChannel, sign, verify


CARD = Card(app_id="a1", company="Acme", role="Principal PM", tier="standard",
            panel_avg=84.0, raw_panel_avg=79.0, weakest_persona="recruiter",
            weakest_reason="the summary buries the platform work under tooling",
            claims=["$20M annualized revenue", "3,000 monthly active users"])


class FakeChannel(Channel):
    """A transport that returns whatever inbound we hand it."""
    name = "fake"

    def __init__(self, inbound=None, caps=None):
        self._inbound = list(inbound or [])
        self._caps = caps or Capabilities(True, True, True, True, True)
        self.sent, self.acked, self.cards = [], [], []

    @property
    def capabilities(self):
        return self._caps

    def send_text(self, text):
        self.sent.append(text)
        return f"m{len(self.sent)}"

    def send_card(self, card):
        self.cards.append(card)
        return card.app_id

    def poll_inbound(self, since=None):
        out, self._inbound = self._inbound, []
        return out

    def acknowledge(self, message_id):
        self.acked.append(message_id)
        return True


class NoBatchApprovalExists(unittest.TestCase):
    """The single most important test in this package.

    The doctrine this project inherits argues that once a tool can submit at the
    cheap end, the expensive end becomes a config value between the user and
    their worst instinct at 1am. The ancestor system had exactly that endpoint --
    POST /api/approve-all, which approved every scored row at once. It is not
    ported, and this test is why it stays unported.
    """

    FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                 "auto_approve", "approve_above", "batch_approve", "approve_batch")

    def test_the_channel_interface_has_no_bulk_verb(self):
        names = [n for n, _ in inspect.getmembers(Channel)]
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, [n.lower() for n in names],
                             f"Channel grew a bulk-approval verb: {bad}")

    def test_no_module_in_the_package_defines_one(self):
        import openrecruiter.channels as pkg
        import openrecruiter.channels.sendblue as sb
        import openrecruiter.channels.telegram as tg
        for mod in (pkg, base, sb, tg):
            src = inspect.getsource(mod).lower()
            for bad in self.FORBIDDEN:
                self.assertNotIn(f"def {bad}", src,
                                 f"{mod.__name__} defines {bad}")

    def test_a_decision_applies_to_exactly_one_application(self):
        """There is no decision type that means 'and the rest'."""
        self.assertEqual({d.value for d in Decision},
                         {"approve", "reject", "timeout", "ambiguous"})


class AmbiguityIsNotConsent(unittest.TestCase):
    def test_clear_yes(self):
        for t in ("Y", "y", "yes", "Yeah!", "ok", "go", "apply", "\U0001F44D"):
            self.assertIs(parse_reply(t), Decision.APPROVE, t)

    def test_clear_no(self):
        for t in ("N", "no", "skip", "pass", "stop", "\U0001F44E"):
            self.assertIs(parse_reply(t), Decision.REJECT, t)

    def test_qualified_yes_is_ambiguous(self):
        """'yes but change the summary' is not consent to send THIS resume."""
        for t in ("yes but change the summary", "y if the salary is right",
                  "maybe", "why?", "what's the score", "yes no", "", "   "):
            self.assertIs(parse_reply(t), Decision.AMBIGUOUS, t)

    def test_an_ambiguous_reply_re_asks_and_does_not_decide(self):
        ch = FakeChannel(inbound=[{"id": "1", "text": "hmm not sure", "ts": 0}])
        clock = iter([0.0, 1.0, 2.0, 999.0])
        d = ch.await_decision("a1", timeout_s=5, poll_s=0,
                              _clock=lambda: next(clock), _sleep=lambda _s: None)
        self.assertIs(d, Decision.TIMEOUT, "an unclear reply must not be read as a decision")
        self.assertTrue(any("yes or no" in s for s in ch.sent), "user was never re-asked")


class StaleConsentIsNotHonoured(unittest.TestCase):
    def test_a_reply_after_the_deadline_times_out(self):
        ch = FakeChannel(inbound=[{"id": "1", "text": "Y", "ts": 0}])
        clock = iter([0.0, 100.0])          # already past the deadline on first check
        d = ch.await_decision("a1", timeout_s=10, poll_s=0,
                              _clock=lambda: next(clock), _sleep=lambda _s: None)
        self.assertIs(d, Decision.TIMEOUT,
                      "consent to send an application is perishable; a stale yes is re-asked")

    def test_a_reply_inside_the_window_is_honoured(self):
        ch = FakeChannel(inbound=[{"id": "1", "text": "Y", "ts": 0}])
        clock = iter([0.0, 1.0, 2.0])
        d = ch.await_decision("a1", timeout_s=60, poll_s=0,
                              _clock=lambda: next(clock), _sleep=lambda _s: None)
        self.assertIs(d, Decision.APPROVE)
        self.assertEqual(ch.acked, ["1"], "the user got no read receipt")


class CardCarriesEnoughToBeADecision(unittest.TestCase):
    """A card showing a filename and a number converts fifteen seconds of READING
    into one second of CLICKING. Approval fatigue does the rest."""

    def test_card_text_names_the_weakest_reason(self):
        t = CARD.as_text()
        self.assertIn("recruiter", t)
        self.assertIn("buries the platform work", t,
                      "the card must carry the ACTUAL reason, not just a score")

    def test_card_text_lists_the_claims_being_asserted(self):
        t = CARD.as_text()
        self.assertIn("$20M annualized revenue", t)
        self.assertIn("3,000 monthly active users", t)

    def test_card_shows_company_role_and_tier(self):
        t = CARD.as_text()
        for token in ("Acme", "Principal PM", "standard"):
            self.assertIn(token, t)


class DecisionTokensAreBoundToOneApplication(unittest.TestCase):
    def test_a_token_is_valid_only_for_its_own_application(self):
        t = sign("secret", "app7", "approve")
        self.assertTrue(verify("secret", "app7", "approve", t))
        self.assertFalse(verify("secret", "app8", "approve", t),
                         "a token approved a DIFFERENT application")

    def test_a_token_is_valid_only_for_its_own_decision(self):
        t = sign("secret", "app7", "approve")
        self.assertFalse(verify("secret", "app7", "reject", t))

    def test_a_forged_token_is_refused(self):
        self.assertFalse(verify("secret", "app7", "approve", "0" * 24))
        self.assertFalse(verify("secret", "app7", "approve", ""))

    def test_callback_from_another_card_does_not_decide_this_one(self):
        ch = TelegramChannel(token="t", chat_id="c", secret="secret")
        other = f"a|app8|{sign('secret', 'app8', 'approve')}"
        self.assertIs(ch.decision_from_callback(other, "app7"), Decision.AMBIGUOUS)

    def test_a_valid_callback_decides(self):
        ch = TelegramChannel(token="t", chat_id="c", secret="secret")
        good = f"a|app7|{sign('secret', 'app7', 'approve')}"
        self.assertIs(ch.decision_from_callback(good, "app7"), Decision.APPROVE)


class CapabilitiesAreHonest(unittest.TestCase):
    def test_sendblue_does_not_claim_buttons(self):
        """Their Reactions API 422s on outbound targets and no webhook carries a
        reaction, so approval there is a reply keyword. Claiming buttons would
        make the loop skip the reply path and silently never get an answer."""
        from openrecruiter.channels.sendblue import SendBlueChannel
        caps = SendBlueChannel(api_key="k", api_secret="s",
                               from_number="+1", to_number="+2").capabilities
        self.assertFalse(caps.has_buttons)

    def test_sendblue_defaults_to_not_initiating(self):
        """Whether the free sandbox may message first is genuinely unresolved in
        their docs. Defaulting to False makes the session user-initiated, which
        also means it cannot page anyone at 3am."""
        from openrecruiter.channels.sendblue import SendBlueChannel
        caps = SendBlueChannel(api_key="k", api_secret="s",
                               from_number="+1", to_number="+2",
                               can_initiate=None).capabilities
        self.assertFalse(caps.can_initiate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
