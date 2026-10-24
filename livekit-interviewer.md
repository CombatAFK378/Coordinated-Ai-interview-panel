# 968ms — LiveKit Architecture

An adaptive AI interview panel. Five AI interviewers with different evaluation
objectives conduct a live voice interview, coordinated by a silent Orchestrator
that decides who speaks. Scores are private during the interview, locked at the
end, then debated. A recruiter can rejoin the session later and cross-examine
any interviewer about its reasoning.

This document is the implementation contract for the **LiveKit build** — the final
end-to-end architecture: LiveKit for real-time voice, MiniMax M3 on Ollama Cloud
as the reasoning model, and Postgres + Redis as the persistence layer. It describes
structure, data flow and invariants — not code.

## 0. Stack and layout

**Backend: Python + FastAPI**, one process and one active session. LiveKit Agents
runs on a dedicated asyncio loop; serialized domain workers run the panel bidding,
coding, evidence and Q&A logic. Blocking provider calls do not block inbound
audio. Scoring and provider configuration live in `shared/`.

**Frontend: React + Vite**, using `livekit-client` for audio and camera and an
events WebSocket for speaker signals, captions and evidence. The built frontend is
served from `web/dist` by FastAPI.

**Storage: Postgres (durable record) + Redis (live state).** Postgres holds the
tamper-evident interview record (§3); Redis holds in-flight turn state and the
shared LLM rate-limit bucket (§8).

```
media-worker/     FastAPI, embedded LiveKit AgentSession, interview and Q&A workers
web/              React candidate room and recruiter dashboard
shared/           Room tokens, named-speaker routing, agents/prompts, bidding,
                  scoring, dossiers, LLM routing, persistence (Postgres + Redis)
```

`shared/` matters more than it looks. The prompt builder and the LLM router both
live there, and both are enforcement points for invariants in §13 — keep them in
one place the app imports.

---

## 1. The one idea that shapes everything

**LiveKit carries audio. The panel brains live on our server.**

One `panel` RTC participant publishes the serialized floor. Five interviewer
personas and Julian each have an isolated prompt and a distinct Deepgram TTS
plugin instance. Selecting an addressee selects both the grounded answer and the
voice — so a single participant speaks as any of six people. The same transport is
used for the interview and Ask the Panel.

The listening `AgentSession` uses streaming Deepgram STT, bundled Silero VAD and
`inference.TurnDetector(version="v1-mini")`. It calls the domain worker on a
completed user turn; it has no second automatic LLM response. The worker passes
the chosen persona's streaming audio to `AgentSession.say()`. LiveKit owns
playout, interruptions and transcript truncation. The mini audio model runs
locally in Docker — not the deprecated text turn-detector plugin.

BVC noise cancellation is applied once on inbound audio and requires LiveKit
Cloud. Self-hosted rooms use `LIVEKIT_NOISE_CANCELLATION=off`. The browser retains
echo cancellation and disables its own noise suppression when server BVC/NC is on.

Consequences:

- One RTC participant, six voices — not five extra participants and 5× minutes.
- Agents cannot hear each other, because they were never in the room.
- Turn-taking is ours — we own the single outbound audio stream.
- Voice identity is a TTS parameter (a per-persona plugin), not infrastructure.

---

## 2. Components

```
Browser (LiveKit room audio/video + HTTP/events)
  ↕
FastAPI worker
  ├─ LiveKitSession: BVC, Silero, Deepgram STT, v1-mini, per-persona TTS
  ├─ InterviewPipeline: bids, deterministic floor, evidence, coding
  ├─ AskPipeline: named/sticky routing, locked-record answers, overrides
  └─ shared: LLM router, prompts, independent scoring
        ↕
  Postgres (durable record)   Redis (live turn state + rate-limit bucket)
```

The browser Gemini Live coding session receives screen frames and supplies its
coding assessment. The LiveKit microphone is muted while that session owns the
coding round. No public custom-LLM callback or tunnel is needed for voice.

### Model routing

