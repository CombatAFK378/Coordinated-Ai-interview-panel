"""Grounded voice Q&A with named, distinct panel voices over LiveKit."""
import logging
import queue
import threading
import time

from pipeline import _is_filler
from shared import ask_panel, prompts, store
from shared.panel_routing import PanelRouter
from shared.models import REC_DECLINE, REC_PROCEED, REC_PROCEED_FLAGGED

logger = logging.getLogger(__name__)

# Deterministic voice-override detection (§7). Overriding the panel's
# recommendation is too important to depend on an LLM tool-call (which can 400 or
# fall back to a plain answer that can't override) — so we catch clear spoken
# commands in code and apply them directly.
_OVERRIDE_TRIGGERS = ("override", "overrule", "overwrite")
_ACCEPT_PHRASES = (
    "accept the candidate", "accept this candidate", "accept him", "accept her",
    "select the candidate", "select this candidate", "select him", "select her",
    "hire him", "hire her", "hire the candidate", "hire this candidate",
    "proceed with the candidate", "proceed with this", "move forward with",
    "go ahead with", "approve the candidate", "approve this candidate",
)
_REJECT_PHRASES = (
    "reject the candidate", "reject this candidate", "reject him", "reject her",
    "decline the candidate", "decline this candidate", "do not hire", "don't hire",
    "pass on the candidate", "pass on this candidate", "drop the candidate",
)
_ACCEPT_WORDS = ("accept", "select", "hire", "proceed", "approve", "go ahead", "move forward")
_REJECT_WORDS = ("reject", "decline", "pass", "drop")

# A question mentioning "reject the candidate" ("why did you reject the candidate?")
# is NOT an override command. If any of these interrogative signals appear, we
# treat the utterance as a question and never override on it. This was the bug:
# "why did he reject the candidate" matched _REJECT_PHRASES and flipped the record.
_QUESTION_SIGNALS = (
    "?", "why did", "why do", "why is", "why was", "why are", "why does", "why would",
    "what are", "what is", "what was", "what were", "what do", "what did", "what about",
    "how did", "how do", "how does", "how was", "how come",
    "when did", "which ", "who ", "where ",
    "tell me", "explain", "walk me through", "talk about", "his weakness", "her weakness",
    "weaknesses", "his strength", "her strength", "score so low", "so low",
)


def _looks_like_question(t: str) -> bool:
    """A question / request for information — never an override command."""
    return any(sig in t for sig in _QUESTION_SIGNALS)


def _detect_override(text: str):
    """Return (decision, reason) ONLY if the recruiter clearly *commanded* an
    override (decline or proceed — no other states), else None.

    Guards: a bare 'override' with no direction returns None (the panel asks which
    way); a *question* that merely mentions accept/reject ('why did you reject the
    candidate?') returns None so the panel answers instead of flipping the record."""
    t = text.lower()
    if _looks_like_question(t):
        return None
    trig = any(w in t for w in _OVERRIDE_TRIGGERS)
    accept = any(p in t for p in _ACCEPT_PHRASES) or (trig and any(w in t for w in _ACCEPT_WORDS))
    reject = any(p in t for p in _REJECT_PHRASES) or (trig and any(w in t for w in _REJECT_WORDS))
    # Override is binary: DECLINE or PROCEED. (No PROCEED_FLAGGED from voice —
    # the recruiter is making a clean call.)
    if reject and not accept:
        return REC_DECLINE, text[:500]
    if accept and not reject:
        return REC_PROCEED, text[:500]
    return None


