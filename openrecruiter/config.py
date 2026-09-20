"""Where settings come from, and what to tell the user to type next.

Two small problems lived here, and both of them ended a non-technical setup
without an error message worth reading:

  * START-HERE told people to keep their keys in a `.env` file. Nothing read
    one. The keys survived until the terminal closed, `doctor` then said "no
    messaging rail configured", and there was no hint anywhere that the file
    had never been opened.
  * The CLI printed `Next: openrecruiter setup` after every step. That command
    only exists if the package was pip-installed, and the documented setup
    deliberately has no install step. So the guided path handed the reader a
    `command not found` at each handoff, in the tool's own voice.
"""
from __future__ import annotations

import dataclasses
import os
import sys

ENV_FILENAME = ".env"


@dataclasses.dataclass(frozen=True)
class DotenvResult:
    """What `load_dotenv` did, so `doctor` can say it out loud rather than
    leaving the reader to infer it from a missing rail."""
    path: str | None = None
    applied: tuple[str, ...] = ()
    already_set: tuple[str, ...] = ()
    bad_lines: tuple[int, ...] = ()
    error: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None


def candidate_paths() -> list[str]:
    """Where a `.env` is looked for, in order.

    The current directory first, because that is where someone following
    START-HERE is standing. Then the repo root, so the file keeps working after
    a `cd` somewhere else. `OPENRECRUITER_ENV` overrides both and is the only
    way to point at a file with another name.
    """
    explicit = os.environ.get("OPENRECRUITER_ENV")
    if explicit:
        return [os.path.expanduser(explicit)]
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return [os.path.abspath(ENV_FILENAME), os.path.join(here, ENV_FILENAME)]


def parse_dotenv(text: str) -> tuple[dict, tuple[int, ...]]:
    """KEY=VALUE per line. Returns the pairs and the line numbers it could not read.

    Deliberately forgiving about `export`, quotes and blank lines: the docs say
    to write the lines without `export`, and a reader who pastes the earlier
    shell block anyway is not wrong enough to deserve silence. Inline `#` is NOT
    treated as a comment, because an API secret is allowed to contain one and
    truncating a key at a character the user cannot see is worse than a comment
    that does not work.
    """
    pairs: dict = {}
    bad: list[int] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or any(c.isspace() for c in key):
            bad.append(n)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs[key] = value
    return pairs, tuple(bad)


def load_dotenv(*, environ=None) -> DotenvResult:
    """Load the first `.env` found into the environment. Never raises.

    A variable already present in the real environment WINS. Someone who just
    ran `export SENDBLUE_API_API_KEY=...` to test a new key would otherwise be
    silently served a stale one out of a file they forgot about, and the symptom
    of that is an auth error that points at the wrong thing entirely.
    """
    env = os.environ if environ is None else environ
    for path in candidate_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            return DotenvResult(path=path, error=str(e))
        pairs, bad = parse_dotenv(text)
        applied, skipped = [], []
        for k, v in pairs.items():
            if k in env:
                skipped.append(k)
            else:
                env[k] = v
                applied.append(k)
        return DotenvResult(path=path, applied=tuple(applied),
                            already_set=tuple(skipped), bad_lines=bad)
    return DotenvResult()


def invocation() -> str:
    """The command prefix the reader actually typed, so every `Next:` is copyable.

    Hardcoding `openrecruiter` assumed an install the documented setup does not
    do. Hardcoding `python3 -m openrecruiter.cli` would be wrong for anyone who
    DID pip-install it, and wrong about the interpreter for the Homebrew 3.12
    that START-HERE tells most Mac users to install. Ask the running process
    instead: it knows both.
    """
    argv0 = os.path.basename(sys.argv[0] or "")
    # `-c`, `-m` and an empty argv[0] all mean "not launched as a named script".
    # Returning them would print `Next: -c setup`, which is worse than the bug
    # this function exists to fix.
    if argv0 and not argv0.startswith("-") and not argv0.endswith(".py"):
        return argv0
    exe = os.path.basename(sys.executable or "") or "python3"
    return f"{exe} -m openrecruiter.cli"


def ensure_home() -> str:
    """Make sure the state directory exists.

    So that `touch ~/.openrecruiter/STOP`, which START-HERE offers as the
    emergency brake, works the FIRST time someone tries it. It used to fail
    until some other command had created the directory, which means the one
    command a nervous reader tests early was the one that errored.
    """
    from openrecruiter import store
    return os.path.dirname(store.default_path())
