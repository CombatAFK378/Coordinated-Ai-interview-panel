"""Single-session HTTP API, LiveKit voice transport and panel events."""
import asyncio
import io
import logging
import logging.handlers
import os
import shutil
import time
import uuid
import threading
from contextlib import asynccontextmanager
from functools import wraps

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shared.config import get_settings
from shared.livekit_token import build_room_token
from shared.panel_routing import PanelRouter
from shared.models import AskAnswer, Dossier, InterviewReport, Override, SessionStartResponse, WhatIfQuery
from shared import ask_panel, dossier as dossier_mod, gemini, prompts, scoring, store

from livekit_session import LiveKitSession
from pipeline import InterviewPipeline
from ask_pipeline import AskPipeline

_LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
logger = logging.getLogger("media-worker")

# Mirror ALL backend logs to data/server.log (mounted volume) so the full picture —
# every module, every turn, STT, bids, floor, scoring, coding, errors + tracebacks
# — is inspectable in a file, not only the Docker terminal. Pairs with client.log.
try:
    _log_dir = get_settings().data_dir
    os.makedirs(_log_dir, exist_ok=True)
    # Fresh logs every container start: wipe the previous run's files and per-session
    # folders so each `docker compose up` begins clean (no accumulation).
    shutil.rmtree(os.path.join(_log_dir, "logs"), ignore_errors=True)
    for _name in ("server.log", "client.log"):
        try:
            open(os.path.join(_log_dir, _name), "w", encoding="utf-8").close()
        except Exception:
            pass
    # mode="w" so this handler also starts the file fresh on boot.
    _fh = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "server.log"), mode="w",
        maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter(_LOG_FMT))
    logging.getLogger().addHandler(_fh)      # attach to root → captures every module
    logger.info("logs reset for this run → %s (server.log, client.log, logs/)", _log_dir)
except Exception:
    logger.exception("could not set up server.log file handler")

# Per-session logs: each interview run gets its own folder under data/logs/ with its
# own client.log + server.log, so a single run is easy to isolate for debugging (the
# top-level server.log/client.log stay as a rolling all-sessions catch-all).
_session_log_dir: str | None = None
_session_handler: logging.Handler | None = None


def _begin_session_logs(interview_id: str):
    global _session_log_dir, _session_handler
    try:
        tag = f"{time.strftime('%Y%m%d_%H%M%S')}_{interview_id}"
        d = os.path.join(get_settings().data_dir, "logs", tag)
        os.makedirs(d, exist_ok=True)
        _session_log_dir = d
        if _session_handler is not None:        # detach the previous run's handler
            logging.getLogger().removeHandler(_session_handler)
            _session_handler.close()
        h = logging.FileHandler(os.path.join(d, "server.log"), encoding="utf-8")
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter(_LOG_FMT))
        logging.getLogger().addHandler(h)
        _session_handler = h
        logger.info("=== new session %s — logs in %s ===", interview_id, d)
    except Exception:
        logger.exception("could not start per-session logs")

_lifecycle_lock = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _lifecycle_lock:
            return fn(*args, **kwargs)
    return guarded


@asynccontextmanager
async def lifespan(app):
    _broker.bind_loop(asyncio.get_running_loop())
    yield
    await asyncio.to_thread(_teardown)


app = FastAPI(title="968ms LiveKit media worker", lifespan=lifespan)

# Held for the lifetime of the single active session.
_session: LiveKitSession | None = None
_pipeline: InterviewPipeline | None = None
_ask_pipeline: AskPipeline | None = None   # voice Ask the Panel (Phase 6)
_report: InterviewReport | None = None   # last locked report (Phase 5)
_panel_record: ask_panel.PanelRecord | None = None   # for Ask the Panel (Phase 6)
_dossier: Dossier | None = None          # active interview's dossier (Phase 7)


class StartRequest(BaseModel):
    jd: str = ""                  # job description (§9); empty → generic all-panel
    resume: str = ""             # candidate résumé (§9)


class DossierRequest(BaseModel):
    jd: str = ""
    resume: str = ""


class JoinRequest(BaseModel):
    interview_id: str | None = None   # revive a stored interview; else the last one


class AskRequest(BaseModel):
    question: str
    mode: str = "open"           # open | addressed
    target: str | None = None    # agent id, if addressed


class CounterfactualRequest(BaseModel):
    turn: int
    hypothetical: str
    agent_id: str


class OverrideRequest(BaseModel):
    decision: str
    reason: str = ""

# The built Vite app (dist/). In Docker this is set to /app/web/dist. For local
# frontend dev, run `npm run dev` instead (Vite serves the app and proxies here).
WEB_DIR = os.environ.get(
    "WEB_DIR", os.path.join(os.path.dirname(__file__), "..", "web", "dist")
)