class AskPipeline:
    def __init__(self, session, settings, record, on_event=None):
        self.session, self.settings, self.record = session, settings, record
        self.on_event = on_event
        self.router = PanelRouter()
        self._stop = threading.Event()
        self._jobs = queue.Queue()
        self._generation = 0
        self._worker_state = threading.local()
        self._greeted = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="ask-panel")

    def start(self):
        self.session.on_text = self.submit_text
        self._thread.start()

    def stop(self):
        self._generation += 1
        self._stop.set()
        self._jobs.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def on_recruiter_joined(self, user_id=None):
        if self._greeted:
            return
        self._greeted = True
        def greet():
            time.sleep(1.2)
            rec = self.record.report.conclusion.recommendation.replace("_", " ").lower()
            self._speak("orchestrator", f"Welcome back. The panel concluded: {rec}. "
                        "You can ask Liam, Maya, or another interviewer directly by name, "
                        "or ask me about the overall recommendation.")
        self._jobs.put((self._generation, greet))

    def submit_text(self, text):
        if self._stop.is_set() or _is_filler(text):
            return
        self._generation += 1
        self._emit({"type": "heard", "text": text})
        self._emit({"type": "partial", "text": ""})
        self._jobs.put((self._generation, lambda: self._answer(text)))

    def request_interrupt(self):
        active = self.session.is_speaking()
        self._generation += 1
        self.session.interrupt()
        return active

    def _current(self):
        return (not self._stop.is_set()
                and self._worker_state.generation == self._generation)

    def _run(self):
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                return
            self._worker_state.generation, target = job
            if self._current():
                try:
                    target()
                except Exception:
                    logger.exception("panel voice failed")
                    self._emit({"type": "voice_error", "message": "Panel response failed. Please retry."})
                finally:
                    self._emit({"type": "idle"})

    def _answer(self, text: str):
        try:
            self._emit({"type": "thinking"})
            # Deterministic override (§7): a clear spoken command applies immediately,
            # never dropped to a flaky LLM tool-call.
            ov_intent = _detect_override(text)
            if ov_intent is not None:
                decision, _reason = ov_intent
                current = (self.record.overrides[-1].decision if self.record.overrides
                           else self.record.report.conclusion.recommendation)
                proceed_now = current in (REC_PROCEED, REC_PROCEED_FLAGGED)
                # Override only flips the record to the OTHER state. If it's already
                # there ("decline the candidate" when already declined), it's a no-op
                # — don't announce a bogus override; answer the question instead.
                already = (decision == REC_DECLINE and not proceed_now) or \
                          (decision == REC_PROCEED and proceed_now)
                if already:
                    state = "declined" if not proceed_now else "cleared to proceed"
                    logger.info("ask: override to %s ignored — already %s", decision, current)
                    self._speak("orchestrator",
                                f"The panel has already {state} this candidate, so there's "
                                "nothing to flip. If you want to reverse that, tell me to "
                                f"{'proceed with' if not proceed_now else 'decline'} the candidate.")
                    return
                ov = ask_panel.override(self.record, decision, _reason)
                try:                       # persist so the dashboard reflects it (§11)
                    store.save_record(self.record)
                except Exception:
                    logger.exception("failed to persist voice override")
                self._emit({"type": "override", "override": ov.model_dump()})
                orig = ov.original_recommendation.replace("_", " ").lower()
                new = ov.decision.replace("_", " ").lower()
                self._speak("orchestrator",
                            f"Done. I've overridden the panel's recommendation from "
                            f"{orig} to {new}, and logged that you asked for it. The "
                            "panel's original call stays on record.")
                return
            target = self.router.resolve(text)
            logger.info("ask: routing to %s", target or "open/orchestrator")
            if target:
                ans = ask_panel.answer(self.record, text, mode="addressed", target=target)
            else:
                ans = ask_panel.answer(self.record, text)   # open → Orchestrator + override tool
            if not self._current():
                return
            logger.info("ask: %s answers: %r", ans.answered_by, (ans.answer or "")[:160])
            if ans.override is not None:
                self._emit({"type": "override", "override": ans.override.model_dump()})
            self._speak(ans.answered_by, ans.answer)
        except Exception:
            logger.exception("ask answer failed")
            self._emit({"type": "voice_error", "message": "Panel response failed. Please retry."})
        finally:
            self._emit({"type": "idle"})


    def _speak(self, agent_id: str, text: str):
        if not self._current():
            return
        agent = prompts.AGENTS[agent_id]
        self._emit({"type": "speaking", "agent": agent_id,
                    "name": agent.name, "title": agent.title, "text": text})
        self.session.speak(agent_id, text)

    def _emit(self, event):
        if self.on_event and not self._stop.is_set():
            self.on_event(event)
