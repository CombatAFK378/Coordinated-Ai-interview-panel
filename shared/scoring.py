"""Phase 5: lock, debate, conclusion (ARCHITECTURE §6).

Runs once at the final bell, on the reasoning model (latency-insensitive — the
candidate has left). Blind, independent scoring per interviewer off the evidence
ledger; deterministic conviction; a SHA-256 hash of the canonical record;
sequential debate where a STRONG agent physically cannot move its score; and one
Orchestrator conclusion that reports the split (never an average).

Nothing here is a web/voice transport concern, so it lifts cleanly into the standalone
Orchestrator service later.
"""
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from shared import llm_router, prompts
from shared.config import get_settings
from shared.ledger import INTEGRITY_FLAG, NOT_ANSWERED
from shared.models import (
    CLAIM_SOLID, CLAIM_UNVERIFIED, CONVICTION_NEUTRAL, CONVICTION_STRONG,
    REC_DECLINE, REC_INSUFFICIENT, REC_PROCEED, REC_PROCEED_FLAGGED,
    AgentScore, DebateStatement, InterviewReport, PanelConclusion,
)

# Fewer candidate answers than this: too little was asked to judge.
MIN_ANSWERS_TO_JUDGE = 3
# A model `overall` further than this from the mean of its own competency scores
# is replaced by that mean (a "not assessed" 0.5 next to competency 0.0s).
OVERALL_TOLERANCE = 0.2

logger = logging.getLogger(__name__)

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_VALID_RECS = {REC_PROCEED, REC_PROCEED_FLAGGED, REC_INSUFFICIENT, REC_DECLINE}

# Which reasoning model is actually serving right now — so the UI can show when
# the primary (MiniMax on Ollama) fails and we fall back to Gemini or Groq. A single
# notifier (registered by the worker) is called ONLY on a transition, not per call.
_reasoning_notifier = None
_reasoning_serving: dict = {"model": None}


def set_reasoning_notifier(fn):
    """Register a callback(event_dict) invoked when the serving reasoning model
    changes (e.g. Gemini → Groq fallback, or recovery)."""
    global _reasoning_notifier
    _reasoning_notifier = fn


def _friendly_model(model_id: str) -> str:
    """'gemini:gemini-2.5-flash' -> 'Gemini 2.5 Flash'; 'groq:openai/gpt-oss-120b'
    -> 'Groq (gpt-oss-120b)'; 'ollama:minimax-m3' -> 'Ollama (minimax-m3)'.
    Best-effort, for the UI badge."""
    bare = model_id.split(":", 1)[-1]
    if "gemini" in model_id:
        return "Gemini " + bare.replace("gemini-", "").replace("-", " ").title()
    if model_id.startswith("groq:"):
        return f"Groq ({bare.split('/')[-1]})"
    if model_id.startswith("ollama:"):
        return f"Ollama ({bare})"
    return bare.split("/")[-1]


def _note_serving(model_id: str, primary: str):
    """Emit a UI event only when the serving reasoning model changes."""
    if _reasoning_serving["model"] == model_id:
        return
    _reasoning_serving["model"] = model_id
    if _reasoning_notifier is None:
        return
    try:
        _reasoning_notifier({
            "type": "model_status",
            "fell_back": model_id != primary,
            "model": model_id,
            "primary": primary,
            "model_name": _friendly_model(model_id),
            "primary_name": _friendly_model(primary),
        })
    except Exception:
        logger.exception("reasoning notifier failed")


