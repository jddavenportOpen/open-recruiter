"""A local ATS that fails the way real ones do.

The apply path cannot be developed against real employers. Every attempt is
one-shot, a botched submission is not retractable, and the one employer that
mattered most in the ancestor run capped the applicant for 180 days. So the
target here is a server on 127.0.0.1 that reproduces the failure modes worth
testing against, and the real-employer switch stays off by default.

Each route is a failure mode that actually happens:

  /normal     a form that behaves, and confirms with text of your own
  /sneaky     a required field buried below a wall of legal text
  /iframe     the confirmation renders INSIDE AN IFRAME -- the top-level page
              says only "thanks for stopping by". This is the bug that made the
              ancestor's one real attempt unverifiable, which in turn blocked
              the rail from ever graduating, since graduation wanted a verified
              attempt.
  /redirect   a 302 to a static /thank-you carrying nothing about you. The
              application really is saved; you simply cannot prove it from the
              response, which is exactly why "we landed on /thank-you" is not a
              receipt.
  /drops      accepts a field and silently does not store it
  /lies       reports success and saves nothing at all
  /unmarked   a form that declares no required fields in its markup (they are
              enforced in JS you cannot see). A verifier that reads this as
              "nothing is required" is blind and does not know it.

Nothing here talks to the network: the socket is bound to 127.0.0.1 and the
records live in memory for the life of the process.
"""
from __future__ import annotations

import argparse
import html
import http.cookies
import http.server
import itertools
import secrets
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import default as _EMAIL_POLICY

HOST = "127.0.0.1"
BRAND = "Northwind Careers"
ROLE = "Principal Product Manager"

# Every page carries the same nav and footer. That is not decoration: a verifier
# that diffs before/after has to cope with chrome it has already seen, and a mock
# whose pages share nothing would let a broken diff look like a working one.
NAV = f"<nav><a href='/'>{BRAND}</a> | Openings | Benefits | Life here</nav>"
FOOTER = f"<footer><p>{BRAND} is an equal opportunity employer.</p></footer>"

LEGAL_WALL = (
    "<section><p>Notice regarding your application: information you submit is "
    "processed under our applicant privacy policy. We retain application records "
    "for twenty-four months. We consider qualified applicants regardless of race, "
    "colour, religion, sex, sexual orientation, gender identity, national origin, "
    "disability or veteran status. If you require an accommodation to complete "
    "this application, contact the recruiting team. Submission of this form does "
    "not create an employment relationship, an offer, or a promise of "
    "consideration. Fields marked with an asterisk are required.</p></section>"
)

# name, label, kind, required
BASE_FIELDS = [
    ("full_name", "Full name", "text", True),
    ("email", "Email", "email", True),
    ("phone", "Phone", "tel", False),
    ("resume_text", "Paste your resume", "textarea", True),
]

# The /upload route asks for the resume as a FILE instead of pasted text, which
# is what a real ATS does and what the apply path could not do until it learned
# multipart. Everything else about the form is the same.
UPLOAD_FIELDS = [f for f in BASE_FIELDS if f[0] != "resume_text"] + [
    ("resume", "Resume (PDF)", "file", True),
]

WORK_AUTH = ("work_authorization",
             "Are you legally authorized to work in the United States?",
             "select", True)


def _control(name: str, label: str, kind: str, required: bool) -> str:
    req = " required" if required else ""
    star = " *" if required else ""
    if kind == "textarea":
        ctrl = f"<textarea id='{name}' name='{name}' rows='6'{req}></textarea>"
    elif kind == "select":
        ctrl = (f"<select id='{name}' name='{name}'{req}>"
                "<option value=''>-- select --</option>"
                "<option value='yes'>Yes</option>"
                "<option value='no'>No</option></select>")
    else:
        ctrl = f"<input id='{name}' type='{kind}' name='{name}'{req}>"
    return f"<p><label for='{name}'>{html.escape(label)}{star}</label><br>{ctrl}</p>"


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html><head>"
            f"<title>{BRAND} - {html.escape(title)}</title></head><body>"
            f"{NAV}<h1>{html.escape(title)}</h1>{body}{FOOTER}</body></html>")


