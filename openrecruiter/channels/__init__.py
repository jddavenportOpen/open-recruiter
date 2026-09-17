"""Messaging transports. The loop talks to `Channel`, never to an adapter."""
import os

from .base import Capabilities, Card, Channel, Decision, parse_reply  # noqa: F401

__all__ = ["Capabilities", "Card", "Channel", "Decision", "parse_reply",
           "load_channels"]


def load_channels(config: dict | None = None) -> list:
    """Build every configured transport. Both rails may run at once, which is the
    point: one user wants buttons, another wants a text thread on their own
    number, and a third wants both."""
    config = config or {}
    out: list = []
    if config.get("telegram") or os.environ.get("TELEGRAM_BOT_TOKEN"):
        from .telegram import TelegramChannel
        out.append(TelegramChannel(**(config.get("telegram") or {})))
    if config.get("sendblue") or os.environ.get("SENDBLUE_API_API_KEY"):
        from .sendblue import SendBlueChannel
        out.append(SendBlueChannel(**(config.get("sendblue") or {})))
    return out
