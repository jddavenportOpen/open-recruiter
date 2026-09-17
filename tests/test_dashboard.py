"""Invariants for the localhost dashboard.

Every test here corresponds to something that was actually shipped once, in the
system this project was extracted from: a server on every interface, no auth, a
host check mistaken for a CSRF defence, a silenced access log, an endpoint that
piped a request body into a shell, and `POST /api/approve-all`.

These are written to FAIL if the control is removed, not to pass because a
function exists. Where a source-level check is used it is done over the AST, so
a comment saying the word "subprocess" does not satisfy it and renaming a helper
does not evade it.
"""
import ast
import contextlib
import http.client
import inspect
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

from openrecruiter import dashboard
from openrecruiter.store import State, Store

SOURCE = inspect.getsource(dashboard)
TREE = ast.parse(SOURCE)
_UNSET = object()


# -- fixtures -----------------------------------------------------------------

class Harness:
    """A real server on a real loopback port, in a background thread."""

    def __init__(self, test):
        self.home = tempfile.mkdtemp(prefix="or-dash-")
        test.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.db = os.path.join(self.home, "or.db")
        self.log = []
        self.srv = dashboard.make_server(port=0, store_path=self.db,
                                         access_log=self.log.append, home=self.home)
        self.port = self.srv.server_address[1]
        self.token = self.srv.or_token
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)

    def stop(self):
        self.srv.shutdown()
        self.thread.join(5)
        self.srv.server_close()

    def store(self):
        """A connection owned by the CALLING thread. The server has its own."""
        return Store(self.db)

    def request(self, method, path, token=_UNSET, body=_UNSET, headers=None,
                content_type="application/json", browser=True):
        """Returns (status, parsed-body). `headers` values of None remove a header."""
        h = {}
        tok = self.token if token is _UNSET else token
        if tok is not None:
            h["X-OpenRecruiter-Token"] = tok
        data = None
        if body is not _UNSET:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            if content_type:
                h["Content-Type"] = content_type
            if browser:
                h["Origin"] = f"http://127.0.0.1:{self.port}"
                h["Sec-Fetch-Site"] = "same-origin"
        for k, v in (headers or {}).items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _parse(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, _parse(e.read()), dict(e.headers)


def _parse(raw):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def awaiting(store, app_id="a1", company="Acme", role="Principal PM", **fields):
    store.upsert_discovered(app_id, company, role, f"https://jobs.example/{app_id}")
    store.transition(app_id, State.BUILDING)
    f = {"score": 84.0, "raw_score": 79.0, "weakest": "recruiter",
         "weakest_reason": "the summary buries the platform work under tooling",
         "claims": ["$20M annualized revenue"]}
    f.update(fields)
    store.transition(app_id, State.AWAITING_APPROVAL, **f)
    return app_id


def below_bar(store, app_id="b1"):
    store.upsert_discovered(app_id, "Bee", "PM", f"https://jobs.example/{app_id}")
    store.transition(app_id, State.BUILDING)
    store.transition(app_id, State.BELOW_BAR, score=41.0)
    return app_id


def state_of(h, app_id):
    s = h.store()
    try:
        app = s.get(app_id)
        return app.state if app else None
    finally:
        s.close()


def defined_functions():
    """Every function/method whose body is in THIS module (not inherited)."""
    out = []
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
    return out


# -- 1. the bind ---------------------------------------------------------------

class BindsLoopbackOnly(unittest.TestCase):
    """The ancestor bound 0.0.0.0, which put an unauthenticated approval switch
    on every network the user's laptop ever joined."""

    def test_the_constant_is_loopback(self):
        self.assertEqual(dashboard.LOOPBACK, "127.0.0.1")
        self.assertEqual(dashboard.LOOPBACK_ONLY, frozenset({"127.0.0.1"}))

    def test_a_running_server_is_bound_to_loopback(self):
        h = Harness(self)
        self.assertEqual(h.srv.server_address[0], "127.0.0.1")
        self.assertEqual(h.srv.socket.getsockname()[0], "127.0.0.1")
        self.assertEqual(h.srv.address_family, socket.AF_INET,
                         "an IPv6 server could be asked to bind ::")

    def test_it_refuses_to_bind_a_public_address(self):
        for addr in ("0.0.0.0", "", "::", "0.0.0.0:0", "192.168.1.10", "localhost"):
            with self.assertRaises(dashboard.RefusedBind, msg=addr):
                dashboard.LoopbackOnlyServer((addr, 0), dashboard.Handler)

    def test_nothing_in_this_module_takes_a_bind_address(self):
        """No parameter, anywhere, that could relocate the listener."""
        banned = {"host", "hostname", "bind", "address", "addr", "interface",
                  "iface", "listen", "server_address", "ip"}
        for fn in defined_functions():
            args = fn.args
            names = [a.arg for a in list(args.posonlyargs) + list(args.args)
                     + list(args.kwonlyargs)]
            for extra in (args.vararg, args.kwarg):
                if extra:
                    names.append(extra.arg)
            bad = banned & {n.lower() for n in names}
            self.assertFalse(bad, f"{fn.name}() takes {bad} — the bind address must not "
                                  f"be a parameter")

    def test_no_wildcard_address_literal_appears_anywhere(self):
        for literal in ("0.0.0.0", "0:0:0:0:0:0:0:0", "INADDR_ANY"):
            self.assertNotIn(literal, SOURCE)

    def test_make_server_rejects_a_nonsense_port(self):
        home = tempfile.mkdtemp(prefix="or-port-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        for bad in (-1, 70000, "8765", None):
            with self.assertRaises(dashboard.DashboardError, msg=repr(bad)):
                dashboard.make_server(port=bad, home=home)


# -- 2. the token --------------------------------------------------------------

class EveryRequestNeedsTheToken(unittest.TestCase):

    def setUp(self):
        self.h = Harness(self)
        s = self.h.store()
        awaiting(s)
        s.close()

    def test_a_get_without_a_token_is_refused(self):
        for path in ("/", "/api/queue", "/api/goals", "/api/report",
                     "/api/application?id=a1"):
            status, body, _ = self.h.request("GET", path, token=None)
            self.assertEqual(status, 401, path)
            self.assertEqual(body, {"error": "unauthorized"})

    def test_a_wrong_token_is_refused(self):
        for bad in ("", "x" * 43, self.h.token[:-1], self.h.token + "a",
                    self.h.token.upper()):
            status, _, _ = self.h.request("GET", "/api/queue", token=bad)
            self.assertEqual(status, 401, repr(bad))

    def test_the_right_token_works_in_a_header_or_the_bootstrap_query(self):
        self.assertEqual(self.h.request("GET", "/api/queue")[0], 200)
        self.assertEqual(
            self.h.request("GET", f"/api/queue?token={self.h.token}", token=None)[0], 200)

    def test_an_unauthenticated_post_decides_nothing(self):
        status, _, _ = self.h.request("POST", "/api/decision", token=None,
                                      body={"id": "a1", "decision": "approve"})
        self.assertEqual(status, 401)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL,
                      "an unauthenticated request changed application state")

    def test_the_comparison_is_constant_time(self):
        """`==` on a secret leaks its prefix one byte at a time."""
        real = dashboard.hmac.compare_digest
        with unittest.mock.patch.object(dashboard.hmac, "compare_digest",
                                        side_effect=real) as spy:
            self.h.request("GET", "/api/queue")
            self.assertTrue(spy.called,
                            "the token was compared without hmac.compare_digest")

    def test_check_token_refuses_empty_and_missing(self):
        self.assertFalse(dashboard.check_token("abc", None))
        self.assertFalse(dashboard.check_token("abc", ""))
        self.assertFalse(dashboard.check_token("", ""))
        self.assertFalse(dashboard.check_token("", "abc"))
        self.assertTrue(dashboard.check_token("abc", "abc"))

    def test_an_unknown_route_still_needs_the_token(self):
        """A 404 that does not require auth is an unauthenticated route map."""
        self.assertEqual(self.h.request("GET", "/nope", token=None)[0], 401)


class TokenAtRest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="or-tok-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_first_run_writes_it_0600(self):
        tok = dashboard.ensure_token(self.home)
        p = dashboard.token_path(self.home)
        self.assertGreaterEqual(len(tok), 32)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(dashboard.ensure_token(self.home), tok, "the token rotated itself")

    def test_a_world_readable_token_is_refused_loudly(self):
        dashboard.ensure_token(self.home)
        p = dashboard.token_path(self.home)
        os.chmod(p, 0o644)
        with self.assertRaises(dashboard.InsecureToken):
            dashboard.ensure_token(self.home)

    def test_a_group_readable_token_is_refused(self):
        dashboard.ensure_token(self.home)
        os.chmod(dashboard.token_path(self.home), 0o640)
        with self.assertRaises(dashboard.InsecureToken):
            dashboard.ensure_token(self.home)

    def test_an_empty_token_file_is_refused_rather_than_accepted(self):
        open(dashboard.token_path(self.home), "w").close()
        os.chmod(dashboard.token_path(self.home), 0o600)
        with self.assertRaises(dashboard.InsecureToken):
            dashboard.ensure_token(self.home)

    def test_home_follows_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {"OPENRECRUITER_HOME": self.home}):
            self.assertEqual(dashboard.home_dir(), self.home)


