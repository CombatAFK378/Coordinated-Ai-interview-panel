# 968ms — Architecture

An adaptive AI interview panel. Five AI interviewers with different evaluation
objectives conduct a live voice interview, coordinated by a silent Orchestrator
that decides who speaks. Scores are private during the interview, locked at the
end, then debated. A recruiter can rejoin the session later and cross-examine
any interviewer about its reasoning.

This document is the implementation contract. It describes structure, data flow
and invariants — not code.

## 0. Stack and layout

**Backend: Python + FastAPI**, one process and one active session. LiveKit Agents
runs on a dedicated asyncio loop; serialized domain workers run the existing
panel bidding, coding, evidence and Q&A logic. Blocking provider calls do not
block inbound audio. Scoring and provider configuration remain in `shared/`.

**Frontend: React + Vite**, using `livekit-client` for audio and camera and the
existing events WebSocket for speaker signals, captions and evidence. The built
frontend is served from `web/dist` by FastAPI.

```
media-worker/     FastAPI, embedded LiveKit AgentSession, interview and Q&A workers
web/              React candidate room and recruiter dashboard
shared/           Room tokens, named-speaker routing, agents/prompts, bidding,
                  scoring, dossiers, LLM routing, JSON record persistence
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
voice. The same transport is used for the interview and Ask the Panel.

The listening `AgentSession` uses streaming Deepgram STT, bundled Silero VAD and
`inference.TurnDetector(version="v1-mini")`. It calls the domain worker on a
completed user turn; it has no second automatic LLM response. The worker passes
the chosen persona's streaming audio to `AgentSession.say()`. LiveKit owns
playout, interruptions and transcript truncation. The mini audio model runs
locally in Docker and is not the deprecated text turn-detector plugin.

BVC is applied once on inbound audio and requires LiveKit Cloud. Self-hosted
rooms use `LIVEKIT_NOISE_CANCELLATION=off`. The browser retains echo cancellation
and disables its noise suppression when server BVC/NC is configured.

---

## 2. Components

```
Browser (LiveKit room audio/video + HTTP/events)
  ↕
FastAPI worker
  ├─ LiveKitSession: BVC, Silero, Deepgram STT, v1-mini, persona TTS
  ├─ InterviewPipeline: bids, deterministic floor, evidence, coding
  ├─ AskPipeline: named/sticky routing, locked-record answers, overrides
  └─ shared: LLM router, prompts, independent scoring, JSON records
