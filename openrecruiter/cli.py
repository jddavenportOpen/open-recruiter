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


def intake(args) -> int:
    """Build the experience bank from resumes the user already has."""
    from openrecruiter import intake as I, wire
    folder = os.path.abspath(os.path.expanduser(args.folder))
    print(f"\nreading {folder}\n")
    paths = I.discover_resumes(folder)
    if not paths:
        print("  no resume-shaped files found there.")
        return 1
    extractions, low = [], []
    for path in paths:
        got = I.extract(path)
        level = str(got.get("level") or "").upper()
        conf = got.get("confidence")
        name = os.path.basename(path)
        label = {"HIGH": "ok  ", "LOW": "weak", "UNREADABLE": "bad "}.get(level, "weak")
        pct = f"{float(conf):.0%}" if isinstance(conf, (int, float)) else "?"
        print(f"  {label} {name}  ({pct})")
        for w in got.get("warnings") or []:
            print(f"         {w}")
        if level != "HIGH":
            low.append(f"{name} ({level.lower() or 'unknown'})")
        extractions.append(got)
    bank = I.merge_to_bank(extractions)
    path = wire.save_bank(bank)
    print(f"\nbank written to {path}")
    gaps = bank.get("_gaps") or []
    if gaps:
        # Printed, never silently absorbed: a bank that is missing something and
        # does not say so is how invented facts get in later.
        print("\ngaps it could NOT fill honestly (fix these by hand):")
        for g in gaps:
            if isinstance(g, dict):
                print(f"  - {g.get('field','?')}: {g.get('why','')}")
            else:
                print(f"  - {g}")
    conflicts = bank.get("_conflicts") or []
    if conflicts:
        # Two of your own resumes disagree about a number. Nobody picks a winner
        # for you -- that is the drift this tool exists to make visible.
        print("\nyour sources DISAGREE (no winner was chosen for you):")
        for c in conflicts[:10]:
            print(f"  - {c}")
    if low:
        print(f"\n{len(low)} file(s) read poorly. Check them before trusting the bank:")
        for n in low:
            print(f"  - {n}")
    print("\nNext: `openrecruiter setup`")
    return 0


def setup(args) -> int:
    """Interview, then propose a pipeline the user confirms."""
    from openrecruiter import interview as V, store as S, wire
    import json as _json
    bank = wire.load_bank()
    store = S.Store()
    answers = {}
    print("\nA few questions. Press enter to skip any of them.\n")
    for q in V.QUESTIONS:
        try:
            got = input(f"  {q.prompt}\n  > ").strip()
        except EOFError:
            got = ""
        if got:
            answers[q.key] = got
        print()
    goals = V.build_goals(answers)
    store.save_goals(goals)
    proposal = V.propose_pipeline(goals, bank)
    for w in proposal.get("warnings") or []:
        print(f"  note: {w}\n")

    proposal["boards"] = wire.boards_from_proposal(proposal)
    reasons = proposal.get("reasons") or {}
    print("Proposed pipeline. Every board token below is a GUESS from the company")
    print("name — check them before you scan.\n")
    for row in proposal["boards"]:
        why = reasons.get(f"company:{row['name']}", "")
        print(f"  {row['name']:22} {row['ats']:11} token={row['token']:18} {why[:54]}")

    path = os.path.join(wire.home(), "pipeline.json")
    with open(path, "w") as fh:
        _json.dump(proposal, fh, indent=1)
    print(f"\nwritten to {path}")
    print("\nThis is yours to edit — add companies, fix a token, delete what you do")
    print("not want. Nothing here was chosen for you.")
    print("Then: `openrecruiter scan`")
    return 0


def scan(args) -> int:
    from openrecruiter import store as S, wire
    import json as _json
    path = os.path.join(wire.home(), "pipeline.json")
    if not os.path.exists(path):
        print("no pipeline yet — run `openrecruiter setup`")
        return 1
    with open(path) as fh:
        pipeline = _json.load(fh)
    res = wire.scan(S.Store(), pipeline)
    print(f"\n  {res['found']} postings seen, {res['added']} new")
    for f in res.get("failures") or []:
        # A board that is down is NOT an empty market. Always surfaced.
        print(f"  FAILED: {f}")
    if res.get("note"):
        print(f"  {res['note']}")
    return 0


def run(args) -> int:
    from openrecruiter import store as S, wire
    from openrecruiter.loop import run_forever, run_once
    store = S.Store()
    bank = wire.load_bank()
    # A 24-hour approval window is right for a runner you leave up, and wrong for
    # the first thing a new user types: a rail that can send but not receive is
    # indistinguishable from a human who has not answered yet, and they would
    # wait a day to find out. --once therefore waits minutes and says so.
    timeout = args.timeout_min * 60.0 if args.timeout_min else (900.0 if args.once else 86400.0)
    deps = wire.build_deps(store, bank, approval_timeout_s=timeout)
    print(f"\nrunning. stop with: touch {os.path.join(wire.home(), 'STOP')}")
    print(f"approval window: {timeout/60:.0f} min\n")
    if args.once:
        print(run_once(store, deps))
        return 0
    run_forever(store, deps)
    return 0