# -- 3. cross-site -------------------------------------------------------------

class CrossSiteRequestsAreRefused(unittest.TestCase):
    """A cross-origin form POST carries the SAME Host value as a legitimate one.
    That is why the ancestor's host check defended nothing, and why these tests
    all keep the Host header correct while varying only what a browser adds."""

    def setUp(self):
        self.h = Harness(self)
        s = self.h.store()
        awaiting(s)
        s.close()
        self.body = {"id": "a1", "decision": "approve"}

    def assertRefused(self, status):
        self.assertEqual(status, 403)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL,
                      "a cross-site request decided an application")

    def test_an_evil_origin_with_a_correct_host_is_refused(self):
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=self.body,
            headers={"Origin": "http://evil.example", "Sec-Fetch-Site": None})
        self.assertRefused(status)

    def test_a_cross_site_fetch_marker_is_refused(self):
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=self.body,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": None})
        self.assertRefused(status)

    def test_same_site_is_not_same_origin(self):
        """A sibling port on 127.0.0.1 is same-site and must not count."""
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=self.body,
            headers={"Sec-Fetch-Site": "same-site",
                     "Origin": f"http://127.0.0.1:{self.h.port + 1}"})
        self.assertRefused(status)

    def test_a_request_proving_nothing_is_refused(self):
        """Neither header sent: a check that cannot run is a refusal, not a pass."""
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=self.body,
            headers={"Origin": None, "Sec-Fetch-Site": None})
        self.assertRefused(status)

    def test_a_lying_sec_fetch_site_with_a_foreign_origin_is_refused(self):
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=self.body,
            headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"})
        self.assertRefused(status)

    def test_a_genuine_same_origin_post_is_allowed(self):
        status, body, _ = self.h.request("POST", "/api/decision", body=self.body)
        self.assertEqual(status, 200, body)
        self.assertIs(state_of(self.h, "a1"), State.APPROVED)

    def test_a_form_encoded_body_is_refused(self):
        """An HTML form cannot send application/json without a preflight this
        server never answers, so requiring it is a second, structural barrier."""
        status, _, _ = self.h.request(
            "POST", "/api/decision", body=b"id=a1&decision=approve",
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 400)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL)

    def test_a_state_change_cannot_be_a_get(self):
        for path in dashboard.WRITE_ROUTES:
            self.assertEqual(self.h.request("GET", path)[0], 405, path)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL)

    def test_a_rebinding_host_is_refused_even_with_the_token(self):
        status, _, _ = self.h.request("GET", "/api/queue",
                                      headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_host_is_checked_but_is_not_the_csrf_check(self):
        self.assertTrue(dashboard.host_is_loopback("127.0.0.1:8765"))
        self.assertTrue(dashboard.host_is_loopback("localhost"))
        self.assertTrue(dashboard.host_is_loopback("[::1]:8765"))
        self.assertFalse(dashboard.host_is_loopback("evil.example"))
        self.assertFalse(dashboard.host_is_loopback(None))
        # ...and the same Host passes while the origin check still refuses.
        ok, _ = dashboard.same_origin({"Origin": "http://evil.example"}, 8765)
        self.assertFalse(ok)

    def test_no_cors_headers_are_ever_sent(self):
        _, _, headers = self.h.request("GET", "/api/queue")
        for k in headers:
            self.assertFalse(k.lower().startswith("access-control-"),
                             f"{k} would let another origin read this")


# -- 4. no bulk ----------------------------------------------------------------

class NoBulkDecisionExists(unittest.TestCase):
    """The ancestor had POST /api/approve-all, which approved every scored row at
    once. It is not ported, and this is why it stays unported."""

    FORBIDDEN = ("approve_all", "approveall", "approve_many", "bulk_approve",
                 "auto_approve", "approve_above", "batch_approve", "approve_batch",
                 "decide_all", "approve_queue")

    def test_no_route_path_suggests_a_batch(self):
        for path in dashboard.ROUTES:
            low = path.lower()
            for bad in ("all", "bulk", "batch", "many", "auto"):
                self.assertNotIn(bad, low, f"route {path} looks like a bulk verb")

    def test_no_function_in_the_module_is_a_bulk_verb(self):
        names = {fn.name.lower() for fn in defined_functions()}
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, names, f"dashboard defines {bad}")

    def test_there_are_exactly_two_decisions_and_each_names_one_state(self):
        self.assertEqual(sorted(dashboard.DECISIONS), ["approve", "decline"])
        self.assertEqual(set(dashboard.DECISIONS.values()),
                         {State.APPROVED, State.DECLINED})

    def test_a_list_of_ids_is_refused_and_decides_nothing(self):
        h = Harness(self)
        s = h.store()
        awaiting(s, "a1")
        awaiting(s, "a2", company="Bolt")
        s.close()
        status, body, _ = h.request("POST", "/api/decision",
                                    body={"id": ["a1", "a2"], "decision": "approve"})
        self.assertEqual(status, 400)
        self.assertIn("one application per request", body["error"])
        for app_id in ("a1", "a2"):
            self.assertIs(state_of(h, app_id), State.AWAITING_APPROVAL, app_id)

    def test_other_container_shapes_are_refused_too(self):
        h = Harness(self)
        s = h.store()
        awaiting(s)
        s.close()
        for shape in ({"a1": True}, [], {"ids": ["a1"]}, 7, None, True):
            status, _, _ = h.request("POST", "/api/decision",
                                     body={"id": shape, "decision": "approve"})
            self.assertEqual(status, 400, repr(shape))
        self.assertIs(state_of(h, "a1"), State.AWAITING_APPROVAL)

    def test_the_page_offers_no_bulk_control(self):
        page = dashboard.render_page("n0nce").lower()
        for bad in ("approve all", "approve_all", "select all", "check all",
                    "auto-approve", "approve everything", "approve the rest"):
            self.assertNotIn(bad, page, f"the page offers {bad!r}")
        # The page DOES say the words "approve-all" -- in the sentence explaining
        # that there is not one. Every occurrence must be that negation.
        self.assertEqual(page.count("approve-all"), page.count("no approve-all"),
                         "the page mentions approve-all somewhere other than "
                         "the sentence saying there isn't one")

    def test_the_page_has_exactly_one_place_that_decides(self):
        """A second call site is how 'and the rest' gets added later."""
        page = dashboard.render_page("n")
        self.assertEqual(page.count('api("/api/decision"'), 1)
        self.assertEqual(page.count("/api/decision"), 1)

    def test_the_page_says_the_absence_is_deliberate(self):
        self.assertIn("no approve-all", dashboard.render_page("n").lower())


