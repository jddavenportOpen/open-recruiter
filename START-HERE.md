# Start here

This is the plain-English setup. No experience assumed.

**About 3 minutes** if you use the Claude Code prompt below, since it does the
work and just asks you questions. Closer to 15 if you do it by hand, and most
of that is waiting on the texting signup.

**What you are setting up:** a program that lives on your own computer, finds jobs
you actually want, writes a resume for each one, and then **texts you and waits**.
It does not apply to anything until you reply. There is no "apply to everything"
button, on purpose.

**What it costs:** nothing to me, and nothing for the tool itself. You need a Claude subscription (Pro or Max),
which you may already have. The texting service has a free tier that does not ask
for a card.

---

## The easy way: let Claude Code do it

If you have [Claude Code](https://claude.com/claude-code), open your terminal, type
`claude`, and paste this in:

```
Set up OpenRecruiter for me. I am not a developer, so explain what you are doing
in plain language and tell me when you need something from me.

1. Clone https://github.com/jddavenportOpen/open-recruiter.git into my home folder
   and read its README and START-HERE.md.
2. It needs Python 3.10 or newer. Check what I have. macOS ships 3.9, so if that
   is what I have, install a newer one with Homebrew and use that everywhere.
3. Run `doctor`, then `selftest`. Selftest must finish with "All 8 mutants
   killed". If it does not, stop and tell me what happened.
4. Set up my texting rail. Ask me which I want: SendBlue (a normal text thread on
   my own phone number, free sandbox, no card) or Telegram (buttons I tap, needs
   the Telegram app). Walk me through whichever I pick, step by step, then copy
   .env.example to .env and put the keys in there. Re-run doctor until it is
   green, and confirm doctor says it loaded my .env.
5. Ask me for my resume. If I have a folder of resumes, run `intake` on it and
   tell me honestly how well it read them. If it reads them badly, say so.
6. Run `setup` and ask me the questions it asks. Then help me check the board
   tokens in pipeline.json, because those are guesses. Then run `scan`.
7. Run `run --once` and tell me what happened: the score, what the judges said,
   and whether a card reached my phone.

Do not set OPENRECRUITER_ALLOW_REAL_SUBMIT. I do not want anything sent to a real
employer until I have seen how this works.
```

That is the whole thing. Skip to **[What happens next](#what-happens-next)**.

---

## The manual way

### 1. Open a terminal

On a Mac, press `Cmd+Space`, type `Terminal`, press enter. That black window is
where everything below goes. Copy a line, paste it, press enter, wait.

### 2. Check your Python

```bash
python3 --version
```

If that says **3.10 or higher**, you are fine, use `python3` everywhere below.

If it says **3.9** (normal on a Mac), get a newer one:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

Then use `python3.12` instead of `python3` everywhere below.

### 3. Download it

```bash
git clone https://github.com/jddavenportOpen/open-recruiter.git
cd open-recruiter
python3 -m openrecruiter.cli doctor
```

`doctor` tells you what is missing. It will complain about messaging, that is the
next step.

### 4. Set up texting

Pick one. SendBlue is a normal text message on your own number. Telegram is an app
with tappable buttons.

**SendBlue** (free sandbox, no credit card):

```bash
npm i -g @sendblue/cli
sendblue setup --phone +1YOURNUMBER
```

It texts you a code to prove the number is yours. That is the entire signup. It
then gives you two keys. Put them somewhere safe and set them:

```bash
export SENDBLUE_API_API_KEY=the-first-key
export SENDBLUE_API_API_SECRET=the-second-key
export SENDBLUE_FROM_NUMBER=+1the-number-sendblue-gave-you
export SENDBLUE_TO_NUMBER=+1your-own-phone
```

You will approve applications by texting back **Y** (yes, apply) or **N** (skip).

**Telegram** (buttons):

In the Telegram app, start a chat with **@BotFather** and use `/newbot`. It gives
you a token. Then open a chat with your new bot and say anything to it. Visit
`https://api.telegram.org/bot<YOUR-TOKEN>/getUpdates` in a browser and find the
number after `"id":` in the `chat` part.

```bash
export TELEGRAM_BOT_TOKEN=your-token
export TELEGRAM_CHAT_ID=the-number-you-found
```

Run `doctor` again. It should be green.

> Those `export` lines only last until you close the terminal. To keep them:
>
> ```bash
> cp .env.example .env
> ```
>
> and fill in your values in that file, one per line. `doctor` tells you which
> file it loaded and how many settings it got out of it. If you already typed
> `export` for something in this terminal, that wins over the file, and `doctor`
> says so, which means a key that looks ignored is never a mystery. `.env` is
> gitignored, so your keys cannot be committed by accident.

### 5. Give it your history

It needs to know what you have done. Put your existing resumes in a folder and:

```bash
python3 -m openrecruiter.cli intake ~/Documents/my-resumes
```

It will tell you what it could and could not read. **Read that output.** If it
says a file was read poorly, or that it found no bullet points, believe it.

> ### The single biggest thing you can do here
>
> **If your resume lives in Google Docs, do not give it a PDF.** In Google Docs
> choose **File → Download → Plain Text (.txt)** and put that in the folder
> instead.
>
> A designed, two-column PDF is a picture of a resume with the text scattered
> around it. Half the time the bullet characters are not even in the file, they
> are in a font that comes back as nothing, and everything under a job heading
> vanishes. A `.txt` export has none of that problem and parses close to
> perfectly. Same words, one menu click, enormously better result.
>
> Microsoft Word: **File → Save As → Plain Text**. Pages: **File → Export To →
> Plain Text**.

### 6. Tell it what you want

```bash
python3 -m openrecruiter.cli setup
```

Eight questions: what job titles, how senior, what industry, where, remote or not,
minimum pay, company size, and anything that is an automatic no.

It then proposes a list of companies. **Every company's "token" is a guess**, and a
wrong guess means that company finds nothing. Open the file it names
(`pipeline.json`), look at the list, and delete or fix anything wrong.

```bash
python3 -m openrecruiter.cli scan
```

This goes and gets the open jobs. If a company fails, it says so with the exact
address it tried. A company that is broken is never reported as "no jobs".

### 7. Do one

```bash
python3 -m openrecruiter.cli run --once
```

Takes two or three minutes. Then check your phone.

---

## What happens next

You get one message per job, and only one at a time. It says the company, the
role, the score, **the harshest judge's actual reason**, and the specific claims
the resume is making on your behalf. Read that last part, it is the whole point.

Reply **Y** to apply or **N** to skip. Or tap the button, on Telegram.

**Nothing is sent to a real employer yet.** That is switched off until you turn it
on deliberately. Until then it practises against a fake employer running on your
own machine, so you can see the whole thing work without risking anything.

### To stop it, at any time

```bash
touch ~/.openrecruiter/STOP
```

It checks that before every single application and cannot un-stop itself.

### To see what is going on

```bash
python3 -m openrecruiter.cli dashboard    # a page in your browser
python3 -m openrecruiter.cli outcomes     # did any of this work?
```

`outcomes` will be empty at first. It stays honest about that rather than showing
you an encouraging number that means nothing.

---

## When something breaks

It is designed to fail loudly rather than pretend. If something is wrong you
should get a sentence saying what and why, not a wall of red text. If you get the
wall of red text instead, that is a bug worth
[reporting](https://github.com/jddavenportOpen/open-recruiter/issues), paste the
last twenty lines.

Two known ones:

- **"needs Python 3.10 or newer"** step 2 above.
- **intake found no bullet points** your resume is probably a designed,
  multi-column PDF. Try a plain-text or simply-formatted version, or if you
  already have a structured resume file: `python3 -m openrecruiter.cli import
  yourfile.json`.
