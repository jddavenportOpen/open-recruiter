"""Invariants for the apply path, proved against a local mock ATS.

Every test here is written so that deleting the behaviour it describes turns it
red. Three of them exist because the ancestor system shipped the bug they
describe:

  * the verifier read only the top-level page, so an iframe confirmation was
    invisible and the one real attempt was recorded as unverified
  * a landing URL was treated as a receipt
  * a check that could not run was indistinguishable from a check that passed

Nothing in this file talks to the network. `guard_url` would refuse it.
"""
from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from unittest import mock

from openrecruiter import apply, mock_ats
from openrecruiter.apply import (NEEDS_HUMAN, Answer, ApplyPacket, ExternalHostRefused,
                                 Outcome, Verdict, answer_for, confirm_signals,
                                 confirmation_evidence, extract_reference, guard_url,
                                 http_fetch, is_done, normalize, preflight, read_back,
                                 submit)
from openrecruiter.store import State, Store

NAME = "Dana O'Neil"          # the apostrophe is load-bearing: the mock curls it
EMAIL = "dana@example.org"
RESUME = "Product leader who ships. " * 10


def full_packet(**over) -> ApplyPacket:
    fields = {"full_name": NAME, "email": EMAIL, "phone": "415-555-0142",
              "resume_text": RESUME, "work_authorization": "yes"}
    fields.update(over.pop("fields", {}))
    return ApplyPacket(fields=fields, applicant_name=over.pop("applicant_name", NAME),
                       applicant_email=over.pop("applicant_email", EMAIL), **over)