# -- 5. no code execution ------------------------------------------------------

class NoCodeExecutionSurface(unittest.TestCase):
    """The ancestor exposed a route that fed an unauthenticated request body to a
    subprocess with the user's credentials in the environment."""

    BANNED_MODULES = {"subprocess", "os.system", "pty", "multiprocessing",
                      "shlex", "ctypes", "commands", "popen2", "asyncio.subprocess"}
    BANNED_CALLS = {"eval", "exec", "compile", "__import__", "system", "popen",
                    "Popen", "run", "call", "check_output", "check_call",
                    "fork", "forkpty", "execv", "execve", "execvp", "execl",
                    "spawnv", "spawnl", "posix_spawn", "startfile"}

    def test_no_banned_module_is_imported(self):
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name, self.BANNED_MODULES, a.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module or "", self.BANNED_MODULES, node.module)

    def test_no_call_can_run_a_program(self):
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name is None:
                continue
            if isinstance(f, ast.Attribute):
                owner = f.value.id if isinstance(f.value, ast.Name) else ""
                if owner in ("os", "subprocess", "sys") and name in self.BANNED_CALLS:
                    self.fail(f"{owner}.{name}() executes code")
                continue
            self.assertNotIn(name, {"eval", "exec", "compile", "__import__"},
                             f"{name}() executes code")

    def test_the_module_never_binds_a_process_helper(self):
        for attr in ("subprocess", "Popen", "system", "popen"):
            self.assertFalse(hasattr(dashboard, attr), f"dashboard.{attr} exists")

    def test_every_import_is_stdlib(self):
        allowed = {"hmac", "http", "http.server", "json", "os", "secrets", "socket",
                   "sys", "threading", "time", "urllib", "urllib.parse", "argparse",
                   "__future__", "typing"}
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name, allowed, f"third-party import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # relative: our own package
                    continue
                self.assertIn(node.module, allowed, f"third-party import {node.module}")


