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
    passed allow_external=True at the call site. Out of the box this module
    cannot reach a real employer at all -- develop against `mock_ats`.

There is no verb here that decides, fills, or sends more than one application.
`submit` takes one id, requires that id to already be APPROVED by a human in the
store, and returns one result.
"""
from __future__ import annotations

import dataclasses
import enum
import ipaddress
import os
import re
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
    "confirm_signals", "confirmation_evidence", "extract_reference",
    "verify_record", "preflight", "submit", "is_done", "is_reach_target",
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
    p = _TextParser()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        # A malformed page is still worth whatever text came out before the
        # parser gave up; returning nothing would read as "the page was empty".
        pass
    out = []
    for chunk in "".join(p.parts).split("\n"):
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
               timeout: float = DEFAULT_TIMEOUT) -> Fetched:
    """GET, or POST when `data` is given. Never raises for a network fault.

    `kind` separates "the connection never opened" from "something went wrong
    after we started talking", because only the first one proves nothing was
    sent. Guessing wrong in the optimistic direction is how an application gets
    submitted twice.
    """
    try:
        guard_url(url, allow_external=allow_external)
    except ExternalHostRefused as e:
        return Fetched(url=url, error=str(e), kind="guard")

    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
            return Fetched(url=resp.geturl(), status=resp.status,
                           body=raw.decode(charset, "replace"))
    except urllib.error.HTTPError as e:
        # A 4xx often IS the page: a re-rendered form with an error banner. Read it.
        try:
            raw = e.read(MAX_BYTES + 1)
        except Exception:
            raw = b""
        return Fetched(url=getattr(e, "url", url), status=e.code,
                       body=raw.decode("utf-8", "replace"))
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
    unread_frames: tuple[str, ...] = ()   # frames we saw and could NOT read

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
    p = _FrameParser()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        pass
    return p.srcs, p.docs


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
                                     visible=visible, prefilled=bool(a.get("value"))))

    handle_startendtag = handle_starttag


_PLACEHOLDERS = {"todo", "tbd", "fixme", "xxx", "n/a?", "needs_human", "needs human",
                 "?", "-", "--", "fill in", "<fill in>"}


def _committed(value) -> bool:
    """A value a human stood behind. Not blank, not a placeholder, not a deferral."""
    if value is None or value is NEEDS_HUMAN:
        return False
    if isinstance(value, Answer):
        return not value.needs_human and _committed(value.value)
    if isinstance(value, bool):
        return True
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
      BLIND    we could not read the form, found no controls at all, or found a
               form that declares nothing required. That last one is the trap:
               many real forms enforce their requirements in JavaScript, so "no
               required markers" means we cannot see the rules, NOT that there
               are none. Reading it as permission is the confident-wrong answer.

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
                    prefilled=prev.prefilled or c.prefilled)

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
    missing = []
    for name, value in sorted(packet.fields.items()):
        if name in packet.unchecked_fields:
            continue
        if not _committed(value):
            continue
        v = normalize(value.value if isinstance(value, Answer) else value)
        # Long free text is often truncated in a record view; match the head of it
        # rather than declaring a mismatch on a display decision.
        probe = v[:60]
        if probe and probe not in blob:
            missing.append(name)
    if missing:
        return RecordCheck(False, "the stored record is missing value(s) we submitted: "
                                  + ", ".join(missing),
                           missing=tuple(missing), url=record_url)
    return RecordCheck(True, "the stored record carries every value we submitted",
                       url=record_url)


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
    # Machinery the employer's own page filled in. Not ours, so a record view that
    # omits it is not a dropped field.
    unchecked_fields: frozenset = frozenset({"tracking_id", "source", "csrf",
                                             "csrf_token", "authenticity_token"})

    def markers(self) -> tuple[str, ...]:
        return tuple(m for m in (self.applicant_email, self.applicant_name) if m.strip())

    def deferred(self) -> tuple[str, ...]:
        """Fields still holding NEEDS_HUMAN. Optional fields included: an
        unanswered question typed into a form as the literal word NEEDS_HUMAN is
        a fabrication with a straight face."""
        return tuple(sorted(
            k for k, v in self.fields.items()
            if v is NEEDS_HUMAN or (isinstance(v, Answer) and v.needs_human)))

    def wire(self) -> dict:
        """The form-encoded body. Unwraps Answer, refuses to guess at anything else."""
        out = {}
        for k, v in self.fields.items():
            if isinstance(v, Answer):
                v = v.value
            if v is NEEDS_HUMAN or v is None:
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
            "the packet still holds unanswered question(s): " + ", ".join(deferred)
            + ". An unanswered question goes to a human, never onto the form.",
            state=app.state)

    do_fetch = fetch or (lambda u, data=None: http_fetch(
        u, data, allow_external=allow_external, timeout=timeout))

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

    pre = preflight(before, packet)
    if not pre.ok:
        # Nothing has been sent, so the row stays APPROVED and a rebuild or a human
        # can pick it up. Recording a submission here would be a lie in the ledger.
        return SubmitResult(Outcome.BLOCKED,
                            f"preflight {pre.verdict.value}: {pre.reason}",
                            state=app.state, preflight=pre)

    store.transition(app_id, State.SUBMITTING, note="posting the form")
    posted = do_fetch(app.url, packet.wire())

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
