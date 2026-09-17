"""A localhost dashboard, written as if it were exposed — because it is.

The ancestor of this project shipped a dashboard that bound every interface with
no authentication, no cross-site protection, a silenced access log, and a route
that fed an unauthenticated request body to a shell with the user's credentials
in the environment. On a self-hosted tool, that is remote code execution on every
user's home network, reachable by anything that can route a packet to their
laptop. None of it was malicious; each piece was individually convenient.

So the posture here is the opposite of convenient, and each control below exists
because its absence was shipped once:

  BIND         127.0.0.1, enforced in `server_bind` BEFORE the socket binds, on
               an IPv4-only server. There is no parameter, flag or environment
               variable anywhere in this module that changes the address.
  AUTH         a 256-bit token generated on first run, stored 0600, required on
               EVERY request including GETs, compared with `hmac.compare_digest`.
               It travels in a header or the bootstrap query string, never in a
               cookie -- a cookie is precisely what would make a cross-site POST
               authenticate itself.
  CSRF         every state change is a POST that must prove same-origin via
               Sec-Fetch-Site or Origin. It deliberately does NOT trust Host: a
               cross-origin form POST carries the same Host value as a legitimate
               one, so a Host check looks like a CSRF defence and is not one.
               Host is still checked, for a different attack (DNS rebinding).
  EXECUTION    there is no route that runs a program. No subprocess, no shell, no
               eval. A test parses this module's AST and fails if one appears.
  BULK         there is no route that decides more than one application. Not
               behind a flag, not with a list body. `/api/decision` takes exactly
               one id and refuses a list.
  AUDIT        real access logging, to a file and to stderr, including refusals.
               The token is redacted from every line by construction.

Other lanes: `is_paused()` / `set_paused()` are the public pause switch, and the
work loop should consult `is_paused()` before it builds or submits anything.
"""
from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse

from .store import Application, IllegalTransition, LEGAL, State, Store, TERMINAL

# One address, written as a literal. Not a hostname: name resolution is somebody
# else's configuration file, and "localhost" is only loopback by convention.
LOOPBACK = "127.0.0.1"
LOOPBACK_ONLY = frozenset({LOOPBACK})
DEFAULT_PORT = 8765

TOKEN_FILE = "dashboard.token"
PAUSE_FILE = "paused.json"
ACCESS_LOG_FILE = "dashboard-access.log"

MAX_BODY = 64 * 1024
MAX_QUEUE_ROWS = 200
# Below this many recorded outcomes the report shows its numbers but refuses to
# call them a finding. Three data points that agree are still three data points.
MIN_OUTCOMES_FOR_A_READING = 10

# The only two decisions this surface accepts, as exact literals. Free text is
# not parsed here on purpose: a button either was pressed or was not, and
# anything else is ambiguity, which is never consent.
DECISIONS = {"approve": State.APPROVED, "decline": State.DECLINED}

READ_ROUTES = ("/", "/api/queue", "/api/application", "/api/goals", "/api/report")
WRITE_ROUTES = ("/api/decision", "/api/pause")
ROUTES = READ_ROUTES + WRITE_ROUTES


class DashboardError(RuntimeError):
    pass


class RefusedBind(DashboardError):
    """Raised instead of listening anywhere but loopback."""


class InsecureToken(DashboardError):
    """Raised instead of serving with a token anyone on the box can read."""


# -- home, token, pause -------------------------------------------------------

def home_dir(home: str | None = None) -> str:
    d = home or os.environ.get("OPENRECRUITER_HOME") or os.path.expanduser("~/.openrecruiter")
    os.makedirs(d, exist_ok=True)
    return d


def token_path(home: str | None = None) -> str:
    return os.path.join(home_dir(home), TOKEN_FILE)


def ensure_token(home: str | None = None) -> str:
    """Return the dashboard token, minting it 0600 on first run.

    Refuses to hand back a token file that other users on the machine can read.
    A weakened permission is the whole credential gone, and repairing it silently
    would hide that something already read it.
    """
    p = token_path(home)
    if not os.path.exists(p):
        tok = secrets.token_urlsafe(32)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # lost a race with another process; fall through and read theirs
        else:
            with os.fdopen(fd, "w") as f:
                f.write(tok + "\n")
            return tok
    mode = os.stat(p).st_mode
    if mode & 0o077:
        raise InsecureToken(
            f"{p} is mode {oct(mode & 0o777)} — readable by other users on this machine.\n"
            f"Anyone who read it can approve your applications. Rotate it:\n"
            f"  rm {p}   (a new token is generated on the next start)")
    with open(p) as f:
        tok = f.read().strip()
    if not tok:
        raise InsecureToken(f"{p} is empty. Delete it and a new token is generated on start.")
    return tok


