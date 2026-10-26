"""Grounded voice Q&A with named, distinct panel voices over LiveKit.

In the browser the recruiter has already opened an interview. On the phone the
call starts with no record: Julian asks which interview, the caller names it
("Vikram's 4:30 interview"), and the panel then answers from that record. The
caller can switch interviews mid-call or say goodbye to hang up.
"""
import difflib
import logging
import queue
import re
import threading
import time

from pipeline import _is_filler
from shared import ask_panel, interview_lookup, prompts, store
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


# Phone sign-offs. Not "that's it": it also answers "is that the one?".
_GOODBYES = ("bye", "goodbye", "hang up", "end the call", "that's all", "that is all")


def _is_goodbye(text: str) -> bool:
    """A short sign-off ("ok thanks, bye"), not a question that mentions one."""
    t = text.lower().strip()
    return (len(re.findall(r"[a-z']+", t)) <= 8 and not _looks_like_question(t)
            and any(re.search(rf"\b{re.escape(g)}\b", t) for g in _GOODBYES))


def _has_override_trigger(t: str) -> bool:
    """'override' / 'overrule' / 'overwrite', tolerating typos from chat
    ("ovveride"): misspellings score ≥ 0.88 against them, ordinary words like
    "overdue" or "overview" ≤ 0.8."""
    if any(w in t for w in _OVERRIDE_TRIGGERS):
        return True
    return any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.85
               for tok in re.findall(r"[a-z]+", t) if len(tok) >= 6
               for w in _OVERRIDE_TRIGGERS)


def _detect_override(text: str):
    """Return (decision, reason) ONLY if the recruiter clearly *commanded* an
    override (decline or proceed — no other states), else None.

    Guards: a bare 'override' with no direction returns None (the panel asks which
    way); a *question* that merely mentions accept/reject ('why did you reject the
    candidate?') returns None so the panel answers instead of flipping the record."""
    t = text.lower()
    if _looks_like_question(t):
        return None
    trig = _has_override_trigger(t)
    accept = any(p in t for p in _ACCEPT_PHRASES) or (trig and any(w in t for w in _ACCEPT_WORDS))
    reject = any(p in t for p in _REJECT_PHRASES) or (trig and any(w in t for w in _REJECT_WORDS))
    # Override is binary: DECLINE or PROCEED. (No PROCEED_FLAGGED from voice —
    # the recruiter is making a clean call.)
    if reject and not accept:
        return REC_DECLINE, text[:500]
    if accept and not reject:
        return REC_PROCEED, text[:500]
    return None


def apply_override(record, text: str):
    """Deterministic override (§7) for a clear spoken or typed command. Returns
    (Julian's reply, the Override — None if it was a no-op), or None when `text`
    isn't an override command. Shared by voice and WhatsApp."""
    intent = _detect_override(text)
    if intent is None:
        t = text.lower()
        if _has_override_trigger(t) and not _looks_like_question(t):
            # "override it" with no (or both) directions: ask, don't guess.
            return ("Which way should I override it: proceed with the candidate, or "
                    "decline? Tell me and I'll log it.", None)
        return None
    decision, reason = intent
    current = (record.overrides[-1].decision if record.overrides
               else record.report.conclusion.recommendation)
    proceed_now = current in (REC_PROCEED, REC_PROCEED_FLAGGED)
    # Override only flips the record to the OTHER state. If it's already there
    # ("decline the candidate" when already declined), it's a no-op — don't
    # announce a bogus override.
    already = (decision == REC_DECLINE and not proceed_now) or \
              (decision == REC_PROCEED and proceed_now)
    if already:
        state = "declined" if not proceed_now else "cleared to proceed"
        logger.info("ask: override to %s ignored — already %s", decision, current)
        return (f"The panel has already {state} this candidate, so there's nothing to "
                f"flip. If you want to reverse that, tell me to "
                f"{'proceed with' if not proceed_now else 'decline'} the candidate.", None)
    ov = ask_panel.override(record, decision, reason)
    try:                       # persist so the dashboard reflects it (§11)
        store.save_record(record)
    except Exception:
        logger.exception("failed to persist override")
    orig = ov.original_recommendation.replace("_", " ").lower()
    new = ov.decision.replace("_", " ").lower()
    return (f"Done. I've overridden the panel's recommendation from {orig} to {new}, "
            "and logged that you asked for it. The panel's original call stays on "
            "record.", ov)


