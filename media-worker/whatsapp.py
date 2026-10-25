"""Ask the Panel on WhatsApp, through Twilio.

Twilio posts each incoming message to /whatsapp/webhook, reached through the
ngrok tunnel. The endpoint checks Twilio's signature and the allow-list, hands
the message here and returns at once; a worker thread answers and sends the
replies through Twilio's REST API, in order.

Each recruiter number is its own chat. Julian hosts until an interview is picked
(the same matching as the phone line, plus a numbered list), then panel members
answer from that locked record, each reply headed with who is speaking — every
message comes from the one Twilio number, so the header is the identity. Chat
only reads saved records, so it runs alongside any voice session.

The same number also sends candidates their feedback after scoring
(send_candidate_feedback). That's one-way: a candidate's number never reaches
the panel, so their replies cost nothing.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field

import requests

from ask_pipeline import apply_override
from shared import ask_panel, feedback, interview_lookup, prompts, store
from shared.panel_routing import PanelRouter

logger = logging.getLogger(__name__)

_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_MESSAGE_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages/{msg}.json"
# Why a WhatsApp message didn't arrive, by Twilio error code.
_UNDELIVERED = {
    63016: "outside WhatsApp's 24-hour window — the candidate must message the panel's "
           "number first",
    63015: "the candidate's phone hasn't joined the WhatsApp sandbox",
    63003: "that number isn't on WhatsApp",
}
_MAX_BODY = 1500            # Twilio caps a WhatsApp message at 1600 characters
_LIST_SIZE = 5              # interviews shown in a numbered list
# A greeting or reset starts the chat fresh, like a brand-new conversation.
_GREETINGS = {"hi", "hello", "hey", "hii", "start", "hi julian", "hey julian", "hello julian"}
_RESET = {"reset", "restart", "start over", "new chat", "clear"}
_MENU = {"menu", "list", "interviews", "back"}
_HELP = {"help", "?"}
_SORRY = "Sorry, something went wrong on my side. Please try again."


def valid_signature(auth_token: str, url: str, params: dict, signature: str | None) -> bool:
    """Twilio's request signature: base64 HMAC-SHA1, keyed by the auth token, of
    the full URL followed by every POST param (sorted by name, name+value)."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def send(settings, to: str, body: str) -> list[str]:
    """Send one message, split if it's over Twilio's limit. Returns the ids of
    the parts Twilio accepted (acceptance isn't delivery — see message_status)."""
    sids = []
    for part in _chunks(body):
        resp = requests.post(
            _MESSAGES_URL.format(sid=settings.twilio_account_sid),
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={"From": settings.twilio_whatsapp_from, "To": to, "Body": part},
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.error("whatsapp: send to %s failed: HTTP %s %s",
                         to, resp.status_code, resp.text[:200])
        else:
            sids.append(resp.json().get("sid", ""))
    return sids


def message_status(settings, sid: str) -> tuple[str, int | None]:
    """(status, error code) of a sent message: queued, sent, delivered, read,
    failed or undelivered."""
    resp = requests.get(
        _MESSAGE_URL.format(sid=settings.twilio_account_sid, msg=sid),
        auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=10)
    data = resp.json()
    return data.get("status", ""), data.get("error_code")


def send_candidate_feedback(settings, record, phone: str, name: str = "", role: str = "",
                            wait_secs: float = 20.0) -> dict:
    """Write the candidate's feedback and send it to `phone` on WhatsApp, then
    follow it for a few seconds — WhatsApp refusals (the 24-hour window, a phone
    that never joined the sandbox) arrive after Twilio accepts the message.
    Returns what happened, for the record and the UI. Never raises."""
    info = {"to": phone, "sent_at": time.time(), "status": "error", "detail": "", "text": ""}
    try:
        info["text"] = feedback.write_feedback(record, name, role)
    except Exception as e:
        logger.exception("feedback: could not write feedback for %s", record.interview_id)
        info["detail"] = f"couldn't write the feedback ({e})"
        return info
    try:
        sids = send(settings, f"whatsapp:{phone}", info["text"])
        if not sids:
            info["detail"] = "Twilio refused the message"
            return info
        status, code = "queued", None
        deadline = time.time() + wait_secs
        while time.time() < deadline and status not in ("delivered", "read", "failed", "undelivered"):
            time.sleep(2)
            status, code = message_status(settings, sids[-1])
    except Exception as e:
        logger.exception("feedback: sending to %s failed", phone)
        info["detail"] = f"sending failed ({e})"
        return info
    if status in ("failed", "undelivered"):
        info.update(status="not_delivered",
                    detail=_UNDELIVERED.get(code, f"WhatsApp refused it (Twilio error {code})"))
    else:
        info["status"] = "delivered" if status in ("delivered", "read") else "sent"
    logger.info("feedback: %s to %s %s", record.interview_id, phone, info["status"])
    return info


def _chunks(text: str, limit: int = _MAX_BODY):
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        yield text[:cut].rstrip()
        text = text[cut:].lstrip()
    if text:
        yield text


def _with_header(agent_id: str, text: str) -> str:
    agent = prompts.AGENTS[agent_id]
    return f"*{agent.name} · {agent.title}*\n{text}"


@dataclass
class Chat:
    """One recruiter's conversation, kept across messages and restarts."""
    number: str                                        # "whatsapp:+91…"
    interview_id: str = ""                             # the loaded interview, if any
    candidate: str = ""
    target: str | None = None                          # sticky addressee (agent id)
    offered: list[str] = field(default_factory=list)   # ids in the last numbered list


class WhatsAppChats:
    def __init__(self, settings, send_fn=send):
        self.settings = settings
        self._send = send_fn
        self._jobs = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="whatsapp")

    def start(self):
        self._thread.start()

    def stop(self):
        self._jobs.put(None)
        self._thread.join(timeout=5)

    def submit(self, number: str, text: str):
        self._jobs.put((number, text))

    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            number, text = job
            try:
                self.handle(number, text)
            except Exception:
                logger.exception("whatsapp: failed to answer %s", number)
                self._send(self.settings, number, _with_header("orchestrator", _SORRY))

    def handle(self, number: str, text: str):
        chat = self._load_chat(number)
        logger.info("whatsapp %s: %r", number, text[:200])
        for agent_id, body in self.reply(chat, text.strip()):
            logger.info("whatsapp %s ← %s: %r", number, agent_id, body[:160])
            self._send(self.settings, number, _with_header(agent_id, body))
        self._save_chat(chat)

    # ---- the conversation ----------------------------------------------------

    def reply(self, chat: Chat, text: str) -> list[tuple[str, str]]:
        """(agent id, message) replies to one incoming message."""
        words = re.sub(r"[^a-z? ]", "", text.lower()).strip()
        rows = store.list_summaries()
        now = interview_lookup.now()
        if words in _HELP:
            return [("orchestrator", self._help())]
        if words in _GREETINGS or words in _RESET:
            chat.interview_id, chat.candidate, chat.target, chat.offered = "", "", None, []
        if words in _GREETINGS or words in _RESET or words in _MENU:
            intro = ("Hi, I'm Julian, host of the interview panel. Which interview would "
                     "you like to discuss?" if not chat.interview_id else "Which interview next?")
            return self._offer(chat, rows, now, intro) if rows else [
                ("orchestrator", "There are no scored interviews on record yet.")]
        if not chat.interview_id or interview_lookup.wants_other_interview(
                text, chat.candidate, rows):
            return self._find(chat, text, rows, now)
        return self._ask(chat, text)

    def _find(self, chat: Chat, text: str, rows: list[dict], now):
        by_id = {r["interview_id"]: r for r in rows}
        offered = [by_id[i] for i in chat.offered if i in by_id]
        res = interview_lookup.resolve(text, rows, now, offered)
        if res.outcome == "found":
            return self._load(chat, res.row, now)
        if res.outcome == "none_saved":
            return [("orchestrator", "There are no scored interviews on record yet.")]
        if res.outcome == "unclear":
            return self._offer(chat, rows, now, "Which interview would you like to discuss?")
        if res.outcome == "ambiguous":
            return self._offer(chat, res.options, now,
                               f"I have {len(res.options)} interviews that could be it:")
        return self._offer(chat, res.options, now, "I couldn't find that one. Here's what I have:")

    def _offer(self, chat: Chat, rows: list[dict], now, intro: str):
        shown = rows[:_LIST_SIZE]
        chat.offered = [r["interview_id"] for r in shown]
        lines = "\n".join(f"{i}. {interview_lookup.describe(r, now)}"
                          for i, r in enumerate(shown, 1))
        return [("orchestrator", f"{intro}\n{lines}\n\n"
                                 "Reply with a number, or the candidate's name and time.")]

    def _load(self, chat: Chat, row: dict, now):
        record = store.load_record(row["interview_id"])
        if record is None:
            return [("orchestrator", "I couldn't open that interview's record. "
                                     "Send *menu* to pick another.")]
        chat.interview_id, chat.candidate = record.interview_id, row.get("candidate_name") or ""
        chat.target, chat.offered = None, []
        rec = record.report.conclusion.recommendation.replace("_", " ").lower()
        names = [prompts.AGENTS[s.agent_id].name for s in record.report.scores
                 if s.agent_id in prompts.AGENTS]
        who = (f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1
               else (names[0] if names else "The panel"))
        return [("orchestrator",
                 f"Got it: {interview_lookup.describe(row, now)}. The panel recommended "
                 f"{rec}.\n{who} assessed {chat.candidate or 'the candidate'}. Ask any of "
                 "them by name, or ask me. Send *menu* to switch interviews.")]

    def _ask(self, chat: Chat, text: str):
        record = store.load_record(chat.interview_id)
        if record is None:
            chat.interview_id = ""
            return [("orchestrator", "That interview's record is no longer available. "
                                     "Send *menu* to pick another.")]
        overridden = apply_override(record, text)
        if overridden is not None:
            return [("orchestrator", overridden[0])]
        router = PanelRouter()
        router.last_target = chat.target
        target = router.resolve(text)
        chat.target = router.last_target
        if target:
            ans = ask_panel.answer(record, text, "addressed", target, channel="chat")
        else:
            ans = ask_panel.answer(record, text, channel="chat")
        return [(ans.answered_by, ans.answer)]

    @staticmethod
    def _help() -> str:
        return ("Send *menu* to pick an interview (or just tell me the candidate's name and "
                "time). Then ask anyone by name: \"Liam, why that coding score?\" Follow-ups "
                "stay with whoever answered; ask me to hear the overall view. You can also "
                "tell me to proceed with or decline the candidate, and I'll log it. Send *hi* "
                "or *reset* to start over from scratch.")

    # ---- persistence ---------------------------------------------------------

    def _path(self, number: str) -> str:
        d = os.path.join(self.settings.data_dir, "whatsapp")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, re.sub(r"\D", "", number) + ".json")

    def _load_chat(self, number: str) -> Chat:
        try:
            with open(self._path(number), encoding="utf-8") as f:
                return Chat(**json.load(f))
        except FileNotFoundError:
            return Chat(number=number)
        except Exception:
            logger.exception("whatsapp: unreadable chat state for %s; starting fresh", number)
            return Chat(number=number)

    def _save_chat(self, chat: Chat):
        with open(self._path(chat.number), "w", encoding="utf-8") as f:
            json.dump(asdict(chat), f)
