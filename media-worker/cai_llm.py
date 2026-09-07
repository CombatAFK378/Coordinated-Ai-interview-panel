"""The bridge that keeps OUR brain inside Agora's Conversational AI Engine.

CAI runs the voice loop (ASR, turn-taking, TTS, RTC) but delegates "what to say"
to a custom LLM that must speak the OpenAI Chat Completions protocol over SSE.
This module is that LLM: it takes the message history CAI POSTs, runs our
grounded Ask-the-Panel logic (with the same addressee routing and override
detection the in-process voice pipeline uses), and streams the answer back as
`chat.completion.chunk` events.

It also mirrors each turn to the UI event bus (heard + speaking) so the
recruiter still sees live captions — CAI does ASR in the cloud, so without this
the on-screen transcript would stay blank.

Non-streaming responses are rejected by CAI, so everything here yields SSE.
"""
import json
import logging
import time
import uuid

from shared import ask_panel, prompts, store
from shared.config import Settings
from shared.models import REC_DECLINE, REC_PROCEED, REC_PROCEED_FLAGGED

# Reuse the exact override detection the voice Ask-the-Panel uses, so CAI and the
# in-process pipeline behave identically.
from ask_pipeline import _detect_override
from pipeline import _is_filler

logger = logging.getLogger(__name__)


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chunk(content: str, *, model: str, cid: str, finish: str | None = None) -> dict:
    delta = {"content": content} if content else {}
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _last_user_message(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):   # OpenAI content-parts form
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _resolve_answer(record: ask_panel.PanelRecord, question: str,
                    interview_id: str) -> tuple[str, str, dict | None]:
    """Return (answer_text, answered_by_agent_id, override_dump) for one recruiter
    turn: a deterministic override if clearly commanded (and it flips the state),
    else the grounded orchestrator. override_dump is the applied Override (for the
    UI badge) or None."""
    ov_intent = _detect_override(question)
    if ov_intent is not None:
        decision, _reason = ov_intent
        current = record.report.conclusion.recommendation
        proceed_now = current in (REC_PROCEED, REC_PROCEED_FLAGGED)
        already = (decision == REC_DECLINE and not proceed_now) or \
                  (decision == REC_PROCEED and proceed_now)
        if already:
            state = "declined" if not proceed_now else "cleared to proceed"
            return (f"The panel has already {state} this candidate, so there's "
                    "nothing to flip. Tell me to reverse it if that's what you want.",
                    "orchestrator", None)
        ov = ask_panel.override(record, decision, _reason)
        try:
            store.save_record(record)
        except Exception:
            logger.exception("CAI-LLM: failed to persist override")
        orig = ov.original_recommendation.replace("_", " ").lower()
        new = ov.decision.replace("_", " ").lower()
        return (f"Done. I've overridden the panel's recommendation from {orig} to "
                f"{new}, and logged that you asked for it. The original call stays on record.",
                "orchestrator", ov.model_dump())

    # Orchestrator answers everything in ONE voice. CAI gives the agent a single
    # TTS voice, so routing to Ethan/Liam/Maya made a female voice speak as men —
    # jarring. The orchestrator still cites each interviewer's scores/reasons from
    # the record ("Ethan gave 0.28…"), just in one consistent narrator voice.
    ans = ask_panel.answer(record, question)
    return (ans.answer or "I don't have that in the record.", "orchestrator", None)


def stream_panel_answer(record: ask_panel.PanelRecord, messages: list,
                        settings: Settings, interview_id: str, on_event=None,
                        model: str = "panel-brain"):
    """Generator of SSE strings for one CAI LLM request. Mirrors the turn to the
    UI event bus via on_event (heard/speaking/idle) so captions stay live."""
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    question = _last_user_message(messages).strip()

    def emit(ev):
        if on_event is not None:
            try:
                on_event(ev)
            except Exception:
                logger.exception("CAI-LLM: on_event failed")

    # CAI endpoints eagerly on short pauses, so blank/filler turns arrive — don't
    # re-answer the whole decision to those; ask them to repeat.
    override_dump = None
    if not question or _is_filler(question):
        text, agent_id = "Sorry, I didn't catch that — could you say it again?", "orchestrator"
        logger.info("CAI-LLM: empty/filler turn → reprompt")
    else:
        logger.info("CAI-LLM: recruiter asks: %r", question[:200])
        emit({"type": "heard", "text": question})
        try:
            text, agent_id, override_dump = _resolve_answer(record, question, interview_id)
        except Exception:
            logger.exception("CAI-LLM: answer failed")
            text, agent_id = "Sorry, something went wrong reading the record.", "orchestrator"
        logger.info("CAI-LLM: %s answers: %r", agent_id, text[:200])

    agent = prompts.AGENTS.get(agent_id) or prompts.ORCHESTRATOR
    emit({"type": "speaking", "agent": agent_id, "name": agent.name,
          "title": agent.title, "text": text})
    # Recruiter override applied → tell the UI so the badge appears (mirrors the
    # in-process AskPipeline, which emitted this event).
    if override_dump is not None:
        emit({"type": "override", "override": override_dump})

    # Stream in small word-groups so CAI's TTS can start speaking before the full
    # answer is assembled (lower first-audio latency).
    words = text.split(" ")
    group = 4
    for i in range(0, len(words), group):
        piece = " ".join(words[i:i + group])
        if i + group < len(words):
            piece += " "
        yield _sse(_chunk(piece, model=model, cid=cid))
    yield _sse(_chunk("", model=model, cid=cid, finish="stop"))
    yield "data: [DONE]\n\n"
    emit({"type": "idle"})