class AskPipeline:
    def __init__(self, session, settings, record, on_event=None, *, phone=False,
                 on_hangup=None):
        # `record` is None for a phone call until the caller names an interview.
        self.session, self.settings, self.record = session, settings, record
        self.on_event = on_event
        self.phone = phone
        self.on_hangup = on_hangup or (lambda: None)
        self._candidate = ""          # loaded interview's candidate name (phone)
        self._options: list[dict] = []   # interviews offered in a "which one?" question
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
            if self.record is None:
                self._speak("orchestrator", "Hi, this is Julian from the interview panel. "
                            "Which interview would you like to talk about? Tell me the "
                            "candidate's name and roughly when it was.")
                return
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
            if self.phone and _is_goodbye(text):
                self._speak("orchestrator", "Thanks for calling. Goodbye.")
                time.sleep(1.0)   # let the line finish playing before hanging up
                self.on_hangup()
                return
            if self.phone and (self.record is None or interview_lookup.wants_other_interview(
                    text, self._candidate, store.list_summaries())):
                self._find_interview(text)
                return
            # Deterministic override (§7): a clear spoken command applies immediately,
            # never dropped to a flaky LLM tool-call.
            overridden = apply_override(self.record, text)
            if overridden is not None:
                reply, ov = overridden
                if ov is not None:
                    self._emit({"type": "override", "override": ov.model_dump()})
                self._speak("orchestrator", reply)
                return
            target = self.router.resolve(text)
            logger.info("ask: routing to %s", target or "open/orchestrator")
            asked = time.monotonic()
            if target:
                ans = ask_panel.answer(self.record, text, mode="addressed", target=target)
            else:
                ans = ask_panel.answer(self.record, text)   # open → Orchestrator + override tool
            answer_s = time.monotonic() - asked
            if not self._current():
                return
            logger.info("ask: %s answers: %r", ans.answered_by, (ans.answer or "")[:160])
            if ans.override is not None:
                self._emit({"type": "override", "override": ans.override.model_dump()})
            self._speak(ans.answered_by, ans.answer)
            self._log_latency(answer_s)
        except Exception:
            logger.exception("ask answer failed")
            self._emit({"type": "voice_error", "message": "Panel response failed. Please retry."})
        finally:
            self._emit({"type": "idle"})

    def _log_latency(self, answer_s: float):
        """One line per answered question: where the wait between the recruiter's
        last word and the panel's voice went."""
        parts, total = [], 0.0
        for label, value in (("turn end", getattr(self.session, "last_turn_end_s", None)),
                             ("answer", answer_s),
                             ("first audio", getattr(self.session, "last_first_audio_s", None))):
            if isinstance(value, (int, float)):
                parts.append(f"{label} {value:.2f}s")
                total += value
        logger.info("latency: %s = %.2fs from your last word to the panel's voice",
                    " + ".join(parts), total)

    # ---- phone lobby: which interview? ---------------------------------------

    def _find_interview(self, text: str):
        """Resolve the interview the caller means; ask which one if it's ambiguous."""
        now = interview_lookup.now()
        res = interview_lookup.resolve(text, store.list_summaries(), now, self._options)
        if res.outcome == "found":
            self._load(res.row, now)
        elif res.outcome == "none_saved":
            self._speak("orchestrator", "There are no scored interviews on record yet, "
                        "so there's nothing for the panel to discuss.")
        elif res.outcome == "unclear":
            self._speak("orchestrator", "Which candidate? Tell me their name, and roughly "
                        "when the interview was.")
        elif res.outcome == "ambiguous":
            n = len(res.options)
            three = "; here are three" if n > 3 else ""
            self._offer(res.options, now, f"I have {n} interviews that could be it{three}:")
        else:
            latest = ", the latest three" if len(res.options) > 3 else ""
            self._offer(res.options, now, f"I couldn't find that one. Here's what I have{latest}:")

    def _offer(self, rows: list[dict], now, intro: str):
        """Read out up to three interviews (newest/closest first) and ask which."""
        self._options = rows[:3]
        listing = [interview_lookup.describe(r, now) for r in self._options]
        if len(listing) == 1:
            self._speak("orchestrator", f"{intro} {listing[0]}. Is that the one?")
        else:
            self._speak("orchestrator",
                        f"{intro} {'; '.join(listing[:-1])}; or {listing[-1]}. Which one?")

    def _load(self, row: dict, now):
        record = store.load_record(row["interview_id"])
        if record is None:
            self._speak("orchestrator", "I couldn't open that interview's record. "
                        "Which other interview can I help with?")
            return
        self.record, self.router, self._options = record, PanelRouter(), []
        self._candidate = row.get("candidate_name") or ""
        logger.info("phone: loaded interview %s (%s)", record.interview_id,
                    self._candidate or "unnamed")
        rec = record.report.conclusion.recommendation.replace("_", " ").lower()
        names = [prompts.AGENTS[s.agent_id].name for s in record.report.scores
                 if s.agent_id in prompts.AGENTS]
        if len(names) > 1:
            on_line = f"{', '.join(names[:-1])} and {names[-1]} are on the line"
        else:
            on_line = f"{names[0] if names else 'The panel'} is on the line"
        self._speak("orchestrator",
                    f"Got it: {interview_lookup.describe(row, now)}. The panel recommended "
                    f"{rec}. {on_line}. Ask any of them by name, or ask me.")

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