def pause_path(home: str | None = None) -> str:
    return os.path.join(home_dir(home), PAUSE_FILE)


def read_pause(home: str | None = None) -> dict:
    """The pause switch, with its reason.

    An unreadable or malformed file reads as PAUSED. The safe direction for a
    flag that gates applying on someone's behalf is "do nothing", and the reason
    is carried so a corrupt file is never mistaken for a deliberate pause.
    """
    p = pause_path(home)
    if not os.path.exists(p):
        return {"paused": False, "at": None, "reason": "never paused"}
    try:
        with open(p) as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("paused"), bool):
            raise ValueError("no boolean `paused` field")
        return {"paused": data["paused"], "at": data.get("at"),
                "reason": "set from the dashboard"}
    except Exception as e:
        return {"paused": True, "at": None,
                "reason": f"{p} is unreadable ({e}); treating as paused"}


def is_paused(home: str | None = None) -> bool:
    return bool(read_pause(home)["paused"])


def set_paused(value: bool, home: str | None = None) -> dict:
    if not isinstance(value, bool):
        raise TypeError("paused must be a bool")
    p = pause_path(home)
    payload = {"paused": value, "at": time.time()}
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, p)
    return read_pause(home)


# -- request checks -----------------------------------------------------------

def check_token(expected: str, provided: str | None) -> bool:
    """Constant-time token comparison. `==` on a secret leaks its prefix."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _hostname_of(host_header: str) -> str:
    h = (host_header or "").strip()
    if h.startswith("["):                       # [::1]:8765
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def host_is_loopback(host_header: str | None) -> bool:
    """Defeats DNS rebinding: a browser lured to evil.example that resolves to
    127.0.0.1 still sends `Host: evil.example`.

    This is NOT the CSRF check. A cross-origin form POST carries the correct Host
    value, which is exactly why the ancestor's host check protected nothing.
    """
    return _hostname_of(host_header or "").lower() in {"127.0.0.1", "localhost", "[::1]"}


def allowed_origins(port: int) -> set[str]:
    return {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}


def same_origin(headers, port: int) -> tuple[bool, str]:
    """Whether a state-changing request provably came from this dashboard.

    Returns (ok, reason). When neither Sec-Fetch-Site nor Origin is present the
    answer is NO: a check that cannot run is a refusal, never a pass. A caller
    that is not a browser (a script on this machine, which already holds the
    token) sends `Origin: http://127.0.0.1:<port>` to say so.
    """
    site = headers.get("Sec-Fetch-Site")
    origin = headers.get("Origin")
    allowed = allowed_origins(port)
    if site is not None:
        if site != "same-origin":
            return False, f"Sec-Fetch-Site: {site}"
        if origin is not None and origin not in allowed:
            return False, f"Origin {origin!r} is not this dashboard"
        return True, "same-origin"
    if origin is None:
        return False, "neither Origin nor Sec-Fetch-Site sent; cannot prove same-origin"
    if origin not in allowed:
        return False, f"Origin {origin!r} is not this dashboard"
    return True, "origin matches"


# -- access log ---------------------------------------------------------------

def file_access_log(home: str | None = None):
    """Append-only access log to disk AND stderr.

    Two sinks because the failure being designed against is silence: if the disk
    write fails, the line still reaches stderr and the failure itself is printed
    rather than swallowed.
    """
    path = os.path.join(home_dir(home), ACCESS_LOG_FILE)
    lock = threading.Lock()

    def write(line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        rec = f"{stamp} {line}"
        with lock:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(rec + "\n")
            except OSError as e:
                print(f"[dashboard] ACCESS LOG WRITE FAILED ({e}) — {rec}", file=sys.stderr)
                return
            print(rec, file=sys.stderr)
    return write


def _redacting(sink, token: str):
    """The token must never reach a log line, whatever the format string did.

    Enforced here rather than at each call site, so a future log line cannot
    reintroduce the leak by interpolating the raw query string.
    """
    def write(line: str) -> None:
        if token and token in line:
            line = line.replace(token, "<redacted>")
        sink(line)
    return write


# -- the server ---------------------------------------------------------------

class LoopbackOnlyServer(http.server.HTTPServer):
    """An HTTP server that cannot be talked into listening on a public address.

    Single-threaded on purpose. One user, one browser, a handful of requests --
    and one SQLite connection with thread affinity. Concurrency here would buy
    nothing and cost a class of bugs on the surface that holds the approval
    switch. The handler carries a socket timeout so a stalled client cannot
    wedge it.
    """
    address_family = socket.AF_INET     # no IPv6: no "::" to be tricked into
    allow_reuse_address = True

    def server_bind(self):
        host = self.server_address[0]
        if host not in LOOPBACK_ONLY:
            raise RefusedBind(
                f"refusing to bind {host!r}: this dashboard serves {LOOPBACK} only. "
                f"It holds your approval switch and your job history; putting it on a "
                f"routable interface exposes both to your whole network.")
        super().server_bind()
        bound = self.socket.getsockname()[0]
        if bound not in LOOPBACK_ONLY:      # unreachable today; the cost of being wrong is total
            self.socket.close()
            raise RefusedBind(f"bound {bound!r}, which is not loopback")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OpenRecruiter"
    sys_version = ""                     # do not advertise the interpreter version
    protocol_version = "HTTP/1.0"        # close per response: no keep-alive starvation
    timeout = 15

    # -- logging ---------------------------------------------------------------
    # Both hooks below ROUTE the log. Neither silences it. The ancestor stubbed
    # log_message with `pass`, which is why its dashboard could be probed from
    # the network and leave no trace at all -- including the refusals, which are
    # the lines you actually need after something goes wrong.
    def log_request(self, code="-", size="-"):
        """One line per response, including every refusal."""
        if isinstance(code, int):
            code = int(code)                    # HTTPStatus formats as its name otherwise
        note = getattr(self, "_note", "")
        self.server.access_log(
            f"{self.address_string()} {getattr(self, 'command', '-')} "
            f"{self._log_target()} -> {code}" + (f" {note}" if note else ""))

    def log_message(self, fmt, *args):
        """Protocol-level errors (timeouts, malformed request lines) land here."""
        try:
            text = fmt % args
        except Exception:
            text = str(fmt)
        self.server.access_log(f"{self.address_string()} {text}")

    def _log_target(self) -> str:
        raw = getattr(self, "path", None) or getattr(self, "requestline", "") or "-"
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            return "<unparseable>"
        if not parts.query:
            return parts.path or raw
        q = [(k, "<redacted>" if k.lower() == "token" else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
        return parts.path + "?" + urllib.parse.urlencode(q)

    # -- responses -------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int, nonce: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        csp = ("default-src 'none'; connect-src 'self'; img-src data:; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if nonce:
            csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()

    def _json(self, status: int, payload: dict, note: str = "") -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self._note = note
        self._responded = True
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _html(self, status: int, text: str, nonce: str) -> None:
        body = text.encode("utf-8")
        self._note = ""
        self._responded = True
        self._headers(status, "text/html; charset=utf-8", len(body), nonce=nonce)
        self.wfile.write(body)

    def _refuse(self, status: int, public: str, note: str = "") -> None:
        """Say little to the client, write everything to the log."""
        self._drain()
        self._json(status, {"error": public}, note=note or public)

    def _drain(self) -> None:
        """Read and discard an unread request body so the client sees the response
        rather than a reset connection.

        Idempotent: draining a body that was already read would block on bytes
        that are never coming, and the request would hang until the socket
        timeout instead of being refused.
        """
        if getattr(self, "_drained", False):
            return
        self._drained = True
        try:
            n = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        except ValueError:
            return
        if n > 0:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    # -- dispatch --------------------------------------------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _method_not_allowed(self):
        self._refuse(405, "method not allowed")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def _handle(self, method: str) -> None:
        self._note = ""
        self._responded = False
        self._drained = False
        try:
            parts = urllib.parse.urlsplit(self.path)
        except ValueError:
            return self._refuse(400, "bad request line")
        path = parts.path.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))

        if not host_is_loopback(self.headers.get("Host")):
            return self._refuse(403, "forbidden",
                                f"Host {self.headers.get('Host')!r} is not loopback "
                                f"(DNS rebinding?)")

        provided = self.headers.get("X-OpenRecruiter-Token") or query.get("token")
        if not check_token(self.server.or_token, provided):
            return self._refuse(401, "unauthorized",
                                "no token" if not provided else "bad token")

        if path not in ROUTES:
            return self._refuse(404, "no such route")

        if path in WRITE_ROUTES:
            if method != "POST":
                return self._refuse(405, "this route changes state; use POST")
            ok, why = same_origin(self.headers, self.server.or_port)
            if not ok:
                return self._refuse(403, "cross-site request refused", why)
        elif method != "GET":
            return self._refuse(405, "this route is read-only; use GET")

        try:
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                return self._html(200, render_page(nonce), nonce)
            if path == "/api/queue":
                return self._json(200, self._queue())
            if path == "/api/application":
                return self._application(query)
            if path == "/api/goals":
                return self._goals()
            if path == "/api/report":
                return self._report()
            if path == "/api/decision":
                return self._decision()
            if path == "/api/pause":
                return self._pause()
        except Exception as e:                     # never answer 200 with a half-truth
            if self._responded:                    # headers already out; say so in the log
                self.server.access_log(
                    f"{self.address_string()} FAILED AFTER RESPONDING "
                    f"{type(e).__name__}: {e}")
                return
            return self._refuse(500, "the dashboard failed to answer that",
                                f"{type(e).__name__}: {e}")

    # -- body ------------------------------------------------------------------
    def _body(self) -> tuple[dict | None, str | None]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # Also a structural CSRF barrier: an HTML form can only send
            # urlencoded/multipart/plain, and a JSON content type from a page
            # would need a preflight this server never answers.
            self._drain()
            return None, "expected Content-Type: application/json"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "bad Content-Length"
        if n <= 0:
            return None, "empty body"
        if n > MAX_BODY:
            self._drain()
            return None, "body too large"
        self._drained = True                       # whatever happens below, it is read
        raw = self.rfile.read(n)
        if len(raw) != n:
            return None, "truncated body"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "body is not valid JSON"
        if not isinstance(data, dict):
            return None, "body must be a JSON object"
        return data, None

    # -- routes ----------------------------------------------------------------
    def _store(self) -> Store:
        if self.server.or_store is None:
            self.server.or_store = self.server.or_store_factory()
        return self.server.or_store

    def _queue(self) -> dict:
        st = self._store()
        # Only AWAITING_APPROVAL. A resume that failed its tier's gate is not
        # shown here even as a greyed-out row: the gate must not be something a
        # human can rubber-stamp past, and a visible row is an invitation to try.
        apps = st.by_state(State.AWAITING_APPROVAL)
        rows = [_app_public(a, st) for a in apps[:MAX_QUEUE_ROWS]]
        stats = st.stats()
        out = {
            "count": len(apps),
            "shown": len(rows),
            "truncated": len(apps) > len(rows),
            "queue": rows,
            "stats": stats,
            "pause": read_pause(self.server.or_home),
            "decisions": sorted(DECISIONS),
        }
        if not rows:
            # Never an empty list on its own: "nothing is waiting" and "the
            # pipeline is stalled with forty below-bar builds" look identical
            # otherwise, and only one of them is fine.
            out["empty_because"] = (
                "nothing is awaiting your approval right now"
                if stats else
                "the store is empty — nothing has been discovered yet")
        return out

    def _application(self, query: dict) -> None:
        app_id = (query.get("id") or "").strip()
        if not app_id:
            return self._refuse(400, "give one application id: /api/application?id=...")
        st = self._store()
        app = st.get(app_id)
        if app is None:
            return self._refuse(404, f"no application with id {app_id!r}")
        payload = _app_public(app, st)
        payload["history"] = st.history(app_id)
        payload["outcome"] = _outcome_for(st, app_id)
        payload["legal_next"] = sorted(s.value for s in LEGAL[app.state])
        payload["terminal"] = app.state in TERMINAL
        payload["decidable_here"] = app.state is State.AWAITING_APPROVAL
        return self._json(200, payload)

    def _goals(self) -> None:
        g = self._store().load_goals()
        if g is None:
            return self._json(200, {
                "configured": False, "goals": None,
                "reason": "no goals have been saved yet, so nothing is being matched "
                          "against your search. Run the intake interview."})
        return self._json(200, {"configured": True, "goals": g})

    def _report(self) -> None:
        st = self._store()
        by_outcome = st.score_vs_outcome()
        total = sum(v["n"] for v in by_outcome.values())
        stats = st.stats()
        submitted = (stats.get(State.SUBMITTED_VERIFIED.value, 0)
                     + stats.get(State.SUBMITTED_UNVERIFIED.value, 0))
        answerable = total >= MIN_OUTCOMES_FOR_A_READING
        return self._json(200, {
            "by_outcome": by_outcome,
            "total_outcomes": total,
            "submitted": submitted,
            "awaiting_outcome": max(submitted - total, 0),
            "answerable": answerable,
            # The store records outcomes so this question is answerable at all.
            # Answering it from four rows would replace "we do not know" with a
            # number, which is worse.
            "reading": ("scores below are computed from recorded outcomes"
                        if answerable else
                        f"{total} recorded outcome(s) — too few to read anything into. "
                        f"Shown for completeness, not as a finding."),
        })

    def _decision(self) -> None:
        body, err = self._body()
        if err:
            return self._refuse(400, err)
        app_id = body.get("id")
        if isinstance(app_id, (list, tuple, set, dict)):
            # The single most important refusal in this module. There is no route
            # here that decides more than one application, and handing a list to
            # the one-decision route is how that would quietly be built.
            return self._refuse(400, "one application per request: `id` must be a single id",
                                "refused a multi-id decision")
        if not isinstance(app_id, str) or not app_id.strip():
            return self._refuse(400, "give one application id in `id`")
        app_id = app_id.strip()

        decision = body.get("decision")
        # The isinstance guard is load-bearing: `["approve"] in DECISIONS` raises
        # on an unhashable value, and a 500 on a malformed decision is a refusal
        # that does not look like one.
        if not isinstance(decision, str) or decision not in DECISIONS:
            return self._refuse(
                400, f"`decision` must be exactly one of {sorted(DECISIONS)}",
                f"refused decision {decision!r}: not unmistakably a yes or a no")

        st = self._store()
        app = st.get(app_id)
        if app is None:
            return self._refuse(404, f"no application with id {app_id!r}")
        if app.state is not State.AWAITING_APPROVAL:
            # Covers the case this project exists for: an application that failed
            # its tier's gate sits in BELOW_BAR and can never be approved from
            # here. It also makes a double-click idempotent instead of a second
            # decision on an already-decided application.
            return self._refuse(
                409, f"{app_id} is {app.state.value}, not awaiting approval",
                f"refused a decision on a {app.state.value} application")

        target = DECISIONS[decision]
        fields = {"channel": "dashboard"}
        if target is State.APPROVED:
            fields["approved_at"] = time.time()
        try:
            after = st.transition(app_id, target, note=f"dashboard: {decision}", **fields)
        except IllegalTransition as e:
            return self._refuse(409, str(e))
        return self._json(200, {"id": app_id, "decision": decision, "state": after.state.value},
                          note=f"decided {app_id} -> {after.state.value}")

    def _pause(self) -> None:
        body, err = self._body()
        if err:
            return self._refuse(400, err)
        value = body.get("paused")
        if not isinstance(value, bool):
            return self._refuse(400, "`paused` must be true or false",
                                f"refused paused={value!r}")
        return self._json(200, set_paused(value, self.server.or_home),
                          note=f"paused={value}")


def _app_public(app: Application, store: Store) -> dict:
    """The fields a human needs to decide, which is more than a name and a score.

    The card doctrine this inherits: showing a filename and a number turns
    fifteen seconds of reading into one second of clicking, and approval fatigue
    does the rest. So the weakest judge's actual reason and the claims the resume
    asserts travel with every row, not behind a click.
    """
    waiting_since = None
    for ev in store.history(app.id):
        if ev["to_state"] == State.AWAITING_APPROVAL.value:
            waiting_since = ev["at"]
    return {
        "id": app.id, "company": app.company, "role": app.role, "url": app.url,
        "ats": app.ats, "tier": app.tier, "state": app.state.value,
        "score": app.score, "raw_score": app.raw_score,
        "weakest": app.weakest, "weakest_reason": app.weakest_reason,
        "claims": list(app.claims), "resume_path": app.resume_path,
        "channel": app.channel, "approved_at": app.approved_at,
        "waiting_since": waiting_since,
    }


def _outcome_for(store: Store, app_id: str):
    """Read one outcome through the Store's own connection.

    Prefers a `get_outcome` method if the store grows one, so this module never
    becomes a second place that knows the schema.
    """
    getter = getattr(store, "get_outcome", None)
    if callable(getter):
        return getter(app_id)
    row = store.db.execute(
        "SELECT outcome, at, days_to, note FROM outcomes WHERE app_id=?", (app_id,)).fetchone()
    return dict(row) if row else None


# -- construction -------------------------------------------------------------

def make_server(port: int = DEFAULT_PORT, *, store_path: str | None = None,
                store_factory=None, token: str | None = None,
                access_log=None, home: str | None = None) -> LoopbackOnlyServer:
    """Build the server. There is deliberately no address parameter."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise DashboardError(f"port {port!r} is not a port number")
    resolved_home = home_dir(home)
    tok = token or ensure_token(resolved_home)
    sink = access_log or file_access_log(resolved_home)

    srv = LoopbackOnlyServer((LOOPBACK, port), Handler)
    srv.or_token = tok
    srv.or_home = resolved_home
    srv.or_port = srv.server_address[1]
    srv.access_log = _redacting(sink, tok)
    # The store is opened lazily, inside the serving thread: a SQLite connection
    # belongs to the thread that created it.
    srv.or_store = None
    srv.or_store_factory = store_factory or (lambda: Store(store_path))
    return srv


