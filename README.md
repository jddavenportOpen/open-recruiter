# OpenRecruiter

**A recruiter that lives on your laptop and texts your phone.**

I hate filling in job applications, so I automated the whole thing and made an
agent I text to get it done.

**130 applications. 5 interviews. I have not filled out a single one myself.**

It runs on my laptop and texts my phone. Always working. Six months of toying
with it, and it is open source and free.

---

## How it works

You drop in your context. It builds a master resume out of everything you have
ever done, finds the jobs, writes a resume for that specific job, and spins up a
dashboard so you can see where everything sits.

Then it texts you. You say yes. It applies.

If there is an essay question, or anything personal, it texts you and waits.
iMessage or Telegram, whichever you want.

The part I like is that it lives on my laptop. So when something gets stuck I can
just take over.

> **Not a developer?** Read **[START-HERE.md](START-HERE.md)**. It has a prompt
> you paste into Claude Code and it does the whole setup for you, in plain
> language, about 3 minutes.

---

## Setup

You need Python 3.10 or newer. macOS still ships 3.9, so on a fresh Mac run
`brew install python@3.12` first and use `python3.12` below.

```bash
git clone https://github.com/jddavenportOpen/open-recruiter.git
cd open-recruiter
python3 -m openrecruiter.cli doctor      # tells you exactly what is missing
python3 -m openrecruiter.cli selftest    # runs offline, takes a few seconds
```

No install step, and no dependencies beyond Python itself. The engine is stdlib
only on purpose, so it runs on a laptop you have not configured.

About 3 minutes with the Claude Code prompt in START-HERE, since it does the work
and just asks you questions. Closer to 15 by hand, and most of that is waiting on
the texting signup.

### Pick a texting rail

**iMessage / SMS** through SendBlue. Free sandbox, no credit card.

```bash
npm i -g @sendblue/cli
sendblue setup --phone +1XXXXXXXXXX      # texts you a code, that is the whole signup
export SENDBLUE_API_API_KEY=...  SENDBLUE_API_API_SECRET=...
export SENDBLUE_FROM_NUMBER=+1...        # the number SendBlue gave you
export SENDBLUE_TO_NUMBER=+1...          # your phone
```

**Telegram** if you prefer tappable buttons. Talk to `@BotFather`, then:

```bash
export TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...
```

Your credentials stay on your machine. Nothing is proxied through a server of
mine, and there is no account to make.

### Already have your history in structured form?

Skip the PDF parsing:

```bash
python3 -m openrecruiter.cli import path/to/bank.json
```

---

## Applying, one at a time

Every application is one message and one decision. On iMessage you reply **`Y`**
or **`N`**. On Telegram you tap a button.

The card shows the company, the role, the score, the weakest judge's actual
reason, and the specific claims the resume is making on your behalf. That last
part is the point. A card showing a filename and a number turns fifteen seconds
of reading into one second of clicking.

A few things it deliberately will not do:

- **An unclear answer is not a yes.** "yes but change the summary" re-asks. So
  does "maybe", and so does silence.
- **A late yes is not a yes.** If the card expired, it asks again.
- **There is no approve-all**, behind any flag, at any tier. A test asserts it
  does not exist. The moment bulk approval exists, the careful version is one
  config line away from your worst instinct at 1am.
- **It will not page you at 3am.** You text it `go` and it hands you the top of
  your queue. Sessions start on your side.

This is built for fewer, better-targeted applications with a real record of what
happened to each one. Spraying is the thing it is designed not to do.

**Real submissions are off until you turn them on.** Out of the box the apply
path runs against a local mock so you can watch the whole loop safely. When you
are ready, set `OPENRECRUITER_ALLOW_REAL_SUBMIT=1`. That default exists so nobody
fires live applications on day one by accident.

---

## The tests try to break themselves

Most test suites in this category are decoration. So `selftest` runs the suite,
then deliberately breaks the engine eight ways and checks the suite goes red each
time. A break that survives is reported as a failure, because a suite that passes
with its invariant gone is worse than no suite.

```
  [KILLED] weighting deleted
  [KILLED] vote-coupling unbounded
  [KILLED] raw floor removed
  [KILLED] advance floor to 100
  [KILLED] default threshold to 0
  [KILLED] reach set emptied
  [KILLED] incomplete panel tolerated
  [KILLED] boolean accepted as a score
```

The second one caught a real bug. Three "yes" votes were passing the standard
tier no matter what the judges actually scored, so a resume scoring zero on every
dimension came back a pass.

---

## One tip that will save you

**Feed it plain text, not a designed PDF.** A styled two column resume is really
a picture of a resume with the text scattered around it, and the bullet character
usually does not survive extraction at all.

Export it first (Google Docs → File → Download → Plain Text, or Word → Save As →
Plain Text) and run `intake` on that. The same document as `.txt` parses close to
perfectly. If you already have a clean structured bank, use `import` instead.

---

## Roadmap

| | |
|---|---|
| ✅ | verification engine, texting layer, self-breaking test suite |
| ✅ | experience bank, with per-file confidence and conflict detection |
| ✅ | intake interview, then a pipeline you edit and confirm |
| ✅ | one-at-a-time work loop, paced against your actual Claude usage |
| ✅ | apply, and read the confirmation back |
| ✅ | localhost dashboard, token gated and loopback only |
| ✅ | outcomes recorded by code |
| ◻ | intake that survives a two column PDF |
| ◻ | refine goals by text, dictate an essay |

---

## What this is built on

The verification engine is pulled out of
[recruit-copilot](https://github.com/jddavenportOpen/recruit-copilot), which has
no submit path at all and argues that case at length.

This one adds a submit path and keeps every condition that repo put on it. The
argument there was never against applying. It was against applying without a
gate.

Clone it, fork it, steal pieces, have some fun with it.

## License

MIT.