class ATSCase(unittest.TestCase):
    """A mock ATS and a store, fresh per test so request counts mean something."""

    def setUp(self):
        self.ats = mock_ats.MockATS().start()
        self.addCleanup(self.ats.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.addCleanup(self.store.close)
        self._n = 0

    def approved(self, route: str, *, company="Northwind Systems", tier="standard") -> str:
        """One application, walked through the store's real state machine to
        APPROVED -- which is only reachable from AWAITING_APPROVAL, i.e. a human."""
        self._n += 1
        app_id = f"app{self._n}"
        url = f"{self.ats.url(route)}?a={app_id}"
        self.store.upsert_discovered(app_id, company, "Principal PM", url, tier=tier)
        self.store.transition(app_id, State.BUILDING)
        self.store.transition(app_id, State.AWAITING_APPROVAL)
        self.store.transition(app_id, State.APPROVED)
        return app_id


# --------------------------------------------------------------------------- #
class TheMockReproducesTheFailureModes(ATSCase):
    """A fixture that lies makes every test above it meaningless, so the fixture
    gets asserted too."""

    def test_thank_you_is_byte_identical_for_every_applicant(self):
        a = http_fetch(self.ats.url("thank-you")).body
        b = http_fetch(self.ats.url("thank-you")).body
        self.assertEqual(a, b)
        for token in (NAME, EMAIL, "AB-"):
            self.assertNotIn(token, a, "the static page leaked something specific")

    def test_the_lying_route_stores_nothing(self):
        app = self.approved("lies")
        submit(self.store, app, full_packet())
        self.assertEqual(self.ats.posts("/lies"), 1)
        self.assertEqual(self.ats.stored(), 0, "the lying route kept a record")

    def test_the_redirect_route_really_does_store_the_application(self):
        """Unverifiable is not the same as unsent. This is why the result is held
        for a human rather than retried."""
        app = self.approved("redirect")
        submit(self.store, app, full_packet())
        self.assertEqual(self.ats.stored(), 1)

    def test_the_dropping_route_keeps_everything_except_the_phone(self):
        app = self.approved("drops")
        r = submit(self.store, app, full_packet(),
                   record_url_template=self.ats.record_url_template())
        row = self.ats.record(r.reference)
        self.assertIsNotNone(row)
        self.assertEqual(row.get("email"), EMAIL)
        self.assertNotIn("phone", row)

    def test_the_iframe_route_hides_the_receipt_one_frame_down(self):
        app = self.approved("iframe")
        submit(self.store, app, full_packet())
        posted = http_fetch(f"{self.ats.url('iframe')}?a=x",
                            {"full_name": NAME, "email": EMAIL, "resume_text": RESUME})
        self.assertIn("<iframe", posted.body)
        for token in (EMAIL, "AB-", "Application received"):
            self.assertNotIn(token, posted.body,
                             "the top-level page gave the answer away -- not even in an "
                             "attribute; the iframe traversal test would prove nothing")

    def test_the_unmarked_form_declares_nothing_required(self):
        markup = http_fetch(self.ats.url("unmarked")).body
        visible = markup.split("<input type='hidden'")[0] + markup.split("</p>", 1)[-1]
        self.assertNotIn("required>", visible.replace("hidden' name='tracking_id' "
                                                      "value='t-88' required>", ""))

    def test_it_listens_on_loopback_only(self):
        self.assertTrue(self.ats.base.startswith("http://127.0.0.1:"))


# --------------------------------------------------------------------------- #
class TheNetworkIsShutByDefault(unittest.TestCase):
    """This lane must not be capable of touching a real employer out of the box."""

    def setUp(self):
        self._saved = os.environ.pop("OPENRECRUITER_ALLOW_REAL_SUBMIT", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("OPENRECRUITER_ALLOW_REAL_SUBMIT", None)
        else:
            os.environ["OPENRECRUITER_ALLOW_REAL_SUBMIT"] = self._saved

    def test_a_real_host_is_refused_with_no_flags(self):
        with self.assertRaises(ExternalHostRefused):
            guard_url("https://boards.greenhouse.io/acme/jobs/1")

    def test_opting_in_at_the_call_site_is_not_enough(self):
        """The environment switch is the human's; the parameter is the caller's.
        One without the other opens nothing."""
        with self.assertRaises(ExternalHostRefused):
            guard_url("https://boards.greenhouse.io/acme/jobs/1", allow_external=True)

    def test_the_environment_switch_alone_is_not_enough(self):
        os.environ["OPENRECRUITER_ALLOW_REAL_SUBMIT"] = "1"
        with self.assertRaises(ExternalHostRefused):
            guard_url("https://boards.greenhouse.io/acme/jobs/1")

    def test_both_together_open_it(self):
        os.environ["OPENRECRUITER_ALLOW_REAL_SUBMIT"] = "1"
        u = guard_url("https://boards.greenhouse.io/acme/jobs/1", allow_external=True)
        self.assertEqual(u.hostname, "boards.greenhouse.io")

    def test_loopback_is_always_allowed(self):
        for url in ("http://127.0.0.1:9/x", "http://localhost:9/x", "http://[::1]:9/x"):
            self.assertTrue(guard_url(url).hostname)

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi", ""):
            with self.assertRaises(ExternalHostRefused):
                guard_url(url)

    def test_the_fetcher_refuses_without_making_a_request(self):
        got = http_fetch("http://example.com/apply")
        self.assertEqual(got.kind, "guard")
        self.assertIsNone(got.body)
        self.assertFalse(got.ok)


# --------------------------------------------------------------------------- #
class ReadBackTraversesIframes(ATSCase):
    """THE ancestor bug. The verifier read the top frame, the employer embedded
    its form, and one real submission was recorded as unverified forever."""

    def _post_to_iframe_route(self):
        return http_fetch(self.ats.url("iframe"),
                          {"full_name": NAME, "email": EMAIL, "resume_text": RESUME})

    def test_the_confirmation_inside_the_frame_is_read(self):
        posted = self._post_to_iframe_route()
        page = read_back(posted.url, markup=posted.body)
        self.assertTrue(page.frames, "no frame was followed")
        self.assertIn(EMAIL, page.text())
        self.assertIsNotNone(extract_reference(page))

    def test_reading_only_the_top_frame_would_have_missed_it(self):
        """If this ever passes, the traversal above stopped being load-bearing."""
        posted = self._post_to_iframe_route()
        top_only = read_back(posted.url, markup=posted.body, max_depth=0)
        self.assertEqual(top_only.frames, ())
        self.assertNotIn(EMAIL, top_only.text())

    def test_an_iframe_route_verifies_end_to_end(self):
        app = self.approved("iframe")
        r = submit(self.store, app, full_packet())
        self.assertIs(r.outcome, Outcome.VERIFIED, r.reason)
        self.assertIs(self.store.get(app).state, State.SUBMITTED_VERIFIED)

    def test_a_frame_we_cannot_read_is_recorded_not_dropped(self):
        # port 1 refuses instantly: a frame we can SEE and cannot READ. Claiming
        # "we looked everywhere" off a partial read is the same class of mistake
        # as not looking at all.
        markup = "<p>hi</p><iframe src='http://127.0.0.1:1/receipt'></iframe>"
        page = read_back(self.ats.url("normal"), markup=markup)
        self.assertEqual(page.frames, ())
        self.assertEqual(page.unread_frames, ("http://127.0.0.1:1/receipt",),
                         "a frame we could not read vanished silently")

    def test_an_inline_srcdoc_frame_is_read(self):
        page = read_back("http://127.0.0.1:1/x",
                         markup="<p>chrome</p><iframe srcdoc='&lt;p&gt;ref AB-9999&lt;/p&gt;'></iframe>")
        self.assertEqual(extract_reference(page), "AB-9999")


# --------------------------------------------------------------------------- #
class PreflightRefusesWhatItCannotSee(ATSCase):
    def test_a_missing_required_field_blocks_and_nothing_is_sent(self):
        app = self.approved("sneaky")
        thin = ApplyPacket(fields={"full_name": NAME, "email": EMAIL,
                                   "resume_text": RESUME},   # no work_authorization
                           applicant_name=NAME, applicant_email=EMAIL)
        r = submit(self.store, app, thin)
        self.assertIs(r.outcome, Outcome.BLOCKED)
        self.assertIn("work_authorization", r.reason)
        self.assertEqual(self.ats.posts(), 0, "a blocked application was still posted")
        self.assertIs(self.store.get(app).state, State.APPROVED,
                      "a blocked application must stay approved, not be recorded as sent")

    def test_the_easy_to_miss_field_is_genuinely_required_by_the_server(self):
        """Preflight's judgement has to match reality, or it is just an opinion."""
        got = http_fetch(self.ats.url("sneaky"),
                         {"full_name": NAME, "email": EMAIL, "resume_text": RESUME})
        self.assertEqual(got.status, 200, "the status line hides the failure on purpose")
        self.assertIn("Please complete", got.body)
        self.assertEqual(self.ats.stored(), 0)

    def test_a_form_with_no_required_markers_is_blind_not_permissive(self):
        page = read_back(self.ats.url("unmarked"))
        pf = preflight(page, full_packet())
        self.assertIs(pf.verdict, Verdict.BLIND)
        self.assertFalse(pf.ok)
        self.assertIn("required", pf.reason)

    def test_the_blind_form_is_refused_end_to_end(self):
        app = self.approved("unmarked")
        r = submit(self.store, app, full_packet())
        self.assertIs(r.outcome, Outcome.BLOCKED)
        self.assertEqual(self.ats.posts(), 0)

    def test_an_unreadable_page_is_blind(self):
        page = read_back("http://127.0.0.1:1/apply")      # nothing is listening
        self.assertIsNone(page.text())
        self.assertIs(preflight(page, full_packet()).verdict, Verdict.BLIND)
        self.assertIs(preflight(None, full_packet()).verdict, Verdict.BLIND)

    def test_a_missing_posting_is_blind_rather_than_unrequired(self):
        """A 404 body is still read -- a re-rendered form with an error banner
        often arrives as one -- but it carries no controls, so it refuses."""
        page = read_back(self.ats.url("no-such-route"))
        self.assertIsNotNone(page.text())
        self.assertIs(preflight(page, full_packet()).verdict, Verdict.BLIND)

    def test_a_page_with_no_controls_at_all_is_blind(self):
        page = read_back("http://127.0.0.1:1/x", markup="<p>Applications are closed.</p>")
        self.assertIs(preflight(page, full_packet()).verdict, Verdict.BLIND)

    def test_a_preflight_that_throws_refuses_rather_than_passing(self):
        page = read_back(self.ats.url("normal"))
        with mock.patch.object(apply, "_ControlParser",
                               side_effect=RuntimeError("parser exploded")):
            pf = preflight(page, full_packet())
        self.assertIs(pf.verdict, Verdict.BLIND)
        self.assertIn("parser exploded", pf.reason)

    def test_a_hidden_required_control_is_reported_but_not_demanded(self):
        pf = preflight(read_back(self.ats.url("normal")), full_packet())
        self.assertIs(pf.verdict, Verdict.OK)
        self.assertIn("tracking_id", pf.hidden_required)
        self.assertNotIn("tracking_id", pf.missing)
        self.assertNotIn("tracking_id", pf.required)

    def test_a_deferred_answer_is_not_a_committed_value(self):
        page = read_back(self.ats.url("sneaky"))
        pf = preflight(page, full_packet(fields={"work_authorization": NEEDS_HUMAN}))
        self.assertIs(pf.verdict, Verdict.BLOCKED)
        self.assertIn("work_authorization", pf.missing)

    def test_blank_and_placeholder_values_do_not_count_as_answers(self):
        page = read_back(self.ats.url("normal"))
        for bad in ("", "   ", "TODO", "tbd", "<fill in>"):
            pf = preflight(page, full_packet(fields={"email": bad}))
            self.assertIs(pf.verdict, Verdict.BLOCKED, f"{bad!r} passed as an answer")

    def test_preflight_names_its_proof(self):
        pf = preflight(read_back(self.ats.url("normal")), full_packet())
        self.assertEqual(set(pf.proof), {"full_name", "email", "resume_text"})
        self.assertEqual(pf.proof["email"], EMAIL)

    def test_an_unanswered_optional_field_is_never_typed_onto_the_form(self):
        app = self.approved("normal")
        r = submit(self.store, app,
                   full_packet(fields={"phone": NEEDS_HUMAN}))   # optional, not required
        self.assertIs(r.outcome, Outcome.BLOCKED)
        self.assertEqual(self.ats.posts(), 0)


# --------------------------------------------------------------------------- #
class CouldNotLookIsNotNothingThere(unittest.TestCase):
    def test_an_unreadable_side_returns_none_not_an_empty_set(self):
        self.assertIsNone(confirm_signals(None, "after"))
        self.assertIsNone(confirm_signals("before", None))
        unread = apply.PageState(url="x", error="timed out")
        self.assertIsNone(confirm_signals("before", unread))
        self.assertIsNone(confirm_signals(unread, "after"))

    def test_read_but_unchanged_returns_an_empty_set(self):
        out = confirm_signals("same\ntext", "same\ntext")
        self.assertIsNotNone(out, "a successful read came back as 'could not look'")
        self.assertEqual(out, set())

    def test_only_new_text_counts(self):
        out = confirm_signals("Northwind Careers\nApply now", "Northwind Careers\nThanks!")
        self.assertEqual(out, {"Thanks!"})

    def test_cosmetic_differences_are_not_new_text(self):
        out = confirm_signals("Northwind  Careers", "northwind careers ")
        self.assertEqual(out, set())

    def test_a_blind_read_is_not_treated_as_a_confirmation(self):
        ev = confirmation_evidence(None, full_packet())
        self.assertFalse(ev.specific)
        self.assertFalse(ev.said_received)


# --------------------------------------------------------------------------- #
class AUrlNeverVerifies(ATSCase):
    def test_a_static_thank_you_page_is_refused(self):
        app = self.approved("redirect")
        r = submit(self.store, app, full_packet())
        self.assertIs(r.outcome, Outcome.UNVERIFIED, r.reason)
        self.assertTrue(r.final_url.endswith("/thank-you"),
                        "the run must really have landed on the static page")
        self.assertIs(self.store.get(app).state, State.SUBMITTED_UNVERIFIED)
        self.assertFalse(is_done(self.store.get(app)))

    def test_a_generic_success_message_is_refused(self):
        app = self.approved("lies")
        r = submit(self.store, app, full_packet())
        self.assertIs(r.outcome, Outcome.UNVERIFIED, r.reason)
        self.assertTrue(r.evidence.said_received,
                        "the page did claim success; that is the trap")
        self.assertEqual(r.evidence.markers, (),
                         "a page carrying nothing of ours must yield no markers")

    def test_a_confirmation_echoing_our_own_text_verifies(self):
        app = self.approved("normal")
        r = submit(self.store, app, full_packet())
        self.assertIs(r.outcome, Outcome.VERIFIED, r.reason)
        self.assertIn(NAME, r.evidence.markers)
        self.assertTrue(is_done(self.store.get(app)))

    def test_typographic_apostrophes_do_not_defeat_the_match(self):
        """The mock curls the name the way a CMS does. A raw comparison misses it,
        records a real submission as unproven, and (in the ancestor) that blocked
        the rail from ever graduating."""
        posted = http_fetch(self.ats.url("normal"),
                            {"full_name": NAME, "email": EMAIL, "resume_text": RESUME})
        self.assertIn("’", posted.body, "the mock stopped curling the apostrophe")
        self.assertNotIn(NAME, posted.body, "a raw match would have worked; nothing proved")
        signals = confirm_signals(http_fetch(self.ats.url("normal")).body, posted.body)
        ev = confirmation_evidence(signals, full_packet())
        self.assertTrue(ev.specific)
        self.assertIn(NAME, ev.markers)

    def test_the_record_read_back_catches_a_silently_dropped_field(self):
        """The confirmation page is rendered from the request, so it looks perfect.
        Only the stored row shows the phone never landed."""
        app = self.approved("drops")
        r = submit(self.store, app, full_packet(),
                   record_url_template=self.ats.record_url_template())
        self.assertIs(r.outcome, Outcome.UNVERIFIED, r.reason)
        self.assertEqual(r.record.missing, ("phone",))
        self.assertIs(self.store.get(app).state, State.SUBMITTED_UNVERIFIED)

    def test_that_same_route_would_have_passed_on_the_page_text_alone(self):
        """Names what the record probe is actually buying."""
        app = self.approved("drops")
        r = submit(self.store, app, full_packet())     # no record probe
        self.assertIs(r.outcome, Outcome.VERIFIED)

    def test_a_record_probe_with_no_reference_to_look_up_is_refused(self):
        app = self.approved("normal")     # this route confirms without a reference
        r = submit(self.store, app, full_packet(),
                   record_url_template=self.ats.record_url_template())
        self.assertIs(r.outcome, Outcome.UNVERIFIED)
        self.assertIn("no reference", r.reason)

    def test_a_record_that_cannot_be_read_is_refused(self):
        app = self.approved("drops")
        r = submit(self.store, app, full_packet(),
                   record_url_template=self.ats.base + "/records/NOPE-{ref}")
        self.assertIs(r.outcome, Outcome.UNVERIFIED)
        self.assertFalse(r.record.ok)


# --------------------------------------------------------------------------- #
class AnswersAreNeverFabricated(unittest.TestCase):
    BANK = {"answers": [
        {"id": "work_auth",
         "question": "Are you legally authorized to work in the United States?",
         "aliases": ["authorized to work", "work authorization"],
         "value": "Yes", "source": "stated by the applicant, 2026-09-01"},
        {"id": "sponsorship",
         "question": "Will you now or in the future require sponsorship?",
         "aliases": ["require sponsorship"], "value": "No"},
        {"id": "py_years",
         "question": "How many years of experience do you have with Python?",
         "aliases": ["years of experience with python"], "value": "7"},
        {"id": "why_us", "question": "Why do you want to work at Northwind?",
         "aliases": ["why do you want to work"],
         "value": "Because I have used the product daily for three years."},
        {"id": "salary", "question": "Desired salary", "value": ""},
    ]}

    def test_an_exact_question_is_answered(self):
        a = answer_for("Are you legally authorized to work in the United States? *",
                       self.BANK)
        self.assertEqual(a.value, "Yes")
        self.assertEqual(a.matched, "work_auth")
        self.assertIn("2026-09-01", a.source)

    def test_a_phrase_the_bank_owns_is_answered(self):
        self.assertEqual(answer_for("Are you authorized to work in the US?",
                                    self.BANK).value, "Yes")

    def test_a_neighbouring_question_is_not_answered(self):
        """The bank knows Python. It does not know Rust, and the shape of the
        question is not permission to reuse the number."""
        a = answer_for("How many years of experience do you have with Rust?", self.BANK)
        self.assertIs(a.value, NEEDS_HUMAN)

    def test_nothing_is_synthesised_from_the_rest_of_the_bank(self):
        bank = dict(self.BANK, jobs=[{"company": "Acme", "dates": "2016 to 2024"}])
        a = answer_for("How many years of professional experience do you have?", bank)
        self.assertIs(a.value, NEEDS_HUMAN,
                      "an answer was computed out of the job history")

    def test_an_ambiguous_question_is_never_resolved_by_picking_one(self):
        a = answer_for("Do you require sponsorship, or are you authorized to work here?",
                       self.BANK)
        self.assertIs(a.value, NEEDS_HUMAN)
        self.assertIn("more than one", a.reason)

    def test_an_essay_prompt_matched_on_a_keyword_goes_to_a_human(self):
        """The bank owns the phrase 'why do you want to work', so this DOES match.
        Answering it would paste a paragraph about Northwind onto Acme's form --
        a fabrication that reads perfectly and names the wrong company."""
        q = "Why do you want to work at Acme, and what would you do first?"
        a = answer_for(q, self.BANK)
        self.assertIs(a.value, NEEDS_HUMAN, "a canned essay was reused across employers")
        self.assertEqual(a.matched, "why_us", "the match really did fire; the guard is "
                                              "what refused it, not a failure to match")

    def test_the_same_essay_asked_exactly_is_answered(self):
        self.assertNotEqual(answer_for("Why do you want to work at Northwind?",
                                       self.BANK).value, NEEDS_HUMAN)

    def test_an_empty_bank_value_is_not_an_answer(self):
        a = answer_for("Desired salary", self.BANK)
        self.assertIs(a.value, NEEDS_HUMAN)
        self.assertIn("placeholder", a.reason)

    def test_an_unknown_question_is_never_guessed(self):
        for q in ("What is your greatest weakness?",
                  "Please list three references with phone numbers.",
                  "What is your current base salary?",
                  "Do you hold an active security clearance?",
                  "Are you related to a current employee?"):
            self.assertIs(answer_for(q, self.BANK).value, NEEDS_HUMAN, q)

    def test_a_missing_or_broken_bank_refuses_rather_than_crashing(self):
        for bank in (None, {}, [], "nonsense", {"answers": None}, {"answers": [1, 2]}):
            self.assertIs(answer_for("Are you authorized to work?", bank).value,
                          NEEDS_HUMAN)

    def test_an_empty_question_is_refused(self):
        self.assertIs(answer_for("", self.BANK).value, NEEDS_HUMAN)

    def test_a_single_word_alias_does_not_match_by_containment(self):
        bank = {"answers": [{"id": "x", "question": "Salary", "aliases": ["salary"],
                             "value": "$200,000"}]}
        self.assertIs(answer_for("What did your last employer pay in salary bands?",
                                 bank).value, NEEDS_HUMAN)

    def test_a_deferred_answer_reads_as_false(self):
        """So `if answer.value:` cannot accidentally send it."""
        self.assertFalse(NEEDS_HUMAN)
        self.assertTrue(Answer(NEEDS_HUMAN).needs_human)


# --------------------------------------------------------------------------- #
class ReachEmployersAreNeverSubmitted(ATSCase):
    def setUp(self):
        super().setUp()
        os.environ["OPENRECRUITER_ALLOW_REAL_SUBMIT"] = "1"   # every flag on
        self.addCleanup(os.environ.pop, "OPENRECRUITER_ALLOW_REAL_SUBMIT", None)

    def test_a_reach_employer_is_refused_with_every_flag_set(self):
        app = self.approved("normal", company="Anthropic")
        r = submit(self.store, app, full_packet(), allow_external=True,
                   record_url_template=self.ats.record_url_template())
        self.assertIs(r.outcome, Outcome.REFUSED)
        self.assertEqual(self.ats.posts(), 0, "a reach employer was contacted")
        self.assertIs(self.store.get(app).state, State.APPROVED)

    def test_a_tier_zero_row_is_refused_whatever_the_company(self):
        for tier in ("reach", "tier0", "tier 0", "Tier-0", "t0", "0"):
            app = self.approved("normal", company="Widgets Inc", tier=tier)
            r = submit(self.store, app, full_packet(), allow_external=True)
            self.assertIs(r.outcome, Outcome.REFUSED, tier)
        self.assertEqual(self.ats.posts(), 0)

    def test_a_mislabelled_reach_company_is_still_refused(self):
        """The row says standard. The employer is OpenAI. Either read is enough."""
        app = self.approved("normal", company="OpenAI", tier="standard")
        r = submit(self.store, app, full_packet(), allow_external=True)
        self.assertIs(r.outcome, Outcome.REFUSED)
        self.assertEqual(self.ats.posts(), 0)

    def test_an_ordinary_employer_still_goes_through(self):
        app = self.approved("normal", company="Northwind Systems")
        self.assertIs(submit(self.store, app, full_packet()).outcome, Outcome.VERIFIED)


# --------------------------------------------------------------------------- #
class UnverifiableIsHeldNotRetried(ATSCase):
    def test_a_second_attempt_on_a_held_application_is_refused(self):
        app = self.approved("lies")
        first = submit(self.store, app, full_packet())
        self.assertIs(first.outcome, Outcome.UNVERIFIED)
        sent = self.ats.posts()

        again = submit(self.store, app, full_packet())
        self.assertIs(again.outcome, Outcome.REFUSED)
        self.assertEqual(self.ats.posts(), sent, "a held application was re-sent")
        self.assertIn("waiting on a human", again.reason,
                      "the refusal has to say it is waiting on a person -- 'wrong "
                      "state' reads like a bug and invites someone to force it")

    def test_the_store_itself_also_refuses_the_retry(self):
        """Defence in depth, and worth asserting: even if submit() lost its own
        check, SUBMITTED_UNVERIFIED -> SUBMITTING is not a legal transition."""
        from openrecruiter.store import LEGAL
        self.assertNotIn(State.SUBMITTING, LEGAL[State.SUBMITTED_UNVERIFIED])

    def test_held_is_not_done(self):
        app = self.approved("lies")
        submit(self.store, app, full_packet())
        self.assertFalse(is_done(self.store.get(app)),
                         "SUBMITTED_UNVERIFIED was counted as done")

    def test_the_hold_is_written_to_the_ledger_with_its_reason(self):
        app = self.approved("redirect")
        submit(self.store, app, full_packet())
        notes = [e["note"] for e in self.store.history(app)
                 if e["to_state"] == State.SUBMITTED_UNVERIFIED.value]
        self.assertTrue(notes and notes[0].strip(),
                        "nothing in the ledger says WHY a human has to look")

    def test_a_connection_that_never_opened_is_failed_not_held(self):
        """Nothing was sent, so this one is genuinely safe to rebuild and retry."""
        app = self.approved("normal")
        real = apply.http_fetch

        def dead(url, data=None):
            return real(url) if data is None else apply.Fetched(
                url=url, error="[Errno 61] Connection refused", kind="connect")

        r = submit(self.store, app, full_packet(), fetch=dead)
        self.assertIs(r.outcome, Outcome.FAILED)
        self.assertIs(self.store.get(app).state, State.FAILED)

    def test_a_failure_after_the_connection_opened_is_held(self):
        app = self.approved("normal")
        real = apply.http_fetch

        def flaky(url, data=None):
            return real(url) if data is None else apply.Fetched(
                url=url, error="timed out", kind="read")

        r = submit(self.store, app, full_packet(), fetch=flaky)
        self.assertIs(r.outcome, Outcome.UNVERIFIED)
        self.assertIs(self.store.get(app).state, State.SUBMITTED_UNVERIFIED)


# --------------------------------------------------------------------------- #
class OnlyAHumanDecidesAndOnlyOneAtATime(ATSCase):
    FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                 "auto_approve", "approve_above", "batch_approve", "approve_batch",
                 "submit_all", "submit_many", "apply_all")

    def test_neither_module_defines_a_verb_that_decides_many(self):
        for mod in (apply, mock_ats):
            src = inspect.getsource(mod).lower()
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, src, f"{mod.__name__} mentions {bad}")

    def test_submit_takes_one_application_id(self):
        params = list(inspect.signature(submit).parameters)
        self.assertEqual(params[:3], ["store", "app_id", "packet"])

    def test_an_unapproved_application_is_never_sent(self):
        """The only door into APPROVED is AWAITING_APPROVAL -> a human decision on
        this one application. submit() will not open any other door."""
        self.store.upsert_discovered("x1", "Northwind Systems", "PM",
                                     self.ats.url("normal") + "?a=x1")
        for state in (State.DISCOVERED, State.BUILDING, State.AWAITING_APPROVAL):
            if state is not State.DISCOVERED:
                self.store.transition("x1", state)
            r = submit(self.store, "x1", full_packet())
            self.assertIs(r.outcome, Outcome.REFUSED, state.value)
        self.assertEqual(self.ats.posts(), 0)

    def test_a_resume_below_its_bar_can_never_reach_this_path(self):
        """BELOW_BAR is terminal except via a rebuild, so there is no sequence of
        transitions that carries a failed resume into submit()."""
        self.store.upsert_discovered("x2", "Northwind Systems", "PM",
                                     self.ats.url("normal") + "?a=x2")
        self.store.transition("x2", State.BUILDING)
        self.store.transition("x2", State.BELOW_BAR)
        r = submit(self.store, "x2", full_packet())
        self.assertIs(r.outcome, Outcome.REFUSED)
        self.assertEqual(self.ats.posts(), 0)

    def test_an_unknown_application_is_refused_loudly(self):
        r = submit(self.store, "never-heard-of-it", full_packet())
        self.assertIs(r.outcome, Outcome.REFUSED)
        self.assertIn("never-heard-of-it", r.reason)


# --------------------------------------------------------------------------- #
class TextNormalisation(unittest.TestCase):
    def test_curly_and_straight_apostrophes_compare_equal(self):
        self.assertEqual(normalize("Dana O’Neil"), normalize("dana o'neil"))

    def test_dashes_spaces_and_case_fold_together(self):
        self.assertEqual(normalize("A—B C"), "a-b c")

    def test_extract_reference_keeps_its_case(self):
        self.assertEqual(extract_reference({"Reference AB-1001 received"}), "AB-1001")
        self.assertIsNone(extract_reference({"thank you"}))
        self.assertIsNone(extract_reference(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