def serve(port: int = DEFAULT_PORT, store_path: str | None = None,
          home: str | None = None) -> int:
    srv = make_server(port=port, store_path=store_path, home=home)
    bound = srv.server_address[1]
    print("\nOpenRecruiter dashboard")
    print(f"  http://{LOOPBACK}:{bound}/?token={srv.or_token}")
    print(f"  loopback only — this URL works on this machine and nowhere else")
    print(f"  token: {token_path(home)} (0600)   access log: "
          f"{os.path.join(home_dir(home), ACCESS_LOG_FILE)}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="openrecruiter-dashboard",
        description=f"The local dashboard. Serves {LOOPBACK} only; there is no flag "
                    f"to change the listen address.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--db", default=None, help="path to openrecruiter.db")
    a = p.parse_args(argv)
    try:
        return serve(port=a.port, store_path=a.db)
    except (DashboardError, OSError) as e:
        print(f"dashboard did not start: {e}", file=sys.stderr)
        return 1


# -- the page -----------------------------------------------------------------

_PAGE = '''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="data:,">
<title>OpenRecruiter</title>
<style nonce="__NONCE__">
:root{
  --bg:#f6f7f9; --panel:#ffffff; --fg:#16181d; --muted:#666c78; --line:#e3e5ea;
  --ok:#0f7b46; --ok-bg:#e8f6ef; --no:#a23b12; --no-bg:#fdefe8; --accent:#2f5fd0;
  --chip:#eef0f4;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e1014; --panel:#161a20; --fg:#e7e9ee; --muted:#98a0ad; --line:#262b34;
    --ok:#4ade80; --ok-bg:#12281d; --no:#fb923c; --no-bg:#2a1a11; --accent:#7aa2f7;
    --chip:#1e232b;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
  padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel);
  position:sticky;top:0}
h1{font-size:15px;margin:0;letter-spacing:.02em}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:0 0 10px}
.sp{flex:1}
main{max-width:1060px;margin:0 auto;padding:20px;display:grid;gap:20px;
  grid-template-columns:minmax(0,1.6fr) minmax(260px,1fr)}
@media (max-width:820px){main{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px;margin-bottom:12px}
.role{font-weight:600;font-size:16px}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;
  padding:2px 9px;font-size:12px;margin-right:6px;color:var(--muted)}
.reason{margin:12px 0 4px;padding:10px 12px;border-left:3px solid var(--line);
  background:var(--chip);border-radius:0 6px 6px 0;font-size:14px}
.claims{margin:8px 0 0;padding-left:18px;font-size:14px}
.claims li{margin:2px 0}
.actions{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--fg);
  border-radius:8px;padding:7px 14px;cursor:pointer}
button:hover{border-color:var(--accent)}
button[disabled]{opacity:.45;cursor:default}
.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-bg)}
.no{color:var(--no);border-color:var(--no);background:var(--no-bg)}
.muted{color:var(--muted)}
.small{font-size:13px}
.err{color:var(--no);font-size:13px;margin-left:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}
pre{white-space:pre-wrap;font-size:12px;color:var(--muted);margin:8px 0 0}
.banner{padding:10px 20px;background:var(--no-bg);color:var(--no);
  border-bottom:1px solid var(--line);font-size:14px}
.note{font-size:12px;color:var(--muted);margin-top:14px;line-height:1.5}
a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>OpenRecruiter</h1>
  <span class="chip" id="qcount">—</span>
  <span class="sp"></span>
  <span class="err" id="err"></span>
  <button id="pause">—</button>
  <button id="refresh">Refresh</button>
</header>
<div class="banner" id="banner" hidden></div>
<main>
  <section>
    <h2>Awaiting your decision</h2>
    <div id="queue"></div>
    <p class="note" id="foot"></p>
  </section>
  <aside>
    <div class="panel" style="margin-bottom:20px">
      <h2>Pipeline</h2>
      <table id="stats"><tbody></tbody></table>
    </div>
    <div class="panel" style="margin-bottom:20px">
      <h2>Goals</h2>
      <div id="goals" class="small muted">loading…</div>
    </div>
    <div class="panel">
      <h2>Score vs outcome</h2>
      <div id="report" class="small muted">loading…</div>
    </div>
  </aside>
</main>
<script nonce="__NONCE__">
(function(){
  "use strict";
  var qs = new URLSearchParams(location.search), tok = qs.get("token");
  try {
    if (tok) { sessionStorage.setItem("or_token", tok); }
    else { tok = sessionStorage.getItem("or_token"); }
  } catch (e) { /* private mode: the token stays in this variable only */ }
  if (qs.get("token")) { try { history.replaceState(null, "", location.pathname); } catch (e) {} }

  function el(t, c, txt){
    var e = document.createElement(t);
    if (c) e.className = c;
    if (txt !== undefined && txt !== null) e.textContent = String(txt);
    return e;
  }
  function say(m){ document.getElementById("err").textContent = m || ""; }

  // Every value below is placed with textContent. Company and role come from job
  // postings, which are somebody else's markup; interpolating one into live
  // markup would hand this page's token to whoever wrote the posting.
  function api(path, opts){
    opts = opts || {};
    opts.credentials = "omit";
    opts.headers = Object.assign({"X-OpenRecruiter-Token": tok || ""}, opts.headers || {});
    return fetch(path, opts).then(function(r){
      return r.text().then(function(t){
        var body; try { body = JSON.parse(t); } catch (e) { body = {error: "unreadable response"}; }
        return {status: r.status, body: body};
      });
    });
  }

  function num(v){ return (v === null || v === undefined) ? "—" : v; }

  function row(a){
    var c = el("div", "card");
    c.appendChild(el("div", "role", a.role));
    c.appendChild(el("div", "sub", a.company + (a.ats ? "  ·  " + a.ats : "")));
    var chips = el("div"); chips.style.marginTop = "10px";
    chips.appendChild(el("span", "chip", a.tier));
    chips.appendChild(el("span", "chip", "score " + num(a.score) + "  (raw " + num(a.raw_score) + ")"));
    if (a.waiting_since) {
      var h = Math.round((Date.now() / 1000 - a.waiting_since) / 3600);
      chips.appendChild(el("span", "chip", "waiting " + h + "h"));
    }
    c.appendChild(chips);
    if (a.weakest_reason) {
      c.appendChild(el("div", "reason", "weakest — " + (a.weakest || "?") + ": " + a.weakest_reason));
    }
    if (a.claims && a.claims.length) {
      c.appendChild(el("div", "sub", "this resume asserts:"));
      var ul = el("ul", "claims");
      a.claims.forEach(function(cl){ ul.appendChild(el("li", null, cl)); });
      c.appendChild(ul);
    }
    if (a.url) {
      var p = el("div", "sub"), link = el("a", null, a.url);
      link.href = a.url; link.rel = "noreferrer noopener"; link.target = "_blank";
      p.appendChild(link); c.appendChild(p);
    }
    var act = el("div", "actions");
    var yes = el("button", "ok", "Approve this one");
    var no = el("button", "no", "Decline");
    var hist = el("button", null, "History");
    var msg = el("span", "err");
    function decide(d){
      [yes, no].forEach(function(b){ b.disabled = true; });
      api("/api/decision", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: a.id, decision: d})
      }).then(function(r){
        if (r.status === 200) { load(); }
        else {
          msg.textContent = r.body.error || ("refused (" + r.status + ")");
          [yes, no].forEach(function(b){ b.disabled = false; });
        }
      });
    }
    yes.onclick = function(){ decide("approve"); };
    no.onclick = function(){ decide("decline"); };
    hist.onclick = function(){
      hist.disabled = true;
      api("/api/application?id=" + encodeURIComponent(a.id)).then(function(r){
        var pre = el("pre");
        if (r.status !== 200) { pre.textContent = r.body.error || "could not load"; }
        else {
          pre.textContent = (r.body.history || []).map(function(e){
            return new Date(e.at * 1000).toLocaleString() + "  " +
                   (e.from_state || "·") + " -> " + e.to_state +
                   (e.note ? "  (" + e.note + ")" : "");
          }).join("\\n") + (r.body.outcome ? "\\noutcome: " + r.body.outcome.outcome : "");
        }
        c.appendChild(pre);
      });
    };
    act.appendChild(yes); act.appendChild(no); act.appendChild(hist); act.appendChild(msg);
    c.appendChild(act);
    return c;
  }

  function stats(s){
    var tb = document.querySelector("#stats tbody"); tb.textContent = "";
    var keys = Object.keys(s || {}).sort();
    if (!keys.length) { tb.appendChild(el("tr")).appendChild(el("td", "muted", "nothing recorded yet")); return; }
    keys.forEach(function(k){
      var tr = el("tr");
      tr.appendChild(el("td", null, k.replace(/_/g, " ")));
      tr.appendChild(el("td", null, s[k]));
      tb.appendChild(tr);
    });
  }

  function load(){
    say("");
    api("/api/queue").then(function(r){
      if (r.status !== 200) { say(r.body.error || ("error " + r.status)); return; }
      var d = r.body, host = document.getElementById("queue");
      host.textContent = "";
      document.getElementById("qcount").textContent = d.count + " waiting";
      if (!d.queue.length) { host.appendChild(el("p", "muted", d.empty_because || "nothing waiting")); }
      d.queue.forEach(function(a){ host.appendChild(row(a)); });
      document.getElementById("foot").textContent =
        "One decision per application, on purpose. There is no approve-all here and "
        + "there is no route that could add one."
        + (d.truncated ? "  Showing " + d.shown + " of " + d.count + "." : "");
      stats(d.stats);
      var b = document.getElementById("banner"), p = document.getElementById("pause");
      b.hidden = !d.pause.paused;
      b.textContent = d.pause.paused ? ("Paused — " + d.pause.reason) : "";
      p.textContent = d.pause.paused ? "Resume" : "Pause";
      p.onclick = function(){
        p.disabled = true;
        api("/api/pause", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({paused: !d.pause.paused})
        }).then(function(){ p.disabled = false; load(); });
      };
    });
    api("/api/goals").then(function(r){
      var g = document.getElementById("goals"); g.textContent = "";
      if (r.status !== 200) { g.textContent = r.body.error || "unavailable"; return; }
      if (!r.body.configured) { g.textContent = r.body.reason; return; }
      var list = r.body.goals && r.body.goals.goals;
      if (Array.isArray(list) && list.length) {
        var ul = el("ul", "claims");
        list.forEach(function(x){ ul.appendChild(el("li", null, x.goal || JSON.stringify(x))); });
        g.appendChild(ul);
      } else {
        g.appendChild(el("pre", null, JSON.stringify(r.body.goals, null, 2)));
      }
    });
    api("/api/report").then(function(r){
      var d = document.getElementById("report"); d.textContent = "";
      if (r.status !== 200) { d.textContent = r.body.error || "unavailable"; return; }
      d.appendChild(el("div", null, r.body.reading));
      var keys = Object.keys(r.body.by_outcome || {});
      if (keys.length) {
        var t = el("table"), tb = el("tbody");
        var hr = el("tr");
        ["outcome", "n", "mean score", "mean days"].forEach(function(h){ hr.appendChild(el("th", null, h)); });
        tb.appendChild(hr);
        keys.sort().forEach(function(k){
          var v = r.body.by_outcome[k], tr = el("tr");
          [k, v.n, num(v.mean_score), num(v.mean_days)].forEach(function(x){ tr.appendChild(el("td", null, x)); });
          tb.appendChild(tr);
        });
        t.appendChild(tb); d.appendChild(t);
      }
      d.appendChild(el("div", "note", r.body.submitted + " submitted · "
        + r.body.awaiting_outcome + " still waiting on an outcome"));
    });
  }

  document.getElementById("refresh").onclick = load;
  load();
})();
</script>
</body></html>
'''


def render_page(nonce: str) -> str:
    """The single page. It carries no application data and no token: everything
    is fetched with the token the browser already has, so the HTML itself is
    never a place a secret can be left behind."""
    return _PAGE.replace("__NONCE__", nonce)


if __name__ == "__main__":
    sys.exit(main())