def reason(messages: list[dict], max_tokens: int = 600) -> str:
    """Call the reasoning model, walking the configured fallbacks (Gemini, then
    gpt-oss — reliable clean JSON via the Groq pool, since its chain-of-thought
    goes to a separate channel).

    Every caller parses JSON out of the result, so this is JSON-AWARE: a 200-OK
    response that contains no JSON object — a "thinking" model that spent its
    whole token budget on chain-of-thought and never answered — is treated as a
    failure and we fall through to the next model. retries=0 so we don't linger on
    an overloaded model; the chain is the resilience."""
    settings = get_settings()
    chain = [settings.llm_reasoning_model] + settings.reasoning_fallbacks
    last_err: Exception | None = None
    last_raw = ""
    for model_id in chain:
        kwargs = dict(temperature=0.4, max_tokens=max_tokens, timeout=60,
                      use_fallback_chain=False)
        if "gpt-oss" in model_id:
            # gpt-oss: low reasoning effort so it emits JSON instead of thinking the
            # budget away, with retries to ride out empty-content blips.
            kwargs.update(retries=2, reasoning_effort="low")
        else:
            # Gemini 2.5 and MiniMax think by default and truncate the JSON — turn
            # it off (the router drops it for models without the knob). One retry
            # for a transient blip, then move on.
            kwargs.update(retries=1, reasoning_effort="none")
        try:
            raw = llm_router.chat(messages, model=model_id, **kwargs)
            last_raw = raw
            # JSON-aware: a 200-OK with no JSON (a "thinking" model that never
            # answered) counts as a failure → fall through to the next model.
            if _last_json_object(raw) is None:
                raise ValueError("response had no JSON (model thought without answering)")
            _note_serving(model_id, chain[0])   # surface which model actually served
            return raw
        except Exception as e:
            last_err = e
            logger.warning("reasoning model %s failed (%s); trying next", model_id, e)
    logger.error("all reasoning models failed; last error: %s", last_err)
    return last_raw   # let the caller's _parse_json raise a clean, catchable error


def _clamp(v, lo=0.0, hi=1.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _parse_json(raw: str) -> dict:
    """Extract a JSON object from a model response.

    Reasoning models (Nemotron etc.) emit chain-of-thought *then* the JSON, so a
    greedy first-brace-to-last-brace grab spans the thinking and fails. Scan from
    the right for the LAST balanced {...} object that actually parses."""
    obj = _last_json_object(raw)
    if obj is None:
        raise ValueError(f"no JSON object in: {raw[:160]!r}")
    return obj


def _last_json_object(s: str):
    """Return the last balanced, parseable JSON object in `s`, or None."""
    end = s.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            c = s[i]
            if c == "}":
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[i:end + 1])
                    except Exception:
                        break   # this candidate is malformed; try an earlier '}'
        end = s.rfind("}", 0, end)
    return None


# ---- lock (scoring) ----------------------------------------------------

def compute_conviction(overall: float, evidence_count: int) -> str:
    """Deterministic (§6, §13.5): STRONG only if the score is far from the middle
    AND backed by enough evidence. Never asked of the model."""
    s = get_settings()
    if abs(overall - 0.5) > s.conviction_margin and evidence_count >= s.conviction_min_evidence:
        return CONVICTION_STRONG
    return CONVICTION_NEUTRAL


def score_agent(agent_id: str, transcript: list, claims: list, interview_id: str,
                context: str = "") -> AgentScore:
    my_comps = set(prompts._agent_competencies(agent_id))
    # evidence_count for conviction is the objective count of ledger claims in this
    # agent's area, computed regardless of whether the model call succeeds. Résumé
    # claims nobody backed up are not evidence.
    ev_count = sum(1 for c in claims
                   if c.competency in my_comps and c.status != CLAIM_UNVERIFIED)
    try:
        raw = reason(prompts.build_scoring_prompt(agent_id, transcript, claims, context), max_tokens=1100)
        data = _parse_json(raw)
    except Exception as e:
        # A single interviewer's scoring failing must NOT crash the whole conclude.
        # Neutral placeholder so the report still locks; the recruiter sees it wasn't scored.
        logger.warning("scoring %s failed (%s); using a neutral placeholder score", agent_id, e)
        return AgentScore(
            interview_id=interview_id, agent_id=agent_id,
            competency_scores={}, overall=0.5,
            conviction=compute_conviction(0.5, ev_count), evidence=[],
            rationale="Automatic scoring was unavailable for this interviewer (model error).",
        )
    comp = {str(k): _clamp(v) for k, v in (data.get("competency_scores") or {}).items()}
    mean = (sum(comp.values()) / len(comp)) if comp else 0.5
    overall = _clamp(data.get("overall", mean))
    if comp and abs(overall - mean) > OVERALL_TOLERANCE:
        logger.info("scoring %s: overall %.2f contradicts its own competency scores "
                    "(mean %.2f); using the mean", agent_id, overall, mean)
        overall = mean
    evidence = [str(e) for e in (data.get("evidence") or [])][:20]
    return AgentScore(
        interview_id=interview_id, agent_id=agent_id,
        competency_scores=comp, overall=overall,
        conviction=compute_conviction(overall, ev_count),
        evidence=evidence, rationale=str(data.get("rationale", ""))[:400],
    )


