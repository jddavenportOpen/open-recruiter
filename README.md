# OpenRecruiter

*A recruiter that lives on your computer and texts your phone.*

It learns what job you want by talking to you, builds the pipelines to find those
jobs, tailors a resume per posting, proves a machine can still read it — then
**asks you before it applies, one application at a time.**

Runs on your own machine, on your own Claude subscription, through your own
messaging account. There is no server of ours in the path and nothing to sign up
for that costs money.

---

## Status: early. Read this before you fork.

**What works today** — the verification engine and the approval rail:

- the zero-dependency PDF stack (typeset → measure the rendered page → read the
  text back out and diff it)
- the three-persona panel and the tiered pass/fail gate
- the channel layer: Telegram and SendBlue adapters behind one interface
- 55 invariant tests, plus a mutation harness that proves they are not decoration

**What is not built yet**: the intake interview, the job scout, the work loop,
the apply path, the dashboard, and outcome tracking. The roadmap is real work,
not a wish list — see [WORKPLAN](#roadmap) below.

**We make no claim that this improves your callback rate.** We have one honest
baseline and it is not flattering (below), and nothing in this repo will pretend
otherwise until there is outcome data to look at.

---

## Why it does not have an "apply to everything" button

The system this was extracted from ran the experiment already:

> **55 applications in ten days → 11 rejections, 2 throttle notices, 0 interviews, 0 screens.**

The detail that matters: the median time-to-rejection was ~73 hours and **none
was under 24**. The resume was not filtered out by a keyword screen. It reached
humans, who read it and declined. Volume was not the missing ingredient.

And volume had a price. Two of the thirteen replies were throttle notices, and
one employer — the single most important one on that list — **capped the applicant
for 180 days after 17 submissions in four days.**

Stated fairly, that is not proof that volume applying fails for everyone. A third
of that wave went out *ungraded*, and the quality bar had already been lowered
twice to unstick a deadlock. But combined with Greenhouse's own 640M-application
benchmark — **applications per hire up 157.7% since 2022**, so the marginal
application now converts at roughly 39% of its 2022 rate — the direction is clear
enough to design against.

So: **fewer, better-targeted applications, and a working record of what happened.**
There is no volume knob, and there is no `approve-all`. A test asserts the absence.

---

## The approval gate

Every application is a separate message and a separate decision.

- **Telegram** — an inline button. The callback is HMAC-signed and bound to
  *(application, decision)*, so a tap on one application is not a valid token for
  another.
- **SendBlue** — a real iMessage/SMS thread on your own number. Approval is a
  reply: **`Y`** or **`N`**.

Some deliberate constraints:

- **An ambiguous reply is not consent.** "yes but change the summary" re-asks. So
  does "maybe", and so does silence.
- **Consent is perishable.** A `Y` that arrives after the card expired is
  re-asked, never honoured.
- **There is no batch approval, at any tier, behind any flag.** Not `approve-all`,
  not `auto-approve above N`. The moment a tool can approve in bulk, the careful
  tier is one config value away from your worst instinct at 1am.
- **Reach employers are never auto-submitted.** A standard employer is a
  repeatable event; a reach employer is close to one-shot.

The card shows the company, the role, the tier, the score, **the weakest judge's
actual reason**, and **the specific claims the resume is asserting** — because a
card that shows a filename and a number turns fifteen seconds of *reading* into
one second of *clicking*.

### A note on tapbacks

You cannot approve by thumbs-up on SendBlue, and this is not an oversight. Their
Reactions API accepts only *inbound* targets — a request naming an outbound
message returns **422** — and no webhook they emit carries a reaction field. So a
tapback on a card we sent is not observable. Reply keywords are.

(A local macOS adapter that *can* read a real tapback is on the roadmap and is
deliberately last: Full Disk Access has several silent failure modes, and the
failure presents as a hang rather than an error.)

---

## Install

```bash
git clone https://github.com/jddavenportOpen/open-recruiter.git
cd open-recruiter
python3 -m openrecruiter.cli doctor      # says exactly what is missing
python3 -m openrecruiter.cli selftest    # invariant suite + mutation check, offline
```

No install step, no dependencies. The engine is stdlib-only by policy so it runs
in CI, in a cron job, and on a laptop you have not configured.

### Messaging

Pick either, or run both.

**Telegram** — talk to `@BotFather`, then:
```bash
export TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...
```

**SendBlue** — their sandbox is **$0 and needs no credit card**, and
`--phone` auto-verifies your own number, which is the only contact a personal
recruiter needs:
```bash
npm i -g @sendblue/cli && sendblue setup --phone +1XXXXXXXXXX
export SENDBLUE_API_API_KEY=... SENDBLUE_API_API_SECRET=...
export SENDBLUE_FROM_NUMBER=+1... SENDBLUE_TO_NUMBER=+1...
```

**Your credentials stay on your machine.** We never proxy them, never hold them,
and there is no account with us.

---

## The self-test checks itself

Most test suites in this category are decoration. The one this repo was forked
from stayed fully green while you deleted the dimension weighting, removed the
incomplete-panel refusal, moved the advance floor to 100, dropped the pass
threshold to zero, and emptied the reach list.

So `selftest` runs the suite **and then deliberately breaks the engine eight
different ways and asserts the suite goes red.** A mutation that survives is
reported as a failure, because a suite that passes while its invariant is gone is
worse than no suite.

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

That second one was a real, shipped bug: an unbounded vote-coupling floor meant
three "yes" votes passed the standard tier **no matter what the judges scored** —
a resume scoring zero on every dimension came back as a pass. Anything wired to
that verdict would have been submitting on three booleans.

---

## Roadmap

| | |
|---|---|
| ✅ | verification engine, channel layer, invariant + mutation suite |
| ◻ | experience bank from resumes you already have, with per-file extraction confidence |
| ◻ | the intake interview → a proposed pipeline you edit and confirm |
| ◻ | the one-at-a-time work loop, paced against your actual Claude quota |
| ◻ | the conversational surface — ask it why, refine goals by text, dictate an essay |
| ◻ | apply + read the confirmation back ("we clicked submit" is not "we sent it") |
| ◻ | a localhost dashboard the agent keeps current |
| ◻ | outcomes recorded **by code**, so "does the score predict anything?" becomes answerable |

---

## Prior art, and what this owes it

The verification engine here is extracted from
[recruit-copilot](https://github.com/jddavenportOpen/recruit-copilot), which
deliberately has **no submit path at all** and argues for that position at length.

This project adds one, and it inherits that repo's conditions rather than
inventing its own — because the argument there was never against submission, it
was against *ungated* submission. Every constraint in "The approval gate" above
comes from it.

Three of the four comparable open-source projects shipped in the last five months
also refuse to press submit. That convergence is worth taking seriously.

## License

MIT.
