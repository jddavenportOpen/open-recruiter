"""Invariants for the board client.

Two of these classes exist to fail if a specific past bug comes back:

  * `FailureIsNeverEmpty` -- the ancestor's scout wrote an empty jobs file when a
    host was down, and downstream read that as "no jobs matched". Every test in
    that class asserts a failure is LOUD and distinguishable from a board that
    genuinely has no openings.
  * `StableIdTests` -- the same scout had no stable id, so "what is new since the
    last run" was not computable and the same posting was re-offered daily.

Everything here injects a fake fetcher. No test touches a network; one test
asserts that.
"""
import inspect
import json
import os
import tempfile
import unittest
import urllib.request

from openrecruiter import boards
from openrecruiter.boards import (BoardClient, BoardError, HttpResponse,
                                  POSTING_KEYS, Source, canonical_url,
                                  normalize_ashby, normalize_greenhouse,
                                  normalize_lever, select_new, stable_id)


# -- fakes --------------------------------------------------------------------

class FakeClock:
    """Time that only moves when something sleeps -- so a test can assert the
    exact delays without wall-clock flake."""

    def __init__(self, t=1_000_000.0):
        self.t = float(t)
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(float(seconds), 6))
        self.t += float(seconds)


class FakeFetcher:
    """Maps a url substring to a response, a list of responses (the last one
    repeats), or a callable."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        entry = None
        for key, val in self.routes.items():
            if key in url:
                entry = val
                break
        if entry is None:
            raise AssertionError(f"test has no fake response for {url}")
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        if callable(entry):
            return entry(url, headers)
        return entry

    @property
    def count(self):
        return len(self.calls)


def ok(payload, headers=None):
    return HttpResponse(200, json.dumps(payload), headers or {})


GH_PAYLOAD = {"jobs": [{
    "id": 4012345,
    "title": "Principal Product Manager",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
    "location": {"name": "Remote — US"},
    "updated_at": "2026-09-01T12:00:00-04:00",
    # Greenhouse sends HTML that is itself HTML-escaped. A decoder that unescapes
    # once and stops leaves visible tags in the card.
    "content": "&lt;p&gt;Own the &lt;strong&gt;platform&lt;/strong&gt; roadmap.&lt;/p&gt;",
}]}

LEVER_PAYLOAD = [{
    "id": "7f0e1234",
    "text": "Staff Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/7f0e1234",
    "categories": {"location": "San Francisco", "team": "Engineering"},
    "createdAt": 1756684800000,          # milliseconds, not seconds
    "descriptionPlain": "Build the thing.",
}]

ASHBY_PAYLOAD = {"jobs": [
    {"id": "abc", "title": "Product Lead", "location": "New York",
     "publishedAt": "2026-08-15T09:30:00Z",
     "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
     "descriptionPlain": "Lead product.", "isListed": True},
    {"id": "def", "title": "Pulled from the board",
     "jobUrl": "https://jobs.ashbyhq.com/acme/def", "isListed": False},
]}

ALL_ROUTES = {
    "boards-api.greenhouse.io": ok(GH_PAYLOAD),
    "api.lever.co": ok(LEVER_PAYLOAD),
    "api.ashbyhq.com": ok(ASHBY_PAYLOAD),
}


def client(routes, clock=None, **kw):
    clock = clock or FakeClock()
    kw.setdefault("cache_ttl_s", 0)
    kw.setdefault("min_interval_s", 0)
    kw.setdefault("max_retries", 0)
    fetcher = routes if callable(routes) else FakeFetcher(routes)
    c = BoardClient(fetcher=fetcher, clock=clock.time, sleep=clock.sleep, **kw)
    return c, fetcher, clock


# -- stable ids ---------------------------------------------------------------

class StableIdTests(unittest.TestCase):

    URL = "https://boards.greenhouse.io/acme/jobs/4012345"

    def test_id_is_deterministic_across_runs(self):
        # Pinned literal on purpose: an id derived from a timestamp, a counter,
        # a uuid or the list position would still be "stable" within one run.
        self.assertEqual(stable_id(self.URL), "5f70f284cbeae452")

    def test_tracking_parameters_do_not_change_the_id(self):
        base = stable_id(self.URL)
        for variant in (
            self.URL + "/",
            self.URL + "?gh_src=a1b2c3",
            self.URL + "?utm_source=newsletter&utm_campaign=sept",
            self.URL + "#apply",
            "https://BOARDS.greenhouse.io/acme/jobs/4012345",
        ):
            self.assertEqual(stable_id(variant), base, variant)

    def test_meaningful_query_parameters_still_count(self):
        self.assertNotEqual(stable_id(self.URL + "?location=remote"), stable_id(self.URL))

    def test_different_postings_get_different_ids(self):
        other = "https://boards.greenhouse.io/acme/jobs/4012346"
        self.assertNotEqual(stable_id(self.URL), stable_id(other))

    def test_an_empty_url_is_a_refusal_not_a_blank_id(self):
        with self.assertRaises(BoardError):
            stable_id("")

    def test_canonical_url_drops_the_fragment_and_sorts_the_query(self):
        self.assertEqual(canonical_url("https://x.io/a?b=2&a=1#frag"),
                         "https://x.io/a?a=1&b=2")

    def test_two_runs_of_the_same_board_yield_no_new_postings(self):
        c, _, _ = client(dict(ALL_ROUTES))
        first = c.greenhouse("acme")
        known = {p["id"] for p in first}
        c2, _, _ = client(dict(ALL_ROUTES))
        second = c2.greenhouse("acme")
        self.assertEqual(select_new(second, known), [],
                         "the same posting was reported as new on a second run")

    def test_an_added_posting_is_the_only_thing_reported_new(self):
        c, _, _ = client(dict(ALL_ROUTES))
        known = {p["id"] for p in c.greenhouse("acme")}
        grown = {"jobs": GH_PAYLOAD["jobs"] + [{
            "id": 9, "title": "Designer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/9",
            "location": {"name": "NYC"}, "updated_at": "2026-09-02T00:00:00Z"}]}
        c2, _, _ = client({"greenhouse": ok(grown)})
        new = select_new(c2.greenhouse("acme"), known)
        self.assertEqual([p["role"] for p in new], ["Designer"])

    def test_a_posting_without_an_id_is_an_error_not_silently_new(self):
        with self.assertRaises(BoardError):
            select_new([{"url": "https://x.io/1"}], set())


# -- cache --------------------------------------------------------------------

class CacheTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_second_fetch_of_the_same_url_does_not_hit_the_network(self):
        c, f, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=900)
        first = c.greenhouse("acme")
        second = c.greenhouse("acme")
        self.assertEqual(f.count, 1, "the cache did not prevent a second request")
        self.assertEqual(c.cache_hits, 1)
        self.assertEqual(first, second)

    def test_the_cache_expires(self):
        clock = FakeClock()
        c, f, _ = client(dict(ALL_ROUTES), clock=clock,
                         cache_dir=self.tmp.name, cache_ttl_s=100)
        c.greenhouse("acme")
        clock.t += 101
        c.greenhouse("acme")
        self.assertEqual(f.count, 2, "a stale cache entry was served past its TTL")

    def test_the_cache_is_on_disk_and_shared_between_clients(self):
        c1, f1, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=900)
        c1.greenhouse("acme")
        c2, f2, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=900)
        c2.greenhouse("acme")
        self.assertEqual(f2.count, 0, "a fresh client re-fetched an on-disk cached response")
        self.assertEqual(c2.cache_hits, 1)

    def test_zero_ttl_disables_the_cache_entirely(self):
        c, f, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=0)
        c.greenhouse("acme")
        c.greenhouse("acme")
        self.assertEqual(f.count, 2)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_failures_are_never_cached(self):
        routes = {"greenhouse": [HttpResponse(503, "down"), ok(GH_PAYLOAD)]}
        c, f, _ = client(routes, cache_dir=self.tmp.name, cache_ttl_s=900, max_retries=0)
        with self.assertRaises(BoardError):
            c.greenhouse("acme")
        self.assertEqual(os.listdir(self.tmp.name), [],
                         "a failed response was written to the cache")
        self.assertEqual(c.greenhouse("acme")[0]["role"], "Principal Product Manager")

    def test_a_corrupt_cache_entry_refetches_instead_of_crashing(self):
        c, f, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=900)
        c.greenhouse("acme")
        for name in os.listdir(self.tmp.name):
            with open(os.path.join(self.tmp.name, name), "w") as fh:
                fh.write("{not json")
        self.assertEqual(len(c.greenhouse("acme")), 1)
        self.assertEqual(f.count, 2)

    def test_force_bypasses_a_fresh_cache_entry(self):
        c, f, _ = client(dict(ALL_ROUTES), cache_dir=self.tmp.name, cache_ttl_s=900)
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        c.fetch_json(url)
        c.fetch_json(url, force=True)
        self.assertEqual(f.count, 2)


# -- rate limiting ------------------------------------------------------------

class RateLimitTests(unittest.TestCase):

    def test_two_requests_to_one_host_are_spaced(self):
        clock = FakeClock()
        c, _, _ = client(dict(ALL_ROUTES), clock=clock, min_interval_s=2.0)
        c.greenhouse("acme")
        c.greenhouse("other")
        self.assertEqual(clock.sleeps, [2.0],
                         "the second request to the same host was not rate limited")

    def test_an_unrelated_host_is_not_made_to_wait(self):
        clock = FakeClock()
        c, _, _ = client(dict(ALL_ROUTES), clock=clock, min_interval_s=2.0)
        c.greenhouse("acme")
        c.lever("acme")
        self.assertEqual(clock.sleeps, [],
                         "rate limiting is global rather than per-host")

    def test_elapsed_time_counts_against_the_interval(self):
        clock = FakeClock()
        c, _, _ = client(dict(ALL_ROUTES), clock=clock, min_interval_s=2.0)
        c.greenhouse("acme")
        clock.t += 5.0
        c.greenhouse("other")
        self.assertEqual(clock.sleeps, [])

    def test_a_cache_hit_costs_no_rate_budget(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = FakeClock()
        c, _, _ = client(dict(ALL_ROUTES), clock=clock, min_interval_s=2.0,
                         cache_dir=tmp.name, cache_ttl_s=900)
        c.greenhouse("acme")
        c.greenhouse("acme")
        self.assertEqual(clock.sleeps, [], "a cached read was made to wait for the host")


# -- retry / backoff ----------------------------------------------------------

class BackoffTests(unittest.TestCase):

    def test_a_429_is_retried_and_then_succeeds(self):
        clock = FakeClock()
        routes = {"greenhouse": [HttpResponse(429, "slow down"), ok(GH_PAYLOAD)]}
        c, f, _ = client(routes, clock=clock, max_retries=3, backoff_base_s=1.0)
        self.assertEqual(len(c.greenhouse("acme")), 1)
        self.assertEqual(f.count, 2)
        self.assertEqual(clock.sleeps, [1.0], "a 429 was retried without backing off")

    def test_backoff_is_exponential_and_then_gives_up_loudly(self):
        clock = FakeClock()
        c, f, _ = client({"greenhouse": HttpResponse(503, "unavailable")},
                         clock=clock, max_retries=3, backoff_base_s=1.0)
        with self.assertRaises(BoardError) as cm:
            c.greenhouse("acme")
        self.assertEqual(f.count, 4)
        self.assertEqual(clock.sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.attempts, 4)

    def test_backoff_is_capped(self):
        clock = FakeClock()
        c, _, _ = client({"greenhouse": HttpResponse(500, "boom")},
                         clock=clock, max_retries=5, backoff_base_s=10.0,
                         backoff_cap_s=20.0)
        with self.assertRaises(BoardError):
            c.greenhouse("acme")
        self.assertEqual(clock.sleeps, [10.0, 20.0, 20.0, 20.0, 20.0])

    def test_a_client_error_is_not_retried(self):
        clock = FakeClock()
        c, f, _ = client({"greenhouse": HttpResponse(404, "no such board")},
                         clock=clock, max_retries=3)
        with self.assertRaises(BoardError) as cm:
            c.greenhouse("nope")
        self.assertEqual(f.count, 1, "a 404 was retried; that is rudeness, not resilience")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(cm.exception.status, 404)

    def test_retry_after_is_honoured_when_longer_than_our_backoff(self):
        clock = FakeClock()
        routes = {"greenhouse": [HttpResponse(429, "", {"Retry-After": "30"}),
                                 ok(GH_PAYLOAD)]}
        c, _, _ = client(routes, clock=clock, max_retries=2, backoff_base_s=1.0)
        c.greenhouse("acme")
        self.assertEqual(clock.sleeps, [30.0])

    def test_a_hostile_retry_after_cannot_park_the_run(self):
        clock = FakeClock()
        routes = {"greenhouse": [HttpResponse(429, "", {"retry-after": "86400"}),
                                 ok(GH_PAYLOAD)]}
        c, _, _ = client(routes, clock=clock, max_retries=2, backoff_base_s=1.0,
                         backoff_cap_s=60.0)
        c.greenhouse("acme")
        self.assertEqual(clock.sleeps, [60.0])

    def test_a_transport_failure_is_retried_and_then_raised(self):
        clock = FakeClock()

        def blow_up(url, headers):
            raise boards.TransportError("dns: no such host", url=url)

        c, _, _ = client(blow_up, clock=clock, max_retries=2, backoff_base_s=1.0)
        with self.assertRaises(BoardError) as cm:
            c.greenhouse("acme")
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        self.assertIn("no such host", str(cm.exception))

    def test_a_fetcher_that_raises_anything_is_a_failure_not_a_pass(self):
        def blow_up(url, headers):
            raise ValueError("unexpected")

        c, _, _ = client(blow_up, max_retries=0)
        with self.assertRaises(BoardError):
            c.greenhouse("acme")


# -- normalization ------------------------------------------------------------

class NormalizationTests(unittest.TestCase):

    def test_greenhouse(self):
        c, f, _ = client(dict(ALL_ROUTES))
        (p,) = c.greenhouse("acme", company="Acme Corp")
        self.assertEqual(tuple(p), POSTING_KEYS)
        self.assertEqual(p["company"], "Acme Corp")
        self.assertEqual(p["role"], "Principal Product Manager")
        self.assertEqual(p["url"], "https://boards.greenhouse.io/acme/jobs/4012345")
        self.assertEqual(p["location"], "Remote — US")
        self.assertEqual(p["posted_at"], "2026-09-01T16:00:00Z")
        self.assertEqual(p["ats"], "greenhouse")
        self.assertEqual(p["description"], "Own the platform roadmap.")
        self.assertEqual(p["id"], stable_id(p["url"]))
        self.assertIn("content=true", f.calls[0][0])

    def test_greenhouse_falls_back_to_the_token_for_a_display_name(self):
        c, _, _ = client(dict(ALL_ROUTES))
        self.assertEqual(c.greenhouse("acme")[0]["company"], "acme")

    def test_lever(self):
        c, f, _ = client(dict(ALL_ROUTES))
        (p,) = c.lever("acme", company="Acme Corp")
        self.assertEqual(tuple(p), POSTING_KEYS)
        self.assertEqual(p["role"], "Staff Engineer")
        self.assertEqual(p["url"], "https://jobs.lever.co/acme/7f0e1234")
        self.assertEqual(p["location"], "San Francisco")
        self.assertEqual(p["ats"], "lever")
        self.assertEqual(p["description"], "Build the thing.")
        # millisecond epoch, not seconds -- reading it as seconds dates this 1970
        self.assertEqual(p["posted_at"], "2025-09-01T00:00:00Z")
        self.assertIn("mode=json", f.calls[0][0])

    def test_ashby(self):
        c, _, _ = client(dict(ALL_ROUTES))
        postings = c.ashby("acme")
        self.assertEqual([p["role"] for p in postings], ["Product Lead"],
                         "a posting Ashby marked unlisted was still offered")
        p = postings[0]
        self.assertEqual(tuple(p), POSTING_KEYS)
        self.assertEqual(p["url"], "https://jobs.ashbyhq.com/acme/abc")
        self.assertEqual(p["location"], "New York")
        self.assertEqual(p["posted_at"], "2026-08-15T09:30:00Z")
        self.assertEqual(p["ats"], "ashby")

    def test_an_unreadable_date_is_none_and_never_today(self):
        payload = {"jobs": [{"title": "X", "absolute_url": "https://b.io/x",
                             "updated_at": "sometime last spring"}]}
        (p,) = normalize_greenhouse(payload, "acme")
        self.assertIsNone(p["posted_at"],
                          "an unparseable date was fabricated, making an old "
                          "posting look fresh")

    def test_every_adapter_returns_the_same_shape(self):
        c, _, _ = client(dict(ALL_ROUTES))
        for postings in (c.greenhouse("a"), c.lever("a"), c.ashby("a")):
            for p in postings:
                self.assertEqual(tuple(p), POSTING_KEYS)


# -- failure is never an empty list -------------------------------------------

class FailureIsNeverEmpty(unittest.TestCase):
    """The bug this module exists to prevent.

    A host that is down, a token that is wrong, and an API that changed shape all
    have to be distinguishable from a company with no open roles.
    """

    def test_a_dead_host_raises_rather_than_returning_nothing(self):
        c, _, _ = client({"greenhouse": HttpResponse(500, "boom")}, max_retries=1)
        with self.assertRaises(BoardError):
            c.greenhouse("acme")

    def test_a_payload_missing_its_jobs_key_is_failed_not_empty(self):
        for payload in ({"results": []}, {"error": "unauthorized"}, [], "nope", None):
            with self.subTest(payload=payload):
                with self.assertRaises(BoardError):
                    normalize_greenhouse(payload, "acme")

    def test_lever_expects_a_list_and_says_so(self):
        with self.assertRaises(BoardError):
            normalize_lever({"postings": []}, "acme")

    def test_ashby_needs_its_jobs_key(self):
        with self.assertRaises(BoardError):
            normalize_ashby({"data": {"jobs": []}}, "acme")

    def test_a_genuinely_empty_board_is_empty_and_ok(self):
        c, _, _ = client({"greenhouse": ok({"jobs": []})})
        self.assertEqual(c.greenhouse("acme"), [])
        result = c.sweep([Source("greenhouse", "acme")])
        self.assertTrue(result.ok)
        self.assertEqual(result.postings, [])

    def test_a_posting_without_a_url_is_refused(self):
        with self.assertRaises(BoardError):
            normalize_greenhouse({"jobs": [{"title": "X"}]}, "acme")
        with self.assertRaises(BoardError):
            normalize_lever([{"text": "X"}], "acme")

    def test_a_non_json_body_is_an_error(self):
        c, _, _ = client({"greenhouse": HttpResponse(200, "<html>maintenance</html>")})
        with self.assertRaises(BoardError):
            c.greenhouse("acme")

    def test_sweep_fails_closed_by_default(self):
        routes = dict(ALL_ROUTES)
        routes["api.lever.co"] = HttpResponse(500, "boom")
        c, _, _ = client(routes, max_retries=0)
        with self.assertRaises(BoardError) as cm:
            c.sweep([Source("greenhouse", "acme"), Source("lever", "acme")])
        self.assertIn("lever", str(cm.exception))

    def test_a_partial_sweep_names_the_failure_and_keeps_the_good_results(self):
        routes = dict(ALL_ROUTES)
        routes["api.lever.co"] = HttpResponse(500, "boom")
        c, _, _ = client(routes, max_retries=0)
        result = c.sweep([Source("greenhouse", "acme"), Source("lever", "acme")],
                         allow_partial=True)
        self.assertFalse(result.ok, "a sweep with a dead host reported itself healthy")
        self.assertEqual([f.ats for f in result.failures], ["lever"])
        self.assertEqual(result.failures[0].status, 500)
        self.assertEqual([p["ats"] for p in result.postings], ["greenhouse"])
        with self.assertRaises(BoardError):
            result.raise_for_failures()

    def test_a_failure_carries_which_board_it_was(self):
        c, _, _ = client({"api.lever.co": HttpResponse(500, "boom")}, max_retries=0)
        with self.assertRaises(BoardError) as cm:
            c.fetch(Source("lever", "acme"))
        self.assertEqual(cm.exception.ats, "lever")
        self.assertEqual(cm.exception.token, "acme")


# -- politeness and configuration ---------------------------------------------

class UserAgentTests(unittest.TestCase):

    def test_the_user_agent_identifies_the_project(self):
        c, f, _ = client(dict(ALL_ROUTES))
        c.greenhouse("acme")
        ua = f.calls[0][1]["User-Agent"]
        self.assertIn("openrecruiter/", ua)
        self.assertIn("github.com", ua)
        self.assertEqual(f.calls[0][1]["Accept"], "application/json")

    def test_a_contact_is_included_when_configured(self):
        os.environ["OPENRECRUITER_CONTACT"] = "me@example.com"
        self.addCleanup(os.environ.pop, "OPENRECRUITER_CONTACT", None)
        self.assertIn("me@example.com", boards.default_user_agent())


class SourceTests(unittest.TestCase):

    def test_parse(self):
        s = Source.parse("greenhouse:acme=Acme Corp")
        self.assertEqual((s.ats, s.token, s.company), ("greenhouse", "acme", "Acme Corp"))
        self.assertIsNone(Source.parse("lever:acme").company)

    def test_an_unknown_ats_raises_rather_than_being_skipped(self):
        with self.assertRaises(BoardError):
            Source.parse("workday:acme")
        with self.assertRaises(BoardError):
            Source.parse("acme")
        with self.assertRaises(BoardError):
            Source("greenhouse", "")


class NoNetworkAndNoDecisions(unittest.TestCase):

    def test_an_injected_fetcher_means_no_socket_is_opened(self):
        calls = []
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: calls.append(a) or real(*a, **k)
        self.addCleanup(setattr, urllib.request, "urlopen", real)
        c, _, _ = client(dict(ALL_ROUTES))
        c.sweep([Source("greenhouse", "a"), Source("lever", "a"), Source("ashby", "a")])
        self.assertEqual(calls, [])

    def test_the_board_layer_decides_nothing(self):
        """Discovery is not application. This module finds postings; it must not
        grow a verb that acts on them, in singular or plural form."""
        forbidden = ("approve", "submit", "decide")
        names = [n for n, _ in inspect.getmembers(boards, callable)]
        names += [n for n, _ in inspect.getmembers(boards.BoardClient)]
        for name in names:
            for bad in forbidden:
                self.assertNotIn(bad, name.lower(),
                                 f"boards grew a decision verb: {name}")


if __name__ == "__main__":
    unittest.main()
