"""The channel contract: one interface, several transports.

Why an interface at all, rather than just picking a messenger: the approval rail
is the one part of this system a user touches on every single application, and
the three candidates differ in ways that would otherwise leak into the work loop.

  Telegram   inline buttons, can message you first, cross-platform, free
  SendBlue   a real iMessage/SMS thread on your own number, free sandbox tier,
             approval by REPLY KEYWORD -- their Reactions API returns 422 on
             outbound targets, so a tapback is not an available primitive
  iMessage   local chat.db; reads a real tapback, but macOS-only and it needs
             Full Disk Access, whose failure mode is a HANG rather than an error

The loop must not know which of those it is talking to. It asks for capabilities
and degrades honestly.

INVARIANTS ENFORCED HERE, not left to adapters:
  * One card per application. Always. A tapback carries no text, so batching
    makes "which one did they approve?" unanswerable -- and the doctrine this
    project inherits refuses batch approval on separate grounds anyway.
  * There is NO approve-all, no auto-approve-above-N, and no bulk decision verb
    anywhere in this interface. That absence is the feature. A test asserts it.
  * An ambiguous reply is NOT an approval.
  * An approval that arrives after the card expired is re-asked, never honoured.
"""
from __future__ import annotations

import abc
import dataclasses
import enum
import re
import time
from typing import Iterable


class Decision(enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    TIMEOUT = "timeout"
    AMBIGUOUS = "ambiguous"   # said something, but not a decision -- re-ask, never assume


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What a transport can actually do. The loop branches on these, never on the
    adapter's class name."""
    can_initiate: bool        # may we message first, unprompted?
    has_buttons: bool         # a tappable decision with an unambiguous payload
    has_reactions: bool       # can we react to the USER's message (a read receipt)
    can_update_card: bool     # can we edit a sent card in place?
    can_send_media: bool


@dataclasses.dataclass
class Card:
    """One application, presented for one decision."""
    app_id: str
    company: str
    role: str
    tier: str                 # "reach" | "standard"
    panel_avg: float
    raw_panel_avg: float
    weakest_persona: str
    weakest_reason: str       # the ACTUAL reason, not a number
    claims: list[str]         # the specific assertions this resume makes
    resume_path: str | None = None
    preview_png: str | None = None
    url: str | None = None

    def as_text(self) -> str:
        """The message body. Deliberately carries the weakest reason and the
        claims: a card that shows a filename and a score turns fifteen seconds of
        READING into one second of CLICKING, and approval fatigue does the rest."""
        lines = [
            f"{self.role}",
            f"{self.company}  ({self.tier})",
            f"score {self.panel_avg:.1f}  (raw {self.raw_panel_avg:.1f})",
            "",
            f"weakest — {self.weakest_persona}: {self.weakest_reason}",
        ]
        if self.claims:
            lines += ["", "asserting:"] + [f"  • {c}" for c in self.claims]
        if self.url:
            lines += ["", self.url]
        return "\n".join(lines)


# Reply-keyword parsing. Kept here, not per-adapter, so every transport agrees on
# what counts as a yes -- and so "ambiguous is not approval" is enforced once.
_YES = {"y", "yes", "yep", "yeah", "ok", "okay", "go", "send", "apply", "approve", "1", "👍"}
_NO = {"n", "no", "nope", "skip", "pass", "reject", "stop", "0", "👎"}
_WORD = re.compile(r"[a-z0-9\U0001F300-\U0001FAFF👍👎]+")


def parse_reply(text: str) -> Decision:
    """Map a free-text reply to a decision.

    Conservative ON PURPOSE. Anything that is not unmistakably a yes or a no
    comes back AMBIGUOUS and gets re-asked. "yes but change the summary" is not
    consent to send the resume as it stands.
    """
    if not text:
        return Decision.AMBIGUOUS
    words = _WORD.findall(text.strip().lower())
    if not words:
        return Decision.AMBIGUOUS
    hits = {w for w in words if w in _YES} | {w for w in words if w in _NO}
    if len(words) > 3 or not hits:
        return Decision.AMBIGUOUS
    yes = any(w in _YES for w in words)
    no = any(w in _NO for w in words)
    if yes and no:
        return Decision.AMBIGUOUS
    return Decision.APPROVE if yes else Decision.REJECT if no else Decision.AMBIGUOUS


class Channel(abc.ABC):
    """A transport. Adapters implement the verbs; the invariants live above."""

    name: str = "channel"

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abc.abstractmethod
    def send_text(self, text: str) -> str:
        """Send a plain message. Returns a transport message id."""

    @abc.abstractmethod
    def send_card(self, card: Card) -> str:
        """Send exactly ONE application for decision. Returns a card id."""

    @abc.abstractmethod
    def poll_inbound(self, since: float | None = None) -> Iterable[dict]:
        """Yield inbound messages as {id, text, ts, reply_to (optional)}."""

    def update_card(self, card_id: str, text: str) -> bool:
        """Edit a sent card in place. False when the transport cannot."""
        return False

    def acknowledge(self, message_id: str) -> bool:
        """React to the USER's message as a read receipt. False when unsupported."""
        return False

    def send_media(self, path: str, caption: str = "") -> str | None:
        return None

    # -- the decision loop, shared by every transport --------------------------
    def await_decision(self, card_id: str, timeout_s: float = 86400.0,
                       poll_s: float = 5.0, _clock=time.time,
                       _sleep=time.sleep) -> Decision:
        """Block until the user decides, or the card expires.

        A reply arriving AFTER the deadline is deliberately not honoured: consent
        to send an application is perishable, and a stale yes is re-asked.
        """
        deadline = _clock() + timeout_s
        seen: set[str] = set()
        while _clock() < deadline:
            for msg in self.poll_inbound():
                mid = str(msg.get("id"))
                if mid in seen:
                    continue
                seen.add(mid)
                if not self._msg_is_for(msg, card_id):
                    continue
                d = parse_reply(msg.get("text", ""))
                if d is Decision.AMBIGUOUS:
                    self.send_text("Sorry — I didn't read that as yes or no. "
                                   "Reply Y to apply, N to skip.")
                    continue
                self.acknowledge(mid)
                return d
            _sleep(poll_s)
        return Decision.TIMEOUT

    def _msg_is_for(self, msg: dict, card_id: str) -> bool:
        """Whether an inbound message answers this card.

        Default: any reply during the window answers the outstanding card, which
        is sound only because we send ONE card at a time. A transport with a real
        reply-binding (Telegram callbacks, SendBlue `reply_to`) overrides this.
        """
        rt = msg.get("reply_to")
        return True if rt is None else str(rt) == str(card_id)