class EventBroker:
    """Bridges pipeline events (emitted from worker threads) to WebSocket clients
    (async). Each connection gets a queue; publish() hops onto the event loop
    thread-safely and drops events for a slow/full consumer rather than blocking."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def publish(self, event: dict):
        loop = self._loop
        if loop is None:
            return
        for q in list(self._subs):
            loop.call_soon_threadsafe(self._offer, q, event)

    @staticmethod
    def _offer(q: asyncio.Queue, event: dict):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


_broker = EventBroker()

# Surface reasoning-model fallback (Gemini free-tier cap → Groq) to the UI.
scoring.set_reasoning_notifier(_broker.publish)


def _panel_roster() -> list[dict]:
    return [
        {"id": a, "name": prompts.AGENTS[a].name, "title": prompts.AGENTS[a].title}
        for a in prompts.PANEL_IDS
    ]


@app.get("/panel")
def panel():
    return {"agents": _panel_roster()}


@app.websocket("/session/events")
async def session_events(ws: WebSocket):
    await ws.accept()
    _broker.bind_loop(asyncio.get_running_loop())
    q = _broker.subscribe()
    try:
        await ws.send_json({"type": "panel", "agents": _panel_roster()})
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("events websocket error")
    finally:
        _broker.unsubscribe(q)


@app.get("/health")
def health():
    return {"status": "ok", "session_active": _session is not None}


MAX_PDF_BYTES = 10 * 1024 * 1024   # 10 MB — a JD/résumé is never bigger


@app.post("/parse-pdf")
async def parse_pdf(file: UploadFile = File(...)):
    """Extract plain text from an uploaded JD or résumé PDF (§9). Text is returned
    to the client so it can be reviewed/edited, then sent back with the dossier."""
    from pypdf import PdfReader
    name = (file.filename or "").lower()
    if not name.endswith(".pdf") and (file.content_type or "") != "application/pdf":
        raise HTTPException(400, "please upload a PDF file")
    data = await file.read()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF too large (max 10 MB)")
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as e:
        logger.warning("PDF parse failed: %s", e)
        raise HTTPException(400, "couldn't read that PDF — is it a scanned image?")
    if not text:
        raise HTTPException(422, "no selectable text found (scanned/image-only PDF)")
    return {"filename": file.filename, "pages": len(reader.pages), "text": text}


@app.post("/session/dossier", response_model=Dossier)
def session_dossier(req: DossierRequest):
    """Preview the parsed dossier (§9) before starting — panel, weights, rubrics,
    résumé claims — so the recruiter can review the panel the JD/résumé produced."""
    if not (req.jd.strip() or req.resume.strip()):
        raise HTTPException(400, "provide a JD and/or résumé to parse")
    return dossier_mod.build_dossier(req.jd, req.resume)


def _voice_settings():
    settings = get_settings()
    missing = [name for name, value in {
        "LIVEKIT_URL": settings.livekit_url,
        "LIVEKIT_API_KEY": settings.livekit_api_key,
        "LIVEKIT_API_SECRET": settings.livekit_api_secret,
        "DEEPGRAM_API_KEY": settings.deepgram_api_key,
    }.items() if not value]
    if not (settings.groq_keys or settings.openrouter_api_key or settings.gemini_api_key):
        missing.append("an LLM provider key (Groq, OpenRouter, or Gemini)")
    if missing:
        raise HTTPException(503, f"Missing config: {', '.join(missing)}")
    return settings


def _start_voice(session, pipeline, joined_callback):
    session.set_on_user_joined(joined_callback)
    try:
        pipeline.start()
        session.start()
    except Exception:
        pipeline.stop()
        session.stop()
        logger.exception("LiveKit room startup failed")
        raise HTTPException(502, "Could not connect the voice agent. Check LiveKit configuration and server logs.")


def _join_response(settings, room, identity, panel):
    return SessionStartResponse(
        server_url=settings.livekit_url, room=room, identity=identity,
        token=build_room_token(settings, room, identity), panel=panel,
        noise_cancellation=settings.livekit_noise_cancellation,
    )


@app.post("/session/start", response_model=SessionStartResponse)
@serialized
def session_start(req: StartRequest | None = None):
    global _session, _pipeline, _report, _panel_record, _dossier
    settings = _voice_settings()
    req = req or StartRequest()
    # Parse before replacing the active session, so a dossier error is recoverable.
    dossier = (dossier_mod.build_dossier(req.jd, req.resume)
               if req.jd.strip() or req.resume.strip() else None)
    _teardown()
    _report = _panel_record = None
    _dossier = dossier
    interview_id = uuid.uuid4().hex[:12]
    _begin_session_logs(interview_id)
    room = f"iv-{interview_id}"
    session = LiveKitSession(settings, room, "candidate", on_event=_broker.publish)
    pipeline = InterviewPipeline(session, settings, interview_id, dossier=_dossier)
    pipeline.on_event = _broker.publish
    _start_voice(session, pipeline, pipeline.on_candidate_joined)
    _session, _pipeline = session, pipeline
    return _join_response(settings, room, "candidate", list(pipeline.panel))


class FrameRequest(BaseModel):
    frame: str   # data: URI of a screen-share JPEG/PNG


class CodingResultRequest(BaseModel):
    verdict: str = "done"     # done | cheating | error
    summary: str = ""         # Gemini's read of how the round went


@app.post("/coding/frame")
def coding_frame(req: FrameRequest):
    """Latest screen-share frame from the browser during the coding round (§8)."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    _pipeline.set_frame(req.frame)
    return {"status": "ok"}