def outcomes(args) -> int:
    from openrecruiter import store as S
    store = S.Store()
    if args.record:
        app_id, result = args.record
        store.record_outcome(app_id, result)
        print(f"recorded {app_id}: {result}")
        return 0
    rep = store.score_vs_outcome()
    if not rep:
        print("\nNo outcomes recorded yet. That is the honest state, not an error —\n"
              "the question 'does the score predict anything?' needs data first.\n")
        return 0
    print(f"\n  {'outcome':16} {'n':>4} {'mean score':>11} {'mean days':>10}")
    for k, v in sorted(rep.items()):
        print(f"  {k:16} {v['n']:>4} {str(v['mean_score'] or '-'):>11} "
              f"{str(v['mean_days'] or '-'):>10}")
    total = sum(v["n"] for v in rep.values())
    if total < 30:
        print(f"\n  n={total}. Too few to conclude anything. Reported, not interpreted.")
    return 0


def serve(args) -> int:
    from openrecruiter import dashboard
    return dashboard.serve(port=args.port)


def import_bank(args) -> int:
    """Adopt an experience bank you already have, instead of re-deriving one.

    Parsing a styled PDF back into structured history is lossy, and a bank you
    have already curated is better than one this tool re-guessed. If you have a
    structured resume from anywhere -- recruit-copilot, your own JSON, a previous
    run -- this takes it, checks it, and tells you what it holds rather than
    assuming it is fine.
    """
    from openrecruiter import wire
    import json as _json
    src = os.path.abspath(os.path.expanduser(args.path))
    try:
        with open(src) as fh:
            bank = _json.load(fh)
    except Exception as e:
        print(f"could not read {src}: {e}")
        return 1
    if not isinstance(bank, dict):
        print(f"{src} is not a bank object")
        return 1

    jobs = bank.get("jobs") or []
    def _n(j):
        b = j.get("bullets") or {}
        return len(b) if isinstance(b, dict) else len(b)
    bullets = sum(_n(j) for j in jobs)
    contact = bank.get("contact") or {}
    name = bank.get("name") or contact.get("name") or ""

    problems = []
    if not name:
        problems.append("no name (top-level `name` or `contact.name`)")
    if not contact.get("email"):
        problems.append("no email in `contact`")
    if not jobs:
        problems.append("no jobs")
    if not bullets:
        problems.append("no bullets under any job")

    print(f"\n  name    : {name or '(missing)'}")
    print(f"  email   : {contact.get('email') or '(missing)'}")
    print(f"  jobs    : {len(jobs)}")
    print(f"  bullets : {bullets}")
    print(f"  summaries: {len(bank.get('summaries') or {})}")
    if problems:
        # Refused, not imported-with-a-warning: a bank missing these produces a
        # resume missing them too, and that is discovered at the worst moment.
        print("\n  REFUSED — this bank cannot build a resume:")
        for x in problems:
            print(f"    - {x}")
        return 1
    path = wire.save_bank(bank)
    print(f"\n  imported to {path}")
    print("\nNext: `openrecruiter setup`")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openrecruiter",
                                description="A recruiter that lives on your computer.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="what is configured, what is missing").set_defaults(fn=doctor)
    sub.add_parser("selftest", help="run the invariant suite + mutation check").set_defaults(fn=selftest)
    sub.add_parser("channels", help="show configured messaging rails").set_defaults(fn=channels)

    p_in = sub.add_parser("intake", help="build your experience bank from resumes you have")
    p_in.add_argument("folder", help="a folder containing your existing resumes")
    p_in.set_defaults(fn=intake)

    p_imp = sub.add_parser("import", help="adopt an experience bank you already have")
    p_imp.add_argument("path", help="a bank JSON file")
    p_imp.set_defaults(fn=import_bank)

    sub.add_parser("setup", help="interview, then propose a pipeline you confirm").set_defaults(fn=setup)
    sub.add_parser("scan", help="pull the configured boards for new postings").set_defaults(fn=scan)

    p_run = sub.add_parser("run", help="work the queue, one application at a time")
    p_run.add_argument("--once", action="store_true", help="a single cycle, then stop")
    p_run.add_argument("--timeout-min", type=float, default=None,
                       help="minutes to wait for your answer (default 15 with --once)")
    p_run.set_defaults(fn=run)

    p_out = sub.add_parser("outcomes", help="what happened, and whether the score predicted it")
    p_out.add_argument("--record", nargs=2, metavar=("APP_ID", "RESULT"),
                       help="rejected|screen|interview|offer|hired|no_response|withdrawn")
    p_out.set_defaults(fn=outcomes)

    p_srv = sub.add_parser("dashboard", help="the localhost dashboard")
    p_srv.add_argument("--port", type=int, default=8765)
    p_srv.set_defaults(fn=serve)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
