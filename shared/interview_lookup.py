"""Find the saved interview a recruiter means ("Shashank's 4:30 interview").

Shared by Ask the Panel by phone and on WhatsApp. The fast model only PARSES the
request into hints — a name as heard, a time, a day. Matching those hints against
the saved interviews is deterministic code, so the same request always resolves
the same way and a misheard name can never be talked into a match.
"""
import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared import llm_router, prompts, scoring
from shared.config import get_settings

logger = logging.getLogger(__name__)

# Fuzzy ratio for a heard name vs a saved one — speech-to-text misspells names.
NAME_THRESHOLD = 0.8
# "The 4:30 interview" matches anything that started within this many minutes.
TIME_WINDOW_MIN = 60
# Panel members are who the caller wants to talk TO, never the candidate.
_PANEL_NAMES = {a.name.lower() for a in prompts.AGENTS.values()}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Asking for a different interview mid-conversation, with or without a name.
_OTHER_INTERVIEW = ("another interview", "different interview", "other interview",
                    "another candidate", "different candidate", "other candidate")
_SWITCH_CUES = ("interview", "switch", "talk about", "move on", "instead", "next")
# Picking from the interviews just offered: "the second one", "yes", or just "2".
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
_YES = ("yes", "yeah", "yep", "yup", "correct", "that one", "that's the one", "that's it")
# A bare number only — "the 2:30 one" must not pick option 2.
_NUMBER_PICK = re.compile(r"\s*(?:no\.?|number|option|#)?\s*(\d)\s*[.!]?\s*", re.I)

_PARSE_SYSTEM = (
    "A recruiter has phoned an AI interview panel and is saying which past interview "
    "they want to discuss. The text is live speech-to-text, so names may be misspelled. "
    "Extract ONLY what they actually said — never guess or fill in.\n"
    "Output ONLY compact JSON:\n"
    '{"name": "<candidate name as heard, or empty>", '
    '"time": "<HH:MM 24-hour time they mentioned, or empty>", '
    '"day": "<today | yesterday | a weekday like monday | YYYY-MM-DD, or empty>", '
    '"latest": <true only if they asked for the latest / most recent / last interview>}\n'
    "Times: 'four thirty pm' -> 16:30; 'half past ten in the morning' -> 10:30; with no "
    "am/pm, choose the reading between 08:00 and 19:59 ('4:30' -> 16:30, '9' -> 09:00)."
)


@dataclass
class Hints:
    name: str = ""
    minutes: int | None = None   # local time of day, minutes after midnight
    day: str = ""                # today | yesterday | weekday | YYYY-MM-DD
    latest: bool = False

    @property
    def empty(self) -> bool:
        return not (self.name or self.minutes is not None or self.day or self.latest)


def now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().local_timezone))


def parse_request(text: str, current: datetime) -> Hints:
    """The caller's words → Hints. If the model fails, fall back to matching
    names against the raw words (times are then ignored)."""
    messages = [
        {"role": "system", "content": _PARSE_SYSTEM},
        {"role": "user", "content": f"Current local date and time: "
                                    f"{current:%A %Y-%m-%d %H:%M}.\nRecruiter said: \"{text}\""},
    ]
    try:
        # gpt-oss reasons before the JSON, so leave headroom over the tiny answer.
        data = scoring._parse_json(llm_router.chat(
            messages, max_tokens=400, temperature=0.0, reasoning_effort="low"))
    except Exception as e:
        logger.warning("interview lookup parse failed (%s); matching raw words", e)
        return Hints(name=text)
    minutes = None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(data.get("time") or ""))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        minutes = int(m.group(1)) * 60 + int(m.group(2))
    return Hints(name=str(data.get("name") or "").strip(), minutes=minutes,
                 day=str(data.get("day") or "").strip().lower(),
                 latest=data.get("latest") is True)


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", (s or "").lower())
            if len(t) >= 3 and t not in _PANEL_NAMES]


def name_score(heard: str, saved: str) -> float:
    """Best fuzzy match between any heard word and any word of the saved name.
    Saved names are usually first names only, so "Shashank Upadhyay" must match
    "Shashank"."""
    h, s = _tokens(heard), _tokens(saved)
    if not h or not s:
        return 0.0
    return max(difflib.SequenceMatcher(None, a, b).ratio() for a in h for b in s)


def mentioned(text: str, rows: list[dict]) -> list[dict]:
    """Saved interviews whose candidate is named anywhere in `text`."""
    return [r for r in rows if name_score(text, r.get("candidate_name") or "") >= NAME_THRESHOLD]