# -- 6. the access log ---------------------------------------------------------

class AccessLoggingIsReal(unittest.TestCase):
    """The ancestor stubbed log_message to silence, so its open dashboard could be
    probed from the network without leaving a line anywhere."""

    def setUp(self):
        self.h = Harness(self)
        s = self.h.store()
        awaiting(s)
        s.close()

    def _hook(self, name):
        for node in ast.walk(TREE):
            if isinstance(node, ast.ClassDef) and node.name == "Handler":
                for fn in node.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == name:
                        return fn
        self.fail(f"Handler.{name} is not overridden")

    def test_the_log_hooks_are_not_stubs(self):
        for name in ("log_request", "log_message"):
            body = [n for n in self._hook(name).body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            self.assertTrue(body, f"Handler.{name} is a stub — the ancestor's exact bug")
            self.assertFalse(all(isinstance(n, ast.Pass) for n in body),
                             f"Handler.{name} silences the log")
            self.assertTrue(any(isinstance(n, ast.Call) and _calls_sink(n)
                                for n in ast.walk(self._hook(name))),
                            f"Handler.{name} never reaches the access log")

    def test_a_served_request_is_logged(self):
        self.h.request("GET", "/api/queue")
        line = [l for l in self.h.log if "/api/queue" in l]
        self.assertTrue(line, "a served request left no trace")
        self.assertIn("GET", line[-1])
        self.assertIn("200", line[-1])
        self.assertIn("127.0.0.1", line[-1])

    def test_a_refusal_is_logged_with_its_reason(self):
        self.h.request("GET", "/api/queue", token="wrong")
        self.h.request("POST", "/api/decision", body={"id": "a1", "decision": "approve"},
                       headers={"Origin": "http://evil.example", "Sec-Fetch-Site": None})
        joined = "\n".join(self.h.log)
        self.assertIn("401", joined, "an unauthorized probe left no trace")
        self.assertIn("bad token", joined)
        self.assertIn("403", joined)
        self.assertIn("evil.example", joined)

    def test_a_decision_is_logged(self):
        self.h.request("POST", "/api/decision", body={"id": "a1", "decision": "approve"})
        self.assertTrue(any("decided a1 -> approved" in l for l in self.h.log),
                        "there is no record of who approved what")

    def test_the_token_never_reaches_the_log(self):
        self.h.request("GET", f"/api/queue?token={self.h.token}", token=None)
        self.h.request("GET", f"/api/queue?smuggled={self.h.token}")
        self.assertTrue(self.h.log)
        for line in self.h.log:
            self.assertNotIn(self.h.token, line,
                             "the token was written to the access log")

    def test_redaction_is_enforced_at_the_sink(self):
        out = []
        dashboard._redacting(out.append, "SEKRIT")("GET /x?token=SEKRIT -> 200")
        self.assertEqual(out, ["GET /x?token=<redacted> -> 200"])

    def test_the_file_log_writes_0600_and_appends(self):
        home = tempfile.mkdtemp(prefix="or-log-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        write = dashboard.file_access_log(home)
        with contextlib.redirect_stderr(io.StringIO()) as noise:
            write("first")
            write("second")
        self.assertIn("first", noise.getvalue(),
                      "the second sink is stderr, so a log line survives a full disk")
        p = os.path.join(home, dashboard.ACCESS_LOG_FILE)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        with open(p) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 2)


def _calls_sink(node):
    f = node.func
    return isinstance(f, ast.Attribute) and f.attr == "access_log"


# -- 7. the gate ---------------------------------------------------------------

class BelowBarNeverReachesAHuman(unittest.TestCase):
    """store.BELOW_BAR is terminal except via an explicit rebuild. A dashboard
    that shows one is a dashboard that invites approving past the gate."""

    def setUp(self):
        self.h = Harness(self)
        s = self.h.store()
        awaiting(s, "good")
        below_bar(s, "weak")
        s.close()

    def test_the_queue_holds_only_applications_awaiting_approval(self):
        _, body, _ = self.h.request("GET", "/api/queue")
        ids = [r["id"] for r in body["queue"]]
        self.assertEqual(ids, ["good"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["stats"]["below_bar"], 1,
                         "the below-bar build should still be COUNTED, just not offered")

    def test_a_below_bar_application_cannot_be_approved(self):
        status, body, _ = self.h.request("POST", "/api/decision",
                                         body={"id": "weak", "decision": "approve"})
        self.assertEqual(status, 409)
        self.assertIn("below_bar", body["error"])
        self.assertIs(state_of(self.h, "weak"), State.BELOW_BAR)

    def test_the_dashboard_refuses_before_it_ever_asks_the_store(self):
        """Defence in depth, and the test has to be able to SEE both layers.

        store.LEGAL already forbids below_bar -> approved, so a test that checks
        only the status code passes with the dashboard's own guard deleted — the
        store raises IllegalTransition and the caller still sees 409. That mutant
        genuinely survived until this test existed. What must be true is that the
        dashboard refuses on its own, without ever asking the store to perform
        the transition.
        """
        with unittest.mock.patch.object(
                Store, "transition",
                side_effect=AssertionError("the dashboard asked the store to "
                                           "approve a below-bar build")):
            status, body, _ = self.h.request("POST", "/api/decision",
                                             body={"id": "weak", "decision": "approve"})
        self.assertEqual(status, 409, body)
        self.assertIn("not awaiting approval", body["error"])
        self.assertIs(state_of(self.h, "weak"), State.BELOW_BAR)

    def test_a_below_bar_application_cannot_be_declined_into_a_decision_either(self):
        self.assertEqual(self.h.request("POST", "/api/decision",
                                        body={"id": "weak", "decision": "decline"})[0], 409)
        self.assertIs(state_of(self.h, "weak"), State.BELOW_BAR)

    def test_deciding_twice_is_refused_not_re_applied(self):
        self.assertEqual(self.h.request("POST", "/api/decision",
                                        body={"id": "good", "decision": "approve"})[0], 200)
        status, _, _ = self.h.request("POST", "/api/decision",
                                      body={"id": "good", "decision": "decline"})
        self.assertEqual(status, 409)
        self.assertIs(state_of(self.h, "good"), State.APPROVED)

    def test_an_approval_records_where_the_consent_came_from(self):
        self.h.request("POST", "/api/decision", body={"id": "good", "decision": "approve"})
        s = self.h.store()
        try:
            app = s.get("good")
            self.assertEqual(app.channel, "dashboard")
            self.assertIsNotNone(app.approved_at)
            self.assertTrue(any("dashboard" in (e["note"] or "") for e in s.history("good")))
        finally:
            s.close()


class AmbiguityIsNotConsent(unittest.TestCase):

    def setUp(self):
        self.h = Harness(self)
        s = self.h.store()
        awaiting(s)
        s.close()

    def test_only_the_exact_decision_words_are_accepted(self):
        for value in ("yes", "y", "Approve", "APPROVE", "approve all", "sure",
                      "", " approve ", 1, True, None, ["approve"], "ok"):
            status, _, _ = self.h.request("POST", "/api/decision",
                                          body={"id": "a1", "decision": value})
            self.assertEqual(status, 400, repr(value))
            self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL, repr(value))

    def test_a_missing_decision_decides_nothing(self):
        self.assertEqual(self.h.request("POST", "/api/decision", body={"id": "a1"})[0], 400)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL)

    def test_an_empty_or_unparseable_body_decides_nothing(self):
        for raw in (b"", b"not json", b"[]", b'"approve"', b"null"):
            status, _, _ = self.h.request("POST", "/api/decision", body=raw)
            self.assertEqual(status, 400, raw)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL)

    def test_an_oversized_body_is_refused(self):
        raw = json.dumps({"id": "a1", "decision": "approve",
                          "pad": "x" * (dashboard.MAX_BODY + 10)}).encode()
        self.assertEqual(self.h.request("POST", "/api/decision", body=raw)[0], 400)
        self.assertIs(state_of(self.h, "a1"), State.AWAITING_APPROVAL)


