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

import http.server
import inspect
import os
import tempfile
import threading
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


class _Redirector:
    """A loopback server that answers 3xx, so the redirect CHAIN can be tested.

    It exists because nothing in this suite exercised a redirect at all, and
    urllib follows them without asking: the guard can be a wall on the URL you
    typed and a wide-open door on the one the server picks next.

    Routes:
      /to-external   302 to a host outside this machine
      /to-metadata   302 to the cloud metadata address
      /hop1          302 to /hop2 on this same server (a legal first hop)
      /hop2          302 to a host outside this machine
      /to-file       302 to a file:// url -- urllib refuses this one by raising
                     HTTPError rather than redirecting, which is a different
                     code path with the same destination
      /ok            302 to /landed on this same server
      /landed        200 with a body, so a followed redirect is provable
    """

    EXTERNAL = "http://192.0.2.1/"                              # TEST-NET-1
    METADATA = "http://169.254.169.254/latest/meta-data/"
    LOCAL_FILE = "file:///etc/passwd"

    def __init__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_a):
                pass

            def _redirect(self, to):
                self.send_response(302)
                self.send_header("Location", to)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path == "/to-external":
                    return self._redirect(outer.EXTERNAL)
                if self.path == "/to-metadata":
                    return self._redirect(outer.METADATA)
                if self.path == "/hop1":
                    return self._redirect("/hop2")
                if self.path == "/hop2":
                    return self._redirect(outer.EXTERNAL)
                if self.path == "/to-file":
                    return self._redirect(outer.LOCAL_FILE)
                if self.path == "/ok":
                    return self._redirect("/landed")
                body = b"<p>landed</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self._t = threading.Thread(target=self._srv.serve_forever,
                                   kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self) -> "_Redirector":
        self._t.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._t.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}{path}"


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
class EveryHopOfARedirectIsGuarded(unittest.TestCase):
    """The guard has to hold for the request that leaves, not the URL we typed.

    urllib follows 3xx through its own HTTPRedirectHandler, which re-enters
    nothing. So a page on loopback answering `302 Location: http://<anywhere>/`
    was enough to make a real outbound request with allow_external=False and
    OPENRECRUITER_ALLOW_REAL_SUBMIT unset -- the DEFAULT, supposedly mock-only
    configuration -- and `Fetched.url` came back wearing a host nobody chose.

    A short timeout is deliberate: if the guard ever stops covering redirects,
    these go red in seconds rather than hanging on a black-holed address.
    """

    TIMEOUT = 2.0

    def setUp(self):
        self._saved = os.environ.pop("OPENRECRUITER_ALLOW_REAL_SUBMIT", None)
        self.addCleanup(self._restore)
        self.r = _Redirector().start()
        self.addCleanup(self.r.stop)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("OPENRECRUITER_ALLOW_REAL_SUBMIT", None)
        else:
            os.environ["OPENRECRUITER_ALLOW_REAL_SUBMIT"] = self._saved

    def _assert_refused(self, path, host):
        got = http_fetch(self.r.url(path), timeout=self.TIMEOUT)
        self.assertEqual(got.kind, "guard",
                         f"the redirect to {host} was followed off the sandbox "
                         f"(kind={got.kind!r}, error={got.error!r})")
        self.assertIsNone(got.body)
        self.assertFalse(got.ok)
        self.assertIn(host, got.error)
        return got

    def test_a_redirect_to_an_external_host_is_refused(self):
        self._assert_refused("/to-external", "192.0.2.1")

    def test_a_redirect_to_cloud_metadata_is_refused(self):
        self._assert_refused("/to-metadata", "169.254.169.254")

    def test_the_second_hop_is_guarded_as_hard_as_the_first(self):
        """Hop one is loopback and legal. Hop two must still be judged, or the
        opt-in is something a chain can launder by taking one innocent step."""
        self._assert_refused("/hop1", "192.0.2.1")

    def test_a_refused_redirect_does_not_report_the_external_url_as_final(self):
        got = self._assert_refused("/to-external", "192.0.2.1")
        self.assertEqual(got.url, self.r.url("/to-external"),
                         "the refusal handed back a URL we never reached")

    def test_a_redirect_to_a_local_file_is_refused_and_not_reported_as_final(self):
        """This one never reaches the redirect handler -- urllib raises instead.
        Same wall, different door: the refusal must look identical, and the
        file:// url must not come back as the page we ended on, or read_back
        walks frames against it."""
        got = http_fetch(self.r.url("/to-file"), timeout=self.TIMEOUT)
        self.assertEqual(got.kind, "guard", f"error={got.error!r}")
        self.assertIsNone(got.body)
        self.assertEqual(got.url, self.r.url("/to-file"))

    def test_a_loopback_redirect_is_still_followed(self):
        """The control: the guard refuses hops, it does not break redirects."""
        got = http_fetch(self.r.url("/ok"), timeout=self.TIMEOUT)
        self.assertTrue(got.ok, got.error)
        self.assertTrue(got.url.endswith("/landed"))
        self.assertIn("landed", got.body)

    def test_read_back_cannot_be_walked_off_the_sandbox_either(self):
        """read_back rides the FINAL url, so an unguarded chain contaminates the
        verification read too, not just the fetch."""
        page = read_back(self.r.url("/to-external"), timeout=self.TIMEOUT)
        self.assertFalse(page.readable)
        self.assertIsNone(page.text())
        self.assertIn("192.0.2.1", page.error)


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
class APartlyReadPageIsNotAReadPage(ATSCase):
    """`unread_frames` was recorded by read_back and consulted by nobody.

    Proven before the fix: a top-level form carrying one satisfiable required
    field, plus an iframe on a dead port, came back Verdict.OK -- so the real
    form one frame down was never read and the application went out incomplete,
    with `unread_frames` holding the evidence the whole time.
    """

    FORM = ("<form method='post'><input name='full_name' required>"
            "<iframe src='http://127.0.0.1:1/the-real-form'></iframe></form>")

    def test_an_unread_frame_makes_the_page_blind_not_ok(self):
        page = read_back(self.ats.url("normal"), markup=self.FORM)
        self.assertTrue(page.unread_frames, "the fixture's frame was reachable")
        pf = preflight(page, full_packet())
        self.assertIs(pf.verdict, Verdict.BLIND,
                      "a form with an unread frame passed preflight")
        self.assertFalse(pf.ok)
        self.assertIn("127.0.0.1:1/the-real-form", pf.reason)

    def test_nothing_is_posted_when_part_of_the_form_could_not_be_read(self):
        app = self.approved("normal")
        seen = []

        def fetch(u, data=None):
            seen.append(("POST" if data is not None else "GET", u))
            if data is not None:
                return apply.Fetched(url=u, status=200, body=(
                    f"<p>Application received. A copy was sent to {EMAIL}.</p>"))
            if u.startswith(self.ats.url("normal")):
                return apply.Fetched(url=u, status=200, body=self.FORM)
            return apply.Fetched(url=u, error="[Errno 61] Connection refused",
                                 kind="connect")

        r = submit(self.store, app, full_packet(), fetch=fetch)
        self.assertIs(r.outcome, Outcome.BLOCKED, r.reason)
        self.assertEqual([m for m, _ in seen if m == "POST"], [],
                         "an application was posted while part of its form was unread")
        self.assertIs(self.store.get(app).state, State.APPROVED)

    def test_a_document_the_parser_could_not_finish_is_recorded(self):
        """A frame missed because the parser died is invisible, not absent. Both
        parsers that walk a document are covered, because either one dying means
        the frame list we have is not known to be the frame list there is."""
        for name in ("_TextParser", "_FrameParser"):
            with self.subTest(parser=name):
                with mock.patch.object(apply, name,
                                       side_effect=RuntimeError("parser exploded")):
                    page = read_back(self.ats.url("normal"),
                                     markup="<p>hi</p><iframe src='/receipt'></iframe>")
                self.assertTrue(
                    any(u.endswith("#unparsed") for u in page.unread_frames),
                    "a half-parsed document claimed a complete frame list")
                self.assertIs(preflight(page, full_packet()).verdict, Verdict.BLIND)

    def test_a_fully_read_page_is_not_penalised(self):
        """The control: ordinary pages must not all turn BLIND."""
        page = read_back(self.ats.url("normal"))
        self.assertEqual(page.unread_frames, ())
        self.assertIs(preflight(page, full_packet()).verdict, Verdict.OK)