| Job | Frequency | Latency | Model |
|---|---|---|---|
| Bids + claim extraction | every turn, ×5 parallel | critical | `ollama:gpt-oss:120b` |
| Question generation | every turn | critical | `ollama:gpt-oss:120b` |
| Audio endpointing | each speech pause | critical | LiveKit `v1-mini` (local CPU) |
| Coding round (screen-watch) | live, coding round | streaming | Gemini Live (`gemini-3.1-flash-live-preview`) |
| Final scoring | once, ×5 | none | `ollama:minimax-m3` |
| Debate statements | once, ×5 | none | MiniMax M3 |
| Orchestrator conclusion | once | none | MiniMax M3 |
| Dossier / panel composition | once, at creation | none | MiniMax M3 |
| Ask the Panel replies | on demand | ~1–2s | `ollama:gpt-oss:120b` |
| Counterfactual re-score | on demand | ~2–4s | MiniMax M3 |

Rule: **fast model where the candidate is waiting, deep model where the decision
is made.** Both run on **Ollama Cloud** (Pro plan). The fast path is
`gpt-oss-120b` (~1s per question, ~2s per five-way parallel bid round); scoring,
debate, conclusion and dossier use **MiniMax M3**, where latency doesn't matter
because the candidate has left. Thinking models (MiniMax, Gemini 2.5) are called
with thinking disabled so they emit the JSON directly instead of spending the
budget on chain-of-thought; gpt-oss always reasons, so it runs at `low` effort.

Every LLM call goes through one router module (`shared/llm_router.py`) with a
cross-provider fallback chain. The fast path falls back to `gpt-oss-120b` on a
round-robin pool of Groq keys (spreading the free per-account rate limit), then
Gemini 2.5 Flash. The reasoning path falls back to Gemini 2.5 Flash, then the Groq
pool, and the UI shows a badge naming the active model. Model IDs carry a provider
prefix (`ollama:` / `gemini:` / `groq:` / `openrouter:`); never hardcode a single
ID at a call site.

### The Orchestrator is a service, not a single model

Most of what the Orchestrator does is deterministic code. Only a few jobs use an
LLM. Keep this boundary sharp — it is an engineering decision *and* a defensible
claim.

| Job | Implementation |
|---|---|
| Collecting bids | code — parallel HTTP fan-out |
| Priority formula, coverage map, time budget | code — arithmetic |
| Tie-breaking, recency penalties | code |
| Choosing who speaks | code |
| Writing audit events | code |
| Reading the debate, writing the conclusion | MiniMax M3 |
| Answering in Ask the Panel | gpt-oss; MiniMax M3 for counterfactuals |

Turn-taking being deterministic is the point: we can show the exact five numbers
behind every floor grant and the formula that picked the winner. An LLM router
would be slower, non-reproducible, and unable to explain itself four days later.

---

## 3. Data model

The durable record lives in **Postgres**; every row carries `interview_id`.
In-flight state for the running turn lives in **Redis** (§8). There is no global
mutable state anywhere.

### Shared vs private

Two classes of data, and the distinction is the whole product:

- **Shared** — the transcript (every word spoken aloud, candidate answers *and*
  agent questions) and the evidence ledger. All agents read these.
- **Private** — each agent's own scores, notes and reasoning. Only its owner
  reads it, and only until lock.

The Orchestrator reads the shared data **plus all five private states**. It is the
only component that can. Isolation means agents cannot see each other's
judgments — it does not mean they are deaf to the conversation. Same as a real
panel: everyone hears every question and answer, everyone scores on their own
sheet, the chair collects all five sheets.

**`interview`** — id, status (`CREATED|LIVE|LOCKED|CONCLUDED`), room name,
time budget, started/ended timestamps, panel composition, locked hash.

**`candidate_dossier`** — one per interview. Parsed JD and resume: role,
seniority, required competencies with weights, and resume claims extracted as
pre-registered ledger entries.

**`transcript_turn`** — sequence number, speaker (`candidate` or an agent id),
text, audio offset, start/end ms, `truncated` flag and truncation point if the
turn was interrupted.

**`competency`** — per interview. Name, weight, target evidence count, owning
agent ids. Seeded from the dossier.

**`claim`** — the evidence ledger. Extracted from candidate answers. Fields:
text, competency, source turn + character span, strength (0–1), status
(`SOLID | VAGUE | UNVERIFIED`), `contradicts_claim_id`, and which agent noticed
it.

Resume-derived claims start as `UNVERIFIED`. When the candidate says something in
the interview that supports or contradicts a resume claim, the ledger links them.
That is where resume-vs-interview contradiction detection comes from, free.

**`agent_state`** — one row per (interview, agent). The agent's private working
memory: running estimate per competency, private notes, evidence references.
**Never read across agents during the LIVE phase.**

