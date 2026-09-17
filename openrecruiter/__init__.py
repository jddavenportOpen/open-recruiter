"""OpenRecruiter — a recruiter that lives on your computer and texts your phone."""
import sys

__version__ = "0.1.0"

# Checked HERE, before any submodule imports, because the failure without it is a
# TypeError raised from inside an unrelated import -- which tells a new user
# neither what is wrong nor what to do. Stock macOS still ships 3.9, so this is
# the first wall a stranger hits, not a theoretical one.
MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    have = ".".join(str(x) for x in sys.version_info[:3])
    raise SystemExit(
        f"\nOpenRecruiter needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer. "
        f"You have {have}.\n\n"
        f"macOS still ships 3.9, so this is normal on a fresh Mac. Either:\n"
        f"    brew install python@3.12    then re-run with:  python3.12 -m openrecruiter.cli ...\n"
        f"or install from https://www.python.org/downloads/\n")