# --------------------------------------------------------------------------- #
class ABoolIsNotAnAnswer(ATSCase):
    """answer_for refuses a bool -- "not something to type into a form" -- while
    _committed accepted one and wire() str()'d it to the literal "True". Two
    halves of the same module disagreeing about the same value; answer_for is
    the one that is right."""

    def test_a_bare_bool_is_not_a_committed_value(self):
        page = read_back(self.ats.url("normal"))
        for bad in (True, False):
            pf = preflight(page, full_packet(fields={"email": bad}))
            self.assertIs(pf.verdict, Verdict.BLOCKED, f"{bad!r} passed as an answer")
            self.assertIn("email", pf.missing)

    def test_the_wire_body_never_carries_the_word_True(self):
        wire = full_packet(fields={"phone": True}).wire()
        self.assertNotIn("phone", wire,
                         "a bool reached the employer as the literal word 'True'")
        self.assertNotIn("True", wire.values())

    def test_a_bool_is_named_as_something_a_human_has_to_settle(self):
        self.assertEqual(full_packet(fields={"phone": True}).deferred(), ("phone",))
        self.assertEqual(
            full_packet(fields={"phone": Answer(True, matched="x")}).deferred(),
            ("phone",))

    def test_nothing_is_posted_when_a_field_holds_a_bool(self):
        app = self.approved("normal")
        r = submit(self.store, app, full_packet(fields={"phone": True}))
        self.assertIs(r.outcome, Outcome.BLOCKED, r.reason)
        self.assertIn("phone", r.reason)
        self.assertEqual(self.ats.posts(), 0)
        self.assertIs(self.store.get(app).state, State.APPROVED)

    def test_a_real_number_is_still_an_answer(self):
        """The control: bool is an int subclass, so refusing it must not refuse
        7 years of experience along with it."""
        page = read_back(self.ats.url("normal"))
        pf = preflight(page, full_packet(fields={"email": 7}))
        self.assertIs(pf.verdict, Verdict.OK, pf.reason)
        self.assertEqual(full_packet(fields={"phone": 7}).wire()["phone"], "7")


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
class TheRecordProbeMatchesValuesNotSubstrings(ATSCase):
    """`probe in blob` is a substring test against the whole page, and /drops is
    the route that exists to catch a silently-dropped field.

    Proven before the fix, on that route: a phone of "415-555-0142" was caught,
    but a phone of "no" came back VERIFIED (it is inside "Northwind" in the
    footer) and a phone of "1" came back VERIFIED (it is inside the reference
    code AB-1001). Neither value was stored. VERIFIED is terminal, so a dropped
    yes/no, initial or work-authorization answer became a permanent lie in the
    ledger.
    """

    def _submit_with_phone(self, phone):
        app = self.approved("drops")
        r = submit(self.store, app, full_packet(fields={"phone": phone}),
                   record_url_template=self.ats.record_url_template())
        return app, r

    def test_a_short_value_is_not_verified_by_a_word_that_contains_it(self):
        app, r = self._submit_with_phone("no")       # inside "Northwind"
        self.assertIsNone(self.ats.record(r.reference).get("phone"),
                          "the fixture stopped dropping the phone")
        self.assertIs(r.outcome, Outcome.UNVERIFIED, r.reason)
        self.assertEqual(r.record.missing, ("phone",))
        self.assertIs(self.store.get(app).state, State.SUBMITTED_UNVERIFIED)
        self.assertFalse(is_done(self.store.get(app)),
                         "a value the employer never stored was recorded as done")

    def test_a_digit_is_not_verified_by_a_reference_code_containing_it(self):
        app, r = self._submit_with_phone("1")        # inside "AB-1001"
        self.assertIs(r.outcome, Outcome.UNVERIFIED, r.reason)
        self.assertEqual(r.record.missing, ("phone",))
        self.assertFalse(is_done(self.store.get(app)))

    def test_a_short_value_must_sit_next_to_its_own_field_name(self):
        """A standalone "yes" somewhere on the page is not evidence that THIS
        field holds it. Here the field is on the record and EMPTY, and the word
        appears far away, in prose about something else."""
        far = ("We consider every application on its merits and retain records "
               "for twenty-four months in line with the applicant privacy policy "
               "published on our careers site. ")     # > the scoping window
        self.assertGreater(len(far), 120)
        page = ("<p>Application AB-2002</p>"
                "<table><tr><td>work_authorization</td><td></td></tr></table>"
                f"<p>{far}</p>"
                "<p>Do we sponsor applicants from outside the US? yes</p>")
        rc = apply.verify_record(
            "http://127.0.0.1:1/records/AB-2002",
            ApplyPacket(fields={"work_authorization": "yes"},
                        applicant_name=NAME, applicant_email=EMAIL),
            fetch=lambda u, data=None: apply.Fetched(url=u, status=200, body=page))
        self.assertFalse(rc.ok, rc.reason)
        self.assertEqual(rc.missing, ("work_authorization",))

    def test_the_same_value_next_to_its_field_name_does_verify(self):
        """The other half of the rule, or the one above would pass by refusing
        everything."""
        page = ("<p>Application AB-2002</p>"
                "<table><tr><td>work_authorization</td><td>yes</td></tr></table>")
        rc = apply.verify_record(
            "http://127.0.0.1:1/records/AB-2002",
            ApplyPacket(fields={"work_authorization": "yes"},
                        applicant_name=NAME, applicant_email=EMAIL),
            fetch=lambda u, data=None: apply.Fetched(url=u, status=200, body=page))
        self.assertTrue(rc.ok, rc.reason)

    def test_a_record_with_nothing_of_ours_to_check_refuses(self):
        """Vacuous pass: every field was blank, deferred or the employer's own
        machinery, so the probe checked nothing -- and returned ok=True on any
        page that loaded. A check that could not run is a refusal."""
        packet = ApplyPacket(fields={"tracking_id": "t-88", "csrf": "abc123",
                                     "phone": "", "note": NEEDS_HUMAN},
                             applicant_name=NAME, applicant_email=EMAIL)
        rc = apply.verify_record(self.ats.url("thank-you"), packet)
        self.assertFalse(rc.ok, "an empty check reported the record as good")
        self.assertIn("nothing", rc.reason)

    def test_a_record_that_really_carries_everything_still_verifies(self):
        """The control. A probe that refuses everything is not a check either --
        this route stores every field and hands back a reference, so it must
        come out VERIFIED on the record read, not on the page text."""
        app = self.approved("iframe")
        r = submit(self.store, app, full_packet(),
                   record_url_template=self.ats.record_url_template())
        self.assertIs(r.outcome, Outcome.VERIFIED, r.reason)
        self.assertTrue(r.record.ok, r.record.reason)
        self.assertEqual(r.record.missing, ())
        self.assertIs(self.store.get(app).state, State.SUBMITTED_VERIFIED)

    def test_a_long_value_truncated_by_the_record_view_still_matches(self):
        """Record views truncate free text, so a long value is matched on its
        head. The token rule must not quietly undo that by demanding a boundary
        after a word we sliced in half ourselves."""
        shown = RESUME[:80] + "..."
        page = (f"<table><tr><td>resume_text</td><td>{shown}</td></tr></table>")
        rc = apply.verify_record(
            "http://127.0.0.1:1/records/AB-2003",
            ApplyPacket(fields={"resume_text": RESUME},
                        applicant_name=NAME, applicant_email=EMAIL),
            fetch=lambda u, data=None: apply.Fetched(url=u, status=200, body=page))
        self.assertTrue(rc.ok, rc.reason)


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
