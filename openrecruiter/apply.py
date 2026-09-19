"""The apply path, and the verification that refuses to believe itself.

"We clicked submit" is not "we sent it", and the gap between those two sentences
is where the ancestor system lived. Its verifier read the top-level page only; an
employer embedded its form in an iframe; the one real attempt came back
*unverified*, and because graduation to a wider rail required a verified attempt,
that single blind spot froze the whole thing. Nothing was broken loudly. It just
could not see.

So this module is built around four refusals:

  * **A URL is not a receipt.** Landing on /thank-you proves that a web server
    served you a page. It does not prove a row exists anywhere. Verification
    reads TEXT, and the text has to contain something that belongs to *this*
    application.
  * **"Could not look" is not "nothing there."** `confirm_signals` returns None
    when it could not read, and a set when it read. An empty set is a fact; None
    is the absence of one. Collapsing them is how a gate waves things through.
  * **A form with no required markers is BLIND, not permissive.** Plenty of real
    forms validate in JavaScript you cannot see. Reading that as "nothing is
    required" is the confident-wrong answer.
  * **Unverifiable is held, never retried.** SUBMITTED_UNVERIFIED means it may
    have landed. Re-sending on a guess is how an applicant gets capped for 180
    days at the one employer that mattered.

And two hard stops that no flag reaches:

  * Reach / tier-0 employers are never submitted from here. A standard employer
    is a repeatable event; a reach employer is close to one-shot.
  * Every outbound request passes `guard_url`, which refuses any host that is not
    loopback unless a human has BOTH set OPENRECRUITER_ALLOW_REAL_SUBMIT=1 and
    passed allow_external=True at the call site. That includes every hop of a
    redirect chain: urllib follows 3xx by itself, so a guard that only sees the
    URL you typed is not a choke point at all -- a loopback page answering
    `302 Location: http://169.254.169.254/latest/meta-data/` walks straight out
    of the sandbox and hands back the result wearing a URL nobody chose. Out of
    the box this module cannot reach a real employer at all -- develop against
    `mock_ats`.

There is no verb here that decides, fills, or sends more than one application.
`submit` takes one id, requires that id to already be APPROVED by a human in the
store, and returns one result.
"""
from __future__ import annotations

import dataclasses
import enum
import http.cookiejar
import ipaddress
import mimetypes
import os
import pathlib
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .engine import panel
from .store import Application, State, Store

__all__ = [
    "NEEDS_HUMAN", "Answer", "answer_for",
    "ApplyPacket", "PageState", "Fetched", "Preflight", "Verdict",
    "SubmitResult", "Outcome", "RecordCheck",
    "ExternalHostRefused", "guard_url", "real_employer_submit_enabled",
    "http_fetch", "read_back", "html_to_text", "normalize",
    "Upload", "Session", "encode_multipart",
    "confirm_signals", "confirmation_evidence", "extract_reference",
    "verify_record", "preflight", "submit", "is_done", "is_reach_target",
    "carried_fields", "file_controls", "bind_uploads",
]

USER_AGENT = "openrecruiter/0.1 (+https://github.com/jddavenportOpen/open-recruiter)"
MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# the sentinel                                                                 #
# --------------------------------------------------------------------------- #
class _NeedsHuman:
    """Not a string. A string would get typed into a form by accident."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEEDS_HUMAN"

    def __bool__(self) -> bool:
        return False


NEEDS_HUMAN = _NeedsHuman()


# --------------------------------------------------------------------------- #
# text                                                                         #
# --------------------------------------------------------------------------- #
# Typographic substitutions a CMS makes on the way to the page. Comparing raw
# strings means never matching your own input back: you typed O'Neil, the
# confirmation says O’Neil, and a verifier that cannot see through that records a
# real submission as unproven.
_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", "​": "", "…": "...",
})


def normalize(text: str) -> str:
    """Casefold, fold typography to ASCII, collapse whitespace."""
    return " ".join(str(text).translate(_TYPOGRAPHY).casefold().split())


_SKIP_TAGS = {"script", "style", "title", "head", "meta", "link", "noscript"}
_BLOCK_TAGS = {
    "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
    "tr", "td", "th", "table", "section", "article", "header", "footer", "nav",
    "form", "label", "option", "iframe", "blockquote", "pre", "main", "aside",
    "fieldset", "legend", "button", "textarea", "select", "span",
}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Block-aware text extraction. One line per block element, so a line diff
    between two pages is meaningful rather than one giant string."""
    p = None
    try:
        # Constructed inside the try on purpose: a parser that dies in __init__
        # used to take the caller down with it, which is the one failure mode a
        # text extractor must not have.
        p = _TextParser()
        p.feed(markup)
        p.close()
    except Exception:
        # A malformed page is still worth whatever text came out before the
        # parser gave up; returning nothing would read as "the page was empty".
        # Partial text only ever REMOVES evidence, so it cannot manufacture a
        # pass -- and the fault itself is not swallowed: `read_back` detects it
        # with `_fully_parsed` and records the document in `unread_frames`.
        pass
    out = []
    for chunk in "".join(getattr(p, "parts", [])).split("\n"):
        line = " ".join(chunk.split())
        if line:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the network guard                                                            #
# --------------------------------------------------------------------------- #
class ExternalHostRefused(RuntimeError):
    """Raised rather than returned: a refusal here must not be mistakable for a
    failed fetch."""


_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "localhost.localdomain"}


def _host_is_loopback(host: str | None) -> bool:
    if not host:
        return False
    h = host.strip().strip("[]").casefold()
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    if h not in _LOOPBACK_NAMES:
        # Refuse without resolving. A DNS lookup for a real employer's hostname is
        # already a request that leaves this machine.
        return False
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return False        # cannot tell -> not loopback
    return bool(infos) and all(
        ipaddress.ip_address(i[4][0].split("%")[0]).is_loopback for i in infos)


def real_employer_submit_enabled() -> bool:
    """The off-by-default switch a human sets AFTER reviewing this path."""
    return os.environ.get("OPENRECRUITER_ALLOW_REAL_SUBMIT", "") == "1"


