# 968ms — Coordinated AI Interview Panel

A live AI interview panel with five specialized interviewers and a host. This
branch uses **LiveKit** for both the candidate interview and recruiter Q&A.
Panel bidding, evidence, independent scores, debate, dossiers, saved reports and
the optional Gemini Live coding round retain their existing domain logic.

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

## Voice stack

```
microphone → LiveKit room → BVC → Silero + Deepgram streaming STT
                              → LiveKit audio turn detector v1-mini
                              → existing interview / grounded Q&A brain
                              → selected persona's Deepgram streaming TTS
                              → LiveKit room → browser speaker
```

- `v1-mini` is the **audio** turn detector bundled in LiveKit Agents, explicitly
  selected in every environment. The old text turn-detector plugin is not used.
- BVC removes background voices as well as noise and **requires LiveKit Cloud**.
  Use `LIVEKIT_NOISE_CANCELLATION=off` with a self-hosted LiveKit server. Browser
  echo cancellation remains enabled; browser noise suppression is off when BVC
  or NC is enabled to avoid applying two noise filters.
- LiveKit handles endpointing, speech interruption and playout. **Done** commits
  the current turn. **Interrupt** also works when automatic barge-in is disabled.
- The FastAPI worker embeds the voice session; it does not require an additional
  dispatch worker, a public LLM callback URL, or a tunnel for local development.

## Run with Docker

1. For a fresh setup, copy `.env.example` to `.env`. For an existing setup, keep
   your `.env` and add the new LiveKit fields; do not overwrite provider keys.
2. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` from your
   [LiveKit Cloud project](https://cloud.livekit.io/). Keep `DEEPGRAM_API_KEY` for
   both streaming STT and TTS. Keep keys for the LLM providers configured in your
   model settings (Groq, Gemini and/or OpenRouter).
3. Run `docker compose up --build`. Open [localhost:8080](http://localhost:8080),
   allow microphone and camera, and join an interview. Both local voice models
   are bundled and checked during the image build; no model download is needed.
4. Finish and score, or open a saved interview, then join **Ask the Panel**.

The `.env` file and Python source remain mounted. Restart after changing keys;
rebuild after changing dependencies or frontend code. Saved interviews remain
in `./data`. For frontend development, run `cd web && npm ci && npm run dev`.
Use one Uvicorn worker: active session state is currently in memory and the app
supports one interview or recruiter voice session at a time.

## Branch and rollback

The migration is on `codex/livekit-migration`. Agora code and dependencies are
removed from this branch's runtime. The existing `main` branch retains Agora;
there is no need to install both SDK stacks in the same image. Existing local
Agora credentials and downloaded model files can be retained for rollback.

Compose uses the separate project `interviewer-livekit` and image
`interviewer-livekit:local`. Set `APP_PORT=8081` to run alongside an existing
Agora container on 8080. Do not edit a shared checkout while running both code
mounted containers; use separate checkouts for simultaneous operation. The data
folder is shared with the current checkout, so back it up before demos that write
reports. Stop this project with `docker compose down`; no data volumes are erased.

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

See [ARCHITECTURE.md](ARCHITECTURE.md) and
[the migration checklist](docs/livekit-migration.md) for the design and live
acceptance checks. LiveKit Cloud/BVC, Deepgram and LLM calls require working keys;
a successful build alone does not verify microphone quality or live routing.

References: [LiveKit turn detector](https://docs.livekit.io/agents/logic/turns/turn-detector/),
[noise cancellation plugin](https://pypi.org/project/livekit-plugins-noise-cancellation/).
