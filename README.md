# 968ms — Coordinated AI Interview Panel

A live AI interview panel with five specialized interviewers and a host, over
**LiveKit** real-time voice. A JD + résumé dossier seats the right interviewers; a
deterministic floor-control formula decides who speaks next; each interviewer
scores privately, and the locked, hashed scores are debated into a
recommendation. An optional Gemini Live coding round watches the candidate's
screen. Afterwards a recruiter can cross-examine the panel in the browser, by
phone, or on WhatsApp.

## Ask the Panel

Open a scored interview, join voice, and say **“Hey Liam, why did you give that
coding score?”** or **“Hey Maya, what concerned you?”**. That interviewer answers
from the locked record in their own Deepgram Aura voice. Unnamed follow-ups stay
with them; address Julian or the whole panel to return to the host. An interviewer
who did not assess the candidate says so. Typed questions support names too.

There is one LiveKit RTC participant and one listening pipeline for the panel,
with six persona-specific speech plugins and separate grounded LLM contexts.
The application controls who holds the floor; this design supports each voice
without six simultaneous listeners or agents answering over one another.

### By phone

Call the panel's number and Julian answers: *"Which interview would you like to
talk about?"* Say something like **"Shashank's 4:30 interview"** — the fast model
parses the name and time, and plain code matches them against saved interviews
(asking "which one?" if several fit). Then ask the panel as above, each
interviewer in their own voice. Say "now let's talk about Priya's interview" to
switch, or "that's all, bye" to hang up.

Setup: rent a number in LiveKit Cloud (**Telephony → Phone numbers**; every plan
includes one free US number, inbound only), then create its dispatch rule once:

```bash
docker compose run --rm media-worker python phone_setup.py +14155550123
```

The worker registers as agent `panel-phone` at startup — no tunnel or public URL
needed. One call at a time; a call is refused while a candidate interview is live.

### On WhatsApp

Message the panel and Julian hosts: say *hi* for a numbered list of interviews,
reply with a number (or a name and time), then ask anyone by name — "Liam, why that
coding score?". Each reply is headed with who is speaking (`*Liam · Coding
Interviewer*`), follow-ups stay with them, *menu* switches interviews, and "proceed
with / decline the candidate" logs an override like the voice panel does.

Setup (Twilio WhatsApp Sandbox + an ngrok tunnel, both free):

1. In Twilio's console, **Messaging → Try it out → Send a WhatsApp message**, and
   send the `join <code>` message it shows from each phone that will chat (the
   sandbox needs a re-join every 3 days).
2. Add to `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`
   (`whatsapp:` + the sandbox number), `WHATSAPP_ALLOWED_NUMBERS`, and from
   ngrok's dashboard `NGROK_AUTHTOKEN` and your free `NGROK_DOMAIN`.
3. `docker compose up` also starts the tunnel. In the sandbox settings set **When a
   message comes in** to `https://<NGROK_DOMAIN>/whatsapp/webhook` (POST).

Only that webhook is reachable through the tunnel — everything else answers 404 —
and every request must carry a valid Twilio signature.

## Voice stack

```
microphone → LiveKit room → BVC → Silero + Deepgram streaming STT
                              → LiveKit audio turn detector v1-mini
                              → interview / grounded Q&A brain
                              → selected persona's Deepgram streaming TTS
                              → LiveKit room → browser speaker
```

- `v1-mini` is the **audio** turn detector bundled in LiveKit Agents, explicitly
  selected in every environment.
- BVC removes background voices as well as noise and **requires LiveKit Cloud**.
  Use `LIVEKIT_NOISE_CANCELLATION=off` with a self-hosted LiveKit server. Browser
  echo cancellation remains enabled; browser noise suppression is off when BVC
  or NC is enabled to avoid applying two noise filters.
- LiveKit handles endpointing, speech interruption and playout. **Done** commits
  the current turn. **Interrupt** also works when automatic barge-in is disabled.
- The FastAPI worker embeds the voice session; it does not require an additional
  dispatch worker, a public LLM callback URL, or a tunnel for local development.

## Run with Docker

1. Copy `.env.example` to `.env`.
2. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` from your
   [LiveKit Cloud project](https://cloud.livekit.io/). Keep `DEEPGRAM_API_KEY` for
   both streaming STT and TTS. Set `OLLAMA_API_KEY` (Ollama Cloud runs both the
   interviewers and the orchestrator) and keep Groq and Gemini keys as fallbacks.
3. Run `docker compose up --build`. Open [localhost:8080](http://localhost:8080),
   allow microphone and camera, and join an interview. Both local voice models
   are bundled and checked during the image build; no model download is needed.
4. Finish and score, or open a saved interview, then join **Ask the Panel**.

The `.env` file and Python source remain mounted. Restart after changing keys;
rebuild after changing dependencies or frontend code. Saved interviews remain
in `./data`. For frontend development, run `cd web && npm ci && npm run dev`.
Use one Uvicorn worker: active session state is currently in memory and the app
supports one interview or recruiter voice session at a time.

## Ports and data

Compose runs the project `interviewer-livekit` (image `interviewer-livekit:local`)
on port 8080; set `APP_PORT` in `.env` to use another port. Saved interviews,
WhatsApp chat state and per-session logs live in `./data`, which survives
`docker compose down`; back it up before demos that write reports.

## Versions and checks

Stable releases verified on 2026-09-19:

| Package | Version |
|---|---|
| LiveKit Agents / Deepgram / Silero Python plugins | 1.8.2 |
| Noise cancellation | 0.3.2 |
| LiveKit API | 1.2.1 |
| Browser `livekit-client` | 2.22.3 |

Agents pins its compatible RTC SDK to `livekit==1.1.18`; the independently newer
RTC release must not override that constraint. The browser dependency is locked
in `web/package-lock.json`. Update the Python plugin group together and rebuild.

```bash
python -m pip install -r media-worker/requirements.txt -r requirements-dev.txt
python -m pytest
cd web && npm ci && npm run build
```

See [livekit-interviewer.md](livekit-interviewer.md) for the architecture and
model routing, and [ARCHITECTURE.md](ARCHITECTURE.md) for the original design
contract. LiveKit Cloud/BVC, Deepgram and LLM calls require working keys; a
successful build alone does not verify microphone quality or live routing.

References: [LiveKit turn detector](https://docs.livekit.io/agents/logic/turns/turn-detector/),
[noise cancellation plugin](https://pypi.org/project/livekit-plugins-noise-cancellation/).
