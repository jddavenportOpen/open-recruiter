"""Telegram adapter — the default rail.

Chosen as default over local iMessage for three measured reasons, not taste:

  * macOS Full Disk Access (which the local-iMessage rail needs) has four
    documented silent-failure modes, and the failure presents as a HANG rather
    than an error. "Nothing happens" is the worst possible first run.
  * Apple permanently banned a company on launch day for the local-iMessage
    architecture. This rail carries no such ban surface.
  * An inline button returns an UNAMBIGUOUS callback payload. A tapback has to be
    joined back to a message by GUID, is Apple-only, and ~12% of thumbs-ups
    arrive as a different reaction type than the obvious one.

The callback payload is HMAC-signed and bound to (app_id, decision), so a tap on
one application is not a valid token for another. That binding is the reason this
can never quietly become "approve everything that is pending".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .base import Capabilities, Card, Channel, Decision

API = "https://api.telegram.org"


def sign(secret: str, app_id: str, decision: str) -> str:
    """Bind a decision to ONE application. Truncated to 24 chars: enough that
    guessing is hopeless, short enough for the 64-byte callback limit."""
    return hmac.new(secret.encode(), f"{app_id}:{decision}".encode(),
                    hashlib.sha256).hexdigest()[:24]


def verify(secret: str, app_id: str, decision: str, token: str) -> bool:
    return hmac.compare_digest(sign(secret, app_id, decision), token or "")


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 secret: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.secret = secret or os.environ.get("OPENRECRUITER_HMAC_SECRET", "") or self.token
        self._offset = 0

    def _call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{API}/bot{self.token}/{method}", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"telegram {method} -> {e.code}: "
                               f"{e.read().decode()[:300]}") from None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(can_initiate=True, has_buttons=True, has_reactions=True,
                            can_update_card=True, can_send_media=True)

    def send_text(self, text: str) -> str:
        r = self._call("sendMessage", {"chat_id": self.chat_id, "text": text})
        return str(r.get("result", {}).get("message_id", ""))

    def send_card(self, card: Card) -> str:
        kb = {"inline_keyboard": [[
            {"text": "Apply", "callback_data":
                f"a|{card.app_id}|{sign(self.secret, card.app_id, 'approve')}"},
            {"text": "Skip", "callback_data":
                f"r|{card.app_id}|{sign(self.secret, card.app_id, 'reject')}"},
        ]]}
        r = self._call("sendMessage", {"chat_id": self.chat_id, "text": card.as_text(),
                                       "reply_markup": kb})
        return str(r.get("result", {}).get("message_id", ""))

    def update_card(self, card_id: str, text: str) -> bool:
        try:
            self._call("editMessageText", {"chat_id": self.chat_id,
                                           "message_id": int(card_id), "text": text})
            return True
        except Exception:
            return False

    def send_media(self, path: str, caption: str = "") -> str | None:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            return None
        boundary = "----openrecruiter"
        name = os.path.basename(path)
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
            f"{self.chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"{caption}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        body = head.encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{API}/bot{self.token}/sendDocument", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return str(json.loads(r.read().decode())
                           .get("result", {}).get("message_id", ""))
        except Exception:
            return None

    def acknowledge(self, message_id: str) -> bool:
        try:
            self._call("setMessageReaction",
                       {"chat_id": self.chat_id, "message_id": int(message_id),
                        "reaction": [{"type": "emoji", "emoji": "\U0001F44D"}]})
            return True
        except Exception:
            return False

    def poll_inbound(self, since: float | None = None):
        try:
            r = self._call("getUpdates", {"offset": self._offset, "timeout": 0})
        except Exception:
            return []
        out = []
        for u in r.get("result", []):
            self._offset = max(self._offset, u.get("update_id", 0) + 1)
            cb = u.get("callback_query")
            if cb:
                out.append({"id": f"cb{cb.get('id')}", "text": "",
                            "callback": cb.get("data", ""),
                            "ts": float(cb.get("message", {}).get("date", 0)),
                            "reply_to": str(cb.get("message", {}).get("message_id", ""))})
                continue
            m = u.get("message")
            if m:
                rt = (m.get("reply_to_message") or {}).get("message_id")
                out.append({"id": str(m.get("message_id")), "text": m.get("text", ""),
                            "ts": float(m.get("date", 0)),
                            "reply_to": str(rt) if rt else None})
        return out

    def decision_from_callback(self, data: str, app_id: str) -> Decision:
        """A tapped button. The token must match THIS application and THIS
        decision, so a callback captured from one card cannot approve another."""
        try:
            kind, aid, token = data.split("|", 2)
        except ValueError:
            return Decision.AMBIGUOUS
        if aid != app_id:
            return Decision.AMBIGUOUS
        want = "approve" if kind == "a" else "reject" if kind == "r" else None
        if want is None or not verify(self.secret, app_id, want, token):
            return Decision.AMBIGUOUS
        return Decision.APPROVE if want == "approve" else Decision.REJECT