def guard_url(url: str, *, allow_external: bool = False) -> urllib.parse.SplitResult:
    """The single choke point. Every request in this module goes through it.

    Two independent gates, both required: a call site that opted in, and an
    environment variable a human set on purpose. One alone is not enough --
    a caller passing allow_external=True by mistake should still hit a wall, and
    setting the variable should not silently arm code nobody reviewed.

    "Single choke point" is a claim about every REQUEST, not every call site, so
    it has to hold for the hops this module never typed: see
    `_GuardedRedirectHandler`, which re-enters this function on each 3xx.
    """
    u = urllib.parse.urlsplit(url or "")
    if u.scheme not in ("http", "https"):
        raise ExternalHostRefused(f"refusing {u.scheme or 'empty'}:// -- http(s) only")
    if _host_is_loopback(u.hostname):
        return u
    if not allow_external:
        raise ExternalHostRefused(
            f"{u.hostname!r} is not loopback and this call did not ask for an "
            f"external host. Develop against openrecruiter.mock_ats.")
    if not real_employer_submit_enabled():
        raise ExternalHostRefused(
            f"{u.hostname!r} is a real host. Set OPENRECRUITER_ALLOW_REAL_SUBMIT=1 "
            f"only after reading openrecruiter/apply.py end to end.")
    return u


# --------------------------------------------------------------------------- #
# fetching                                                                     #
# --------------------------------------------------------------------------- #
# The attribute http_fetch stamps on its Request so the redirect handler knows
# which of the two gates the CALLER opened. Absent means closed, so a Request
# built by anything else is loopback-only no matter how it got here.
_ALLOW_ATTR = "openrecruiter_allow_external"


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-runs `guard_url` on every hop of a redirect chain.

    urllib follows 3xx on its own and asks nobody. Without this, `guard_url` is
    a check on the URL you typed rather than on the request that leaves the
    machine: a loopback page replying `302 Location: http://169.254.169.254/` is
    enough to reach cloud metadata with allow_external=False and the real-submit
    switch unset, i.e. from the DEFAULT, supposedly-sandboxed configuration --
    and `Fetched.url` then reports the external URL as though we had chosen it.

    The refusal is raised, not returned, for the reason `ExternalHostRefused`
    exists: mid-chain it has to abort the fetch, and a return value would be
    mistaken for the redirect having been followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allow = getattr(req, _ALLOW_ATTR, False)
        guard_url(newurl, allow_external=allow)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            # Carry the caller's opt-in onto the next Request, or hop three is
            # judged against a default that this chain never established.
            setattr(new, _ALLOW_ATTR, allow)
        return new


# Module-level and built from OUR handler, so the guard cannot be skipped by
# reaching for urlopen()'s global default opener, and nothing outside this
# module is affected (no install_opener).
_OPENER = urllib.request.build_opener(_GuardedRedirectHandler)


# --------------------------------------------------------------------------- #
# uploads, sessions                                                            #
# --------------------------------------------------------------------------- #
MAX_UPLOAD_BYTES = 10_000_000


@dataclasses.dataclass(frozen=True)
class Upload:
    """A real file, held as bytes, destined for a file input on the form.

    Bytes and not a path, deliberately. A path is a string, and a string behind
    a file input is the failure this type exists to make impossible: it reaches
    the employer as the literal characters "/Users/me/resume.pdf" in a text
    field, the form is accepted, the confirmation comes back, and the
    application that just went out has no resume attached to it. `preflight`
    refuses that case by name.
    """
    field: str
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"

    @classmethod
    def from_path(cls, field: str, path, *, content_type: str | None = None) -> "Upload":
        p = pathlib.Path(path).expanduser()
        raw = p.read_bytes()
        if not raw:
            raise ValueError(f"{p} is empty; an empty resume is not an attachment")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError(f"{p} is {len(raw)} bytes, over the {MAX_UPLOAD_BYTES} cap")
        guessed = content_type or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return cls(field=field, filename=p.name, content=raw, content_type=guessed)

    def __repr__(self) -> str:   # never dump the bytes into a log or a traceback
        return (f"Upload(field={self.field!r}, filename={self.filename!r}, "
                f"content_type={self.content_type!r}, bytes={len(self.content)})")


def _quote_part(value: str) -> str:
    """RFC 2388 names the escaping problem and ducks it; browsers percent-encode.

    A filename containing a quote or a newline would otherwise close the header
    early and let the rest of the name be read as headers of its own.
    """
    return value.replace("\\", "%5C").replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def encode_multipart(fields: dict, uploads) -> tuple[str, bytes]:
    """Return (content_type, body) for a multipart/form-data POST.

    The boundary is generated and then CHECKED against every byte it has to
    separate. A boundary that occurs inside a part does not raise anything: it
    silently truncates the body at the employer's parser, so the resume arrives
    half-length or the fields after it vanish. Colliding by chance is
    vanishingly unlikely; colliding because a file contains attacker-chosen
    bytes is not, and the check costs one scan.
    """
    parts: list[bytes] = []

    def _probe() -> bytes:
        blob = b"".join(str(k).encode() + str(v).encode() for k, v in fields.items())
        for u in uploads:
            blob += u.field.encode() + u.filename.encode() + u.content
        return blob

    probe = _probe()
    for _ in range(8):
        boundary = "----openrecruiter-" + secrets.token_hex(16)
        if boundary.encode() not in probe:
            break
    else:                                        # pragma: no cover - 8 x 2^128
        raise RuntimeError("could not find a multipart boundary absent from the body")

    dash = f"--{boundary}\r\n".encode()
    for k, v in fields.items():
        parts.append(dash)
        parts.append(f'Content-Disposition: form-data; name="{_quote_part(str(k))}"\r\n\r\n'.encode())
        parts.append(str(v).encode("utf-8"))
        parts.append(b"\r\n")
    for u in uploads:
        parts.append(dash)
        parts.append(
            f'Content-Disposition: form-data; name="{_quote_part(u.field)}"; '
            f'filename="{_quote_part(u.filename)}"\r\n'
            f"Content-Type: {u.content_type}\r\n\r\n".encode())
        parts.append(u.content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class Session:
    """One cookie jar, scoped to ONE submission.

    Deliberately not module-level. A shared jar sends the session cookie an
    employer set during the GET of its own form along to the next employer's
    host on the next application, which is both a privacy leak and the kind of
    cross-contamination nobody would find by reading a traceback. A jar that
    lives and dies with a single `submit` call cannot do it.

    The guarded redirect handler is first in the chain here for the same reason
    it is in `_OPENER`: the cookie processor must never be reachable by a
    request that skipped `guard_url`.
    """

    __slots__ = ("jar", "opener")

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _GuardedRedirectHandler, urllib.request.HTTPCookieProcessor(self.jar))

    def cookie_names(self) -> tuple[str, ...]:
        return tuple(sorted(c.name for c in self.jar))

    def __len__(self) -> int:
        return len(self.jar)


@dataclasses.dataclass(frozen=True)
class Fetched:
    url: str                      # the FINAL url, after redirects
    status: int | None = None
    body: str | None = None       # None means we did not read a page
    error: str | None = None
    kind: str = ""                # "" | "connect" | "read" | "guard"

    @property
    def ok(self) -> bool:
        return self.body is not None and self.error is None


