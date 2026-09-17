"""SendBlue adapter — a real iMessage/SMS thread on the user's own phone number.

Why this rail exists: it is the one that feels like texting a person, on the
number people already use, with no app to install.

THE ONE THING IT CANNOT DO, verified against SendBlue's live documentation and
their machine-generated SDK, not inferred:

  * Reactions are SEND-ONLY, and they refuse our exact case. Their Reactions API
    accepts only INBOUND iMessage targets; a request naming an OUTBOUND message
    returns 422. So we cannot react to our own sent card, and we certainly cannot
    read a user's tapback on it.
  * No webhook carries a reaction. The `receive` payload has ~25 fields
    (content, message_handle, from_number, reply_to, thread_originator, ...) and
    not one of them is a tapback. The webhook type list -- receive, outbound,
    typing_indicator, call_log, inbound_call, line_blocked, line_assigned,
    contact_profile, contact_created -- has no reaction event.

=> Approval on this rail is a REPLY KEYWORD ("Y"), never a tapback. That is how
   SMS approvals work everywhere, and it is unaffected by the limitation.

What it CAN do, all on the free sandbox tier ($0, no credit card, 10 verified
contacts, `sendblue setup --phone +1...` auto-verifies the owner's own number --
which is exactly one more contact than a personal recruiter needs):

  * media_url    a rendered preview of the resume, in the thread
  * app_card     an Apple-rendered card, UPDATABLE in place afterwards, so the
                 card itself becomes "submitted, confirmation verified"
  * reactions on INBOUND messages -- so when the user texts "Y" we tapback their
    message as an instant read receipt. Inbound is exactly the direction that IS
    supported.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .base import Capabilities, Card, Channel

API = os.environ.get("SENDBLUE_BASE_URL", "https://api.sendblue.com")


class SendBlueChannel(Channel):
    name = "sendblue"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 from_number: str | None = None, to_number: str | None = None,
                 can_initiate: bool | None = None):
        self.api_key = api_key or os.environ.get("SENDBLUE_API_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("SENDBLUE_API_API_SECRET", "")
        self.from_number = from_number or os.environ.get("SENDBLUE_FROM_NUMBER", "")
        self.to_number = to_number or os.environ.get("SENDBLUE_TO_NUMBER", "")
        # Whether the SANDBOX tier may message a verified contact who has not
        # texted first is genuinely unresolved: the pricing table says
        # "inbound-only messaging" while the setup docs say "outbound recipients
        # must be verified before messaging", and the FAQ settles neither. Rather
        # than guess, default to FALSE and let the session be user-initiated --
        # which is the safer design regardless, because it cannot page someone at
        # 3am. Set SENDBLUE_CAN_INITIATE=1 once you have confirmed it on your tier.
        self._can_initiate = (
            can_initiate if can_initiate is not None
            else os.environ.get("SENDBLUE_CAN_INITIATE", "") == "1")
        self._cursor = 0.0

    # -- plumbing -------------------------------------------------------------
    def _call(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"sb-api-key-id": self.api_key,
                     "sb-api-secret-key": self.api_secret,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode() or "{}"
            return json.loads(body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            raise RuntimeError(f"sendblue {method} {path} -> {e.code}: {detail}") from None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_initiate=self._can_initiate,
            has_buttons=False,      # app_card tap->inbound is NOT verified; do not rely on it
            has_reactions=True,     # on INBOUND messages only -- read receipts, not approvals
            can_update_card=True,   # update_app_card
            can_send_media=True,
        )

    # -- verbs ----------------------------------------------------------------
    def send_text(self, text: str) -> str:
        r = self._call("/api/send-message", {
            "from_number": self.from_number, "number": self.to_number, "content": text})
        return str(r.get("message_handle") or r.get("messageHandle") or "")

    def send_card(self, card: Card) -> str:
        body = card.as_text() + "\n\nReply Y to apply, N to skip."
        if card.preview_png:
            handle = self.send_media(card.preview_png, caption=body)
            if handle:
                return handle
        return self.send_text(body)

    def send_media(self, path: str, caption: str = "") -> str | None:
        """Attach a preview. `media_url` is documented for 'images, videos, etc.'
        -- PDF passthrough is NOT documented, which is why the loop renders page 1
        to PNG rather than betting on it."""
        url = path if path.startswith(("http://", "https://")) else None
        if url is None:
            return None  # a local path needs a reachable URL; caller uploads first
        r = self._call("/api/send-message", {
            "from_number": self.from_number, "number": self.to_number,
            "content": caption, "media_url": url})
        return str(r.get("message_handle") or "")

    def update_card(self, card_id: str, text: str) -> bool:
        """Flip the card in place ("submitted, confirmation verified") instead of
        sending a second message the user has to reconcile with the first."""
        try:
            self._call(f"/api/messages/{card_id}/update-app-card", {"content": text})
            return True
        except Exception:
            return False

    def acknowledge(self, message_id: str) -> bool:
        """Tapback the USER's inbound reply as an instant read receipt.

        Inbound is the direction SendBlue supports -- an outbound target returns
        422. But the reaction endpoint is documented on their docs site and does
        NOT appear in the published SDK's endpoint list, so the path here is not
        verified against a live account.

        That is why this fails SOFT and returns False rather than raising: a read
        receipt is a courtesy, and an approval that arrived but could not be
        acknowledged must still count as an approval. Never make the decision
        depend on this.
        """
        try:
            self._call("/api/send-reaction",
                       {"message_handle": message_id, "reaction": "love"})
            return True
        except Exception:
            return False

    def poll_inbound(self, since: float | None = None):
        cursor = since if since is not None else self._cursor
        try:
            # GET /api/v2/messages -- note the v2. The send path is NOT versioned
            # (POST /api/send-message) and the read path is, which is easy to get
            # wrong from an overview page. It was wrong here: this polled
            # /api/messages, so an inbound "Y" was never seen and the approval
            # loop would have waited out its full timeout on every application.
            r = self._call("/api/v2/messages?limit=25", method="GET")
        except Exception:
            return []
        out = []
        for m in r.get("messages", r if isinstance(r, list) else []):
            if m.get("is_outbound"):
                continue
            ts = _ts(m.get("date_sent"))
            if ts <= cursor:
                continue
            self._cursor = max(self._cursor, ts)
            out.append({"id": m.get("message_handle"), "text": m.get("content", ""),
                        "ts": ts, "reply_to": (m.get("reply_to") or {}).get("message_handle")})
        return out


def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return time.mktime(time.strptime(v, fmt))
            except ValueError:
                continue
    return 0.0