```

The optional browser Gemini Live coding session still receives screen frames
and supplies its coding assessment. The LiveKit microphone is muted while that
session owns the coding round.

### Model routing

| Job | Frequency | Latency | Model |
|---|---|---|---|
| Bids + claim extraction | every turn, ×5 parallel | critical | `groq:openai/gpt-oss-120b` |
| Question generation | every turn | critical | `groq:openai/gpt-oss-120b` |
| Audio endpointing | each speech pause | critical | LiveKit `v1-mini` (local CPU) |
| Coding round (screen-watch) | live, coding round | streaming | Gemini Live (`gemini-flash-live`) |
| Final scoring | once, ×5 | none | `openrouter:minimax/minimax-m3` |
| Debate statements | once, ×5 | none | MiniMax M3 |
| Orchestrator conclusion | once | none | MiniMax M3 |
| Ask the Panel replies | on demand | ~1s | `groq:openai/gpt-oss-120b` |
| Counterfactual re-score | on demand | ~2s | MiniMax M3 |

Rule: **fast model where the candidate is waiting, deep model where the decision
is made.** The fast path is Groq's `gpt-oss-120b` (sub-second); scoring/debate/
conclusion use a larger reasoning model (MiniMax M3) via OpenRouter, where latency
doesn't matter because the candidate has left.

Every LLM call goes through one router module (`shared/llm_router.py`) with a
cross-provider fallback chain. The fast path round-robins across a pool of Groq
keys to spread the per-account rate limit. Model IDs carry a provider prefix
(`groq:` / `openrouter:`); never hardcode a single ID at a call site.

### The Orchestrator is a service, not a single model

Most of what the Orchestrator does is deterministic code. Only two jobs use an
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

All tables carry `interview_id`. There is no global mutable state anywhere.

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

**`interview`** — id, status (`CREATED|LIVE|LOCKED|CONCLUDED`), channel name,
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
5  Utterance finalised → write transcript_turn
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

Endpointing, provider generation and first audio should be measured separately.
The configurable endpointing bounds default to 0.5–4.0 seconds; the model chooses
whether to wait through a pause. There are no claimed live latency measurements
for the migrated stack yet. TTS streams as soon as the provider produces audio.

### Interruption

When LiveKit confirms an interruption (or the user presses Interrupt):

1. Stop pushing TTS audio immediately
2. Mark the agent's `transcript_turn` as `truncated` with the character offset
   actually delivered
3. Log the interruption as an audit event

Step 2 is not optional. If Product was cut off halfway through a question, the
ledger must cite what the candidate **actually heard**, not what was generated.
Otherwise Ask the Panel will quote a question that was never asked.

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
   introduces the five by name. This satisfies the PS11 disclosure requirement and
   lets the candidate hear each voice before it carries meaning.
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
`openrouter:minimax/minimax-m3`, referenced in code as `LLM_REASONING_MODEL`.
Nothing here is latency-sensitive; the candidate has left. Use the deepest
reasoning model available and let it take its time.

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
   using only its own state. These are separate calls with separate contexts.
2. Compute `conviction` **deterministically**, not by asking the model:
   `STRONG` if `|score − 0.5| > 0.25` and `evidence_count ≥ 3`, else `NEUTRAL`.
3. Write `agent_score` rows. Mark them immutable.
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

One final call, on MiniMax M3. Input: all five locked scores, all statements, who
held and who moved, and the coverage map. Output:

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
2. Rehydrate the record: full transcript, every agent's locked score + notes +
   debate statement, the evidence ledger, and the panel conclusion
3. Julian greets the recruiter; named interviewers answer in their own voices

Nothing is recomputed. The record is read back exactly as it was locked.

### Voice: LiveKit

`/panel/join` creates a fresh room and a recruiter-scoped token. `AskPipeline`
uses the same LiveKit transport as the interview. A named question goes to that
interviewer and voice, while plain follow-ups keep the last addressee. Addressing
Julian, the host or the whole panel resets that choice. Names and aliases use word
boundaries, so “through” cannot accidentally match “HR”.

Scores, claims and conclusions remain locked. An absent interviewer explicitly
says they did not assess the candidate. Spoken override commands are audited and
persisted separately; questions about rejecting someone do not create overrides.

### Question routing

Three modes:

- **Addressed** — the UI lets the recruiter pick a target ("ask Product"). Route
  directly. Voice and typed names also select the target.
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
| "If he had said X, would that change?" | MiniMax M3 | ~2–3 s |

gpt-oss is a **spokesperson, not a judge**. It reads out a decision that was made
and recorded hours earlier.

**Grounding guardrail — mandatory.** The Ask the Panel prompt must instruct:
answer only from the stored conclusion, debate statements, ledger and audit log;
if it is not in the record, say so. Without this the model will invent reasoning
the panel never had, at exactly the moment a recruiter is testing whether the
scores can be trusted.

Counterfactual latency is fine unhedged — a recruiter asking "would a better
answer have changed your mind?" expects a pause. Scope the re-score to the
affected claims only, not the whole competency.

### Override

Recruiter confirms or overrules. Write an `override` row with the reason, linked
to the interview hash. The original recommendation stays visible forever
alongside it.

---

## 8. Concurrency and isolation

One interview = one LiveKit room + one media worker process + one row set.

- **Partition key is `interview_id`, everywhere.** Every Redis key is
  `iv:{interview_id}:*`. Every Postgres row has the FK. No exceptions.
- **Media workers are ephemeral and single-tenant.** Spawned on interview start,
  killed on end. A crashed worker takes down one interview, never two.
- **The Orchestrator service is stateless and horizontally scalable.** All live
  turn state lives in Redis; all durable state in Postgres. Any instance can
  serve any request.
- **LiveKit room name derives from `interview_id`.** Tokens are scoped per
  channel per participant with short TTL, so a leaked token grants nothing
  outside one interview.
- **LLM rate limits are global**, not per interview. The router needs a shared
  token bucket in Redis plus exponential backoff, or twenty concurrent interviews
  will collectively trip the provider's per-minute cap. This is the real scaling
  constraint — not CPU.

---

## 9. JD and resume ingestion

Parsed **once**, at interview creation. Never per turn.

```
JD + resume → dossier
  → required competencies with weights   (seeds `competency` table)
  → panel composition                    (which interviewers to spawn)
  → per-agent rubrics                    (what "good" means for this role)
  → resume claims as UNVERIFIED ledger entries
