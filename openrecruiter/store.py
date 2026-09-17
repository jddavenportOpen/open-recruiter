"""The one persistence contract. Every other module talks to this, not to disk.

Design notes worth reading before extending:

* **SQLite, not JSON.** The ancestor kept a 22 MB JSON file on the dedupe hot
  path and hand-maintained another ledger in prose that no code ever read. A
  queue needs atomic per-item transitions that survive a crash mid-application;
  a JSON blob rewritten wholesale does not provide that.
* **Outcomes are written BY CODE.** The system this was extracted from could not
  answer "did any of this work?" after 110 submissions, because its outcome
  ledger was a promise that a model would maintain a file by hand. It was never
  kept. Here, every state change writes a row, and the outcome question is
  answerable by construction.
* **Illegal transitions raise.** The state machine is enforced in one place
  rather than trusted to each caller.
"""
from __future__ import annotations

import contextlib
import dataclasses
import enum
import json
import os
import sqlite3
import time


class State(str, enum.Enum):
    """The lifecycle of one application.

    There is deliberately no bulk verb and no state meaning "approved in a
    batch". Approval is per application, always.
    """
    DISCOVERED = "discovered"        # scout found it
    SCREENED_OUT = "screened_out"    # failed the fit gate, never built
    BUILDING = "building"            # tailoring a resume
    BELOW_BAR = "below_bar"          # built, failed its tier's gate -- never offered
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECLINED = "declined"            # the human said no
    EXPIRED = "expired"              # consent window passed, re-ask
    SUBMITTING = "submitting"
    SUBMITTED_VERIFIED = "submitted_verified"      # confirmation read back
    SUBMITTED_UNVERIFIED = "submitted_unverified"  # may have sent; held for a human
    FAILED = "failed"


# A resume that never cleared its bar is never offered for approval: the gate
# must not be a suggestion the human rubber-stamps past.
LEGAL: dict[State, set[State]] = {
    State.DISCOVERED: {State.SCREENED_OUT, State.BUILDING},
    State.SCREENED_OUT: set(),
    State.BUILDING: {State.BELOW_BAR, State.AWAITING_APPROVAL, State.FAILED},
    State.BELOW_BAR: {State.BUILDING},          # only via an explicit rebuild
    State.AWAITING_APPROVAL: {State.APPROVED, State.DECLINED, State.EXPIRED},
    State.EXPIRED: {State.AWAITING_APPROVAL},   # re-ask, never auto-honour
    State.APPROVED: {State.SUBMITTING},
    State.DECLINED: set(),
    State.SUBMITTING: {State.SUBMITTED_VERIFIED, State.SUBMITTED_UNVERIFIED, State.FAILED},
    State.SUBMITTED_VERIFIED: set(),
    State.SUBMITTED_UNVERIFIED: {State.SUBMITTED_VERIFIED},  # a later receipt can confirm
    State.FAILED: {State.BUILDING},
}

TERMINAL = {State.SCREENED_OUT, State.DECLINED, State.SUBMITTED_VERIFIED}


class IllegalTransition(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
  id            TEXT PRIMARY KEY,
  company       TEXT NOT NULL,
  role          TEXT NOT NULL,
  url           TEXT NOT NULL,
  ats           TEXT,
  tier          TEXT NOT NULL DEFAULT 'standard',
  state         TEXT NOT NULL,
  score         REAL,
  raw_score     REAL,
  weakest       TEXT,
  weakest_reason TEXT,
  claims        TEXT,
  resume_path   TEXT,
  card_id       TEXT,
  channel       TEXT,
  approved_at   REAL,
  first_seen    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS applications_url ON applications(url);
CREATE INDEX IF NOT EXISTS applications_state ON applications(state);

-- Append-only. This is how "does the score predict anything?" becomes answerable.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id     TEXT NOT NULL,
  at         REAL NOT NULL,
  from_state TEXT,
  to_state   TEXT NOT NULL,
  note       TEXT,
  FOREIGN KEY(app_id) REFERENCES applications(id)
);
CREATE INDEX IF NOT EXISTS events_app ON events(app_id);

CREATE TABLE IF NOT EXISTS outcomes (
  app_id     TEXT PRIMARY KEY,
  outcome    TEXT NOT NULL,       -- rejected|screen|interview|offer|hired|no_response|withdrawn
  at         REAL NOT NULL,
  days_to    REAL,
  note       TEXT,
  FOREIGN KEY(app_id) REFERENCES applications(id)
);