**`agent_score`** — written once at lock. Final score per competency and overall,
`conviction` (`STRONG | NEUTRAL`), evidence reference list. Immutable.

**`debate_statement`** — round, agent, statement text, action (`HOLD | MOVE`),
score before, score after, rejection flag if a MOVE was refused.

**`panel_conclusion`** — recommendation, headline split, unresolved items with
their evidence timestamps, full reasoning text.

**`audit_event`** — append-only. Every floor grant, score write, lock, debate
statement, recruiter question, counterfactual and override. This is what makes
Ask the Panel answerable and the record defensible.

**`what_if_query`** — counterfactuals from Ask the Panel. Stored separately and
**never** allowed to mutate `agent_score`.

**`override`** — recruiter decision, reason, timestamp, resulting status.

---

## 4. The live turn loop

```
1  Candidate speaks
2  LiveKit cleans inbound audio (BVC) and detects speech with Silero
3  Deepgram streams interim and final transcripts to LiveKit and the UI
4  The local v1-mini audio model decides when the turn ends; Done can commit now
5  Utterance finalised → write transcript_turn (Postgres); live state in Redis
6  Fan out 5 bid calls IN PARALLEL, one per interviewer
7  Orchestrator computes floor priority, picks a winner
8  Winning agent generates its question
9  Deepgram Aura streams TTS in that agent's voice
10 Media worker publishes audio; a WebSocket signal lights the tile
11 Ledger updated with claims noticed by all five agents
```

Steps 6 and 11 use the same call. Each bid response returns:

```
{ interest: 0.0-1.0,
  reason: "one line, for the audit log",
  claims_noticed: [ {text, competency, strength, status} ],
  contradicts: [claim_id] }
```

Different agents notice different things — Product spots business claims,
Technical spots failure-mode claims. Five perspectives on one answer is a feature,
not duplication.

### Latency

Endpointing, provider generation and first audio are measured separately. The
configurable endpointing bounds default to 0.5–4.0 seconds; the model chooses
whether to wait through a pause. TTS streams as soon as the provider produces
audio.

### Interruption

When LiveKit confirms an interruption (or the user presses Interrupt):

1. Stop pushing TTS audio immediately
2. Mark the agent's `transcript_turn` as `truncated` with the character offset
   actually delivered
3. Log the interruption as an audit event

Step 2 is not optional. If Product was cut off halfway through a question, the
ledger must cite what the candidate **actually heard**, not what was generated.
Otherwise Ask the Panel will quote a question that was never asked.

Barge-in policy differs by surface: interview agents finish their questions
(`ALLOW_BARGEIN=false` avoids echo/noise false-triggers on speakers), while
Ask-the-Panel is always interruptible so the recruiter can cut in and redirect.
Manual Skip/Interrupt use a forced interrupt that works regardless of the flag.

---

## 5. Floor control

```
priority(agent) = interest × (1 + λ · gap) × recency_penalty
```

- `interest` — the agent's own bid, 0–1
- `gap` — `1 − coverage` of that agent's weakest owned competency
- `λ` — coverage weight. Starts ~0.5, rises toward ~1.5 in the final third of the
  time budget, so late questions chase what we still don't know
- `recency_penalty` — 0.5 if the agent spoke last turn, 0.8 two turns ago, else
  1.0. Stops one interviewer monopolising the room

`coverage(competency) = min(1, Σ claim.strength / target_evidence)`

The Orchestrator writes an audit event for every floor grant containing all five
priorities and the winner. This is what lets it answer *"why did Product get that
turn?"* four days later.

### Cold start — the first question

Turn one has no candidate answer, so bidding cannot run. The opening is scripted.

1. **Orchestrator speaks.** AI disclosure — the candidate is told plainly that all
   interviewers are AI and the session is recorded and transcribed — then
   introduces the panel by name. This satisfies the disclosure requirement and
   lets the candidate hear each voice before it carries meaning. It is templated
   text (no model), so it is identical every time. A Skip button jumps straight to
   the opener.
2. **Hiring Manager asks the opener.** Broad and low-stakes: what they have been
   working on. Deliberately *not* Technical — opening hard depresses the signal
   quality of everything after, because a nervous candidate underperforms for the
   first two minutes regardless of ability. Real panels know this.