def _ts(row: dict) -> float:
    # When the interview started; older records only know when it was scored.
    return row.get("started_at") or row.get("created_at") or 0.0


def _when(row: dict, tz) -> datetime:
    return datetime.fromtimestamp(_ts(row), tz)


def _target_day(day: str, current: datetime) -> date | None:
    today = current.date()
    if day == "today":
        return today
    if day == "yesterday":
        return today - timedelta(days=1)
    if day in _WEEKDAYS:    # the most recent such day, including today
        return today - timedelta(days=(today.weekday() - _WEEKDAYS.index(day)) % 7)
    try:
        return date.fromisoformat(day)
    except ValueError:
        return None


def match(hints: Hints, rows: list[dict], current: datetime,
          within: list[str] | None = None) -> list[dict]:
    """Saved interviews that fit the hints, best first. `within` limits the search
    to the interviews offered in a previous "which one?" question."""
    tz = current.tzinfo
    rows = [r for r in rows if not within or r.get("interview_id") in within]
    if hints.name:
        rows = [r for r in rows
                if name_score(hints.name, r.get("candidate_name") or "") >= NAME_THRESHOLD]
    day = _target_day(hints.day, current)
    if day:
        rows = [r for r in rows if _when(r, tz).date() == day]
    if hints.minutes is not None:
        def gap(r):
            w = _when(r, tz)
            return abs(w.hour * 60 + w.minute - hints.minutes)
        rows = sorted((r for r in rows if gap(r) <= TIME_WINDOW_MIN),
                      key=lambda r: (gap(r), -_ts(r)))
    else:
        rows = sorted(rows, key=_ts, reverse=True)
    return rows[:1] if hints.latest else rows


@dataclass
class Resolution:
    """What a request resolved to. `outcome` is one of: found (→ `row`),
    ambiguous / not_found (→ `options` to offer), unclear, none_saved."""
    outcome: str
    row: dict | None = None
    options: list[dict] = field(default_factory=list)


def pick_offered(text: str, n: int) -> int | None:
    """Index the recruiter picked from `n` offered interviews, if they picked one."""
    m = _NUMBER_PICK.fullmatch(text)
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1)) - 1
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _ORDINALS and _ORDINALS[word] < n:
            return _ORDINALS[word]
    if n == 1 and any(re.search(rf"\b{re.escape(y)}\b", text.lower()) for y in _YES):
        return 0
    return None


def resolve(text: str, rows: list[dict], current: datetime,
            offered: list[dict] | None = None) -> Resolution:
    """Which saved interview does `text` mean? `offered` is the list the
    recruiter was just asked to choose from, if any."""
    if not rows:
        return Resolution("none_saved")
    if offered:
        pick = pick_offered(text, len(offered))
        if pick is not None:
            return Resolution("found", row=offered[pick])
    hints = parse_request(text, current)
    logger.info("interview lookup: %r → %s", text, hints)
    if hints.empty:   # no name, time or day — nothing to match on
        return Resolution("unclear")
    matches = []
    if offered:   # an answer to "which one?" picks among what was offered
        matches = match(hints, rows, current, within=[r["interview_id"] for r in offered])
    if not matches:
        matches = match(hints, rows, current)
    if len(matches) == 1:
        return Resolution("found", row=matches[0])
    if matches:
        return Resolution("ambiguous", options=matches)
    return Resolution("not_found", options=sorted(rows, key=_ts, reverse=True))


def wants_other_interview(text: str, current_candidate: str, rows: list[dict]) -> bool:
    """Mid-conversation switch: "another interview", or a cue word plus a
    different saved candidate's name ("now let's talk about Priya's interview")."""
    t = text.lower()
    if any(p in t for p in _OTHER_INTERVIEW):
        return True
    if not any(c in t for c in _SWITCH_CUES):
        return False
    current = (current_candidate or "").lower()
    return any((r.get("candidate_name") or "").lower() != current for r in mentioned(text, rows))


def describe(row: dict, current: datetime) -> str:
    """Spoken form: "Shashank, AI Engineer, today at 4:30 PM"."""
    w = _when(row, current.tzinfo)
    days = (current.date() - w.date()).days
    if days == 0:
        when = "today"
    elif days == 1:
        when = "yesterday"
    elif 1 < days < 7:
        when = f"on {w:%A}"
    else:
        when = f"on {w:%B} {w.day}"
    name = row.get("candidate_name") or "an unnamed candidate"
    role = f", {row['role']}" if row.get("role") else ""
    return f"{name}{role}, {when} at {w.hour % 12 or 12}:{w:%M} {w:%p}"