def _form(action: str, fields, *, before_fields: str = "", after_fields: str = "",
          error: str = "") -> str:
    # A hidden control marked required: it is already filled by the page, so a
    # human never sees or types it. A preflight that counts it as an unfilled
    # requirement would block every real form; one that forgets it exists at all
    # is the same class of blindness in the other direction, so preflight reports
    # it separately.
    rows = ["<input type='hidden' name='tracking_id' value='t-88' required>",
            "<input type='hidden' name='source' value='careers-page'>"]
    rows += [_control(*f) for f in fields]
    err = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    return (f"{err}<form method='post' action='{action}'>{before_fields}"
            + "".join(rows) + after_fields +
            "<p><button type='submit'>Submit application</button></p></form>")


class _Records:
    """What the employer actually stored. The only ground truth in this file."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self._files: dict[str, tuple] = {}
        self._ids = itertools.count(1001)

    def save(self, fields: dict, *, drop: tuple[str, ...] = ()) -> str:
        with self._lock:
            ref = f"AB-{next(self._ids)}"
            self._rows[ref] = {k: v for k, v in fields.items() if k not in drop}
            return ref

    def token_for(self, ref: str) -> str:
        """An opaque handle for the receipt frame's src.

        Deliberately carries nothing of the reference: if the frame URL spelled it
        out, a verifier could pull the reference off the top-level markup and the
        iframe walk would look like it worked when it had not run at all.
        """
        with self._lock:
            token = f"sess-{len(self._tokens) + 1:08x}"
            self._tokens[token] = ref
            return token

    def ref_for(self, token: str) -> str | None:
        with self._lock:
            return self._tokens.get(token)

    def get(self, ref: str) -> dict | None:
        with self._lock:
            row = self._rows.get(ref)
            return dict(row) if row else None

    def note_file(self, ref: str, filename: str, blob: bytes, content_type: str) -> None:
        with self._lock:
            self._files[ref] = (filename, blob, content_type)

    def file_for(self, ref: str):
        """The bytes the employer actually stored. A test that asserts on this is
        asserting the resume ARRIVED, not that a page said so."""
        with self._lock:
            return self._files.get(ref)

    def count(self) -> int:
        with self._lock:
            return len(self._rows)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MockATS/0.1"

    def log_message(self, *_a):  # the suite is not a web server log
        pass

    # -- plumbing ------------------------------------------------------------
    def _send(self, body: str, status: int = 200, headers=()) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_raw(self) -> bytes:
        """The request body, read exactly once.

        `do_POST` parses as urlencoded before it knows the route, so a multipart
        route that read the socket again got an empty body and then hung waiting
        for bytes already consumed. One read, cached, and both parsers work off
        it.
        """
        if getattr(self, "_raw_body", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            self._raw_body = self.rfile.read(n)
        return self._raw_body

    def _form_body(self) -> dict[str, str]:
        ctype = self.headers.get("Content-Type", "").lower()
        if "multipart/form-data" in ctype:
            return {}                      # not this parser's encoding
        raw = self._read_raw().decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in parsed.items()}

    @staticmethod
    def _missing(form: dict, required) -> list[str]:
        return [f for f in required if not (form.get(f) or "").strip()]

    def _multipart_body(self) -> tuple[dict, dict]:
        """Parse a multipart POST into (text fields, files).

        A real ATS gets this from its framework. Doing it by hand here is the
        point: the suite has to prove the bytes we send are parseable by someone
        who did not write our encoder.
        """
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype.lower():
            return {}, {}
        raw = self._read_raw()
        head = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        msg = BytesParser(policy=_EMAIL_POLICY).parsebytes(head + raw)
        fields, files = {}, {}
        if not msg.is_multipart():
            return {}, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            fname = part.get_param("filename", header="content-disposition")
            if isinstance(name, tuple):          # RFC 2231 form
                name = name[2]
            if isinstance(fname, tuple):
                fname = fname[2]
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            if fname:
                files[name] = (fname, payload, part.get_content_type())
            else:
                fields[name] = payload.decode("utf-8", "replace")
        return fields, files

    def _cookie(self, name: str) -> str:
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        m = jar.get(name)
        return m.value if m else ""

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        self.server.note("GET", u.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/":
            links = "".join(f"<li><a href='/{r}'>/{r}</a></li>" for r in ROUTES)
            return self._send(_page("Openings", f"<ul>{links}</ul>"))

        if path == "/thank-you":
            # Byte-identical for every applicant, forever. This page is the whole
            # argument for why a landing URL is not a receipt.
            return self._send(_page("Thank you",
                                    "<p>Thank you. Your submission has been received.</p>"))

        if path == "/iframe/receipt":
            ref = self.server.records.ref_for((q.get("t") or [""])[0]) or ""
            row = self.server.records.get(ref)
            if not row:
                return self._send(_page("Not found", "<p>No such receipt.</p>"), 404)
            return self._send(_page(
                "Receipt",
                f"<p>Application received - reference {html.escape(ref)} for "
                f"{html.escape(ROLE)}.</p>"
                f"<p>A copy was sent to {html.escape(row.get('email', ''))}.</p>"))

        if path.startswith("/records/"):
            ref = path[len("/records/"):]
            row = self.server.records.get(ref)
            if not row:
                return self._send(_page("Not found", "<p>No such application.</p>"), 404)
            rows = "".join(f"<tr><td>{html.escape(k)}</td>"
                           f"<td>{html.escape(str(v))}</td></tr>" for k, v in row.items())
            return self._send(_page(f"Application {ref}", f"<table>{rows}</table>"))

        if path == "/normal":
            return self._send(_page(f"Apply - {ROLE}", _form("/normal", BASE_FIELDS)))

        if path == "/upload":
            # Two things a urlencoded POST cannot satisfy, which is the whole
            # reason this route exists: a session cookie the form's token is
            # bound to, and a file input that needs actual bytes.
            sid = secrets.token_hex(8)
            token = self.server.new_session(sid)
            hidden = f"<input type='hidden' name='csrf' value='{token}' required>"
            return self._send(
                _page(f"Apply - {ROLE}",
                      _form("/upload", UPLOAD_FIELDS, before_fields=hidden)),
                headers=(("Set-Cookie", f"sid={sid}; Path=/"),))

        if path == "/sneaky":
            # The required control sits AFTER the legal wall, which is how a human
            # (and a filler working from the first screenful) misses it.
            return self._send(_page(f"Apply - {ROLE}", _form(
                "/sneaky", BASE_FIELDS, after_fields=LEGAL_WALL + _control(*WORK_AUTH))))

        if path == "/iframe":
            return self._send(_page(f"Apply - {ROLE}", _form("/iframe", BASE_FIELDS)))

        if path == "/redirect":
            return self._send(_page(f"Apply - {ROLE}", _form("/redirect", BASE_FIELDS)))

        if path == "/drops":
            return self._send(_page(f"Apply - {ROLE}", _form("/drops", BASE_FIELDS)))

        if path == "/lies":
            return self._send(_page(f"Apply - {ROLE}", _form("/lies", BASE_FIELDS)))

        if path == "/unmarked":
            plain = [(n, l, k, False) for n, l, k, _ in BASE_FIELDS]
            return self._send(_page(f"Apply - {ROLE}", _form(
                "/unmarked", plain,
                before_fields="<p>All questions below are mandatory.</p>")))

        return self._send(_page("Not found", "<p>No such page.</p>"), 404)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        form = self._form_body()
        self.server.note("POST", u.path)
        path = u.path
        required = [f[0] for f in BASE_FIELDS if f[3]]

        if path == "/normal":
            missing = self._missing(form, required)
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/normal", BASE_FIELDS,
                    error=f"Please complete: {', '.join(missing)}")), 200)
            self.server.records.save(form)
            # The applicant's own name, typeset the way a CMS does it -- straight
            # apostrophes turned curly. A verifier comparing raw strings will not
            # match its own input back. This confirmation deliberately carries no
            # reference code and no email, so the name is the ONLY thing on the
            # page that belongs to this application.
            pretty = html.escape(form.get("full_name", "")).replace("&#x27;", "’")
            return self._send(_page(
                "Application received",
                f"<p>Thank you. We have logged {pretty}’s application and a "
                f"recruiter will review it.</p>"))

        if path == "/upload":
            fields, files = self._multipart_body()
            sid = self._cookie("sid")
            expected = self.server.session_token(sid)
            if not sid or not expected:
                # What a cookie-less client gets. The page is a 200 with a human
                # sentence on it, exactly like the real ones: nothing about the
                # status line says the request was rejected.
                return self._send(_page("Session expired",
                                        "<p>Your session expired. Please reload the "
                                        "form and try again.</p>"))
            if fields.get("csrf") != expected:
                return self._send(_page("Session expired",
                                        "<p>That form is no longer valid. Please "
                                        "reload and try again.</p>"))
            up = files.get("resume")
            if not up or not up[1]:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/upload", UPLOAD_FIELDS,
                    before_fields=f"<input type='hidden' name='csrf' value='{expected}' required>",
                    error="Please complete: resume")), 200)
            missing = self._missing(fields, [f[0] for f in UPLOAD_FIELDS
                                             if f[3] and f[0] != "resume"])
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/upload", UPLOAD_FIELDS,
                    before_fields=f"<input type='hidden' name='csrf' value='{expected}' required>",
                    error=f"Please complete: {', '.join(missing)}")), 200)
            fname, blob, ftype = up
            stored = dict(fields)
            stored["resume"] = f"{fname} ({len(blob)} bytes, {ftype})"
            ref = self.server.records.save(stored, drop=("csrf",))
            self.server.records.note_file(ref, fname, blob, ftype)
            return self._send(_page(
                "Application received",
                f"<p>Thank you. Reference <strong>{html.escape(ref)}</strong>. We "
                f"received {html.escape(fname)} ({len(blob)} bytes) for "
                f"{html.escape(fields.get('email',''))}.</p>"))

        if path == "/sneaky":
            missing = self._missing(form, required + [WORK_AUTH[0]])
            if missing:
                # 200, not 4xx: the status line says nothing is wrong. Only the
                # body does, and only if you read it.
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/sneaky", BASE_FIELDS,
                    after_fields=LEGAL_WALL + _control(*WORK_AUTH),
                    error=f"Please complete: {', '.join(missing)}")), 200)
            self.server.records.save(form)
            return self._send(_page("Application received",
                                    f"<p>Thank you. A copy was sent to "
                                    f"{html.escape(form.get('email',''))}.</p>"))

        if path == "/iframe":
            missing = self._missing(form, required)
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/iframe", BASE_FIELDS,
                    error=f"Please complete: {', '.join(missing)}")), 200)
            token = self.server.records.token_for(self.server.records.save(form))
            # Top level says nothing specific -- not the reference, not the email,
            # not even a URL you could read one out of. Everything that proves the
            # submission is one frame down.
            return self._send(_page(
                "Thanks for stopping by",
                "<p>Your session is complete. You may close this window.</p>"
                f"<iframe title='receipt' src='/iframe/receipt?t="
                f"{urllib.parse.quote(token)}' width='600' height='200'></iframe>"))

        if path == "/redirect":
            missing = self._missing(form, required)
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/redirect", BASE_FIELDS,
                    error=f"Please complete: {', '.join(missing)}")), 200)
            self.server.records.save(form)   # it really did land
            return self._send("", 302, headers=[("Location", "/thank-you")])

        if path == "/drops":
            missing = self._missing(form, required)
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/drops", BASE_FIELDS,
                    error=f"Please complete: {', '.join(missing)}")), 200)
            ref = self.server.records.save(form, drop=("phone",))
            return self._send(_page("Application received",
                                    f"<p>Application received - reference "
                                    f"{html.escape(ref)}.</p>"
                                    f"<p>A copy was sent to "
                                    f"{html.escape(form.get('email',''))}.</p>"))

        if path == "/lies":
            # Success, warmly worded, storing nothing. There is no record to look
            # up afterwards and nothing of yours on the page.
            return self._send(_page("Application submitted",
                                    "<p>Application submitted successfully!</p>"
                                    "<p>We will be in touch.</p>"))

        if path == "/unmarked":
            missing = self._missing(form, required)
            if missing:
                return self._send(_page(f"Apply - {ROLE}", _form(
                    "/unmarked", [(n, l, k, False) for n, l, k, _ in BASE_FIELDS],
                    error=f"Please complete: {', '.join(missing)}")), 200)
            self.server.records.save(form)
            return self._send(_page("Application received",
                                    f"<p>Thank you. A copy was sent to "
                                    f"{html.escape(form.get('email',''))}.</p>"))

        return self._send(_page("Not found", "<p>No such form.</p>"), 404)


ROUTES = ("normal", "sneaky", "iframe", "redirect", "drops", "lies", "unmarked",
          "upload")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, _Handler)
        self.records = _Records()
        self.hits: list[tuple[str, str]] = []
        self._hit_lock = threading.Lock()
        self._sessions: dict[str, str] = {}

    def new_session(self, sid: str) -> str:
        token = secrets.token_hex(8)
        with self._hit_lock:
            self._sessions[sid] = token
        return token

    def session_token(self, sid: str) -> str:
        with self._hit_lock:
            return self._sessions.get(sid, "")

    def note(self, method: str, path: str) -> None:
        with self._hit_lock:
            self.hits.append((method, path))


class MockATS:
    """Start with `with MockATS() as ats:` and point the apply path at ats.url().

    Binds 127.0.0.1 on an ephemeral port, so two suites running at once do not
    collide and nothing outside this machine can reach it.
    """

    def __init__(self, port: int = 0):
        self._srv = _Server((HOST, port))
        self._thread: threading.Thread | None = None

    # -- lifecycle
    def start(self) -> "MockATS":
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockATS":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- addressing
    @property
    def port(self) -> int:
        return self._srv.server_address[1]

    @property
    def base(self) -> str:
        return f"http://{HOST}:{self.port}"

    def url(self, route: str = "") -> str:
        return f"{self.base}/{route.lstrip('/')}"

    def record_url_template(self) -> str:
        return f"{self.base}/records/{{ref}}"

    # -- what happened
    @property
    def hits(self) -> list[tuple[str, str]]:
        return list(self._srv.hits)

    def posts(self, path: str | None = None) -> int:
        return sum(1 for m, p in self.hits
                   if m == "POST" and (path is None or p == path))

    def file(self, ref: str):
        """(filename, bytes, content_type) the employer stored, or None.

        A test asserting on THIS is asserting the resume arrived. A test
        asserting on the confirmation page is only asserting that a page said so.
        """
        return self._srv.records.file_for(ref)

    def record(self, ref: str) -> dict | None:
        return self._srv.records.get(ref)

    def stored(self) -> int:
        return self._srv.records.count()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args(argv)
    ats = MockATS(a.port).start()
    print(f"mock ATS on {ats.base}  (routes: {', '.join('/' + r for r in ROUTES)})")
    try:
        while True:
            ats._thread.join(1)
    except KeyboardInterrupt:
        ats.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