def lock(panel: list[str], transcript: list, claims: list, interview_id: str,
         contexts: dict | None = None) -> list[AgentScore]:
    """Independent scoring passes (isolated contexts), run in parallel."""
    ctx = contexts or {}
    with ThreadPoolExecutor(max_workers=len(panel)) as ex:
        return list(ex.map(
            lambda a: score_agent(a, transcript, claims, interview_id, ctx.get(a, "")), panel))


def canonical_hash(transcript: list, claims: list, scores: list[AgentScore]) -> str:
    """SHA-256 of the canonical {transcript, claims, scores, convictions} (§6).
    Computed on the LOCKED scores, before debate — the tamper-evident record."""
    canon = {
        "transcript": [
            {"seq": t.seq, "speaker": t.speaker, "text": t.text,
             "truncated": t.truncated, "truncation_char": t.truncation_char}
            for t in transcript
        ],
        "claims": [
            {"id": c.id, "text": c.text, "competency": c.competency,
             "strength": c.strength, "status": c.status, "source_turn": c.source_turn}
            for c in claims
        ],
        "scores": [
            {"agent": s.agent_id, "competency_scores": s.competency_scores,
             "overall": s.overall, "conviction": s.conviction}
            for s in scores
        ],
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- debate ------------------------------------------------------------

def _debate_order(scores: list[AgentScore]) -> list[str]:
    """Widest-diverging pair first, so the disagreement surfaces early (§6)."""
    agents = [s.agent_id for s in scores]
    overall = {s.agent_id: s.overall for s in scores}
    best = None
    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            d = abs(overall[agents[i]] - overall[agents[j]])
            if best is None or d > best[0]:
                best = (d, agents[i], agents[j])
    if best is None:
        return agents
    first, second = best[1], best[2]
    return [first, second] + [a for a in agents if a not in (first, second)]


def run_debate(scores: list[AgentScore], interview_id: str) -> list[DebateStatement]:
    """Sequential debate. Locked scores are shown constant to every speaker; a
    STRONG agent's MOVE is rejected in code, keeping its locked score (§13.5)."""
    order = _debate_order(scores)
    score_map = {s.agent_id: s for s in scores}
    scores_summary = "\n".join(
        f"- {prompts.AGENTS[s.agent_id].name} ({s.agent_id}): overall {s.overall:.2f}, {s.conviction}"
        for s in scores
    )
    statements: list[DebateStatement] = []
    ctx: list[dict] = []
    for agent_id in order:
        s = score_map[agent_id]
        try:
            data = _parse_json(reason(prompts.build_debate_prompt(agent_id, scores_summary, ctx, s.conviction), max_tokens=1000))
        except Exception as e:
            logger.warning("debate %s failed: %s", agent_id, e)
            data = {"action": "HOLD", "statement": "(no comment)", "new_overall": s.overall}
        action = str(data.get("action", "HOLD")).upper()
        statement = str(data.get("statement", ""))[:400]
        new_overall = _clamp(data.get("new_overall", s.overall))
        rejected = False
        score_after = s.overall
        if action == "MOVE" and s.conviction == CONVICTION_STRONG:
            rejected = True            # code-enforced HOLD — a STRONG agent can't move
            action = "HOLD"
        elif action == "MOVE":
            score_after = new_overall  # a NEUTRAL agent may adjust its position
        statements.append(DebateStatement(
            interview_id=interview_id, round=1, agent_id=agent_id, statement=statement,
            action=action, score_before=s.overall, score_after=score_after, rejected=rejected,
        ))
        ctx.append({"agent": agent_id, "statement": statement})
    return statements


# ---- conclusion --------------------------------------------------------

def conclude(interview_id: str, scores: list[AgentScore],
             statements: list[DebateStatement], coverage: dict,
             claims: list, candidate_answers: int | None = None) -> PanelConclusion:
    scores_summary = "\n".join(
        f"- {prompts.AGENTS[s.agent_id].name} ({s.agent_id}): {s.overall:.2f} ({s.conviction}); {s.rationale}"
        for s in scores
    )
    debate_summary = "\n".join(
        f"- {prompts.AGENTS[st.agent_id].name}: {st.action}"
        f"{' [STRONG hold enforced]' if st.rejected else ''} — {st.statement}"
        for st in statements
    ) or "(no debate)"
    solid = sum(1 for c in claims if c.status == CLAIM_SOLID)
    gaps = sum(1 for c in claims if c.text.startswith(NOT_ANSWERED))
    unbacked = sum(1 for c in claims if c.status == CLAIM_UNVERIFIED)
    integrity = any(INTEGRITY_FLAG in c.text.lower() for c in claims)
    mean_cov = (sum(coverage.values()) / len(coverage)) if coverage else 0.0
    evidence = [f"Evidence: {solid} solid claims; {gaps} questions the candidate could "
                f"not answer; {unbacked} résumé claims never backed up (they count toward "
                f"coverage but are only claims). Mean coverage {int(mean_cov * 100)}%."]
    if candidate_answers is not None:
        evidence.append(f"The candidate answered {candidate_answers} questions.")
        if candidate_answers < MIN_ANSWERS_TO_JUDGE:
            evidence.append("Very few questions were asked — too little to judge: "
                            "INSUFFICIENT_SIGNAL, not DECLINE.")
    if integrity:
        evidence.append("INTEGRITY FLAG: outside AI help was detected in the coding round.")
    cov_summary = ((", ".join(f"{k} {int(v * 100)}%" for k, v in coverage.items()) or "(none)")
                   + "\n" + " ".join(evidence))
    try:
        data = _parse_json(reason(prompts.build_conclusion_prompt(scores_summary, debate_summary, cov_summary), max_tokens=1100))
    except Exception as e:
        logger.warning("conclusion failed: %s", e)
        data = {"recommendation": REC_INSUFFICIENT, "headline": "Conclusion unavailable",
                "unresolved": [], "reasoning": str(e)[:200]}
    rec = str(data.get("recommendation", REC_INSUFFICIENT)).upper()
    if rec not in _VALID_RECS:
        rec = REC_INSUFFICIENT
    # Deterministic guard: with essentially no evidence there is nothing to judge —
    # never DECLINE, always flag for a human (§5). Nuanced calls stay with the model.
    if len(claims) < 2 and rec == REC_DECLINE:
        rec = REC_INSUFFICIENT
    # An integrity flag can never be a clean pass; at most proceed-with-flags.
    if integrity and rec == REC_PROCEED:
        logger.info("conclusion: PROCEED with an integrity flag → PROCEED_FLAGGED")
        rec = REC_PROCEED_FLAGGED
    unresolved = [
        {"item": str(u.get("item", "")), "evidence": str(u.get("evidence", ""))}
        for u in (data.get("unresolved") or []) if isinstance(u, dict)
    ][:10]
    return PanelConclusion(
        interview_id=interview_id, recommendation=rec,
        headline=str(data.get("headline", ""))[:300],
        unresolved=unresolved, reasoning=str(data.get("reasoning", ""))[:1500],
    )


def build_report(interview_id: str, panel: list[str], transcript: list,
                 claims: list, coverage: dict, contexts: dict | None = None) -> InterviewReport:
    """The full final-bell pipeline: lock → hash → debate → conclusion (§6)."""
    logger.info("scoring: locking %d interviewers over %d turns / %d claims",
                len(panel), len(transcript), len(claims))
    scores = lock(panel, transcript, claims, interview_id, contexts)
    locked_hash = canonical_hash(transcript, claims, scores)
    statements = run_debate(scores, interview_id)
    answers = sum(1 for t in transcript if t.speaker == "candidate")
    conclusion = conclude(interview_id, scores, statements, coverage, claims, answers)
    logger.info("scoring done: recommendation=%s hash=%s", conclusion.recommendation, locked_hash[:12])
    return InterviewReport(
        interview_id=interview_id, scores=scores, debate=statements,
        conclusion=conclusion, coverage=coverage, locked_hash=locked_hash,
    )