# -- 8. honest emptiness -------------------------------------------------------

class EmptyIsNeverAmbiguous(unittest.TestCase):
    """Never return an empty result indistinguishable from 'nothing found'."""

    def setUp(self):
        self.h = Harness(self)

    def test_an_empty_queue_says_why_it_is_empty(self):
        _, body, _ = self.h.request("GET", "/api/queue")
        self.assertEqual(body["queue"], [])
        self.assertIn("empty_because", body)
        self.assertTrue(body["empty_because"])

    def test_a_queue_with_work_behind_it_reports_the_pipeline(self):
        s = self.h.store()
        below_bar(s, "w1")
        below_bar(s, "w2")
        s.close()
        _, body, _ = self.h.request("GET", "/api/queue")
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["stats"]["below_bar"], 2,
                         "an empty queue and a stalled pipeline must not look identical")

    def test_unconfigured_goals_are_labelled_not_returned_as_nothing(self):
        _, body, _ = self.h.request("GET", "/api/goals")
        self.assertIs(body["configured"], False)
        self.assertIsNone(body["goals"])
        self.assertTrue(body["reason"])

    def test_configured_goals_come_back_configured(self):
        s = self.h.store()
        s.save_goals({"goals": [{"goal": "principal PM at an AI lab"}]})
        s.close()
        _, body, _ = self.h.request("GET", "/api/goals")
        self.assertIs(body["configured"], True)
        self.assertEqual(body["goals"]["goals"][0]["goal"], "principal PM at an AI lab")

    def test_an_unknown_application_is_a_404_not_an_empty_record(self):
        status, body, _ = self.h.request("GET", "/api/application?id=ghost")
        self.assertEqual(status, 404)
        self.assertIn("ghost", body["error"])

    def test_asking_for_no_application_is_refused(self):
        self.assertEqual(self.h.request("GET", "/api/application")[0], 400)
        self.assertEqual(self.h.request("GET", "/api/application?id=")[0], 400)

    def test_an_application_carries_its_whole_history(self):
        s = self.h.store()
        awaiting(s, "a1")
        s.close()
        _, body, _ = self.h.request("GET", "/api/application?id=a1")
        self.assertEqual([e["to_state"] for e in body["history"]],
                         ["discovered", "building", "awaiting_approval"])
        self.assertTrue(body["decidable_here"])
        self.assertIn("approved", body["legal_next"])
        self.assertIsNone(body["outcome"])

    def test_a_thin_report_refuses_to_be_read_as_a_finding(self):
        s = self.h.store()
        for i in range(2):
            app = awaiting(s, f"a{i}")
            s.transition(app, State.APPROVED)
            s.transition(app, State.SUBMITTING)
            s.transition(app, State.SUBMITTED_VERIFIED)
            s.record_outcome(app, "rejected")
        s.close()
        _, body, _ = self.h.request("GET", "/api/report")
        self.assertEqual(body["total_outcomes"], 2)
        self.assertIs(body["answerable"], False)
        self.assertIn("too few", body["reading"])
        self.assertEqual(body["by_outcome"]["rejected"]["n"], 2,
                         "the numbers are still shown, just not called a finding")

    def test_enough_outcomes_flip_it_to_answerable(self):
        s = self.h.store()
        for i in range(dashboard.MIN_OUTCOMES_FOR_A_READING):
            app = awaiting(s, f"a{i}")
            s.transition(app, State.APPROVED)
            s.transition(app, State.SUBMITTING)
            s.transition(app, State.SUBMITTED_VERIFIED)
            s.record_outcome(app, "rejected" if i % 2 else "screen")
        s.close()
        _, body, _ = self.h.request("GET", "/api/report")
        self.assertIs(body["answerable"], True)
        self.assertEqual(body["submitted"], dashboard.MIN_OUTCOMES_FOR_A_READING)

    def test_the_queue_says_when_it_truncated(self):
        s = self.h.store()
        for i in range(3):
            awaiting(s, f"a{i}")
        s.close()
        with unittest.mock.patch.object(dashboard, "MAX_QUEUE_ROWS", 2):
            _, body, _ = self.h.request("GET", "/api/queue")
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["shown"], 2)
        self.assertIs(body["truncated"], True, "a silently short list is a lie")