def http_fetch(url: str, data: dict | None = None, *, allow_external: bool = False,
               timeout: float = DEFAULT_TIMEOUT, files=None, session=None) -> Fetched:
    """GET, or POST when `data` is given. Never raises for a network fault.

    `kind` separates "the connection never opened" from "something went wrong
    after we started talking", because only the first one proves nothing was
    sent. Guessing wrong in the optimistic direction is how an application gets
    submitted twice.

    `files` switches the body to multipart/form-data, which is the only encoding
    a file input accepts. `session` supplies a cookie jar, which is how the
    hidden token an employer stamps into its own form during the GET is still
    valid when the POST arrives: without one, the two requests are strangers and
    the form comes back rejected for a reason the page will not explain. Both
    default to the old behaviour, so every existing caller is unchanged.
    """
    try:
        guard_url(url, allow_external=allow_external)
    except ExternalHostRefused as e:
        return Fetched(url=url, error=str(e), kind="guard")

    uploads = list(files or [])
    headers = {"User-Agent": USER_AGENT}
    if uploads:
        content_type, body = encode_multipart(data or {}, uploads)
        headers["Content-Type"] = content_type
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers)
    setattr(req, _ALLOW_ATTR, allow_external)
    opener = session.opener if session is not None else _OPENER
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
            return Fetched(url=resp.geturl(), status=resp.status,
                           body=raw.decode(charset, "replace"))
    except ExternalHostRefused as e:
        # A redirect tried to walk us off the sandbox. Reported as kind="guard",
        # identical to the refusal at the top of this function, so no caller can
        # tell the two apart and decide this one was a hiccup worth retrying.
        return Fetched(url=url, error=str(e), kind="guard")
    except urllib.error.HTTPError as e:
        final = getattr(e, "url", None) or url
        try:
            guard_url(final, allow_external=allow_external)
        except ExternalHostRefused as refused:
            # urllib refuses a 3xx to a scheme it will not follow by raising
            # HTTPError instead of redirecting, and `e.url` is then that
            # disallowed target (file://, say). Adopting it as the final url
            # would hand read_back's frame walk a base outside http(s) entirely.
            return Fetched(url=url, error=str(refused), kind="guard")
        # A 4xx often IS the page: a re-rendered form with an error banner. Read it.
        try:
            raw = e.read(MAX_BYTES + 1)
        except Exception:
            raw = b""
        return Fetched(url=final, status=e.code, body=raw.decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        reason = e.reason
        connect_fault = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
        return Fetched(url=url, error=f"{reason}",
                       kind="connect" if connect_fault else "read")
    except Exception as e:                       # noqa: BLE001 -- fail closed, loudly
        return Fetched(url=url, error=f"{type(e).__name__}: {e}", kind="read")


# --------------------------------------------------------------------------- #
# pages and frames                                                             #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Frame:
    url: str
    markup: str
    depth: int


@dataclasses.dataclass(frozen=True)
class PageState:
    url: str
    markup: str | None = None
    frames: tuple[Frame, ...] = ()
    error: str | None = None
    status: int | None = None
    # Frames we saw and could NOT read, plus any document whose frame list we
    # could not finish enumerating (recorded as "<url>#unparsed"). Non-empty
    # means the form we can see may not be the whole form -- `preflight` reads
    # this and refuses.
    unread_frames: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        return self.markup is not None and self.error is None

    def all_markup(self) -> list[str]:
        if not self.readable:
            return []
        return [self.markup] + [f.markup for f in self.frames]

    def text(self) -> str | None:
        """None when the page could not be read. Never "" -- an empty string is a
        page that was read and had nothing on it, which is a different fact."""
        if not self.readable:
            return None
        return "\n".join(html_to_text(m) for m in self.all_markup())


class _FrameParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs: list[str] = []
        self.docs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("iframe", "frame"):
            return
        a = dict(attrs)
        if a.get("srcdoc"):
            self.docs.append(a["srcdoc"])
        elif a.get("src"):
            self.srcs.append(a["src"])

    handle_startendtag = handle_starttag


def _frame_refs(markup: str) -> tuple[list[str], list[str]]:
    p = None
    try:
        p = _FrameParser()
        p.feed(markup)
        p.close()
    except Exception:
        # Whatever it found is real, but there may be more it never reached. The
        # shortfall is NOT inferred from this return value -- a short frame list
        # is indistinguishable from a complete one -- it is detected separately
        # by `_fully_parsed` and recorded by `read_back`.
        pass
    return getattr(p, "srcs", []), getattr(p, "docs", [])


def _fully_parsed(markup: str) -> bool:
    """Did the HTML parser reach the end of this document?

    `html_to_text` and `_frame_refs` both swallow a parser fault and hand back
    what they had. For text that is right: half a page is still evidence. For a
    frame list it is the "we looked everywhere" claim this module exists to
    refuse, because the caller cannot see the difference. So the fault is caught
    here, once, and `read_back` records it instead of it being invisible.
    """
    for make in (_TextParser, _FrameParser):
        try:
            p = make()
            p.feed(markup)
            p.close()
        except Exception:
            return False
    return True


def read_back(url: str, *, markup: str | None = None, fetch=None,
              allow_external: bool = False, max_depth: int = 2,
              max_frames: int = 8, timeout: float = DEFAULT_TIMEOUT) -> PageState:
    """Read a page AND the frames inside it.

    The iframe walk is the point. An employer embedding its form (and therefore
    its confirmation) in an iframe is ordinary -- Greenhouse, Lever and Ashby all
    do it -- and a verifier that reads only the top document sees a page that says
    "thanks for stopping by" and concludes nothing happened.

    A frame we can see but cannot read is recorded in `unread_frames` rather than
    dropped, so "we looked everywhere" is never claimed on a partial read.
    """
    do_fetch = fetch or (lambda u, data=None: http_fetch(
        u, data, allow_external=allow_external, timeout=timeout))

    if markup is None:
        got = do_fetch(url)
        if not got.ok:
            return PageState(url=got.url or url, error=got.error or "unreadable",
                             status=got.status)
        url, markup, status = got.url, got.body, got.status
    else:
        status = None

    frames: list[Frame] = []
    unread: list[str] = []
    # (markup, base_url, depth) of documents whose frames we have not walked yet
    pending = [(markup, url, 0)]
    while pending and len(frames) < max_frames:
        doc, base, depth = pending.pop(0)
        if not _fully_parsed(doc):
            # A document the parser could not finish is a document whose frame
            # list we do not have. Recorded BEFORE the depth check, because a
            # half-read page is a half-read page whether or not we were going to
            # descend into it.
            unread.append(f"{base}#unparsed")
        if depth >= max_depth:
            continue
        srcs, docs = _frame_refs(doc)
        for inline in docs:
            if len(frames) >= max_frames:
                break
            frames.append(Frame(url=f"{base}#srcdoc", markup=inline, depth=depth + 1))
            pending.append((inline, base, depth + 1))
        for src in srcs:
            if len(frames) >= max_frames:
                break
            child = urllib.parse.urljoin(base, src)
            got = do_fetch(child)
            if not got.ok:
                unread.append(child)
                continue
            frames.append(Frame(url=got.url, markup=got.body, depth=depth + 1))
            pending.append((got.body, got.url, depth + 1))

    return PageState(url=url, markup=markup, frames=tuple(frames), status=status,
                     unread_frames=tuple(unread))


# --------------------------------------------------------------------------- #
# preflight                                                                    #
# --------------------------------------------------------------------------- #
class Verdict(enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"     # we can see what is required and we do not have it
    BLIND = "blind"         # we cannot see what is required -- also a refusal


@dataclasses.dataclass(frozen=True)
class Control:
    name: str
    tag: str
    kind: str
    required: bool
    visible: bool
    prefilled: bool
    value: str = ""        # what the employer's own page put in the box


@dataclasses.dataclass(frozen=True)
class Preflight:
    verdict: Verdict
    reason: str = ""
    required: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    hidden_required: tuple[str, ...] = ()
    proof: dict = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK


class _ControlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls: list[Control] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("input", "select", "textarea"):
            return
        a = {k.casefold(): (v if v is not None else "") for k, v in attrs}
        name = (a.get("name") or a.get("id") or "").strip()
        if not name:
            return
        kind = (a.get("type") or tag).casefold()
        if kind in ("submit", "button", "reset", "image"):
            return
        required = ("required" in a
                    or a.get("aria-required", "").casefold() == "true"
                    or a.get("data-required", "").casefold() in ("true", "1"))
        style = a.get("style", "").casefold().replace(" ", "")
        visible = not (kind == "hidden" or "hidden" in a
                       or "display:none" in style or "visibility:hidden" in style)
        self.controls.append(Control(name=name, tag=tag, kind=kind, required=required,
                                     visible=visible, prefilled=bool(a.get("value")),
                                     value=a.get("value") or ""))

    handle_startendtag = handle_starttag


_PLACEHOLDERS = {"todo", "tbd", "fixme", "xxx", "n/a?", "needs_human", "needs human",
                 "?", "-", "--", "fill in", "<fill in>"}


def _untypeable(value) -> bool:
    """A value that goes to a human instead of onto the form.

    Two shapes, one rule: a deferral, and a bool. `answer_for` already refuses a
    bool from the bank because there is no honest way to type it -- str(True) is
    "True" -- and a packet field holding one has to be refused for exactly the
    same reason, or the two halves of this module disagree about the same value.
    """
    if value is NEEDS_HUMAN or isinstance(value, bool):
        return True
    return isinstance(value, Answer) and (value.needs_human
                                          or isinstance(value.value, bool))


def _committed(value) -> bool:
    """A value a human stood behind. Not blank, not a placeholder, not a deferral."""
    if value is None or value is NEEDS_HUMAN:
        return False
    if isinstance(value, Answer):
        return not value.needs_human and _committed(value.value)
    if isinstance(value, bool):
        # `answer_for` refuses a bool outright -- "not something to type into a
        # form" -- and it is right: str(True) is "True", a word no applicant
        # chose. This used to return True, so preflight passed a bool as a
        # committed answer and wire() posted the literal "True".
        return False
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    low = s.casefold()
    return not (low in _PLACEHOLDERS or low.startswith("todo") or low.startswith("<"))


def preflight(page_state, packet: "ApplyPacket") -> Preflight:
    """Prove every VISIBLE required field on the form holds a committed value.

    Three distinct answers, and only one of them is a pass:

      OK       every visible required control maps to a value we hold
      BLOCKED  a required control has nothing committed behind it
      BLIND    we could not read the form, could not read part of it, found no
               controls at all, or found a form that declares nothing required.
               That last one is the trap: many real forms enforce their
               requirements in JavaScript, so "no required markers" means we
               cannot see the rules, NOT that there are none. Reading it as
               permission is the confident-wrong answer.

    Anything that throws lands in BLIND. A check that could not run is a refusal.
    """
    try:
        if page_state is None:
            return Preflight(Verdict.BLIND, "no page state to inspect")
        markups = (page_state.all_markup() if isinstance(page_state, PageState)
                   else [str(page_state)])
        if not markups:
            err = getattr(page_state, "error", None) or "the page could not be read"
            return Preflight(Verdict.BLIND, f"cannot inspect the form: {err}")

        # `read_back` goes to the trouble of recording the frames it could not
        # read; until here nothing ever asked. A top-level form with one
        # satisfiable field and an unreachable iframe read as OK, and the
        # application went out missing whatever the frame was asking for.
        unread = tuple(getattr(page_state, "unread_frames", ()) or ())
        if unread:
            return Preflight(
                Verdict.BLIND,
                "part of this page could not be read, so the controls we can see "
                "are not known to be all of them: " + ", ".join(unread))

        merged: dict[str, Control] = {}
        for markup in markups:
            p = _ControlParser()
            p.feed(markup)
            p.close()
            for c in p.controls:
                prev = merged.get(c.name)
                merged[c.name] = c if prev is None else Control(
                    name=c.name, tag=prev.tag, kind=prev.kind,
                    required=prev.required or c.required,
                    visible=prev.visible or c.visible,
                    prefilled=prev.prefilled or c.prefilled,
                    value=prev.value or c.value)

        if not merged:
            return Preflight(Verdict.BLIND,
                             "no form controls found on the page or in its frames; "
                             "this is a parsing failure, not an empty form")

        visible_required = [c for c in merged.values() if c.required and c.visible]
        hidden_required = tuple(sorted(c.name for c in merged.values()
                                       if c.required and not c.visible))
        if not visible_required:
            return Preflight(
                Verdict.BLIND,
                f"{len(merged)} control(s) but not one marked required; the rules are "
                f"enforced somewhere we cannot read. A human has to look at this form.",
                hidden_required=hidden_required)

        fields = packet.fields if packet is not None else {}
        missing, proof = [], {}
        for c in sorted(visible_required, key=lambda c: c.name):
            if c.kind == "file":
                # A file input is the one control a committed STRING does not
                # satisfy. Before uploads existed, a path in `fields` read as a
                # perfectly good answer here, went out urlencoded as the literal
                # characters of the path, and the employer stored an application
                # with no resume on it -- with a confirmation page that said yes.
                # That is the confident-wrong answer this module exists to refuse,
                # so it is named separately from a plain absence.
                up = packet.upload_for(c.name) if packet is not None else None
                if up is not None:
                    proof[c.name] = f"{up.filename} ({len(up.content)} bytes, {up.content_type})"
                elif _committed(fields.get(c.name)):
                    missing.append(f"{c.name} (a path is not a file; attach an Upload)")
                else:
                    missing.append(f"{c.name} (needs a file)")
                continue
            v = fields.get(c.name)
            if _committed(v):
                raw = v.value if isinstance(v, Answer) else v
                s = str(raw)
                proof[c.name] = s if len(s) <= 48 else s[:45] + "..."
            else:
                missing.append(c.name)

        if missing:
            return Preflight(Verdict.BLOCKED,
                             "required field(s) with nothing committed behind them: "
                             + ", ".join(missing),
                             required=tuple(c.name for c in visible_required),
                             missing=tuple(missing), hidden_required=hidden_required)

        return Preflight(Verdict.OK, "every visible required field holds a committed value",
                         required=tuple(c.name for c in visible_required),
                         hidden_required=hidden_required, proof=proof)
    except Exception as e:                       # noqa: BLE001
        return Preflight(Verdict.BLIND, f"the preflight check itself failed: "
                                        f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# confirmation                                                                 #
# --------------------------------------------------------------------------- #
def carried_fields(page_state) -> dict:
    """The hidden, already-filled controls on the employer's own form.

    A browser posts these back without anyone thinking about it, and a modern
    form is built assuming that: the CSRF token stamped into the page during the
    GET is checked against the session cookie on the POST, and a body that omits
    it is rejected by machinery that has no reason to explain itself. The reply
    is a 200 with a polite sentence, which is indistinguishable from a real
    confirmation to anything that is not reading the words.

    Only hidden controls that already hold a value. A visible empty box is the
    applicant's to fill, and this function never answers one.
    """
    out: dict = {}
    try:
        markups = (page_state.all_markup() if isinstance(page_state, PageState)
                   else [str(page_state)] if page_state is not None else [])
        for markup in markups:
            p = _ControlParser()
            p.feed(markup)
            p.close()
            for c in p.controls:
                if not c.visible and c.value and c.name not in out:
                    out[c.name] = c.value
    except Exception:                            # noqa: BLE001 -- carrying nothing
        return {}                                # is always safe; guessing is not
    return out


def file_controls(page_state) -> tuple[str, ...]:
    """Names of the file inputs on the form, required ones first."""
    found: dict[str, bool] = {}
    try:
        markups = (page_state.all_markup() if isinstance(page_state, PageState)
                   else [str(page_state)] if page_state is not None else [])
        for markup in markups:
            p = _ControlParser()
            p.feed(markup)
            p.close()
            for c in p.controls:
                if c.kind == "file" and c.visible:
                    found[c.name] = found.get(c.name, False) or c.required
    except Exception:                            # noqa: BLE001
        return ()
    return tuple(sorted(found, key=lambda n: (not found[n], n)))


def bind_uploads(page_state, packet: "ApplyPacket") -> "ApplyPacket":
    """Point a single attachment at the single file input the form actually has.

    Forms do not agree on a name: `resume`, `resume_file`, `cv`, `attachment`.
    Binding ONE upload to ONE file control is not a guess, it is the only
    mapping that exists. Two of either IS a guess -- which file belongs in which
    slot is a question with a wrong answer -- so the packet is returned
    untouched and `preflight` refuses it by the normal path.
    """
    if packet is None or len(packet.uploads) != 1:
        return packet
    controls = file_controls(page_state)
    if len(controls) != 1 or controls[0] == packet.uploads[0].field:
        return packet
    bound = dataclasses.replace(packet.uploads[0], field=controls[0])
    return dataclasses.replace(packet, uploads=(bound,))


def _readable_text(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, PageState):
        return x.text()
    if isinstance(x, Fetched):
        return html_to_text(x.body) if x.ok else None
    if isinstance(x, str):
        return x
    return None


def confirm_signals(before, after) -> set[str] | None:
    """The lines that are NEW after the action, or None if we could not read.

    Returning None rather than an empty set is the whole contract. "We could not
    look" and "we looked and there was nothing" are opposite facts, and a caller
    that receives `set()` for both will treat a blind read as a clean negative --
    or, worse, a caller checking `if not signals` will treat them identically and
    then some later branch will treat a non-empty page as proof.

    Chrome that was already on the page before is not evidence. Only the delta is.
    """
    b, a = _readable_text(before), _readable_text(after)
    if b is None or a is None:
        return None
    seen = {normalize(l) for l in b.splitlines() if normalize(l)}
    out, kept = set(), set()
    for line in a.splitlines():
        key = normalize(line)
        if key and key not in seen and key not in kept:
            kept.add(key)
            out.add(line.strip())
    return out


# Words that claim something happened. Necessary, nowhere near sufficient: the
# static /thank-you page has one and proves nothing.
_CONFIRM_WORDS = ("received", "submitted", "thank you", "thanks", "confirmation",
                  "confirmed", "application complete", "we have logged",
                  "successfully", "your application")
REF_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,10}\b")


@dataclasses.dataclass(frozen=True)
class Evidence:
    said_received: bool
    markers: tuple[str, ...] = ()      # things of OURS echoed back
    refs: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()

    @property
    def specific(self) -> bool:
        return self.said_received and bool(self.markers)


def confirmation_evidence(signals: set[str] | None, packet: "ApplyPacket") -> Evidence:
    """Grade the new text. A generic 'thank you' is not evidence of anything.

    A marker is something of the applicant's that the page could only be showing
    because it received it: their name, their email. Comparison runs on normalized
    text, so a confirmation that typeset O'Neil as O’Neil still matches.
    """
    if not signals:
        return Evidence(False)
    lines = tuple(sorted(signals))
    blob = normalize("\n".join(lines))
    said = any(w in blob for w in _CONFIRM_WORDS)
    markers = []
    for m in packet.markers():
        n = normalize(m)
        if len(n) >= 4 and n in blob:
            markers.append(m)
    refs = sorted({r for line in lines for r in REF_RE.findall(line)})
    return Evidence(said_received=said, markers=tuple(markers), refs=tuple(refs),
                    lines=lines)


def extract_reference(source) -> str | None:
    """A reference code out of the confirmation, or None. Case is preserved on
    purpose: it is a key we are about to look up, not a phrase to compare."""
    if source is None:
        return None
    if isinstance(source, (set, frozenset, list, tuple)):
        text = "\n".join(str(s) for s in sorted(source))
    else:
        text = _readable_text(source) or ""
    hits = REF_RE.findall(text)
    return hits[0] if hits else None


@dataclasses.dataclass(frozen=True)
class RecordCheck:
    ok: bool
    reason: str = ""
    missing: tuple[str, ...] = ()
    url: str = ""


# Matching a value against a record page used to be `probe in blob`, which is a
# substring test against the whole page. Proven on /drops, the route that exists
# to catch a silently-dropped field: phone "415-555-0142" was correctly reported
# missing, but phone "no" came back VERIFIED because "no" is inside "Northwind"
# in the footer, and phone "1" came back VERIFIED because "1" is inside the
# reference code AB-1001. A dropped short value -- a yes/no, an initial, a work
# authorization -- was recorded as verified AND terminal.
_TOKEN_CHARS = "0-9a-z_-"
_PROBE_MAX = 60        # long free text is matched on its head, not in full
_SCOPE_WINDOW = 120    # normalized chars a value may sit after its field's name


def _token_hits(probe: str, blob: str, *, whole: bool = True) -> list[int]:
    """Offsets where `probe` sits on token boundaries in `blob`.

    `whole=False` drops the right-hand boundary only: the probe is the HEAD of a
    value we truncated ourselves, so the character after it belongs to text we
    never asked the page to show.
    """
    if not probe:
        return []
    pat = rf"(?<![{_TOKEN_CHARS}]){re.escape(probe)}"
    if whole:
        pat += rf"(?![{_TOKEN_CHARS}])"
    return [m.start() for m in re.finditer(pat, blob)]


def _field_labels(name: str) -> tuple[str, ...]:
    """The shapes a record view might print a field's name in: the wire name as
    it is, and with its separators as spaces ("work_authorization" -> "work
    authorization")."""
    n = normalize(name)
    spaced = " ".join(" ".join(re.split(r"[^0-9a-z]+", n)).split())
    return tuple(dict.fromkeys(x for x in (n, spaced) if x))


def _record_carries(name: str, probe: str, blob: str, *, whole: bool) -> tuple[bool, str]:
    """Is this field's value really in the record, or did it just collide?"""
    hits = _token_hits(probe, blob, whole=whole)
    if not hits:
        return False, "not on the page"
    if len(probe) >= 16 or len(probe.split()) >= 3:
        # Distinctive enough that coincidence is not the likely explanation.
        return True, ""
    # A short value has to sit next to its own field's name. Otherwise a single
    # "yes" anywhere on the page verifies a work-authorization answer the
    # employer never stored -- which is the same false pass in a new costume.
    windows = [(at, at + len(lab) + _SCOPE_WINDOW)
               for lab in _field_labels(name) for at in _token_hits(lab, blob)]
    if not windows:
        return False, ("too short to identify on its own, and its field name is "
                       "nowhere on the page to scope it to")
    if any(start <= h <= end for h in hits for start, end in windows):
        return True, ""
    return False, "on the page, but not next to its own field name"


def verify_record(record_url: str, packet: "ApplyPacket", *, fetch=None,
                  allow_external: bool = False,
                  timeout: float = DEFAULT_TIMEOUT) -> RecordCheck:
    """Read the stored application back and check our values are in it.

    This is the only check that can catch a form which accepts a field and
    silently does not keep it -- the confirmation page looks perfect, because the
    confirmation page is rendered from the request, not from the row.
    """
    page = read_back(record_url, fetch=fetch, allow_external=allow_external,
                     timeout=timeout)
    text = page.text()
    if text is None:
        return RecordCheck(False, f"could not read the record at {record_url}: "
                                  f"{page.error or 'unreadable'}", url=record_url)
    blob = normalize(text)
    missing, why, checked = [], {}, 0
    for name, value in sorted(packet.fields.items()):
        if name in packet.unchecked_fields:
            continue
        if not _committed(value):
            continue
        v = normalize(value.value if isinstance(value, Answer) else value)
        if not v:
            continue
        # Long free text is often truncated in a record view; match the head of it
        # rather than declaring a mismatch on a display decision. The head is cut
        # back to a word boundary so we are not demanding the page reproduce a
        # word we sliced in half.
        whole = len(v) <= _PROBE_MAX
        probe = v if whole else (v[:_PROBE_MAX].rsplit(" ", 1)[0] or v[:_PROBE_MAX])
        checked += 1
        ok, reason = _record_carries(name, probe, blob, whole=whole)
        if not ok:
            missing.append(name)
            why[name] = reason

    if not checked:
        # Nothing of ours was checkable, so reading the record proved nothing.
        # This used to return ok=True on any page that loaded -- a pass that
        # means "we did not look", which is the one answer this module may never
        # give.
        return RecordCheck(
            False,
            "nothing in this packet could be checked against the stored record "
            "(every field is blank, deferred, or the employer's own machinery), "
            "so reading it proves nothing about what was stored",
            url=record_url)
    if missing:
        return RecordCheck(False, "the stored record is missing value(s) we submitted: "
                                  + ", ".join(f"{n} ({why[n]})" for n in missing),
                           missing=tuple(missing), url=record_url)
    return RecordCheck(True, f"the stored record carries all {checked} value(s) we "
                             f"submitted and could check", url=record_url)


# --------------------------------------------------------------------------- #
# answers                                                                      #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Answer:
    value: object
    source: str = ""
    matched: str = ""
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        return self.value is NEEDS_HUMAN


_ESSAY_MARKS = ("why do you", "why are you", "why this", "describe", "tell us",
                "in your own words", "cover letter", "what interests", "explain",
                "how would you", "what makes you", "walk us through")
_ESSAY_WORDS = 25


def _norm_question(q: str) -> str:
    s = normalize(q)
    s = re.sub(r"\((?:required|optional)\)", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " ".join(s.split())


def _bank_entries(bank) -> list[dict]:
    """Accept the shapes a bank actually arrives in, and skip anything malformed
    rather than guessing what it meant."""
    if not isinstance(bank, dict):
        return []
    raw = bank.get("answers", bank)
    items: list[dict] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            items.append(dict(v, id=v.get("id", k)) if isinstance(v, dict)
                         else {"id": k, "question": k, "value": v})
    elif isinstance(raw, list):
        items = [dict(v) for v in raw if isinstance(v, dict)]

    out = []
    for it in items:
        q = it.get("question") or it.get("id") or ""
        aliases = it.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        out.append({
            "id": str(it.get("id") or q),
            "qn": _norm_question(str(q)),
            "aliases": [_norm_question(str(a)) for a in aliases if str(a).strip()],
            "value": it.get("value"),
            "source": str(it.get("source") or ""),
        })
    return [e for e in out if e["qn"] or e["aliases"]]


def answer_for(question: str, bank) -> Answer:
    """An answer only when the bank honestly supports it. Otherwise NEEDS_HUMAN.

    Nothing here synthesizes. It will not count your years from your job history,
    will not turn a nearby answer into this one, and will not pick between two
    plausible entries. Every one of those is a fabrication with a helpful face on
    it, and a fabricated answer on a real application is the applicant's problem
    forever -- a wrong work-authorization answer is a withdrawn offer, not a typo.

    Matching, in order:
      1. the normalized question equals a bank question or alias   -> answer
      2. exactly one multi-word alias appears inside the question   -> answer
      3. anything else -- no match, two matches, an empty value,
         or an essay prompt matched by keyword rather than exactly  -> NEEDS_HUMAN
    """
    try:
        qn = _norm_question(question or "")
        if not qn:
            return Answer(NEEDS_HUMAN, reason="no question text to answer")
        entries = _bank_entries(bank)
        if not entries:
            return Answer(NEEDS_HUMAN, reason="the bank holds no usable answers")

        exact = [e for e in entries if qn == e["qn"] or qn in e["aliases"]]
        if len(exact) > 1:
            return Answer(NEEDS_HUMAN, reason=(
                "two bank entries claim this exact question: "
                + ", ".join(e["id"] for e in exact[:4])))
        hit, how = (exact[0], "exact") if exact else (None, "")

        if hit is None:
            # A one-word alias would match far too much ("experience", "salary"),
            # so containment needs a phrase. Short aliases match exactly or not at all.
            loose = [e for e in entries
                     if any(len(a.split()) >= 2 and a in qn
                            for a in ([e["qn"]] if e["qn"] else []) + e["aliases"])]
            if len(loose) > 1:
                return Answer(NEEDS_HUMAN, reason=(
                    "this question matches more than one bank entry ("
                    + ", ".join(e["id"] for e in loose[:4])
                    + "); picking one would be a guess"))
            if not loose:
                return Answer(NEEDS_HUMAN,
                              reason="nothing in the bank answers this question")
            hit, how = loose[0], "phrase"

        if how != "exact" and (len(qn.split()) > _ESSAY_WORDS
                               or any(m in qn for m in _ESSAY_MARKS)):
            return Answer(NEEDS_HUMAN, matched=hit["id"], reason=(
                "this is a free-text prompt and the bank only matched it on a "
                "keyword; a canned paragraph is not an answer to it"))

        v = hit["value"]
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            return Answer(NEEDS_HUMAN, matched=hit["id"], reason=(
                f"the bank entry {hit['id']!r} holds a "
                f"{type(v).__name__}, which is not something to type into a form"))
        if not _committed(v):
            return Answer(NEEDS_HUMAN, matched=hit["id"], reason=(
                f"the bank entry {hit['id']!r} is a placeholder, not an answer"))
        return Answer(v, source=hit["source"], matched=hit["id"],
                      reason=f"{how} match on bank entry {hit['id']!r}")
    except Exception as e:                       # noqa: BLE001
        return Answer(NEEDS_HUMAN, reason=f"answer lookup failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# submitting -- one application, already approved by a human                   #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ApplyPacket:
    """Exactly what will be sent, plus the identity we expect echoed back."""
    fields: dict
    applicant_name: str = ""
    applicant_email: str = ""
    answers: dict = dataclasses.field(default_factory=dict)
    # Files destined for file inputs. A tuple, not a dict, because a form is
    # allowed to ask for two attachments under two names.
    uploads: tuple = ()
    # Machinery the employer's own page filled in. Not ours, so a record view that
    # omits it is not a dropped field.
    unchecked_fields: frozenset = frozenset({"tracking_id", "source", "csrf",
                                             "csrf_token", "authenticity_token"})

    def markers(self) -> tuple[str, ...]:
        return tuple(m for m in (self.applicant_email, self.applicant_name) if m.strip())

    def upload_for(self, name: str):
        """The Upload bound to a form control, or None."""
        for u in self.uploads:
            if u.field == name:
                return u
        return None

    def deferred(self) -> tuple[str, ...]:
        """Fields holding something that never became an answer. Optional fields
        included: an unanswered question typed into a form as the literal word
        NEEDS_HUMAN is a fabrication with a straight face, and so is a bare bool
        arriving at the employer as the word "True"."""
        return tuple(sorted(k for k, v in self.fields.items() if _untypeable(v)))

    def wire(self) -> dict:
        """The form-encoded body. Unwraps Answer, refuses to guess at anything else."""
        out = {}
        for k, v in self.fields.items():
            if isinstance(v, Answer):
                v = v.value
            # Unreachable through submit(), which blocks on deferred() first, but
            # wire() is public and str(True) == "True" is not a value a human
            # stood behind -- the same judgement answer_for already makes.
            if v is NEEDS_HUMAN or v is None or isinstance(v, bool):
                continue
            out[k] = str(v)
        return out


class Outcome(enum.Enum):
    REFUSED = "refused"        # we did not send, on purpose
    BLOCKED = "blocked"        # preflight said no; nothing was sent
    VERIFIED = "verified"
    UNVERIFIED = "unverified"  # it may have landed; a human looks
    FAILED = "failed"          # the connection never opened


@dataclasses.dataclass(frozen=True)
class SubmitResult:
    outcome: Outcome
    reason: str
    state: State | None = None
    preflight: Preflight | None = None
    signals: set[str] | None = None
    evidence: Evidence | None = None
    record: RecordCheck | None = None
    reference: str | None = None
    final_url: str = ""

    @property
    def sent(self) -> bool:
        return self.outcome in (Outcome.VERIFIED, Outcome.UNVERIFIED)


TIER_ZERO = {"reach", "tier0", "tier 0", "t0", "0", "dream", "stretch"}


def is_reach_target(company: str, tier: str | None = None) -> bool:
    """Two independent reads: what the row says it is, and who the employer is.

    Either one is enough. A mislabelled tier must not be a way past this, because
    the cost is asymmetric -- a standard employer is a repeatable event and a
    reach employer is close to one-shot. The ancestor burned its single most
    important employer by treating it as repeatable.
    """
    t = (tier or "").strip().casefold().replace("_", " ").replace("-", " ")
    return t in TIER_ZERO or panel.is_reach(company)


def is_done(app: Application) -> bool:
    """Only a verified submission is done. SUBMITTED_UNVERIFIED is an open item
    with a human's name on it, and counting it as done is how a rail reports
    success it never had."""
    return app is not None and app.state is State.SUBMITTED_VERIFIED


def submit(store: Store, app_id: str, packet: ApplyPacket, *,
           allow_external: bool = False, record_url_template: str | None = None,
           fetch=None, timeout: float = DEFAULT_TIMEOUT) -> SubmitResult:
    """Send ONE approved application and try hard to disprove that it worked.

    Requires the row to already be APPROVED -- which, per the store's state
    machine, is only reachable from AWAITING_APPROVAL, i.e. a human decided this
    one. This function never makes that decision and cannot be pointed at a list.
    """
    app = store.get(app_id)
    if app is None:
        return SubmitResult(Outcome.REFUSED, f"no application {app_id!r} in the store")

    if is_reach_target(app.company, app.tier):
        # Before the state check, before the flags, before the network: there is no
        # argument and no configuration that gets past this line.
        return SubmitResult(
            Outcome.REFUSED,
            f"{app.company} is a reach/tier-0 employer; this path never submits to "
            f"one. Apply by hand, once, with your eyes on it.",
            state=app.state)

    if app.state is State.SUBMITTED_UNVERIFIED:
        return SubmitResult(
            Outcome.REFUSED,
            "this application is already recorded as possibly-sent and is waiting on a "
            "human. Re-sending on a guess is how an applicant gets rate-capped.",
            state=app.state)
    if app.state is not State.APPROVED:
        return SubmitResult(
            Outcome.REFUSED,
            f"state is {app.state.value}; only an APPROVED application is submitted, "
            f"and approval is one human decision on one application.",
            state=app.state)

    deferred = packet.deferred()
    if deferred:
        return SubmitResult(
            Outcome.BLOCKED,
            "the packet still holds value(s) that never became an answer: "
            + ", ".join(deferred)
            + ". An unanswered question -- or a bare bool, which reaches the "
              "employer as the literal word 'True' -- goes to a human, never "
              "onto the form.",
            state=app.state)

    # ONE session for this one application: the employer's form stamps a token
    # into the page during the GET below and validates it on the POST, which
    # only works if both requests carry the same cookie. It dies with this call,
    # so nothing an employer set is ever sent to the next one.
    session = Session()
    do_fetch = fetch or (lambda u, data=None, files=None: http_fetch(
        u, data, allow_external=allow_external, timeout=timeout,
        files=files, session=session))

    try:
        guard_url(app.url, allow_external=allow_external)
    except ExternalHostRefused as e:
        return SubmitResult(Outcome.REFUSED, str(e), state=app.state)

    before = read_back(app.url, fetch=do_fetch, allow_external=allow_external,
                       timeout=timeout)
    if not before.readable:
        return SubmitResult(Outcome.BLOCKED,
                            f"could not read the form at {app.url}: "
                            f"{before.error or 'unreadable'}", state=app.state)

    # The form names its own file input; bind the one attachment to it before
    # anything judges whether the packet satisfies the form.
    packet = bind_uploads(before, packet)

    pre = preflight(before, packet)
    if not pre.ok:
        # Nothing has been sent, so the row stays APPROVED and a rebuild or a human
        # can pick it up. Recording a submission here would be a lie in the ledger.
        return SubmitResult(Outcome.BLOCKED,
                            f"preflight {pre.verdict.value}: {pre.reason}",
                            state=app.state, preflight=pre)

    # The employer's own hidden fields go back with the body, the way a browser
    # would send them. The applicant's committed values are applied SECOND and
    # therefore win: a page that ships a prefilled hidden copy of a field the
    # human also answered must never overwrite the human.
    body = dict(carried_fields(before))
    body.update(packet.wire())

    store.transition(app_id, State.SUBMITTING, note="posting the form")
    # Only widen the call when there is something to widen it for, so a caller
    # passing its own two-argument `fetch` keeps working exactly as before.
    if packet.uploads:
        posted = do_fetch(app.url, body, files=packet.uploads)
    else:
        posted = do_fetch(app.url, body)

    if not posted.ok:
        if posted.kind == "connect":
            app2 = store.transition(app_id, State.FAILED,
                                    note=f"connection never opened: {posted.error}")
            return SubmitResult(Outcome.FAILED, f"nothing was sent: {posted.error}",
                                state=app2.state, preflight=pre)
        # Anything other than a refused connection may have reached the employer.
        app2 = store.transition(app_id, State.SUBMITTED_UNVERIFIED,
                                note=f"post failed after opening: {posted.error}")
        return SubmitResult(Outcome.UNVERIFIED,
                            f"the request failed after the connection opened, so it may "
                            f"have landed: {posted.error}", state=app2.state, preflight=pre)

    after = read_back(posted.url, markup=posted.body, fetch=do_fetch,
                      allow_external=allow_external, timeout=timeout)
    signals = confirm_signals(before, after)
    ev = confirmation_evidence(signals, packet)
    ref = extract_reference(signals)

    def hold(reason, record=None):
        a = store.transition(app_id, State.SUBMITTED_UNVERIFIED, note=reason[:200])
        return SubmitResult(Outcome.UNVERIFIED, reason, state=a.state, preflight=pre,
                            signals=signals, evidence=ev, record=record, reference=ref,
                            final_url=posted.url)

    if signals is None:
        return hold("could not read the page after submitting, so there is nothing to "
                    "verify against")
    if not signals:
        return hold("nothing on the page changed after submitting")
    if not ev.said_received:
        return hold("nothing new on the page says a submission was received")

    record = None
    if record_url_template:
        if not ref:
            return hold("no reference on the confirmation, so the stored record cannot "
                        "be looked up")
        record = verify_record(record_url_template.format(ref=ref), packet,
                               fetch=do_fetch, allow_external=allow_external,
                               timeout=timeout)
        if not record.ok:
            return hold(record.reason, record=record)
    elif not ev.specific:
        # The /thank-you case, and every other page that congratulates everybody
        # identically. A landing URL and a warm sentence are not a receipt.
        return hold("the confirmation is generic -- it carries nothing belonging to "
                    "this application, and a URL alone verifies nothing")

    a = store.transition(app_id, State.SUBMITTED_VERIFIED,
                         note=("record read back" if record else
                               "confirmation echoed: " + ", ".join(ev.markers)))
    return SubmitResult(Outcome.VERIFIED,
                        "the submission was read back and confirmed",
                        state=a.state, preflight=pre, signals=signals, evidence=ev,
                        record=record, reference=ref, final_url=posted.url)