3. **Turn two onward: normal bidding.** A broad opening answer touches several
   competencies at once, so the ledger and coverage map hold real values
   immediately.

The opener is a dossier field (`opening_agent`), defaulting to Hiring Manager. A
support-engineering role might open with Customer and a scenario instead.

### Tie-breaking

The formula usually separates near-equal bids by itself: five agents all bidding
0.8 diverge once coverage gap and recency penalty are applied. Genuine ties are
rare.

When priorities land within 0.05, resolve in this fixed order:

1. Highest competency weight from the dossier
2. Fewest turns taken so far
3. Stable priority order — a fixed list

**Never randomise.** The same inputs must always produce the same winner, because
Ask the Panel has to answer *"why did Product get that turn?"* days later.

### When every bid is low

If all five interests fall below ~0.25, the panel has nothing it still needs to
ask — either the candidate is comprehensively understood, or the conversation has
stalled. Do not let the panel ask filler.

Trigger one of:

- switch to a scenario or role-play question on the weakest-covered competency
- compress — cut the remaining question budget and move toward wrap-up
- flag `insufficient signal` if coverage is thin despite low interest

This is common with strong candidates and looks bad if unhandled.

### Low confidence never ends an interview early

If a competency's confidence plateaus, the Orchestrator **reallocates**:

- shift remaining time to a different competency, or
- shorten the remaining question count, or
- flag `insufficient signal — recommend human screen`

It never terminates the session. This is a product decision, not a technical one,
and it is deliberate.

---

## 6. Scoring, locking, debate

Everything in this section runs on the reasoning model —
`ollama:minimax-m3`, referenced in code as `LLM_REASONING_MODEL`, with Gemini 2.5
Flash and then `groq:openai/gpt-oss-120b` as fallbacks. Nothing here is
latency-sensitive; the candidate has left. Use the deepest reasoning model
available and let it take its time.

### Isolation is a code invariant, not a convention

There must be exactly one function that constructs agent prompts:

```
build_agent_prompt(agent_id, interview_id, phase)
```

During `phase = LIVE` it reads the shared transcript, the shared ledger, and
**only that agent's own** `agent_state`. Any attempt to read another agent's
state in LIVE phase raises. Make the blindness impossible to break by accident —
it is the core claim of the product.

### Lock

At the final bell:

1. Run 5 independent scoring passes over the full transcript, one per agent, each
   using only its own state. These are separate calls with separate contexts. A
   single failed pass degrades to a neutral placeholder rather than crashing the
   lock.
2. Compute `conviction` **deterministically**, not by asking the model:
   `STRONG` if `|score − 0.5| > 0.25` and `evidence_count ≥ 3`, else `NEUTRAL`.
3. Write `agent_score` rows to Postgres. Mark them immutable.
4. SHA-256 the canonical JSON of `{transcript, claims, scores, convictions}`.
   Store the hash on the interview.

### Debate

Sequential, not parallel — agents must be able to respond to each other.

```
statements = []
for agent in debate_order:
    prompt = base
           + all five locked scores (NOW revealed)
           + statements so far this round
           + conviction rule
    response = call(reasoning_model)
    if agent.conviction == STRONG and response.action == MOVE:
        reject the move, keep locked score, log rejection
    statements.append(response)
```

The conviction rule is enforced **in code**. A STRONG agent physically cannot
move its score, whatever the model outputs. Prompt instructions are guidance;
this is a guarantee.

`debate_order`: put the widest-diverging pair first so the disagreement surfaces
early.

### Conclusion

One final call, on MiniMax M3. Input: all five locked scores, all
statements, who held and who moved, and the coverage map. Output:

- recommendation (`PROCEED | PROCEED_FLAGGED | INSUFFICIENT_SIGNAL | DECLINE`)
- headline split — never an average
- unresolved items, each with the evidence timestamps a human must check
- reasoning text

The Orchestrator reports what the panel concluded. It does not out-vote anyone.

---

## 7. Ask the Panel

The differentiating feature. It works because nothing is thrown away.

### Revival

When the recruiter clicks Join:

1. Open a new LiveKit room for the recruiter
2. Rehydrate the record from Postgres: full transcript, every agent's locked
   score + notes + debate statement, the evidence ledger, and the conclusion
3. Julian greets the recruiter; named interviewers answer in their own voices

Nothing is recomputed. The record is read back exactly as it was locked.

### Voice: LiveKit