class ClientLogRequest(BaseModel):
    line: str


@app.post("/client-log")
def client_log(req: ClientLogRequest):
    """Mirror the browser's on-screen log to a file so it's inspectable server-side
    (esp. the Gemini coding round, which runs entirely in the browser). Writes to the
    current run's per-session folder AND the rolling top-level client.log."""
    line = req.line.rstrip()[:2000] + "\n"
    try:
        os.makedirs(get_settings().data_dir, exist_ok=True)
        with open(os.path.join(get_settings().data_dir, "client.log"), "a", encoding="utf-8") as f:
            f.write(line)
        if _session_log_dir:
            with open(os.path.join(_session_log_dir, "client.log"), "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    return {"status": "ok"}


@app.post("/coding/gemini-token")
def coding_gemini_token():
    """Mint a short-lived ephemeral token so the browser can open the Gemini Live
    coding session (§8) — the real key never leaves the server."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    if not gemini.enabled():
        raise HTTPException(400, "Gemini Live not configured")
    try:
        token = gemini.mint_ephemeral_token()
    except Exception as e:
        logger.exception("gemini token mint failed")
        raise HTTPException(502, f"token mint failed: {e}")
    return {"token": token, "model": get_settings().gemini_live_model}


@app.post("/coding/result")
def coding_result(req: CodingResultRequest):
    """The browser Gemini session reports the coding round's outcome (§8): records
    evidence, drops Liam from the panel, and the rest of the interview continues."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    _pipeline.finish_coding_external(req.verdict, req.summary)
    return {"status": "ok"}


@app.post("/session/interrupt")
def session_interrupt():
    """Candidate pressed the Interrupt button — cut the interviewer off now."""
    pipeline = _ask_pipeline or _pipeline
    if pipeline is None:
        raise HTTPException(409, "no active session")
    interrupted = pipeline.request_interrupt()
    return {"status": "ok", "interrupted": interrupted}


@app.post("/session/skip-intro")
def session_skip_intro():
    """Candidate pressed 'Skip intro' — cut the host's disclosure but keep the
    cold-open going to the first question (does NOT invalidate the turn)."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    return {"status": "ok", "interrupted": _pipeline.skip_intro()}


@app.post("/session/finish-turn")
def session_finish_turn():
    """Candidate pressed 'Done' — finalize their turn now, skip the wait."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    return {"status": "ok", "finalized": _pipeline.finish_turn()}


@app.post("/session/redo")
def session_redo():
    """Candidate pressed 'Talk again' — drop the misheard turn and listen again."""
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    return {"status": "ok", "redone": _pipeline.request_redo()}


@app.post("/session/conclude", response_model=InterviewReport)
@serialized
def session_conclude():
    """Final bell (§6): freeze the interview, then lock → debate → conclusion.
    Blocking and slow (reasoning model, once) — the UI shows a 'scoring…' state."""
    global _report
    if _pipeline is None:
        raise HTTPException(409, "no active session")
    if not any(t.speaker == "candidate" for t in _pipeline.report_inputs()[0]):
        raise HTTPException(400, "no candidate answers to score yet")
    _pipeline.freeze()
    _pipeline.assess_coding()   # §8: thorough vision pass on the final screen → coding evidence
    transcript, claims, coverage, panel, contexts = _pipeline.report_inputs()
    if not any(t.speaker == "candidate" for t in transcript):
        raise HTTPException(400, "no candidate answers to score yet")
    global _panel_record
    logger.info("concluding interview %s", _pipeline.interview_id)
    _report = scoring.build_report(_pipeline.interview_id, panel, transcript, claims,
                                   coverage, contexts)
    _report.trajectory = _pipeline.trajectory()   # per-turn confidence chart (§11)
    # Retain the full record so the recruiter can interrogate it (Phase 6).
    _panel_record = ask_panel.PanelRecord(
        interview_id=_pipeline.interview_id, report=_report,
        transcript=transcript, claims=claims, contexts=contexts,
        audit=[a.model_dump() for a in _pipeline.audit],
    )
    # Persist for the recruiter dashboard (§11) — survives restarts.
    try:
        store.save_record(
            _panel_record,
            candidate_name=(_dossier.candidate_name if _dossier else ""),
            role=(_dossier.role if _dossier else ""),
        )
    except Exception:
        logger.exception("failed to persist interview %s", _pipeline.interview_id)
    return _report


@app.post("/panel/join", response_model=SessionStartResponse)
@serialized
def panel_join(req: JoinRequest | None = None):
    """Join the locked panel with all persona voices available by name."""
    global _session, _ask_pipeline, _panel_record
    settings = _voice_settings()
    req = req or JoinRequest()
    record = _panel_record
    if req.interview_id and (record is None or record.interview_id != req.interview_id):
        record = store.load_record(req.interview_id)
        if record is None:
            raise HTTPException(404, "interview not found")
    if record is None:
        raise HTTPException(409, "no locked report yet — finish & score first")
    _teardown()
    _panel_record = record
    room = f"ask-{record.interview_id[:8]}-{uuid.uuid4().hex[:8]}"
    session = LiveKitSession(settings, room, "recruiter", recruiter=True,
                             on_event=_broker.publish)
    pipeline = AskPipeline(session, settings, record, on_event=_broker.publish)
    _start_voice(session, pipeline, pipeline.on_recruiter_joined)
    _session, _ask_pipeline = session, pipeline
    return _join_response(settings, room, "recruiter", [s.agent_id for s in record.report.scores])


@app.post("/panel/ask", response_model=AskAnswer)
def panel_ask(req: AskRequest):
    """Ask the Panel (§7): grounded Q&A over the locked record."""
    if _panel_record is None:
        raise HTTPException(409, "no locked report yet — finish & score first")
    target = req.target if req.mode == "addressed" else PanelRouter.detect(req.question)
    if target and target != "orchestrator":
        if target not in prompts.AGENTS:
            raise HTTPException(400, "unknown panel member")
        return ask_panel.answer(_panel_record, req.question, "addressed", target)
    return ask_panel.answer(_panel_record, req.question)


@app.post("/panel/counterfactual", response_model=WhatIfQuery)
def panel_counterfactual(req: CounterfactualRequest):
    """Counterfactual re-score (§7) — never mutates the locked scores."""
    if _panel_record is None:
        raise HTTPException(409, "no locked report yet")
    try:
        wq = ask_panel.counterfactual(_panel_record, req.turn, req.hypothetical, req.agent_id)
        _persist_panel_record()
        return wq
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/panel/override", response_model=Override)
def panel_override(req: OverrideRequest):
    """Log a recruiter override (§7). The original recommendation is kept."""
    if _panel_record is None:
        raise HTTPException(409, "no locked report yet")
    ov = ask_panel.override(_panel_record, req.decision, req.reason)
    _persist_panel_record()
    return ov


def _persist_panel_record():
    """Re-save the current record (metadata preserved) after a mutation."""
    if _panel_record is not None:
        try:
            store.save_record(_panel_record)
        except Exception:
            logger.exception("failed to persist override/what-if")


@app.get("/report", response_model=InterviewReport)
def get_report():
    if _report is None:
        raise HTTPException(404, "no report yet")
    return _report


# ---- Recruiter dashboard (Phase 8, §11) --------------------------------

@app.get("/interviews")
def list_interviews():
    """Rows for the recruiter dashboard: past interviews, newest first."""
    return store.list_summaries()


@app.post("/interviews/{interview_id}/open", response_model=InterviewReport)
@serialized
def open_interview(interview_id: str):
    """Open a stored interview: make it the active record (so Ask-the-Panel,
    override, counterfactual and voice-join all operate on it) and return its
    locked report."""
    global _panel_record, _report
    rec = store.load_record(interview_id)
    if rec is None:
        raise HTTPException(404, "interview not found")
    _teardown()
    _panel_record = rec
    _report = rec.report
    return rec.report


@app.post("/session/stop")
@serialized
def session_stop():
    _teardown()
    return {"status": "stopped"}


def _teardown():
    global _session, _pipeline, _ask_pipeline
    if _session is not None:
        try:
            _session.interrupt()
        except Exception:
            logger.warning("voice already disconnected during teardown")
    if _pipeline is not None:
        _pipeline.stop()
        _pipeline = None
    if _ask_pipeline is not None:
        _ask_pipeline.stop()
        _ask_pipeline = None
    if _session is not None:
        _session.stop()
        _session = None


# Serve the built React app (dist/) at "/". Mounted LAST so the API routes and
# the events WebSocket above take precedence; the SPA catches everything else.
# Absent in local frontend-dev mode (use `npm run dev`), so guard on existence.
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    logger.warning("WEB_DIR %s not found; not serving the frontend "
                   "(run `cd web && npm run dev` for the React app)", WEB_DIR)
