"""Read the quota you actually have, instead of discovering it by hitting a wall.

This module exists because of one specific, repeated, expensive mistake: treating
"the call failed" as a single condition. It is two conditions with **opposite**
correct responses.

  * a **server blip** (overloaded, 502, a dropped connection) should be retried
    promptly -- sleeping six days because Anthropic had a bad thirty seconds
    costs a day of applications for nothing;
  * a **plan wall** (the 5-hour or weekly cap) must NOT be retried -- a tight
    retry loop against a wall burns nothing but log lines, and the answer does
    not change until `resetsAt`.

The trap that makes this genuinely hard: the *blip* message contains the *wall*
keywords. Anthropic's overload text says, in as many words, that it is **not**
your usage limit -- which naive substring matching reads as "usage limit" and
files as a wall. `classify_failure` checks the disclaimers BEFORE the keywords
for exactly that reason, and a test pins it in both directions.

The second half is pacing. A healthy Claude CLI stream emits a `rate_limit_event`
BEFORE it does any work, carrying the utilization of every window -- `five_hour`,
`seven_day`, and `seven_day_opus`, which is a separate cap with its own reset.
So the wall is knowable in advance and there is no excuse for walking into it.
**Exit codes alone cannot tell you any of this**: a walled run and a blipped run
and a refusal all exit non-zero, and a run that succeeded at 99.4% utilization
exits 0 while being one application away from a six-day stall.

Stopping at a ceiling rather than at the wall also leaves headroom for the work
you did not schedule -- the interactive session you open to answer a recruiter.

Nothing here is a volume knob: every function in this module can only make the
runner do LESS.
"""
from __future__ import annotations

import datetime
import json
import re

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
SEVEN_DAY_OPUS = "seven_day_opus"      # a separate cap, with its own reset
KNOWN_WINDOWS = (FIVE_HOUR, SEVEN_DAY, SEVEN_DAY_OPUS)

DEFAULT_CEILING = 0.90

PLAN_WALL = "plan_wall"
SERVER_BLIP = "server_blip"
OTHER = "other"

# Epoch values above this are milliseconds, not seconds. (This threshold is the
# year 5138 in seconds.) Guessing here is the safe direction: the alternative to
# converting is sleeping for fifty thousand years on a units mistake.
_MS_THRESHOLD = 1e11


class QuotaUnknown(RuntimeError):
    """We could not read the quota. That is a refusal, never a pass.

    Raised rather than returning "no windows" or "0% used", because those are
    indistinguishable from real headroom at the call site -- and the caller would
    then proceed at full speed precisely when it is blind.
    """


class MalformedRateLimitEvent(QuotaUnknown):
    """A line announced itself as a rate_limit_event and then could not be read.

    Deliberately NOT `None`: returning None here would make a corrupt quota event
    look exactly like an ordinary line of output, which is how a blind runner
    ends up believing it has full headroom.
    """


# -- parsing ------------------------------------------------------------------

def _as_obj(line):
    if isinstance(line, dict):
        return line
    if not isinstance(line, str):
        return None
    s = line.strip()
    if s.startswith("data:"):                    # tolerate an SSE-framed stream
        s = s[5:].strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except ValueError as e:
        # A truncated event is not "not an event". Say so.
        if "rate_limit_event" in s:
            raise MalformedRateLimitEvent(f"rate_limit_event line did not parse: {e}") from e
        return None
    return obj if isinstance(obj, dict) else None


def _utilization(value, where):
    if isinstance(value, bool):                  # True is not 100% of anything
        raise MalformedRateLimitEvent(f"{where}: utilization is a boolean, not a number")
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            raise MalformedRateLimitEvent(f"{where}: utilization {value!r} is not a number") from None
    if not isinstance(value, (int, float)):
        raise MalformedRateLimitEvent(f"{where}: utilization is missing")
    v = float(value)
    if v < 0:
        raise MalformedRateLimitEvent(f"{where}: utilization {v} is negative")
    if v > 1.0:
        # Some producers report percent. Reading 84 as 8400% would pause forever;
        # reading it as 84% is both the likely intent and the cautious reading.
        if v > 100.0:
            raise MalformedRateLimitEvent(f"{where}: utilization {v} is out of range")
        v = v / 100.0
    return v


