"""Public, no-auth job-board APIs behind ONE client.

The system this was extracted from re-implemented the same three board APIs in
fourteen-plus files. Nothing was shared: no cache, no retry, no backoff, and
"politeness" was a local `sleep()` constant per file that drifted between 0.2s
and 3s depending on who wrote the file last. Two consequences, both measured:

  * a board that started returning 429 got hammered by whichever copy ran next,
    because no copy knew another copy had just been throttled;
  * a scout run that failed outright wrote an EMPTY jobs file, which downstream
    read as "no jobs matched today". A broken scraper and an empty market are
    indistinguishable if failure is spelled `[]`. They are spelled differently
    here: failure raises, and an empty board returns an empty list.

Two invariants this module exists to hold:

  1. **A host that fails is FAILED, never zero results.** Every fetch path either
     returns a structurally-recognised payload or raises `BoardError`. A payload
     that parses as JSON but is not the shape the API documents also raises --
     because the shape changing is exactly how a silent zero arrives in practice.
  2. **Every posting gets a stable id derived from its url**, so "what is new
     since the last run" is computable. The ancestor's scout overwrote its jobs
     file wholesale with no stable id, which made that question unanswerable and
     is why the same posting was re-surfaced on consecutive days.

Discovery is not application. Nothing here decides anything, nothing here
submits, and a sweep returning two hundred postings still means two hundred
separate human decisions downstream.

stdlib only.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from . import __version__

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{token}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

SUPPORTED_ATS = ("greenhouse", "lever", "ashby")

# Worth retrying: the host is up and telling us to come back. Everything else
# (401/403/404/422) is a fact about the request, and retrying it is just rudeness
# with extra steps.
#
# The 5xx half is the whole range, not a list of five codes. The enumerated form
# excluded Cloudflare's 520-524 (origin down / origin timed out / handshake
# failed), which is the single most common transient failure for a board behind
# CF -- i.e. the case the retry policy most exists for was the case it skipped.
RETRY_STATUSES = frozenset({408, 425, 429})
RETRY_STATUS_RANGES = ((500, 599),)


def is_retryable_status(status: int) -> bool:
    """Whether a status means 'the host is up, come back' rather than 'no'."""
    if status in RETRY_STATUSES:
        return True
    return any(low <= status <= high for low, high in RETRY_STATUS_RANGES)

DEFAULT_CACHE_TTL_S = 900.0
DEFAULT_MIN_INTERVAL_S = 1.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 60.0
DEFAULT_TIMEOUT_S = 30.0

PROJECT_URL = "https://github.com/jddavenportOpen/open-recruiter"

# Query parameters that identify where a link was *clicked*, not which job it
# points at. They have to be stripped before hashing or the same posting gets a
# different id depending on which page surfaced it, and "new since last run"
# starts reporting everything as new.
#
# `gh_jid` is deliberately NOT in this set, though it looks like `gh_src` and
# once was. For an EMBEDDED Greenhouse board, `absolute_url` points at the
# customer's own careers page and the job id lives only in that parameter:
#     https://www.company.com/careers?gh_jid=4567890
# Stripping it collapses every posting at that company onto one canonical url,
# so one stable_id, so the store's url-unique index keeps exactly one of them
# and `select_new` reports one of N as new while silently discarding the rest.
# That is the "new since last run is unanswerable" failure in the module
# docstring, inverted into silent loss, which is strictly worse. `gh_src` (where
# the link was clicked) stays stripped; `gh_jid` (which job) is the identity.
_TRACKING_PARAMS = frozenset({
    "ref", "source", "src", "utm", "gh_src", "lever-origin",
    "lever-source", "_gl", "gclid", "fbclid", "mc_cid", "mc_eid",
})
_TRACKING_PREFIXES = ("utm_", "lever-source", "hsa_", "_hs")

_TAG = re.compile(r"<[^>]+>")
_SCRIPTISH = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


class BoardError(RuntimeError):
    """A board did not give us an answer we can trust.

    Carries enough to route a fix: which url, what status, how many attempts.
    """

    def __init__(self, message: str, *, url: str | None = None,
                 status: int | None = None, attempts: int = 0,
                 ats: str | None = None, token: str | None = None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.attempts = attempts
        self.ats = ats
        self.token = token


class TransportError(BoardError):
    """The request never reached a server (DNS, connect, timeout, TLS)."""


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    """What a fetcher hands back. Injectable so tests never touch a network."""
    status: int
    body: str
    headers: dict = dataclasses.field(default_factory=dict)

    def header(self, name: str) -> str | None:
        want = name.lower()
        for k, v in self.headers.items():
            if str(k).lower() == want:
                return v
        return None


Fetcher = Callable[[str, dict], HttpResponse]


@dataclasses.dataclass(frozen=True)
class Source:
    """One board to sweep. `company` is display-only."""
    ats: str
    token: str
    company: str | None = None

    def __post_init__(self):
        if self.ats not in SUPPORTED_ATS:
            raise BoardError(
                f"unsupported ats {self.ats!r} (supported: {', '.join(SUPPORTED_ATS)})",
                ats=self.ats, token=self.token)
        if not self.token:
            raise BoardError(f"{self.ats}: empty board token", ats=self.ats)

    @classmethod
    def parse(cls, spec: str) -> "Source":
        """`greenhouse:acme` or `greenhouse:acme=Acme Corp`.

        An unknown prefix raises rather than being skipped: a config typo that
        silently drops a board looks identical to a company that stopped hiring.
        """
        text = (spec or "").strip()
        if ":" not in text:
            raise BoardError(f"bad source spec {spec!r}, expected '<ats>:<token>'")
        ats, _, rest = text.partition(":")
        token, _, company = rest.partition("=")
        return cls(ats.strip().lower(), token.strip(), company.strip() or None)

    def __str__(self) -> str:
        return f"{self.ats}:{self.token}"


@dataclasses.dataclass(frozen=True)
class BoardFailure:
    """A source that did not answer. Never collapsed into 'zero results'."""
    ats: str
    token: str
    error: str
    status: int | None = None
    attempts: int = 0

    def __str__(self) -> str:
        code = f" HTTP {self.status}" if self.status else ""
        return f"{self.ats}:{self.token}{code} — {self.error}"


@dataclasses.dataclass
class SweepResult:
    postings: list[dict]
    failures: list[BoardFailure]

    @property
    def ok(self) -> bool:
        return not self.failures

    def raise_for_failures(self) -> None:
        if self.failures:
            raise BoardError(
                f"{len(self.failures)} of "
                f"{len(self.failures) + self._source_count} board(s) failed: "
                + "; ".join(str(f) for f in self.failures))

    _source_count: int = 0


def stable_id(url: str) -> str:
    """A deterministic id for a posting, derived from its url.

    Derived from the url and not from the board's own id because the native id
    is per-ATS and is re-issued when a posting is cloned to another board, while
    the url is what the store's unique index is keyed on. Tracking parameters,
    the fragment, a trailing slash and host case are all normalised away first,
    so the same posting hashes the same whether it arrived from the API, a
    newsletter link, or a re-run six weeks later.
    """
    if not url or not str(url).strip():
        raise BoardError("cannot derive a stable id from an empty url")
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def canonical_url(url: str) -> str:
    """Normalise a posting url for hashing. A url we cannot parse raises
    `BoardError`, never the bare `ValueError` urlsplit hands out.

    urlsplit is lazy: `https://host:notaport/x` parses fine and only raises when
    `.port` is touched, several frames deep inside this function. A ValueError
    escaping here is not caught by `sweep`, which catches BoardError, so ONE
    malformed url in ONE board's payload aborted the whole sweep and threw away
    the postings already collected from healthy boards -- the exact opposite of
    the documented `allow_partial` contract, with an exception type no caller of
    this module has any reason to catch. Malformed is a per-board failure.
    """
    raw = str(url).strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        scheme = (parts.scheme or "https").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as e:
        raise BoardError(f"malformed url {raw!r}: {e}", url=raw) from None
    if port and not ((scheme == "https" and port == 443)
                     or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    keep = [(k, v) for k, v in query_pairs if not _is_tracking(k)]
    query = urllib.parse.urlencode(sorted(keep))
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or k.startswith(_TRACKING_PREFIXES)


def select_new(postings: Iterable[dict], known_ids: Iterable[str]) -> list[dict]:
    """The postings whose stable id we have not seen before.

    Raises on a posting with no id rather than treating it as new -- an id-less
    posting silently counted as new is how a re-run re-offers work already
    decided.
    """
    seen = {str(i) for i in known_ids}
    out = []
    for p in postings:
        pid = p.get("id")
        if not pid:
            raise BoardError(f"posting has no stable id: {p.get('url') or p!r}")
        if str(pid) not in seen:
            out.append(p)
    return out


# -- transport ----------------------------------------------------------------

def default_user_agent() -> str:
    """Descriptive on purpose: a board operator who wants to throttle or contact
    us can do either without guessing. Set OPENRECRUITER_CONTACT to be reachable."""
    contact = os.environ.get("OPENRECRUITER_CONTACT", "").strip()
    who = f"; {contact}" if contact else ""
    return f"openrecruiter/{__version__} (+{PROJECT_URL}{who}) python-urllib"


def urllib_fetcher(url: str, headers: dict, timeout_s: float = DEFAULT_TIMEOUT_S) -> HttpResponse:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return HttpResponse(getattr(r, "status", 200) or 200,
                                r.read().decode("utf-8", "replace"), dict(r.headers))
    except urllib.error.HTTPError as e:
        # An HTTP error IS an answer -- the retry policy decides what it means.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return HttpResponse(e.code, body, dict(e.headers or {}))
    except urllib.error.URLError as e:
        raise TransportError(f"{url}: {e.reason}", url=url) from None
    except (TimeoutError, OSError) as e:
        raise TransportError(f"{url}: {e}", url=url) from None


# Above this many files on disk, `put` sweeps expired entries first. Entries are
# bounded by board count in the normal case, so this only bites a cache whose
# config churned; it exists so the directory cannot grow without limit forever.
DEFAULT_CACHE_MAX_ENTRIES = 2000


class ResponseCache:
    """On-disk, TTL'd, keyed by the canonical url.

    Only bodies that were fetched successfully AND decoded successfully are
    stored. Both halves matter: the caller writes the entry after the decode,
    because an HTTP 200 carrying a maintenance page, a CDN interstitial or an
    auth redirect is a failure the status line does not admit to, and caching it
    keeps the board broken for the full TTL after the host has recovered --
    off cache, with no network call left to notice the recovery.
    """

    def __init__(self, directory: str, ttl_s: float, clock: Callable[[], float] = time.time,
                 max_entries: int = DEFAULT_CACHE_MAX_ENTRIES):
        self.dir = directory
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._clock = clock
        os.makedirs(self.dir, exist_ok=True)

    def path_for(self, url: str) -> str:
        key = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
        return os.path.join(self.dir, f"{key}.json")

    def get(self, url: str) -> str | None:
        if self.ttl_s <= 0:
            return None
        try:
            with open(self.path_for(url), "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            # An unreadable cache entry degrades to a network fetch. That is
            # safe: the cache is an optimisation, never the source of a verdict.
            return None
        if self._clock() - float(entry.get("fetched_at", 0)) > self.ttl_s:
            # Unlink on read: a stale entry is already known to be useless, and
            # ignoring it without removing it is how the directory grew forever.
            with contextlib.suppress(OSError):
                os.unlink(self.path_for(url))
            return None
        body = entry.get("body")
        return body if isinstance(body, str) else None

    def prune(self) -> int:
        """Remove every expired entry. Returns how many went. Never raises: a
        cache we cannot tidy is a fuller disk, not a wrong answer."""
        removed = 0
        now = self._clock()
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    entry = json.load(fh)
                fresh = now - float(entry.get("fetched_at", 0)) <= self.ttl_s
            except (OSError, ValueError, TypeError):
                fresh = False  # unreadable is unusable; it refetches anyway
            if not fresh:
                with contextlib.suppress(OSError):
                    os.unlink(path)
                    removed += 1
        return removed

    def put(self, url: str, body: str) -> None:
        if self.ttl_s <= 0:
            return
        try:
            if len(os.listdir(self.dir)) > self.max_entries:
                self.prune()
        except OSError:
            pass
        path = self.path_for(url)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"url": url, "fetched_at": self._clock(), "body": body}, fh)
            os.replace(tmp, path)
        except OSError:
            # A cache we cannot write is a slower run, not a wrong one.
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def default_cache_dir() -> str:
    home = os.environ.get("OPENRECRUITER_HOME") or os.path.expanduser("~/.openrecruiter")
    return os.path.join(home, "board-cache")


class BoardClient:
    """One client for every public board API.

    Per-HOST rate limiting (boards-api.greenhouse.io and api.lever.co are
    unrelated services and one being slow is not a reason to slow the other),
    an on-disk response cache, exponential backoff on 429/5xx, and a User-Agent
    that says who we are.
    """

    def __init__(self, cache_dir: str | None = None,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
                 backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 user_agent: str | None = None,
                 fetcher: Fetcher | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval_s = float(min_interval_s)
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_cap_s = float(backoff_cap_s)
        self.timeout_s = float(timeout_s)
        self.user_agent = user_agent or default_user_agent()
        self._clock = clock
        self._sleep = sleep
        self._fetcher: Fetcher = fetcher or (
            lambda url, headers: urllib_fetcher(url, headers, self.timeout_s))
        self._last_call: dict[str, float] = {}
        self.cache = (ResponseCache(cache_dir or default_cache_dir(), cache_ttl_s, clock)
                      if cache_ttl_s > 0 else None)
        # Counters, not logs: a caller can assert on them, and `doctor` can show
        # whether a run actually hit the network or answered out of cache.
        self.network_calls = 0
        self.cache_hits = 0
        self.retries = 0

    # -- http ------------------------------------------------------------------
    def headers(self) -> dict:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def fetch_json(self, url: str, *, force: bool = False) -> Any:
        if not force and self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                self.cache_hits += 1
                return self._decode(url, cached)
        body = self._fetch_body(url)
        # Decode BEFORE caching. A 200 whose body is not JSON is a failure, and
        # writing it first poisons the board for the whole TTL: every subsequent
        # call raises off cache without a network request, so the run stays
        # broken long after the host recovered. Only a body that parsed is
        # allowed to become the cached answer.
        value = self._decode(url, body)
        if self.cache is not None:
            self.cache.put(url, body)
        return value

    def _decode(self, url: str, body: str) -> Any:
        try:
            return json.loads(body)
        except ValueError as e:
            raise BoardError(f"{url}: response was not JSON ({e}); "
                             f"first 200 chars: {body[:200]!r}", url=url) from None

    def _fetch_body(self, url: str) -> str:
        attempts = self.max_retries + 1
        last_error = "no attempt was made"
        last_status: int | None = None
        for attempt in range(attempts):
            self._respect_rate_limit(url)
            self.network_calls += 1
            retry_after = None
            try:
                resp = self._fetcher(url, self.headers())
            except TransportError as e:
                last_error, last_status = str(e), None
            except Exception as e:  # a fetcher that blows up is a failure, not a pass
                last_error, last_status = f"fetcher raised {type(e).__name__}: {e}", None
            else:
                if 200 <= resp.status < 300:
                    return resp.body
                last_status = resp.status
                last_error = f"HTTP {resp.status}: {resp.body[:200].strip()}"
                if not is_retryable_status(resp.status):
                    raise BoardError(f"{url} failed: {last_error}",
                                     url=url, status=resp.status, attempts=attempt + 1)
                retry_after = _retry_after_seconds(resp)
            if attempt < attempts - 1:
                self.retries += 1
                self._sleep(self._backoff_delay(attempt, retry_after))
        raise BoardError(f"{url} failed after {attempts} attempt(s): {last_error}",
                         url=url, status=last_status, attempts=attempts)

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self.backoff_base_s * (2 ** attempt), self.backoff_cap_s)
        if retry_after is not None:
            # The host told us how long to wait. Honour it, but never let a
            # hostile or fat-fingered header park the run for an hour.
            delay = max(delay, min(retry_after, self.backoff_cap_s))
        return delay

    def _respect_rate_limit(self, url: str) -> None:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        last = self._last_call.get(host)
        now = self._clock()
        if last is not None:
            wait = last + self.min_interval_s - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_call[host] = now

    # -- adapters --------------------------------------------------------------
    def greenhouse(self, token: str, company: str | None = None,
                   with_content: bool = True) -> list[dict]:
        url = GREENHOUSE_URL.format(token=urllib.parse.quote(token, safe=""))
        if with_content:
            url += "?content=true"
        return normalize_greenhouse(self.fetch_json(url), token, company)

    def lever(self, token: str, company: str | None = None) -> list[dict]:
        url = LEVER_URL.format(token=urllib.parse.quote(token, safe="")) + "?mode=json"
        return normalize_lever(self.fetch_json(url), token, company)

    def ashby(self, token: str, company: str | None = None) -> list[dict]:
        url = ASHBY_URL.format(token=urllib.parse.quote(token, safe=""))
        return normalize_ashby(self.fetch_json(url), token, company)

    def fetch(self, source: Source) -> list[dict]:
        """One source. Raises BoardError -- it never answers a failure with []."""
        adapter = {"greenhouse": self.greenhouse,
                   "lever": self.lever,
                   "ashby": self.ashby}[source.ats]
        try:
            return adapter(source.token, source.company)
        except BoardError as e:
            e.ats, e.token = source.ats, source.token
            raise

    def sweep(self, sources: Sequence[Source], allow_partial: bool = False) -> SweepResult:
        """Several sources. Fails CLOSED: any failure raises unless the caller
        explicitly opts into a partial answer, in which case the failures are in
        the result and `ok` is False. There is no arrangement in which a dead
        host is reported as a market with no jobs in it."""
        postings: list[dict] = []
        failures: list[BoardFailure] = []
        for src in sources:
            try:
                postings.extend(self.fetch(src))
            except BoardError as e:
                failures.append(BoardFailure(src.ats, src.token, str(e),
                                             e.status, e.attempts))
        result = SweepResult(postings, failures,
                             _source_count=len(sources) - len(failures))
        if failures and not allow_partial:
            result.raise_for_failures()
        return result


def _retry_after_seconds(resp: HttpResponse) -> float | None:
    raw = resp.header("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None  # HTTP-date form; the plain backoff covers it


# -- normalization ------------------------------------------------------------
# Every adapter returns exactly these keys, so a downstream reader never has to
# know which ATS a posting came from.
POSTING_KEYS = ("id", "company", "role", "url", "location", "posted_at", "ats", "description")


def _posting(url, company, role, location, posted_at, ats, description) -> dict:
    return {
        "id": stable_id(url),
        "company": company,
        "role": role,
        "url": str(url).strip(),
        "location": location or None,
        "posted_at": posted_at,
        "ats": ats,
        "description": description or "",
    }


def _require_list(payload: Any, key: str | None, ats: str, token: str) -> list:
    """The shape check. A payload we do not recognise raises.

    This is the single most important line in the file: returning [] here is how
    an API that changed shape becomes 'no jobs today' for a week. A board with
    genuinely no openings still sends the key, with an empty list in it.
    """
    if key is None:
        if isinstance(payload, list):
            return payload
        raise BoardError(
            f"{ats}:{token} returned {type(payload).__name__}, expected a JSON list "
            f"of postings — treating as FAILED, not as an empty board",
            ats=ats, token=token)
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    got = (", ".join(sorted(payload)[:8]) if isinstance(payload, dict)
           else type(payload).__name__)
    raise BoardError(
        f"{ats}:{token} response has no {key!r} list (got: {got}) — "
        f"treating as FAILED, not as an empty board",
        ats=ats, token=token)


def _text(value: Any) -> str:
    """HTML (or double-escaped HTML, which is what Greenhouse sends) to plain text."""
    if not value:
        return ""
    s = html.unescape(str(value))
    s = _SCRIPTISH.sub(" ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s.replace("\xa0", " "))
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _BLANKS.sub("\n\n", s).strip()


def _iso(value: Any) -> str | None:
    """Epoch seconds, epoch millis, or an ISO-ish string -> ISO-8601 UTC.

    An unparseable date returns None and never the current time: a fabricated
    'posted today' would make an old posting look fresh, which is precisely the
    judgement the human is being asked to make.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_greenhouse(payload: Any, token: str, company: str | None = None) -> list[dict]:
    jobs = _require_list(payload, "jobs", "greenhouse", token)
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            raise BoardError(f"greenhouse:{token} job entry is "
                             f"{type(j).__name__}, expected an object",
                             ats="greenhouse", token=token)
        url = j.get("absolute_url") or j.get("url")
        if not url:
            raise BoardError(f"greenhouse:{token} job {j.get('id')!r} has no url; "
                             f"a posting with no url has no stable id",
                             ats="greenhouse", token=token)
        location = (j.get("location") or {}).get("name") if isinstance(
            j.get("location"), dict) else j.get("location")
        out.append(_posting(
            url=url,
            company=j.get("company_name") or company or token,
            role=j.get("title") or "",
            location=location,
            posted_at=_iso(j.get("first_published") or j.get("updated_at")),
            ats="greenhouse",
            description=_text(j.get("content")),
        ))
    return out


