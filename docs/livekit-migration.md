# LiveKit migration and acceptance

The `codex/livekit-migration` branch replaces the Agora browser/server SDK,
Conversational AI REST agent and callback, custom audio buffering, Sarvam batch
STT, Smart Turn and custom TTS client. Both voice modes share LiveKit Agents with
streaming Deepgram STT/TTS, Silero, BVC and the built-in audio `v1-mini` model.

## Configuration

Add these values to the existing ignored `.env` (preserve your other keys):

```dotenv
LIVEKIT_URL=wss://YOUR-PROJECT.livekit.cloud
LIVEKIT_API_KEY=your-project-api-key
LIVEKIT_API_SECRET=your-project-api-secret
LIVEKIT_NOISE_CANCELLATION=bvc
```

The migration added blank LiveKit credential fields to the local `.env` when
absent. Fill them from your own LiveKit project; secrets are not checked in.
`DEEPGRAM_API_KEY` now covers both STT and TTS. Existing LLM and Gemini coding
settings still apply. No Agora keys, public callback URL, or Smart Turn file is
read by this branch. Old local credentials can remain for rollback.

BVC and NC require **LiveKit Cloud**, even when the Python agent runs in your
Docker container. For a self-hosted LiveKit server use `off`; this enables browser
noise suppression and keeps echo cancellation. Set endpointing delay bounds in
`.env` to tune interview pauses; the audio detector is explicitly `v1-mini`.

## Isolation and rollback

The image is `interviewer-livekit:local`, Compose project `interviewer-livekit`.
`APP_PORT=8081 docker compose up --build` can avoid an existing service on 8080.
Do not run code-mounted Agora and LiveKit containers from the same checkout:
branch switching changes the files both would see. Use separate checkouts to run
both versions. Keep `.env` in each checkout, since it is ignored by Git.

Saved report JSON formats remain compatible. `./data` is a bind mount, independent
of the image and retained by `docker compose down`. The migration has not deleted
existing report data, credentials or local model weights. Preserve the original
image and main branch until the following checks pass with real microphones.

## Automated verification

Run `python -m pytest` after installing backend and `requirements-dev.txt`
dependencies. Tests cover room-scoped token claims, missing configuration,
startup failure cleanup, fresh room rejoin, panel interruption, all six names,
word boundaries, sticky addressing, absent panel members, stale response
suppression, report immutability during voice overrides, redo evidence rollback,
and SDK speech playout and local mini-model inference without cloud calls.

`npm ci && npm run build` in `web/` checks the frontend. `docker compose build`
checks the production build and loads both bundled voice models on Linux.

## Live acceptance (requires project credentials)

1. Join a candidate interview; grant camera and microphone. Julian's disclosure
   and Maya's opening question must be audible, with corresponding captions and
   highlighted tiles. Muting/unmuting must change the published mic.
2. Answer with a mid-sentence pause, then a complete sentence. Check that the
   mini detector waits appropriately; use Done to commit a longer thinking pause.
3. Interrupt speech and ask a different question. No stale generated response
   should start after the new turn. Disable `ALLOW_BARGEIN` and verify the manual
   Interrupt button still works.
4. Before coding, use Talk again on a misheard answer. Both that answer and its
   resulting evidence should disappear; re-record and check the final report.
5. Complete the optional Gemini coding round. The LiveKit mic must mute during
   Gemini speech and resume afterwards. Finish and score; reopen the saved report.
6. Join Ask the Panel. Say “Hey Liam, why did you give that coding score?”, then
   an unnamed follow-up, then “Hey Maya, what concerned you?”. Check that both
   the answer persona and audible voice change correctly. Address Julian to reset.
7. Ask an interviewer omitted from the dossier-selected panel. They must say they
   did not assess the candidate. Ask “Why did you reject the candidate?” and verify
   no override appears. Explicitly override and reverse it; the original locked
   report must remain unchanged while the override history persists.
8. Compare quiet speech and background speech with `bvc` and `off`. Judge
   intelligibility and false interruptions with actual microphones; package/model
   loading does not measure acoustic quality.
9. Leave, rejoin, briefly interrupt the network, deny microphone/camera permission,
   and restart Docker. Check cleanup, reconnection, autoplay recovery, new room
   names and saved-report availability.

This app retains its existing single-active-session, unauthenticated local/demo
HTTP model. Multi-recruiter production tenancy and authorization are separate work.
The application does not archive call audio; persisted records contain transcripts
and scores. No performance or noise-reduction superiority is claimed without the
live checks above.