class TheCardCarriesEnoughToDecide(unittest.TestCase):
    """A row showing a filename and a score turns fifteen seconds of reading into
    one second of clicking, and approval fatigue does the rest."""

    def test_the_queue_row_carries_the_weakest_reason_and_the_claims(self):
        h = Harness(self)
        s = h.store()
        awaiting(s, "a1", claims=["$20M annualized revenue", "3,000 monthly actives"])
        s.close()
        _, body, _ = h.request("GET", "/api/queue")
        row = body["queue"][0]
        self.assertEqual(row["weakest"], "recruiter")
        self.assertIn("buries the platform work", row["weakest_reason"])
        self.assertIn("$20M annualized revenue", row["claims"])
        self.assertEqual(row["score"], 84.0)
        self.assertEqual(row["raw_score"], 79.0)
        self.assertTrue(row["waiting_since"])


# -- 9. pause ------------------------------------------------------------------

class ThePauseSwitch(unittest.TestCase):

    def setUp(self):
        self.h = Harness(self)

    def test_it_toggles_and_is_readable_by_other_code(self):
        self.assertFalse(dashboard.is_paused(self.h.home))
        status, body, _ = self.h.request("POST", "/api/pause", body={"paused": True})
        self.assertEqual(status, 200)
        self.assertIs(body["paused"], True)
        self.assertTrue(dashboard.is_paused(self.h.home))
        self.h.request("POST", "/api/pause", body={"paused": False})
        self.assertFalse(dashboard.is_paused(self.h.home))

    def test_only_a_real_boolean_moves_it(self):
        for value in ("true", 1, "yes", None, [], "on"):
            status, _, _ = self.h.request("POST", "/api/pause", body={"paused": value})
            self.assertEqual(status, 400, repr(value))
            self.assertFalse(dashboard.is_paused(self.h.home))

    def test_an_unreadable_pause_file_reads_as_paused(self):
        """Fail closed: the safe direction for a flag that gates applying on
        someone's behalf is 'do nothing', and the reason travels with it."""
        with open(dashboard.pause_path(self.h.home), "w") as f:
            f.write("{not json")
        self.assertTrue(dashboard.is_paused(self.h.home))
        self.assertIn("unreadable", dashboard.read_pause(self.h.home)["reason"])

    def test_a_file_without_a_boolean_reads_as_paused(self):
        with open(dashboard.pause_path(self.h.home), "w") as f:
            json.dump({"paused": "yes"}, f)
        self.assertTrue(dashboard.is_paused(self.h.home))

    def test_the_pause_file_is_0600(self):
        dashboard.set_paused(True, self.h.home)
        self.assertEqual(os.stat(dashboard.pause_path(self.h.home)).st_mode & 0o777, 0o600)

    def test_the_queue_reports_the_pause_state(self):
        dashboard.set_paused(True, self.h.home)
        _, body, _ = self.h.request("GET", "/api/queue")
        self.assertIs(body["pause"]["paused"], True)


