"""Command line entry point.

`openrecruiter doctor` is the one a new user runs first: it says exactly what is
configured, what is missing, and what to type next -- rather than failing later,
in a loop, with a stack trace.
"""
from __future__ import annotations

import argparse
import os
import sys


def _ok(msg):
    print(f"  \033[32mok\033[0m    {msg}")


def _warn(msg):
    print(f"  \033[33mwarn\033[0m  {msg}")


def _bad(msg):
    print(f"  \033[31mmiss\033[0m  {msg}")


def doctor(_args) -> int:
    print("\nopenrecruiter doctor\n")
    problems = 0

    print("engine")
    try:
        from openrecruiter.engine import panel, pdftext, render_resume  # noqa: F401
        _ok("stdlib engine imports (no third-party dependencies)")
        _ok(f"panel gate: standard >= {panel.DEFAULT_THRESHOLD} "
            f"(raw >= {panel.RAW_FLOOR_DEFAULT}), "
            f"reach >= {panel.REACH_THRESHOLD} (raw >= {panel.RAW_FLOOR_REACH}, 2+ votes)")
    except Exception as e:
        _bad(f"engine failed to import: {e}")
        problems += 1

    print("\nmessaging")
    tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    sb = bool(os.environ.get("SENDBLUE_API_API_KEY") and os.environ.get("SENDBLUE_API_API_SECRET"))
    if tg:
        _ok("telegram configured (buttons, can message you first)")
    else:
        _warn("telegram not configured — talk to @BotFather, then set "
              "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if sb:
        _ok("sendblue configured (a real iMessage/SMS thread on your own number)")
        if os.environ.get("SENDBLUE_CAN_INITIATE") != "1":
            _warn("sendblue is set to REPLY-ONLY. Text it 'go' to start a session. "
                  "Set SENDBLUE_CAN_INITIATE=1 only if you have confirmed your tier "
                  "may message a verified contact first.")
    else:
        _warn("sendblue not configured — run `npm i -g @sendblue/cli && "
              "sendblue setup --phone +1XXXXXXXXXX` (free sandbox, no credit card), "
              "then set SENDBLUE_API_API_KEY / SENDBLUE_API_API_SECRET / "
              "SENDBLUE_FROM_NUMBER / SENDBLUE_TO_NUMBER")
    if not (tg or sb):
        _bad("no messaging rail configured — you would have no way to approve anything")
        problems += 1

    print("\nclaude")
    from shutil import which
    if which("claude"):
        _ok("claude CLI found (this spends YOUR subscription; we never touch your token)")
    else:
        _warn("claude CLI not found — needed for tailoring and for the apply agent")

    print()
    if problems:
        print(f"{problems} blocking problem(s). Fix those and re-run.\n")
        return 1
    print("Ready. Next: `openrecruiter selftest`\n")
    return 0


def selftest(_args) -> int:
    """Run the invariant suite AND prove it is not decoration."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("\n1. invariant suite")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=root)
    if r.returncode != 0:
        return 1
    print("\n2. mutation check — the suite must FAIL when an invariant is deleted")
    r2 = subprocess.run([sys.executable, "tests/mutation_check.py"], cwd=root)
    return r2.returncode


def channels(_args) -> int:
    from openrecruiter.channels import load_channels
    chans = load_channels()
    if not chans:
        print("no channels configured — run `openrecruiter doctor`")
        return 1
    for c in chans:
        caps = c.capabilities
        print(f"{c.name}: initiate={caps.can_initiate} buttons={caps.has_buttons} "
              f"reactions={caps.has_reactions} update_card={caps.can_update_card} "
              f"media={caps.can_send_media}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openrecruiter",
                                description="A recruiter that lives on your computer.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="what is configured, what is missing").set_defaults(fn=doctor)
    sub.add_parser("selftest", help="run the invariant suite + mutation check").set_defaults(fn=selftest)
    sub.add_parser("channels", help="show configured messaging rails").set_defaults(fn=channels)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
