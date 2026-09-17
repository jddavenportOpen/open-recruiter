#!/usr/bin/env python3
"""Prove the invariant suite is not decoration.

The ancestor repo's smoke test stayed 40/40 GREEN through every one of these
mutations -- including deleting the exact invariant its own docs called "the
first thing to preserve in a fork." This harness applies each mutation to a
COPY of the engine and asserts the suite goes RED.

A mutation that survives is a hole in the suite, and this exits non-zero when
one does. Run it in CI next to the tests themselves.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = "openrecruiter/engine/panel.py"

# (name, what it simulates, regex, replacement)
MUTATIONS = [
    ("weighting deleted", "every dimension weighted equally -- rubric is decoration",
     r'"quantified_impact_credibility": \d+', '"quantified_impact_credibility": 20'),
    ("vote-coupling unbounded", "THE original bug: max(s, 85) with no cap",
     r"MAX_VOTE_LIFT = \d+", "MAX_VOTE_LIFT = 100"),
    ("raw floor removed", "the unclamped composite no longer has to stand on its own",
     r"RAW_FLOOR_DEFAULT = \d+", "RAW_FLOOR_DEFAULT = 0"),
    ("advance floor to 100", "a yes-vote becomes a perfect score",
     r"ADVANCE_FLOOR = \d+", "ADVANCE_FLOOR = 100"),
    ("default threshold to 0", "the standard tier passes everything",
     r"DEFAULT_THRESHOLD = \d+", "DEFAULT_THRESHOLD = 0"),
    ("reach set emptied", "every reach employer silently downgrades",
     r"REACH = \{[^}]*\}", "REACH = set()"),
    ("incomplete panel tolerated", "averages around a missing persona",
     r"raise IncompletePanel\(\n            \"incomplete panel",
     'raise SystemExit(0) if False else None  # MUTANT: neutered\n        _unused = (\n            "incomplete panel'),
    ("boolean accepted as a score", "True is silently read as a number",
     r"if isinstance\(d, bool\):\n        return None",
     "if isinstance(d, bool):\n        return 100  # MUTANT"),
]


def run_suite(workdir):
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          cwd=workdir, capture_output=True, text=True)


def main():
    base = run_suite(ROOT)
    if base.returncode != 0:
        print("BASELINE IS RED -- fix the suite before checking mutants")
        print(base.stderr[-2000:])
        return 2
    print(f"baseline: GREEN ({base.stderr.strip().splitlines()[-3]})\n")

    survivors = []
    for name, why, pattern, repl in MUTATIONS:
        tmp = tempfile.mkdtemp(prefix="mutant-")
        dst = os.path.join(tmp, "work")
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", ".venv"))
        path = os.path.join(dst, PANEL)
        src = open(path).read()
        mutated, n = re.subn(pattern, repl, src, count=1)
        if n == 0:
            print(f"  [SKIP] {name}: pattern did not match -- the harness is stale")
            survivors.append(name + " (pattern stale)")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        open(path, "w").write(mutated)
        res = run_suite(dst)
        killed = res.returncode != 0
        print(f"  [{'KILLED' if killed else 'SURVIVED'}] {name}  --  {why}")
        if not killed:
            survivors.append(name)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if survivors:
        print(f"FAIL: {len(survivors)} mutant(s) survived -- the suite does not guard them:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"All {len(MUTATIONS)} mutants killed. The suite guards what it claims to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