# -- 10. the page --------------------------------------------------------------

class ThePageLeaksNothing(unittest.TestCase):

    def setUp(self):
        self.h = Harness(self)

    def test_the_html_never_contains_the_token(self):
        status, page, _ = self.h.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.h.token, page)

    def test_security_headers_are_sent(self):
        _, _, headers = self.h.request("GET", "/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")
        csp = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("nonce-", csp)
        self.assertNotIn("unsafe-inline", csp)

    def test_the_script_nonce_changes_every_response(self):
        _, _, h1 = self.h.request("GET", "/")
        _, _, h2 = self.h.request("GET", "/")
        self.assertNotEqual(h1["Content-Security-Policy"], h2["Content-Security-Policy"])

    def test_the_page_builds_dom_instead_of_interpolating_html(self):
        """Company and role come from job postings — somebody else's HTML. An
        innerHTML interpolation here hands this page's token to whoever wrote the
        posting."""
        page = dashboard.render_page("n")
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("document.write", page)
        self.assertNotIn("eval(", page)

    def test_the_page_is_theme_aware(self):
        self.assertIn("prefers-color-scheme:dark", dashboard.render_page("n").replace(" ", ""))

    def test_the_nonce_is_placed_in_the_markup(self):
        page = dashboard.render_page("abc123")
        self.assertIn('nonce="abc123"', page)
        self.assertNotIn("__NONCE__", page)


class TheRouteMapIsClosed(unittest.TestCase):

    def test_read_and_write_routes_are_disjoint_and_complete(self):
        self.assertEqual(set(dashboard.ROUTES),
                         set(dashboard.READ_ROUTES) | set(dashboard.WRITE_ROUTES))
        self.assertFalse(set(dashboard.READ_ROUTES) & set(dashboard.WRITE_ROUTES))

    def test_exactly_two_routes_change_state(self):
        self.assertEqual(set(dashboard.WRITE_ROUTES), {"/api/decision", "/api/pause"})

    def test_an_unknown_route_is_a_404(self):
        h = Harness(self)
        for path in ("/api/approve-all", "/api/run", "/exec", "/../etc/passwd"):
            self.assertEqual(h.request("GET", path)[0], 404, path)

    def test_other_methods_are_refused(self):
        h = Harness(self)
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            conn.request(method, "/api/queue", headers={"X-OpenRecruiter-Token": h.token})
            self.assertEqual(conn.getresponse().status, 405, method)
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