def normalize_lever(payload: Any, token: str, company: str | None = None) -> list[dict]:
    jobs = _require_list(payload, None, "lever", token)
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            raise BoardError(f"lever:{token} posting entry is "
                             f"{type(j).__name__}, expected an object",
                             ats="lever", token=token)
        url = j.get("hostedUrl") or j.get("applyUrl")
        if not url:
            raise BoardError(f"lever:{token} posting {j.get('id')!r} has no hostedUrl",
                             ats="lever", token=token)
        cats = j.get("categories") if isinstance(j.get("categories"), dict) else {}
        out.append(_posting(
            url=url,
            company=company or token,
            role=j.get("text") or "",
            location=cats.get("location"),
            posted_at=_iso(j.get("createdAt") or j.get("createdAt_")),
            ats="lever",
            description=j.get("descriptionPlain") or _text(j.get("description")),
        ))
    return out


def normalize_ashby(payload: Any, token: str, company: str | None = None) -> list[dict]:
    jobs = _require_list(payload, "jobs", "ashby", token)
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            raise BoardError(f"ashby:{token} job entry is "
                             f"{type(j).__name__}, expected an object",
                             ats="ashby", token=token)
        # Ashby marks a posting it is no longer showing on the board. Dropping an
        # explicit False is not a silent filter: absence of the key keeps the job.
        if j.get("isListed") is False:
            continue
        url = j.get("jobUrl") or j.get("applyUrl")
        if not url:
            raise BoardError(f"ashby:{token} job {j.get('id')!r} has no jobUrl",
                             ats="ashby", token=token)
        out.append(_posting(
            url=url,
            company=j.get("organizationName") or company or token,
            role=j.get("title") or "",
            location=j.get("location"),
            posted_at=_iso(j.get("publishedAt") or j.get("updatedAt")),
            ats="ashby",
            description=j.get("descriptionPlain") or _text(j.get("descriptionHtml")),
        ))
    return out


NORMALIZERS = {"greenhouse": normalize_greenhouse,
               "lever": normalize_lever,
               "ashby": normalize_ashby}
