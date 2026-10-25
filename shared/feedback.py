"""Candidate feedback, sent on WhatsApp after the interview is scored.

Written once, on the reasoning model, from the locked record: what went well and
what to work on, specific and kind. It never carries scores, the panel's
recommendation or any hint of the hiring outcome — the panel only recommends and
a recruiter may still override, so the candidate hears about their interview,
not a verdict. A deterministic filter drops any point that slips one through.
It's one-way: candidates can't chat with the panel.
"""
import re

from shared import prompts, scoring
from shared.ask_panel import _cap

_SYSTEM = (
    "You write interview feedback for the CANDIDATE, sent to them on WhatsApp after an "
    "AI panel interview. Be honest and specific, but kind and encouraging — the tone of "
    "a good mentor, never harsh or sarcastic.\n"
    "Rules:\n"
    "- Base every point on what actually happened below, referring to specific things "
    "the candidate said or did. Never invent.\n"
    "- NEVER reveal scores, ratings, numbers, the panel's recommendation, or any hiring "
    "decision or likely outcome. Don't say whether they passed or will move forward.\n"
    "- Don't mention the interviewers' private notes, their debate, or other candidates.\n"
    "- Two or three strengths and two or three things to work on, each one or two "
    "sentences; give every improvement a concrete tip.\n"
    "- Phrase improvements as friendly suggestions ('next time, try…', 'it would help "
    "to…'), never as commands or 'you must'. Stay honest about real gaps, but soften "
    "the delivery. If strengths are thin, name genuine small ones rather than inventing.\n"
    "- If an assessment rule was broken (for example an AI assistant used during the "
    "coding round), mention it once, factually and without accusation, as something "
    "to avoid next time.\n"
    "Output ONLY compact JSON: "
    '{"strengths": ["..."], "improve": ["..."], "closing": "<one warm sentence>"}'
)

# Every message ends with this, whatever the panel concluded.
NEXT_STEPS = "Our team will evaluate your performance further and reach out to you."

# A point that mentions any of these could leak the verdict — it's dropped.
_LEAKS = re.compile(
    r"\b(scores?|scored|scoring|ratings?|rated|recommend\w*|reject\w*|declin\w*|hire|hired|"
    r"hiring|shortlist\w*|next round|move forward|moving forward|verdict|insufficient signal|"
    r"proceed with flags|(passed|cleared|failed) (the|this|your) (interview|panel|round))\b"
    r"|\b\d+(\.\d+)?\s*(/|out of)\s*\d+\b|\b0\.\d+\b",
    re.I,
)


def normalize_phone(raw: str, default_country_code: str = "+91") -> str | None:
    """'+91 98765-43210' → '+919876543210'; a bare 10-digit number gets the default
    country code. None if it can't be a phone number."""
    digits = re.sub(r"[\s\-().]", "", raw or "")
    if not digits:
        return None
    if not digits.startswith("+"):
        digits = default_country_code + digits.lstrip("0")
    return digits if re.fullmatch(r"\+\d{8,15}", digits) else None


def _context(record) -> str:
    """What the writer may draw on: the conversation, the evidence the panel noted,
    and the interviewers' notes — no scores, no recommendation."""
    lines = ["TRANSCRIPT:"]
    for t in record.transcript:
        is_cand = t.speaker not in prompts.AGENTS
        who = "Candidate" if is_cand else prompts.AGENTS[t.speaker].title
        text = t.text[: t.truncation_char or 0] if t.truncated else t.text
        lines.append(f"- {who}: {_cap(text, 400 if is_cand else 160)}")
    lines.append("\nEVIDENCE THE PANEL NOTED:")
    for c in record.claims:
        lines.append(f"- {c.status} · {c.competency} — {_cap(c.text, 160)}")
    lines.append("\nINTERVIEWERS' NOTES (for your understanding only; never quote them, "
                 "never mention scores):")
    for s in record.report.scores:
        if s.agent_id in prompts.AGENTS and s.rationale:
            lines.append(f"- {prompts.AGENTS[s.agent_id].title}: {_cap(s.rationale, 300)}")
    return "\n".join(lines)


def _clean(items) -> list[str]:
    out = []
    for item in items or []:
        text = " ".join(str(item).split())
        if text and not _LEAKS.search(text):
            out.append(text)
    return out[:3]


def write_feedback(record, candidate_name: str = "", role: str = "") -> str:
    """The WhatsApp message for the candidate. Raises if nothing safe to send."""
    user = (f"Role: {role or 'not stated'}\nCandidate: {candidate_name or 'the candidate'}"
            f"\n\n{_context(record)}\n\nWrite the feedback. Output ONLY the JSON.")
    data = scoring._parse_json(scoring.reason(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=1200))
    strengths, improve = _clean(data.get("strengths")), _clean(data.get("improve"))
    if not strengths and not improve:
        raise ValueError("no feedback points survived the outcome filter")
    closing = " ".join(str(data.get("closing") or "").split())
    if _LEAKS.search(closing):
        closing = ""
    parts = [f"Hi {candidate_name or 'there'}, thank you for interviewing"
             f"{f' for the {role} role' if role else ''}. Here's some feedback from the panel."]
    if strengths:
        parts.append("*What went well*\n" + "\n".join(f"• {s}" for s in strengths))
    if improve:
        parts.append("*What to work on*\n" + "\n".join(f"• {s}" for s in improve))
    if closing:
        parts.append(closing)
    parts.append(NEXT_STEPS)
    parts.append("— Julian, on behalf of the interview panel\n"
                 "_This is an automated message; replies aren't monitored._")
    return "\n\n".join(parts)