`/panel/join` creates a fresh room and a recruiter-scoped token. `AskPipeline`
uses the same LiveKit transport as the interview — the same v1-mini turn detector,
BVC and per-persona Deepgram voices. A named question goes to that interviewer and
voice, while plain follow-ups keep the last addressee. Addressing Julian, the host
or the whole panel resets that choice. Names and aliases match on word boundaries,
so "through" cannot accidentally match "HR". The recruiter can interrupt an
interviewer at any time.

Scores, claims and conclusions remain locked. An absent interviewer explicitly
says they did not assess the candidate. Spoken override commands are audited and
persisted separately; questions about rejecting someone do not create overrides.

### By phone

A recruiter can also call the panel's number (a LiveKit Cloud phone number). A
dispatch rule puts each call in a fresh `call-…` room and dispatches agent
`panel-phone`; the worker registers under that name at startup (in-process,
thread jobs) and joins the room with the same panel voice session, using BVC's
telephony model. The call starts with no record: Julian asks which interview, the
fast model parses the request into hints (name, time, day), and **deterministic
code** matches them against saved interviews — fuzzy first-name match, start time
within an hour, newest first — asking "which one?" when several fit. Then the call
is ordinary Ask the Panel over that locked record; the caller can switch
interviews or say goodbye to hang up. One call at a time, never during a live
interview.

### On WhatsApp

The same panel in a chat (Twilio WhatsApp; the sandbox for the demo). Twilio posts
each message to `/whatsapp/webhook` through an ngrok tunnel; the app verifies
Twilio's signature, checks the allow-list, returns at once and answers from a worker
thread via Twilio's REST API. Only that path is served through the tunnel.

Each recruiter number is a chat with saved state (loaded interview, sticky
addressee, last offered list). Julian hosts until an interview is picked — the
same deterministic resolver as the phone line, plus a numbered list — then answers
come from `ask_panel.answer(..., channel="chat")`: the same grounding guardrail,
two to four plain sentences, each reply headed with the speaker's name because
every message comes from one number. Overrides use the same deterministic
detector as voice. Chat only reads saved records, so it runs alongside any voice
session.

### Question routing

Three modes:

- **Addressed** — the UI (or a spoken/typed name) selects a target ("ask
  Product"). Route directly.
- **Open** — no target. The Orchestrator answers, and may cite a specific agent.
- **Counterfactual** — recruiter supplies a hypothetical answer at a specific
  turn. Re-run only that agent's scoring for the affected claims, return the
  diff. Write a `what_if_query` row.

**Counterfactuals never mutate `agent_score`.** The hash must still verify after
any number of what-if questions.

### Which model answers

Most recruiter questions need no new judgment. The reasoning model already reasoned
at lock time, and its conclusion, reasoning and debate statements are stored as
text. Retrieving and phrasing that is reading comprehension, not judgment — so the
fast model handles it.

| Question | Model | Latency |
|---|---|---|
| "Why did you recommend X?" | gpt-oss | ~1 s |
| "What did he say at 11:20?" | gpt-oss | ~1 s |
| "What was Product's objection?" | gpt-oss | ~1 s |
| "If he had said X, would that change?" | MiniMax M3 | ~2–4 s |

gpt-oss is a **spokesperson, not a judge**. It reads out a decision that was made
and recorded hours earlier.

**Grounding guardrail — mandatory.** The Ask the Panel prompt must instruct:
answer only from the stored conclusion, debate statements, ledger and audit log;
if it is not in the record, say so. Without this the model will invent reasoning
the panel never had, at exactly the moment a recruiter is testing whether the
scores can be trusted.

### Override

Recruiter confirms or overrules. Write an `override` row with the reason, linked
to the interview hash. The original recommendation stays visible forever
alongside it. A spoken override flips only the opposite state (declining an
already-declined candidate is a no-op), and questions are never mistaken for
override commands.

---

## 8. Concurrency, storage and isolation

One interview = one LiveKit room + one media worker process + one row set.

- **Partition key is `interview_id`, everywhere.** Every Redis key is
  `iv:{interview_id}:*`. Every Postgres row has the FK. No exceptions.
- **Postgres is the durable record.** Everything in §3 — interviews, dossiers,
  transcript turns, the evidence ledger, locked scores, debate, conclusion, audit
  events, what-ifs, overrides — is written here and survives restarts. The
  recruiter dashboard and Ask-the-Panel read it back.
- **Redis is the live, ephemeral tier**, used for two things:
  1. **In-flight turn state** — the current floor, coverage map, generation
     counter and pending turn for the running interview, keyed `iv:{id}:*`. Because
     this lives in Redis rather than process memory, the Orchestrator is stateless,
     any instance can serve any request, and a reconnect resumes mid-interview.
  2. **A shared LLM rate-limit token bucket** — a global bucket across all worker
     instances so concurrent interviews don't collectively trip a provider's
     per-minute cap. This is the real scaling constraint, not CPU.
- **Media workers are ephemeral and single-tenant.** Spawned on interview start,
  killed on end. A crashed worker takes down one interview, never two.
- **LiveKit room name derives from `interview_id`.** Tokens are scoped per room
  per participant with short TTL, so a leaked token grants nothing outside one
  interview.

---

## 9. JD and resume ingestion

Parsed **once**, at interview creation, on MiniMax M3. Never per turn.

```
JD + resume → dossier
  → required competencies with weights   (seeds `competency`)
  → panel composition                    (which interviewers to seat)
  → per-agent rubrics                    (what "good" means for this role)
  → resume claims as UNVERIFIED ledger entries
```

Panel composition is dynamic: a backend/ML role seats Hiring Manager, Technical
and Coding but drops Customer and Product; a support role does the opposite.
Getting this right is a quality signal — a weak model over-seats everyone, so
panel selection is one reason the reasoning model is MiniMax M3.

The pre-registered resume claims are the quiet win. When the candidate says
something that supports or contradicts a resume claim, the ledger links them
automatically, and contradiction detection covers "what he wrote" versus "what he
said" without any extra machinery.

---

## 10. Failure modes

| Failure | Response |
|---|---|
| Reasoning model capped/down | Fallback chain (MiniMax M3 → Gemini 2.5 Flash → gpt-oss); UI badge shows the active model |
| LLM 429 (incl. Ollama "too many concurrent requests") | Fallback chain; shared Redis token bucket; exponential backoff |
| LLM slow (>2s) | Emit a short filler line in the agent's voice, keep waiting |
| Model returns no JSON | Router treats "thought without answering" as failure, tries the next model |
| Free model delisted | Router falls through to next ID; alert on fallback rate |
| STT drops | Buffer audio, retry; never silently lose a turn |
| TTS fails | Fall back to on-screen captions; the interview continues |
| Gemini token mint 503 | Retry with backoff; if it stays down, coding degrades and the panel continues |
| LiveKit disconnect | Reconnect, resume from Redis turn state |
| Worker crash | Interview marked `INTERRUPTED`; transcript up to that point is preserved and hashable |

**Demo-day rule:** provision rate-limit headroom on the model providers before
presenting (a warm key pool for the fast path, sufficient quota for the reasoning
model), so a provider throttle can't interrupt a live demo — and lean on the
router's fallback chain if one does.

---

## 11. Build phases

Each phase ends in something demoable. Do not start a phase before the previous
one runs end to end.

**Phase 1 — Audio spine.**
Web client joins a LiveKit room. Media worker joins, receives audio, runs Silero
VAD, transcribes with Deepgram, synthesises a hardcoded reply with Deepgram Aura,
publishes it back. No LLM. Success: you speak, it echoes back in a synthetic voice.

**Phase 2 — One interviewer.**
Add LiveKit v1-mini and one LLM-backed interviewer with a system prompt. Add
interruption handling with truncation logging. Success: a real, interruptible
one-on-one voice interview.

**Phase 3 — The panel.**
Five agents with distinct prompts and voices. Parallel bid calls. Floor control
with the priority formula. Prompt-builder isolation invariant. WebSocket speaker
signals and five tiles in the UI. Success: a technically correct answer with no
customer impact routes to Product.

**Phase 4 — Evidence ledger.**
Claim extraction inside the bid call. Competency coverage map. Contradiction and
vagueness linking. Live coverage bars. Success: every claim has a timestamp and a
competency.

**Phase 5 — Lock, debate, conclusion.**
Blind scoring passes. Deterministic conviction. Hashing. Sequential debate with
code-enforced HOLD. Orchestrator conclusion. Report page with the split and
evidence links. Success: a full interview produces a defensible report.

**Phase 6 — Ask the Panel.**
Session revival, named-interviewer question routing, counterfactual re-scoring,
override logging. Success: a recruiter rejoins a completed interview and gets
timestamped answers in each interviewer's own voice.

**Phase 7 — Dossier.**
JD and resume upload, competency seeding, panel auto-composition, rubric
generation, resume claims as UNVERIFIED entries.

**Phase 8 — Polish.**
Coding interviewer running a live task with **Gemini Live** watching the shared
screen in real time (feedback + outside-AI-help detection). Confidence trajectory
chart. Spoken AI disclosure and persistent badge. Recruiter dashboard. Mandatory
camera, mic toggle, live captions, and per-session logging.

**Phase 9 — LiveKit voice.**
Both voice flows run on LiveKit with named persona voices, BVC and local v1-mini
turn detection — one participant that speaks as any interviewer, removing the
single-voice limit of the earlier managed-agent transport.

Phases 1–6 are the product. Phases 7–9 are what make it a demo people remember.

---

## 12. Configuration

```
# Real-time voice (LiveKit)
LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   # room connection / token signing
LIVEKIT_NOISE_CANCELLATION bvc                      # Cloud only; nc or off also supported
DEEPGRAM_API_KEY                                    # streaming STT + per-persona Aura TTS
LIVEKIT_STT_MODEL          nova-3
LIVEKIT_STT_LANGUAGE       en-US
LIVEKIT_ENDPOINTING_MIN_DELAY 0.5
LIVEKIT_ENDPOINTING_MAX_DELAY 4.0
ALLOW_BARGEIN              false                     # interview finishes turns; recruiter always interruptible

# Models
OLLAMA_API_KEY                                      # primary: fast path + reasoning (Ollama Cloud)
LLM_FAST_MODEL            ollama:gpt-oss:120b
LLM_FALLBACK_CHAIN        groq:openai/gpt-oss-120b,gemini:gemini-2.5-flash
LLM_REASONING_MODEL       ollama:minimax-m3
LLM_REASONING_FALLBACKS   gemini:gemini-2.5-flash,groq:openai/gpt-oss-120b
GEMINI_API_KEY                                       # fallback (Gemini 2.5 Flash) + Gemini Live coding
GEMINI_LIVE_MODEL         gemini-3.1-flash-live-preview
GROQ_API_KEY / GROQ_API_KEYS                        # fallback (pool round-robins the rate limit)
OPENROUTER_API_KEY                                  # vision + optional fallback models

# Storage
DATABASE_URL              postgres://…               # durable interview record
REDIS_URL                 redis://…                  # live turn state + LLM rate-limit bucket

# Interview tunables
INTERVIEW_TIME_BUDGET_S   1200
COVERAGE_LAMBDA_START     0.5
COVERAGE_LAMBDA_END       1.5
CONVICTION_MARGIN         0.25
CONVICTION_MIN_EVIDENCE   3
```

Voice assignment (Deepgram Aura, one voice per agent):

| Agent | Name | Voice |
|---|---|---|
| Hiring Manager | Maya | `aura-2-pandora-en` (British female) |
| Technical | Ethan | `aura-2-orpheus-en` (American male) |
| Product | Sophia | `aura-2-asteria-en` (American female) |
| Customer | Nina | `aura-2-amalthea-en` (Filipino female) |
| Coding | Liam | `aura-2-arcas-en` (American male) |
| Orchestrator | Julian | `aura-2-draco-en` (British male, baritone) |

Voices vary accent, pitch and pace, not just gender — the candidate must never
wonder who is speaking. These assignments apply in both voice modes; the CAI-free
LiveKit transport is what lets one participant carry all six.

---

## 13. Non-negotiables

1. LiveKit carries audio; domain logic runs on completed transcripts and selected
   speech; every LLM provider request goes through the shared router.
2. Agent isolation is enforced by the prompt builder, not by convention.
3. Floor control is deterministic code, never a model. Same inputs, same winner,
   always — no randomised tie-breaks.
4. Ask the Panel answers only from the stored record. If it is not written down,
   the answer is "I don't have that."
5. Conviction is computed deterministically; a STRONG hold cannot be overridden
   by model output.
6. Scores are never averaged. The split is the output.
7. Counterfactuals never mutate the locked record.
8. Every score links to a transcript span and timestamp.
9. Low confidence reallocates time; it never ends an interview.
10. The system recommends. A human decides. Every override is logged.