```

Panel composition is dynamic: a backend role might spawn Technical, Coding and
Hiring Manager but not Customer. A support-engineering role would do the
opposite.

The pre-registered resume claims are the quiet win. When the candidate says
something that supports or contradicts a resume claim, the ledger links them
automatically, and contradiction detection covers "what he wrote" versus "what he
said" without any extra machinery.

---

## 10. Failure modes

| Failure | Response |
|---|---|
| LLM 429 | Fallback chain; shared Redis token bucket; exponential backoff |
| LLM slow (>2s) | Emit a short filler line in the agent's voice, keep waiting |
| Malformed JSON from bid call | Retry once, then default `interest = 0.3` and continue |
| Free model delisted | Router falls through to next ID; alert on fallback rate |
| STT drops | Buffer audio, retry; never silently lose a turn |
| TTS fails | Fall back to on-screen captions; the interview continues |
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
Web client joins an LiveKit room. Media worker joins, receives audio, runs Silero
VAD, transcribes with Deepgram, synthesises a hardcoded reply with Deepgram Aura,
publishes it back. No LLM. Success: you speak, it echoes back in a synthetic voice.

**Phase 2 — One interviewer.**
Add LiveKit v1-mini and one LLM-backed interviewer with a system prompt. Add
interruption handling with truncation logging. Success: a real, interruptible
one-on-one voice interview.

**Phase 3 — The panel.**
Five agents with distinct prompts and voices. Parallel bid calls. Floor control
with the priority formula. Prompt-builder isolation invariant. WebSocket speaker
signals and five tiles in the UI. Success: the PS11 example scenario works — a
technically correct answer with no customer impact routes to Product.

**Phase 4 — Evidence ledger.**
Claim extraction inside the bid call. Competency coverage map. Contradiction and
vagueness linking. Live coverage bars on an internal debug view. Success: every
claim has a timestamp and a competency.

**Phase 5 — Lock, debate, conclusion.**
Blind scoring passes. Deterministic conviction. Hashing. Sequential debate with
code-enforced HOLD. Orchestrator conclusion. Report page with the split and
evidence links. Success: a full interview produces a defensible report.

**Phase 6 — Ask the Panel.**
Session revival, question routing, counterfactual re-scoring, override logging.
Success: a recruiter rejoins a completed interview and gets timestamped answers.

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
turn detection — one participant that speaks as any interviewer.

Phases 1–6 are the product. Phases 7–9 are what make it a demo people remember.

---

## 12. Configuration

```
LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET # room connection / server token signing
LIVEKIT_NOISE_CANCELLATION bvc                 # Cloud only; nc or off also supported
DEEPGRAM_API_KEY                              # streaming STT + per-persona Aura TTS
LIVEKIT_STT_MODEL nova-3
LIVEKIT_STT_LANGUAGE en-US
LIVEKIT_ENDPOINTING_MIN_DELAY 0.5
LIVEKIT_ENDPOINTING_MAX_DELAY 4.0
OPENROUTER_API_KEY                            # reasoning + vision models
GROQ_API_KEY / GROQ_API_KEYS                  # fast path (pool round-robins the rate limit)
GEMINI_API_KEY                                # Gemini Live coding round
LLM_FAST_MODEL          groq:openai/gpt-oss-120b
LLM_REASONING_MODEL     openrouter:minimax/minimax-m3
LLM_FALLBACK_CHAIN      comma-separated ids
GEMINI_LIVE_MODEL       gemini-flash-live
INTERVIEW_TIME_BUDGET_S 1200
COVERAGE_LAMBDA_START   0.5
COVERAGE_LAMBDA_END     1.5
CONVICTION_MARGIN       0.25
CONVICTION_MIN_EVIDENCE 3
ALLOW_BARGEIN           true
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
wonder who is speaking. These assignments apply in both voice modes.

---

## 13. Non-negotiables

1. The transport calls domain logic through completed transcripts and selected speech; LLM provider requests use the shared router.
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