def _resets_at(value, where):
    """Epoch seconds, or None when the producer did not say.

    A missing reset is survivable (the caller falls back to a bounded sleep and
    re-reads); an unparseable one is not, because it would be silently dropped.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise MalformedRateLimitEvent(f"{where}: resetsAt is a boolean")
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            v = float(s)
        except ValueError:
            try:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                raise MalformedRateLimitEvent(f"{where}: resetsAt {value!r} is unreadable") from None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            v = dt.timestamp()
    else:
        raise MalformedRateLimitEvent(f"{where}: resetsAt {value!r} is unreadable")
    if v <= 0:
        raise MalformedRateLimitEvent(f"{where}: resetsAt {v} is not a time")
    if v > _MS_THRESHOLD:
        v = v / 1000.0
    return v


def _window(kind, payload):
    if not isinstance(payload, dict):
        raise MalformedRateLimitEvent(f"window {kind!r} is not an object")
    util = payload.get("utilization", payload.get("utilisation"))
    resets = payload.get("resetsAt", payload.get("resets_at"))
    return {"kind": kind,
            "utilization": _utilization(util, f"window {kind!r}"),
            "resets_at": _resets_at(resets, f"window {kind!r}")}


def parse_rate_limit_event(line):
    """Read one line of a Claude CLI stream.

    Returns `{kind, utilization, resets_at, status, windows}` for a rate-limit
    event, or None for any other line. `windows` maps every window the event
    carried -- including kinds this module has never heard of, because an unknown
    cap is still a cap and dropping it would be the optimistic failure.

    Raises MalformedRateLimitEvent if the line IS a rate-limit event and cannot
    be read; see that class for why this is not None.
    """
    obj = _as_obj(line)
    if obj is None:
        return None
    if obj.get("type") != "rate_limit_event":
        return None
    info = obj.get("rate_limit_info", obj.get("rateLimitInfo"))
    if not isinstance(info, dict):
        raise MalformedRateLimitEvent("rate_limit_event has no rate_limit_info object")

    windows = {}
    unified = info.get("unifiedWindows", info.get("unified_windows"))
    if isinstance(unified, dict):
        for kind, payload in unified.items():
            windows[str(kind)] = _window(str(kind), payload)

    kind = info.get("rateLimitType", info.get("rate_limit_type"))
    if kind is not None:
        kind = str(kind)
        if kind not in windows:
            # The top level carries the primary window's own numbers.
            windows[kind] = _window(kind, info)
    elif len(windows) == 1:
        kind = next(iter(windows))
    else:
        raise MalformedRateLimitEvent(
            "rate_limit_event names no rateLimitType and carries "
            f"{len(windows)} unified windows, so the primary window is ambiguous")

    primary = windows[kind]
    return {"kind": kind,
            "utilization": primary["utilization"],
            "resets_at": primary["resets_at"],
            "status": info.get("status"),
            "windows": windows}


def windows_from_stream(lines):
    """Pull the quota windows out of a CLI stream.

    Raises QuotaUnknown when the stream carried no rate-limit event at all: a
    stream with no event is a stream we could not measure, and returning `{}`
    would be read downstream as "nothing is constrained".
    """
    merged = {}
    for line in lines or ():
        ev = parse_rate_limit_event(line)
        if ev:
            merged.update(ev["windows"])
    if not merged:
        raise QuotaUnknown(
            "no rate_limit_event in the stream -- quota is unmeasured. "
            "Exit code 0 does not mean there was headroom.")
    return merged


# -- the pacing decision ------------------------------------------------------

def _normalize(windows):
    """Accept a parse result, a {kind: window} map, a {kind: utilization} map, or
    a list of windows. Raises on anything unreadable rather than skipping it."""
    if windows is None:
        return {}
    if isinstance(windows, dict) and "windows" in windows and isinstance(windows["windows"], dict):
        windows = windows["windows"]
    out = {}
    if isinstance(windows, dict):
        items = windows.items()
    elif isinstance(windows, (list, tuple)):
        items = [(w.get("kind", f"window{i}") if isinstance(w, dict) else f"window{i}", w)
                 for i, w in enumerate(windows)]
    else:
        raise MalformedRateLimitEvent(f"quota windows are a {type(windows).__name__}, not a mapping")
    for kind, w in items:
        kind = str(kind)
        if isinstance(w, dict):
            out[kind] = {"kind": kind,
                         "utilization": _utilization(w.get("utilization", w.get("utilisation")),
                                                     f"window {kind!r}"),
                         "resets_at": _resets_at(w.get("resets_at", w.get("resetsAt")),
                                                 f"window {kind!r}")}
        else:
            out[kind] = {"kind": kind, "utilization": _utilization(w, f"window {kind!r}"),
                         "resets_at": None}
    return out


def normalize_windows(windows):
    """The public form of the window normalizer: `{kind: {kind, utilization,
    resets_at}}`. Raises rather than dropping a window it cannot read."""
    return _normalize(windows)


def should_pause(windows, ceiling=DEFAULT_CEILING):
    """`(pause, resume_at)` -- stop at the ceiling, not at the wall.

    `resume_at` is the LATEST reset among the windows that breached: if both the
    five-hour and the weekly cap are over the ceiling, waking when the five-hour
    one clears would walk straight back into the weekly one.

    Raises QuotaUnknown on an empty window set. "I read nothing" and "you have
    headroom" are the same return value otherwise, and that is the bug this whole
    module exists to prevent.
    """
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or not 0 < ceiling <= 1:
        raise ValueError(f"ceiling must be a fraction in (0, 1], got {ceiling!r}")
    ws = _normalize(windows)
    if not ws:
        raise QuotaUnknown("no quota windows to read -- refusing to assume headroom")
    breaching = [w for w in ws.values() if w["utilization"] >= ceiling]
    if not breaching:
        return False, None
    resets = [w["resets_at"] for w in breaching if w["resets_at"]]
    return True, (max(resets) if resets else None)


def headroom(windows):
    """The tightest window, as `(kind, utilization)`. Raises when blind."""
    ws = _normalize(windows)
    if not ws:
        raise QuotaUnknown("no quota windows to read")
    kind = max(ws, key=lambda k: ws[k]["utilization"])
    return kind, ws[kind]["utilization"]


# -- failure classification ---------------------------------------------------

# Checked FIRST. Anthropic's overload copy explicitly disclaims the usage limit,
# and that disclaimer contains the wall's own keywords.
_BLIP_DISCLAIMERS = (
    "not your usage limit",
    "unrelated to your usage limit",
    "not a usage limit",
    "does not count against your usage limit",
    "doesn't count against your usage limit",
    "independent of your usage limit",
)
_WALL = (
    "usage limit reached",
    "reached your usage limit",
    "5-hour limit",
    "5 hour limit",
    "five-hour limit",
    "weekly limit",
    "7-day limit",
    "seven-day limit",
    "opus limit",
    "plan limit",
    "limit will reset",
    "quota exceeded",
    "out of usage",
    "upgrade to increase your usage limit",
)
_BLIP = (
    "overloaded",
    "overloaded_error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "temporarily unavailable",
    "upstream connect error",
    "connection reset",
    "connection refused",
    "timed out",
    "read timeout",
)
_BLIP_CODES = {408, 429, 500, 502, 503, 504, 529}
# Only read a number as an HTTP status when something nearby says it is one --
# otherwise "$500 signing bonus" in a job description classifies as an outage.
_STATUS = re.compile(
    r"\b(?:http|https|status|code|error|api)\b\D{0,16}(\d{3})\b"
    r"|\b(\d{3})\b\s*(?:error|status)\b", re.I)


def _codes(text):
    out = set()
    for m in _STATUS.finditer(text):
        for g in m.groups():
            if g:
                out.add(int(g))
    return out


def classify_failure(text):
    """'plan_wall' | 'server_blip' | 'other'.

    Order matters and is the whole point:
      1. an explicit "this is not your usage limit" disclaimer wins outright;
      2. then wall keywords -- a wall often arrives as HTTP 429, and 429 is also
         a blip code, so the wall must be tested first or every wall reads as a
         blip and gets hammered;
      3. then blip keywords and HTTP status codes;
      4. otherwise 'other', which means UNCLASSIFIED -- callers must not treat it
         as transient. An empty or unreadable message lands here on purpose.
    """
    if not isinstance(text, str) or not text.strip():
        return OTHER
    t = text.lower()
    if any(d in t for d in _BLIP_DISCLAIMERS):
        return SERVER_BLIP
    if any(w in t for w in _WALL):
        return PLAN_WALL
    if any(b in t for b in _BLIP):
        return SERVER_BLIP
    if _codes(t) & _BLIP_CODES:
        return SERVER_BLIP
    return OTHER


_KIND_HINTS = (
    ("seven_day_opus", ("opus limit", "weekly opus", "opus weekly")),
    ("five_hour", ("5-hour", "5 hour", "five-hour", "five hour")),
    ("seven_day", ("weekly", "7-day", "7 day", "seven-day", "seven day")),
)


def wall_window_kind(text):
    """Which window a wall message is about, or None when it does not say."""
    if not isinstance(text, str):
        return None
    t = text.lower()
    for kind, hints in _KIND_HINTS:
        if any(h in t for h in hints):
            return kind
    return None


def sleep_for(resume_at, now, fallback_s, cap_s=3600.0, floor_s=1.0):
    """How long to sleep before looking again.

    Capped on purpose: a bogus far-future `resetsAt` must cost one hour and a
    re-read, not a silent week of downtime. Sleeping past the cap is always safe
    to shorten, because the next read re-checks the ceiling anyway.
    """
    if resume_at is None:
        wait = fallback_s
    else:
        wait = float(resume_at) - float(now)
    return max(floor_s, min(float(cap_s), wait))
