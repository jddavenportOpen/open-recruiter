# OpenRecruiter

**A recruiter that lives on your laptop and texts your phone.**

You drop in your history. It builds a master resume out of everything you have
ever done, finds the jobs, writes a resume for that specific job, and puts it all
on a dashboard so you can see where everything sits.

Then it texts you. You say yes. It applies.

If a posting has an essay question, or anything personal, it texts you and waits.
iMessage or Telegram, whichever you want.

The part I like: it runs on my own machine. When something gets stuck I can just
take over.

Free, open source, and about 3 minutes to set up.

> **Not a developer?** Read **[START-HERE.md](START-HERE.md)**. It has a prompt
> you paste into Claude Code and it does the whole setup for you, step by step,
> in plain language.

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

There is no install step and no dependencies beyond Python itself. The engine is
stdlib only, on purpose, so it runs on a laptop you have not configured.

**3 minutes** is the Claude Code path in START-HERE: you paste one prompt and
answer questions as they come. Doing it by hand is closer to 15, mostly waiting
on the texting signup.

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

### Already have a resume in structured form?

Skip the PDF parsing, it is the weakest part of this (see **Known limits**):

```bash
python3 -m openrecruiter.cli import path/to/bank.json
```

---

## How applying works

Every application is one message and one decision.

On iMessage you reply **`Y`** or **`N`**. On Telegram you tap a button.

The card shows you the company, the role, the score, the weakest judge's actual
reason, and the specific claims the resume is making on your behalf. That last
one matters. A card showing a filename and a number turns fifteen seconds of
reading into one second of clicking.

A few things it will not do:

- **An unclear answer is not a yes.** "yes but change the summary" re-asks. So
  does "maybe", and so does silence.
- **A late yes is not a yes.** If the card expired, it asks again.
- **There is no approve-all**, behind any flag, at any tier. A test asserts it
  does not exist. The moment bulk approval exists, the careful version is one
  config line away from your worst instinct at 1am.
- **It will not page you at 3am.** You text it `go` and it replies with the top
  of your queue. Sessions start on your side.

**Real submissions are off until you turn them on.** Out of the box the apply
path runs against a local mock so you can watch the whole loop safely. When you
are ready, set `OPENRECRUITER_ALLOW_REAL_SUBMIT=1`. That default is deliberate,
so nobody fires live applications on day one by accident.

---

## Why there is no "apply to everything" button

Because I tried that first, and it did not work.

> 55 applications in ten days. 11 rejections, 2 throttle notices, **0 interviews.**

The detail that changed my mind: median time to rejection was about 73 hours and
none came back under 24. The resume was not getting filtered by a keyword screen.
It reached actual humans, who read it and passed. More volume was not the missing
ingredient.

And volume cost me something. Two of the thirteen replies were throttle notices,
and the one employer I cared about most **capped me for 180 days after 17
applications in four days.**

Greenhouse's own numbers across 640M applications point the same way:
applications per hire are up 157.7% since 2022, so the marginal application now
converts at roughly 39% of its 2022 rate.

So this thing is built for fewer, better-targeted applications, with a real record
of what happened to each one. Spraying is the thing it is designed not to do.

---

## The tests try to break themselves

Most test suites in this category are decoration. The project this was pulled out
of stayed fully green while I deleted the scoring weights, removed a refusal,
moved a pass threshold to zero, and emptied the reach list.

So `selftest` runs the suite, then deliberately breaks the engine eight ways and
checks the suite goes red each time. A break that survives is reported as a
failure, because a suite that passes with its invariant gone is worse than none.

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

The second one was a real shipped bug. Three "yes" votes passed the standard tier
no matter what the judges actually scored, so a resume scoring zero on every
dimension came back a pass.

---

## Known limits

I would rather you hear these from me than find them.

**Reading styled PDFs is weak.** Against four real two column resumes it found
seven job headers and zero bullets, because the bullet character often does not
survive extraction at all. Worse, every file still reported itself read at 100%.

The fix is free and takes one click: export your resume as plain text (Google Docs
→ File → Download → Plain Text, or Word → Save As → Plain Text) and run `intake`
on that instead. A designed PDF is really a picture of a resume with the text
scattered around it. The same document as `.txt` parses almost perfectly.

Check what `intake` gives you before trusting it, and use `import` if you already
have a clean bank.

**No outcome data yet.** The ledger starts empty and stays honest. I am not going
to borrow someone else's numbers to fill it, and I make no claim that this raises
your callback rate.

**A verified submit to a real employer is still on the roadmap**, not done. Real
submitting works and is flag gated, but I have not yet shipped the end to end
proof of a confirmed submission read back from a real employer's ATS.

---

## Roadmap

| | |
|---|---|
| ✅ | verification engine, texting layer, self-breaking test suite |
| ✅ | experience bank, with per-file confidence and conflict detection |
| ✅ | intake interview, then a pipeline you edit and confirm |
| ✅ | one-at-a-time work loop, paced against your actual Claude usage |
| ✅ | apply and read the confirmation back, against a local mock |
| ✅ | localhost dashboard, token gated and loopback only |
| ✅ | outcomes recorded by code, so "does the score predict anything" is answerable |
| ◻ | intake that survives a two column PDF |
| ◻ | refine goals by text, dictate an essay |
| ◻ | a verified submit to a real employer |

---

## What this is built on

The verification engine is pulled out of
[recruit-copilot](https://github.com/jddavenportOpen/recruit-copilot), which has
no submit path at all and argues that case at length.

This one adds a submit path, and keeps every condition that repo put on it.
The argument there was never against applying. It was against applying without a
gate.

Clone it, fork it, steal pieces, have some fun with it.

## License

MIT.
