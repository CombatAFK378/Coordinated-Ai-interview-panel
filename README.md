# 968ms — Coordinated AI Interview Panel

An adaptive, voice-based AI interview panel. A candidate joins a live call and is
interviewed by a coordinated panel of AI interviewers; afterwards a recruiter can
talk to the panel to interrogate the decision. Built on **Agora** for real-time
audio, including the **Agora Conversational AI Engine** for the recruiter Q&A.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

---

## What it does

**The interview.** The candidate joins a web page (mic + camera) and is greeted by
a host who discloses that the panel is AI. Up to five specialized interviewers —
hiring manager, technical, product, customer advocate, and a coding interviewer —
take turns based on **deterministic floor control**: each turn, all interviewers
bid in parallel and the one with the highest priority
(`interest × (1 + λ·coverage_gap) × recency`) gets the floor, so questioning chases
whatever the panel still doesn't know. The candidate can talk over an interviewer
to interrupt; the record notes exactly how much was actually heard.

**The coding round.** The coding interviewer runs a short live exercise: the
candidate shares their screen and a vision model (Gemini Live) watches in real
time, giving feedback and flagging outside AI help.

**Scoring.** Each interviewer scores privately and independently; the scores lock,
the panel debates, and a final recommendation is produced with a full evidence
ledger — every claim tied to the turn it came from.

**Ask the Panel.** After scoring, a recruiter joins by voice and simply talks to
the panel about the candidate — grounded strictly in the locked record. This runs
through the **Agora Conversational AI Engine**: a managed cloud agent handles the
whole voice loop (Deepgram ASR → our grounded LLM → Deepgram TTS) with semantic
turn-detection. The recruiter can also override the recommendation by voice.

## How the audio pipeline works

```
candidate mic → Agora → media worker → Silero VAD → Smart Turn v3.1 → Sarvam STT
                                                                          ↓
                                                                  transcript turn
                                                                          ↓
        speaker ← Agora ← media worker ← Deepgram Aura TTS ← LLM (panel brain)
```

Every model call goes through a shared LLM router with a cross-provider fallback
chain (Groq `gpt-oss-120b` for the sub-second fast path, OpenRouter for reasoning).

## Tech stack

- **Real-time audio:** Agora RTC (candidate/interview) + Agora Conversational AI
  Engine (recruiter Ask-the-Panel).
- **Speech:** Silero VAD, Smart Turn v3.1 (end-of-turn), Sarvam STT, Deepgram Aura
  TTS.
- **Vision (coding round):** Gemini Live.
- **LLMs:** Groq + OpenRouter, via a shared router.
- **Backend:** Python FastAPI media worker (Linux container).
- **Frontend:** React + Vite, served by the worker.

## Layout

```
media-worker/   FastAPI app: Agora join, VAD, Smart Turn, STT, TTS, panel loop,
                coding round, and the Agora Conversational AI integration (Docker)
web/            React + Vite candidate & recruiter UI (built to web/dist, served at "/")
shared/         Config, Agora tokens, models, prompts (agents), orchestrator
                (floor control + parallel bids), LLM router, scoring, Ask-the-Panel
models/         Downloaded ONNX weights (Smart Turn)
```

The media worker runs in a **Linux container** because Agora's server-side audio
SDK ships Linux/macOS binaries only. Your browser runs natively.

---

## Running the app

Follow these steps in order. Steps 3 and 4 must both be running for the app to
work — the Cloudflare tunnel (step 3) and the Docker worker (step 4).

### 1. Add your API keys

Copy the template and fill it in:

```bash
cp .env.example .env
```

You need these keys in `.env`:

| Key | Where to get it |
|---|---|
| `AGORA_APP_ID` + `AGORA_APP_CERTIFICATE` | Agora Console → your project (enable the App Certificate under Security) |
| `AGORA_CUSTOMER_ID` + `AGORA_CUSTOMER_SECRET` | Agora Console → **RESTful API** → Add a Secret (used by the Ask-the-Panel Conversational AI agent) |
| `SARVAM_API_KEY` | Sarvam dashboard (speech-to-text) |
| `DEEPGRAM_API_KEY` | Deepgram console (text-to-speech) |
| `OPENROUTER_API_KEY` | OpenRouter dashboard |
| `GROQ_API_KEY` | Groq console (the sub-second fast path) |
| `GEMINI_API_KEY` | Google AI Studio (the live screen-watch coding round) |
| `PUBLIC_BASE_URL` | Your Cloudflare tunnel URL — set in step 3 |

### 2. Get the Smart Turn weights

```bash
python models/download_smart_turn.py
```

### 3. Start the Cloudflare tunnel

The **Ask the Panel** agent runs in Agora's cloud (the Conversational AI Engine),
so it must reach your worker over a public HTTPS URL. Install
[`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
(`brew install cloudflared` on macOS), then run:

```bash
cloudflared tunnel --protocol http2 --url http://localhost:8080
```

It prints a URL like `https://<random-words>.trycloudflare.com`. Copy it into
`.env` as `PUBLIC_BASE_URL` (no trailing slash — this is **your** tunnel):

```
PUBLIC_BASE_URL=https://<random-words>.trycloudflare.com
```

**Leave this terminal running** while you use the app. Restarting the tunnel gives
a new URL — update `PUBLIC_BASE_URL` and restart the worker (step 4).

### 4. Build and run the media worker

In a **separate** terminal:

```bash
docker compose up --build
```

The first build downloads torch + the Agora SDK, so it takes a few minutes. When
it's ready you'll see `Uvicorn running on http://0.0.0.0:8080`.

### 5. Open the app

Go to **http://localhost:8080**.

- **Run an interview:** click **Join Interview** and allow the mic + camera. The
  panel greets you and asks questions — answer, then pause. Talk over an
  interviewer to interrupt. When done, click **Finish & score**.
- **Ask the Panel:** open a scored interview and click **Ask the Panel**. The
  Conversational AI agent joins and answers your spoken questions about the
  candidate. You can also say *"override and accept the candidate"* (or decline).

### Where the CAI integration lives

- [media-worker/cai_agent.py](media-worker/cai_agent.py) — creates/starts the
  Agora Conversational AI cloud agent (the `join` REST call) and configures ASR,
  the custom LLM, and TTS.
- [media-worker/cai_llm.py](media-worker/cai_llm.py) — the custom
  OpenAI-compatible LLM the CAI agent calls: runs the grounded panel brain and
  streams the answer back.
- [media-worker/main.py](media-worker/main.py) — `/panel/join` starts the agent;
  `/cai/llm/{interview_id}/chat/completions` is the endpoint the agent calls.