CREATE TABLE IF NOT EXISTS goals (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


@dataclasses.dataclass
class Application:
    id: str
    company: str
    role: str
    url: str
    state: State
    ats: str | None = None
    tier: str = "standard"
    score: float | None = None
    raw_score: float | None = None
    weakest: str | None = None
    weakest_reason: str | None = None
    claims: list[str] = dataclasses.field(default_factory=list)
    resume_path: str | None = None
    card_id: str | None = None
    channel: str | None = None
    approved_at: float | None = None
    meta: dict = dataclasses.field(default_factory=dict)


def default_path() -> str:
    home = os.environ.get("OPENRECRUITER_HOME") or os.path.expanduser("~/.openrecruiter")
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "openrecruiter.db")


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        with contextlib.suppress(Exception):
            self.db.close()

    # -- applications ---------------------------------------------------------
    def upsert_discovered(self, app_id: str, company: str, role: str, url: str,
                          ats: str | None = None, tier: str = "standard",
                          meta: dict | None = None) -> bool:
        """Record a posting. Returns True if it is NEW.

        Keyed on url so a re-scout cannot re-offer something already decided --
        the ancestor's scout overwrote its jobs file wholesale with no stable id,
        so "what changed since last run" was not computable at all.
        """
        now = time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO applications"
            " (id, company, role, url, ats, tier, state, first_seen, updated_at, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (app_id, company, role, url, ats, tier, State.DISCOVERED.value,
             now, now, json.dumps(meta or {})))
        if cur.rowcount:
            self._event(app_id, None, State.DISCOVERED, "discovered")
            return True
        return False

    def get(self, app_id: str) -> Application | None:
        r = self.db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return _row_to_app(r) if r else None

    def by_state(self, *states: State) -> list[Application]:
        qs = ",".join("?" * len(states))
        rows = self.db.execute(
            f"SELECT * FROM applications WHERE state IN ({qs}) ORDER BY score DESC, first_seen ASC",
            [s.value for s in states]).fetchall()
        return [_row_to_app(r) for r in rows]

    def next_to_build(self) -> Application | None:
        apps = self.by_state(State.DISCOVERED)
        return apps[0] if apps else None

    def transition(self, app_id: str, to: State, note: str = "", **fields) -> Application:
        """Move one application. Refuses an illegal hop rather than recording it.

        Enforced HERE and not per-caller, so a new call site cannot invent a path
        that skips the gate -- e.g. BUILDING straight to SUBMITTING, which would
        submit a resume that never faced the panel.
        """
        app = self.get(app_id)
        if app is None:
            raise KeyError(app_id)
        if to not in LEGAL[app.state]:
            raise IllegalTransition(
                f"{app_id}: {app.state.value} -> {to.value} is not a legal transition "
                f"(legal: {sorted(s.value for s in LEGAL[app.state]) or 'none, terminal'})")
        sets, vals = ["state=?", "updated_at=?"], [to.value, time.time()]
        for k, v in fields.items():
            if k == "claims":
                v = json.dumps(v)
            elif k == "meta":
                v = json.dumps(v)
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(app_id)
        self.db.execute(f"UPDATE applications SET {','.join(sets)} WHERE id=?", vals)
        self._event(app_id, app.state, to, note)
        return self.get(app_id)

    def _event(self, app_id, frm: State | None, to: State, note: str):
        self.db.execute(
            "INSERT INTO events (app_id, at, from_state, to_state, note) VALUES (?,?,?,?,?)",
            (app_id, time.time(), frm.value if frm else None, to.value, note))

    def history(self, app_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE app_id=? ORDER BY id", (app_id,)).fetchall()]

    # -- outcomes -------------------------------------------------------------
    def record_outcome(self, app_id: str, outcome: str, note: str = "") -> None:
        app = self.get(app_id)
        if app is None:
            raise KeyError(app_id)
        sub = self.db.execute(
            "SELECT at FROM events WHERE app_id=? AND to_state IN (?,?) ORDER BY id LIMIT 1",
            (app_id, State.SUBMITTED_VERIFIED.value, State.SUBMITTED_UNVERIFIED.value)).fetchone()
        days = ((time.time() - sub["at"]) / 86400.0) if sub else None
        self.db.execute(
            "INSERT INTO outcomes (app_id, outcome, at, days_to, note) VALUES (?,?,?,?,?)"
            " ON CONFLICT(app_id) DO UPDATE SET outcome=excluded.outcome, at=excluded.at,"
            " days_to=excluded.days_to, note=excluded.note",
            (app_id, outcome, time.time(), days, note))

    def score_vs_outcome(self) -> dict:
        """The question the ancestor could never answer.

        Returns counts and mean score per outcome. Reports `n` honestly so a
        caller cannot mistake three data points for a finding.
        """
        rows = self.db.execute(
            "SELECT o.outcome, COUNT(*) n, AVG(a.score) mean_score, AVG(o.days_to) mean_days"
            " FROM outcomes o JOIN applications a ON a.id=o.app_id GROUP BY o.outcome").fetchall()
        return {r["outcome"]: {"n": r["n"],
                               "mean_score": round(r["mean_score"], 1) if r["mean_score"] else None,
                               "mean_days": round(r["mean_days"], 1) if r["mean_days"] else None}
                for r in rows}

    # -- goals ----------------------------------------------------------------
    def save_goals(self, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO goals (id, payload, updated_at) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (json.dumps(payload), time.time()))

    def load_goals(self) -> dict | None:
        r = self.db.execute("SELECT payload FROM goals WHERE id=1").fetchone()
        return json.loads(r["payload"]) if r else None

    def stats(self) -> dict:
        rows = self.db.execute(
            "SELECT state, COUNT(*) n FROM applications GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}


def _row_to_app(r: sqlite3.Row) -> Application:
    return Application(
        id=r["id"], company=r["company"], role=r["role"], url=r["url"],
        state=State(r["state"]), ats=r["ats"], tier=r["tier"],
        score=r["score"], raw_score=r["raw_score"], weakest=r["weakest"],
        weakest_reason=r["weakest_reason"],
        claims=json.loads(r["claims"]) if r["claims"] else [],
        resume_path=r["resume_path"], card_id=r["card_id"], channel=r["channel"],
        approved_at=r["approved_at"], meta=json.loads(r["meta"] or "{}"))
