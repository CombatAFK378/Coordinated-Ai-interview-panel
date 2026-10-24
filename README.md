# 968ms — Coordinated AI Interview Panel

> **The hiring verdict you can cross-examine.**

A live, voice-first AI interview panel. Five AI interviewers and a host interview a
candidate together over real-time voice, each probing a different side of the role.
A deterministic formula decides who speaks next. Each interviewer scores privately;
the scores are locked, hashed and debated into a recommendation. Afterwards a
recruiter can **join the completed interview's panel** — right after it ends or any
time later — and **talk to the interviewers by voice** about their verdict, in the
browser or on a **phone call**, or chat with them on **WhatsApp**. Every interviewer
answers in their own voice (on WhatsApp, under their own name) from the locked
record, never from imagination.

---

## Contents

- [What it does](#what-it-does)
- [How an interview works](#how-an-interview-works)
- [The panel](#the-panel)
- [Ask the Panel](#ask-the-panel)
- [Design principles](#design-principles)
- [Architecture](#architecture)
- [Getting started](#getting-started)
- [Using it](#using-it)

---

## What it does

- **A real panel, not one bot.** Hiring Manager, Technical, Product, Customer and
  Coding interviewers, plus Julian the host — six distinct voices in one room.
- **Built for the role.** Upload a job description and résumé; the panel is seated
  for that role (a backend role drops Product and Customer), competencies are
  weighted, and résumé claims are registered as things to verify.
- **Explainable turn-taking.** Every turn, all interviewers bid in parallel; a
  formula — not an LLM — picks who asks next, and every decision is logged with the
  numbers behind it.
- **An evidence ledger.** Claims the candidate makes are extracted live, tagged by
  competency, rated SOLID or VAGUE, and linked when they contradict each other or
  the résumé.
- **A live coding round.** Liam sets a problem and watches the candidate's shared
  screen in real time (Gemini Live), talking them through it — and ends the round
  if an AI assistant shows up on screen.
- **A defensible verdict.** Blind, independent scoring per interviewer; a SHA-256
  lock over the record; a debate where a confident interviewer cannot be talked out
  of their score; a conclusion that reports the split instead of averaging it.
- **Talk to the panel afterwards.** A recruiter can join any completed interview's
  panel — straight after it, or days later — and talk to the interviewers by voice,
  in the browser or over a phone call, or chat with them on WhatsApp. Answers come
  only from the locked record; recruiter overrides are logged next to the original
  call, which never changes.

## How an interview works

1. **Dossier.** The JD and résumé are parsed once into a dossier: role, seniority,
   which interviewers to seat, competency weights, a rubric per interviewer, and
   résumé claims as UNVERIFIED ledger entries.
2. **Cold open.** Julian discloses that every interviewer is AI and that the session
   is recorded, introduces the panel, and Maya opens with a broad, low-stakes
   question. (Skippable.)
3. **Bidding and floor control.** After each answer, every seated interviewer bids
   in parallel (how much they want the next question) and extracts the claims they
   noticed. The next speaker is chosen by:

   ```
   priority = interest × (1 + λ · coverage_gap) × recency_penalty
   ```

   `λ` rises over the interview so late questions chase what is still unknown;
   recency stops one interviewer dominating; ties break on fixed rules — never at
   random. Every floor grant is written to the audit log.
4. **Coding round.** If Liam is on the panel, the coding round starts by the
   candidate's fifth answer at the latest: share the whole screen, solve a classic
   problem out loud, and Liam reacts to what he sees.
5. **Lock, debate, conclusion.** On *Finish and score*, each interviewer scores only
   their competencies from the transcript and ledger, in isolation. Conviction is
   computed in code (`STRONG` if far from neutral with ≥ 3 pieces of evidence). The
   record is hashed, then the panel debates — a STRONG interviewer's attempt to move
   is rejected in code — and Julian writes a conclusion: **PROCEED**, **PROCEED
   FLAGGED**, **INSUFFICIENT SIGNAL** or **DECLINE**, with the items a human should
   verify.

## The panel

| Name | Role | Probes | Voice (Deepgram Aura-2) |
|---|---|---|---|
| **Julian** | Host | Runs the room, reads out the panel's conclusion | `draco` — British male |
| **Maya** | Hiring Manager | Ownership, communication, ambiguity, role fit | `pandora` — British female |
| **Ethan** | Technical Interviewer | Depth, system design, failure modes, trade-offs, scale | `orpheus` — American male |
| **Sophia** | Product Interviewer | Product sense, user/business impact, prioritisation | `asteria` — American female |
| **Nina** | Customer Advocate | Customer empathy, real-world impact, stakeholders | `amalthea` — Filipino female |
| **Liam** | Coding Interviewer | Hands-on coding, algorithms, complexity, debugging | `arcas` — American male |

One LiveKit participant speaks as all six: picking who answers picks both the
grounded context and the voice. Interviewers never see each other's scores while
the interview is live — that isolation is enforced in the one function that builds
their prompts.

## Ask the Panel

Once an interview is scored, its record is locked — but its panel stays available.
A recruiter **joins that completed interview's panel** — right after the interview,
or at their own convenience later — and **talks to the AI interviewers by voice**,
as if the panel were still in the room:

- *"Hey Liam, why did you give that coding score?"* → Liam answers, in his own
  voice, citing what the candidate actually said.
- Follow-ups without a name stay with whoever answered; address Julian (or "the
  panel") to go back to the host.
- An interviewer who wasn't seated for that role says they didn't assess the
  candidate, instead of inventing an opinion.
- **Grounding guardrail:** answers come only from the locked record — scores,
  rationales, debate, ledger and transcript. If it isn't there, the answer says so.
- **Counterfactuals** ("what if they had said X at turn 5?") re-score one
  interviewer as a separate what-if. The locked scores never change, so the hash
  still verifies.
- **Overrides** ("override to proceed") are applied by deterministic code, not the
  model, and logged with the reason; the panel's original recommendation stays on
  the record.

Three ways in — the same record and the same rules. Voice in the browser and on the
phone; text chat only on WhatsApp:

| Where | How you join | The conversation |
|---|---|---|
| Browser | Open the interview's report (now, or later from **Past interviews**) → **Join and talk to the panel** | Live voice — each interviewer speaks in their own voice |
| Phone | Call the panel's number; Julian asks which interview | Live voice — each interviewer speaks in their own voice |
| WhatsApp | Message the panel's number; Julian lists interviews | Text chat — each reply headed with who's answering, e.g. `*Liam · Coding Interviewer*` |

## Design principles

1. Floor control is deterministic code, never a model — same inputs, same speaker.
2. Agent isolation is enforced in the prompt builder, not by convention.
3. Conviction is computed in code; a STRONG hold cannot be overridden by model output.
4. Scores are never averaged — the split is the output.
5. Ask the Panel answers only from the stored record. Not written down → "I don't have that."
6. Counterfactuals never mutate the locked record; the hash always verifies.
7. Low confidence reallocates time; it never ends an interview early.
8. The system recommends. A human decides — and every override is logged.

## Architecture

```
 Browser (React)                Phone caller             WhatsApp
   │  LiveKit audio/video         │  PSTN → LiveKit SIP      │  Twilio → ngrok tunnel
   │  + events WebSocket          │                          │  (signed webhook only)
   ▼                              ▼                          ▼
 ┌──────────────────────── FastAPI media-worker (Docker) ────────────────────────┐
 │  LiveKitSession  BVC noise cancellation → Silero VAD → Deepgram STT           │
 │                  → v1-mini turn detection … per-persona Deepgram TTS          │
 │  InterviewPipeline   bids · floor control · evidence ledger · coding round    │
 │  AskPipeline         named routing · grounded answers · overrides · phone lobby│
 │  PhoneLine           registered LiveKit agent "panel-phone" (inbound calls)   │
 │  WhatsAppChats       per-number chat state · Twilio REST replies              │
 │  shared/             prompts · scoring · dossier · interview lookup · LLM router│
 └────────────────────────────────────────────────────────────────────────────────┘
        │ every LLM call                      │ locked records
        ▼                                     ▼
  Ollama Cloud → Groq → Gemini           data/*.json (one file per interview)
```

**Models** — every call goes through one router (`shared/llm_router.py`) with a
cross-provider fallback chain:

| Job | Primary | Fallbacks |
|---|---|---|
| Bids + claim extraction (×5 in parallel per turn), questions, Ask the Panel answers | `ollama:gpt-oss:120b` | Groq `gpt-oss-120b` (key pool) → Gemini 2.5 Flash |
| Dossier, blind scoring, debate, conclusion, counterfactuals | `ollama:minimax-m3` (thinking off) | Gemini 2.5 Flash → Groq `gpt-oss-120b` |
| Live coding round (screen + voice) | Gemini Live (`gemini-3.1-flash-live-preview`), in the browser | Screenshot reads via an OpenRouter vision model |
| Speech | Deepgram `nova-3` STT · Deepgram Aura-2 TTS, one voice per persona | — |
| Turn-taking audio | LiveKit `v1-mini` turn detector (runs locally) + Silero VAD | — |

The rule: **fast model where the candidate is waiting, deep model where the decision
is made.** If the reasoning model falls back, a badge in the page header says so.

**Storage.** Each locked interview is one JSON file in `./data` (transcript,
ledger, scores, debate, conclusion, audit log, what-ifs, overrides). WhatsApp chat
state and per-session logs live there too. The app runs one interview or voice
session at a time; WhatsApp chats run alongside it.

## Getting started

### Prerequisites

- **Docker Desktop** (everything runs in containers; no local Python or Node needed).
- Accounts and keys:

| Service | `.env` keys | Needed for |
|---|---|---|
| [LiveKit Cloud](https://cloud.livekit.io/) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | **Required** — all real-time voice (BVC noise cancellation needs LiveKit Cloud) |
| [Deepgram](https://deepgram.com/) | `DEEPGRAM_API_KEY` | **Required** — speech-to-text and the six voices |
| [Ollama Cloud](https://ollama.com/) | `OLLAMA_API_KEY` | **Required** — interviewers and orchestrator (a paid plan avoids rate limits) |
| [Groq](https://console.groq.com/) | `GROQ_API_KEY` or `GROQ_API_KEYS` (comma-separated pool) | Optional — fast-path fallback |
| [Google AI Studio](https://aistudio.google.com/) | `GEMINI_API_KEY` | Optional — the Gemini Live coding round, and a model fallback |
| [OpenRouter](https://openrouter.ai/) | `OPENROUTER_API_KEY` | Optional — screenshot-based coding round when Gemini isn't set |
| LiveKit phone number | — (see [By phone](#5-ask-the-panel-by-phone)) | Optional — Ask the Panel by phone |
| [Twilio](https://www.twilio.com/) + [ngrok](https://ngrok.com/) | `TWILIO_*`, `WHATSAPP_ALLOWED_NUMBERS`, `NGROK_*` | Optional — Ask the Panel on WhatsApp |

### Run

```bash
git clone https://github.com/CombatAFK378/Coordinated-Ai-interview-panel.git
cd Coordinated-Ai-interview-panel
cp .env.example .env          # then fill in your keys
docker compose up --build     # first run: builds the image (UI + Python + voice models)
```

Open **http://localhost:8080** and click **Start an interview**. Allow the microphone
and camera when asked.

After the first build, start it with just `docker compose up`. The app is ready when
the log shows `registered worker` (and, with WhatsApp configured, ngrok's
`started tunnel … url=https://<your-domain>`).

### Day to day

| You changed… | Do |
|---|---|
| Python in `media-worker/` or `shared/` | Nothing — it hot-reloads |
| `.env` | `docker compose restart media-worker` |
| Frontend (`web/`) or dependencies | `docker compose up --build` |

- Stop with `Ctrl+C` or `docker compose down` — `./data` is kept.
- If a reload hangs on *Waiting for background tasks*: `docker compose restart -t 3 media-worker`.
- Logs: `data/server.log`, `data/client.log`, and one folder per session in
  `data/logs/<time>_<id>/` (reset on every container start).
- Frontend live-reload: `cd web && npm ci && npm run dev` (Vite proxies the API to the worker).

## Using it

### 1. Run an interview

1. **Start an interview** → you land in the room on the **Live interview** tab.
2. **Interview dossier:** upload the job description and résumé as PDFs
   (**Upload job description**, **Upload resume**) or paste the text. Leave both
   blank for a generic all-five panel. **Preview dossier** (optional) shows the
   role, the seated panel, competency weights, each interviewer's rubric and the
   résumé claims the panel will test.
3. **Join interview.** The camera is required; the microphone too. Headphones
   avoid echo.
4. **During the interview:**
   - Just speak — pauses are fine; the turn detector waits for you to finish.
   - **Done** sends your answer right away.
   - **Talk again** throws away a misheard transcript so you can repeat it.
   - **Interrupt** cuts the current speaker off; **Mic on / Mic off** mutes you.
   - **Skip intro** (while Julian speaks) jumps to the first question.
   - The side rail fills **Coverage** per competency and the **Evidence** ledger
     live; drag the **timeline** to rewind and see what the panel knew at each turn.
     Scores stay sealed until the record locks.
   - If the browser blocks audio, click **Enable panel audio**. Press **P** for
     presenter mode.
5. **Coding round** (when Liam is seated, by your fifth answer): click **Share
   entire screen and start** — a window or tab share is rejected. Liam restates the
   problem; think out loud while you code. Docs for syntax are fine; an AI chat
   visible on screen ends the round as *flagged*. It ends when Liam judges it done,
   when you end it (**End coding round**), or after 4 minutes. Without a Gemini key
   the round runs on screenshots instead, and the panel decides when it's done.
6. **Finish and score** locks the record, scores, debates and concludes (about half
   a minute). The report appears below.

### 2. Read the report

- The **recommendation** and a one-line headline of the split.
- **Panel scores, locked independently:** each interviewer's overall score,
  conviction (STRONG / NEUTRAL), per-competency bars, rationale and the evidence
  they cited.
- **Final evidence coverage** and a chart of **coverage across the interview**.
- **Debate:** who held and who moved — a STRONG interviewer's rejected move shows
  as *held*.
- **Conclusion** with **Unresolved, a human should verify** items and their
  evidence.
- **locked record, SHA-256 …** — the hash of the locked record.

### 3. Ask the Panel in the browser (voice)

Ask the Panel sits below the report — right after **Finish and score**, or whenever
you open an interview from **Past interviews**.

1. Click **Join and talk to the panel.** You join the completed interview's panel
   in a live voice room (no camera needed); Julian welcomes you back and tells you
   the panel's call.
2. **Talk to them by name:** *"Hey Liam, why that coding score?"*, *"and what was
   missing?"*, *"Maya, what concerned you?"*, *"Julian, what's the overall call?"*
   Each interviewer answers in their own voice, from the locked record. Follow-ups
   stay with whoever answered; you can cut anyone off just by speaking.
3. To overrule the panel, say *"override to proceed"* or *"decline the
   candidate"* — it's logged, and the original call stays on record.
4. **Leave voice** when you're done.

Alongside the voice room, the report page also has two forms:

- **Counterfactual, what if they had said this:** choose the interviewer, the turn,
  and a better answer → **Re-score** shows how that one score would move. The
  locked score is unchanged.
- **Override the recommendation:** choose the decision, give a reason → **Log
  override**.

### 4. Past interviews — rejoin any panel, any time

Every scored interview is kept. The **Past interviews** tab lists them — candidate,
role, headline, recommendation (and any override). A recruiter can come back at
their own convenience — hours or days later — open any past interview, and click
**Join and talk to the panel** to rejoin that interview's panel and talk to the AI
interviewers by voice, exactly as if the interview had just finished.

### 5. Ask the Panel by phone

**One-time setup**

1. In LiveKit Cloud: **Telephony → Phone numbers → Rent a number** (every plan
   includes one free US number, inbound only — no KYC).
2. Create the dispatch rule that sends calls to the app:
   ```bash
   docker compose run --rm media-worker python phone_setup.py +1XXXXXXXXXX
   ```
   Calls to that number now land in a fresh `call-…` room, and the running app —
   registered as agent `panel-phone` — picks them up. No tunnel or public URL.

**On the call**

1. Dial the number. Julian: *"Which interview would you like to talk about?"*
2. Say *"Vikram's 4:30 interview"* (name, time, or both; "yesterday" or a weekday
   works too). If several fit, Julian lists them — answer *"the second one"* or give
   the time; if one fits, *"yes"*.
3. Ask the panel by name, as in the browser.
4. *"Now let's talk about Priya's interview"* switches; *"that's all, bye"* hangs up.

One call at a time; a call is refused while a candidate interview is live.

### 6. Ask the Panel on WhatsApp

**One-time setup** (Twilio's WhatsApp Sandbox and ngrok — both free)

1. Twilio console → **Messaging → Try it out → Send a WhatsApp message**. From each
   phone that will chat, send the `join <code>` message shown there (re-join every
   3 days).
2. ngrok dashboard → copy **Your Authtoken** and your free domain (**Domains**).
3. Add to `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_WHATSAPP_FROM=whatsapp:<sandbox number>`, `WHATSAPP_ALLOWED_NUMBERS`
   (comma-separated, with country code; empty = anyone in the sandbox),
   `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`.
4. `docker compose up` starts the tunnel with the app.
5. Sandbox settings → **When a message comes in** →
   `https://<NGROK_DOMAIN>/whatsapp/webhook`, **POST** → **Save**.

**Chatting**

| Send | What happens |
|---|---|
| `hi` | Julian introduces himself and lists recent interviews, numbered |
| `2` · `Priya` · `Vikram's 4:30 interview` | Loads that interview (or asks which one) |
| `Liam, why that score?` | Liam answers — the reply is headed `*Liam · Coding Interviewer*` |
| `and what was missing?` | Follow-up stays with Liam |
| `Julian, overall?` | Back to the host |
| `override to proceed` · `decline the candidate` | Logs an override (typos like "ovveride" are understood) |
| `menu` | Lists interviews again, keeping the current one until you pick |
| `hi` · `reset` · `start over` | Starts the chat fresh |
| `help` | How to use the chat |

Only the webhook is reachable through the tunnel — everything else answers 404 —
and every request must carry a valid Twilio signature. To clear every chat's state
from the terminal: `rm -f data/whatsapp/*.json`.
