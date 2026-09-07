"""Agora Conversational AI Engine (CAI) client.

This is the piece the hackathon is actually about: instead of running our own
STT -> LLM -> TTS loop in-process (see agora_session.py + pipeline.py), we ask
Agora's **Conversational AI Engine** to run a managed voice agent inside the RTC
channel. Agora does the ASR, turn-taking, TTS and RTC; the *brain* stays ours —
CAI calls back into our OpenAI-compatible endpoint (main.py: /cai/llm/...).

Flow:
  1. We mint a normal RTC token for the agent's uid (same App ID + Certificate).
  2. POST .../conversational-ai-agent/v2/projects/{appid}/join with an `asr`,
     `llm` (custom, style="openai", url -> our public endpoint), and `tts` config.
  3. Agora's agent joins the channel, listens to the human, streams transcripts
     to our LLM endpoint, speaks the reply back. We keep the returned agent_id.
  4. POST .../agents/{agent_id}/leave to stop it.

Auth is HTTP Basic with base64(CUSTOMER_ID:CUSTOMER_SECRET) — the Console
"RESTful API" secret, NOT the App Certificate.
"""
import base64
import logging
import requests

from shared.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def enabled(settings: Settings) -> bool:
    """CAI is usable only when we have the REST creds AND a public URL the cloud
    agent can call back on."""
    return bool(settings.agora_customer_id and settings.agora_customer_secret
                and settings.public_base_url)


def _auth_header(settings: Settings) -> str:
    raw = f"{settings.agora_customer_id}:{settings.agora_customer_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _asr_config(settings: Settings) -> dict:
    # Deepgram ASR. Key passed inline (self-managed / BYOK) so we don't have to
    # pre-upload it in the Agora Console. Agora overrides encoding/sample_rate.
    return {
        "vendor": settings.cai_asr_vendor,
        "params": {
            "url": "wss://api.deepgram.com/v1/listen",
            "api_key": settings.deepgram_api_key,
            "model": settings.cai_asr_model,
            "language": settings.cai_asr_language,
        },
    }


def _tts_config(settings: Settings) -> dict:
    # Deepgram Aura via CAI (Beta vendor); inline key, WebSocket speak endpoint.
    return {
        "vendor": settings.cai_tts_vendor,
        "params": {
            "api_key": settings.deepgram_api_key,
            "base_url": "wss://api.deepgram.com/v1/speak",
            "model": settings.cai_tts_model,
            "sample_rate": settings.audio_sample_rate,
        },
    }


def _turn_detection_config() -> dict:
    """Stop CAI endpointing on short pauses (the "empty/filler turn" interruptions).
    Semantic mode waits for a complete thought; the longer silence window + pause
    intent ("hold on") keep it from cutting the recruiter off mid-sentence."""
    return {
        "mode": "default",
        "config": {
            "end_of_speech": {
                "mode": "semantic",
                "semantic_config": {
                    "silence_duration_ms": 800,
                    "max_wait_ms": 4000,
                    "pause_state_enabled": True,
                },
            }
        },
    }


def _llm_config(settings: Settings, llm_path: str, system_prompt: str,
                greeting: str) -> dict:
    """Point CAI at OUR endpoint. style=openai + our url makes the engine speak
    the OpenAI /chat/completions protocol to us; we run the panel brain there."""
    url = settings.public_base_url.rstrip("/") + llm_path
    return {
        "url": url,
        "style": "openai",
        "system_messages": [{"role": "system", "content": system_prompt}],
        "greeting_message": greeting,
        "failure_message": "Sorry, I lost that — could you say it again?",
        "max_history": 16,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "params": {"model": "panel-brain"},
    }


def start_agent(settings: Settings, *, channel: str, token: str, agent_uid: int,
                remote_uid: int, llm_path: str, system_prompt: str,
                greeting: str, name: str) -> str:
    """Start a CAI agent in `channel`. Returns the agent_id (needed to stop it).
    Raises on failure so the caller can fall back to the in-process pipeline."""
    url = (f"{settings.agora_cai_base_url}/api/conversational-ai-agent/v2"
           f"/projects/{settings.agora_app_id}/join")
    props = {
        "channel": channel,
        "token": token,
        "agent_rtc_uid": str(agent_uid),
        "remote_rtc_uids": [str(remote_uid)],
        "enable_string_uid": False,
        "idle_timeout": 120,
        "asr": _asr_config(settings),
        "llm": _llm_config(settings, llm_path, system_prompt, greeting),
        "tts": _tts_config(settings),
        "turn_detection": _turn_detection_config(),
    }
    headers = {"Authorization": _auth_header(settings), "Content-Type": "application/json"}
    logger.info("CAI: starting agent on channel=%s → llm=%s", channel,
                props["llm"]["url"])

    resp = requests.post(url, json={"name": name, "properties": props},
                         timeout=_TIMEOUT, headers=headers)
    # turn_detection's schema is the least-documented part; if Agora rejects the
    # body, retry once WITHOUT it so a bad tuning field can never cost us the agent.
    if resp.status_code >= 300 and "turn_detection" in props:
        logger.warning("CAI join rejected (%s) with turn_detection; retrying without it: %s",
                       resp.status_code, resp.text[:300])
        props.pop("turn_detection", None)
        resp = requests.post(url, json={"name": name, "properties": props},
                             timeout=_TIMEOUT, headers=headers)
    if resp.status_code >= 300:
        logger.error("CAI join failed %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    data = resp.json()
    agent_id = data.get("agent_id") or data.get("agentId") or ""
    logger.info("CAI: agent started id=%s status=%s (turn_detection=%s)",
                agent_id, data.get("status"), "turn_detection" in props)
    return agent_id


def stop_agent(settings: Settings, agent_id: str) -> None:
    """Stop a running CAI agent. Best-effort — logs but never raises."""
    if not agent_id:
        return
    url = (f"{settings.agora_cai_base_url}/api/conversational-ai-agent/v2"
           f"/projects/{settings.agora_app_id}/agents/{agent_id}/leave")
    try:
        resp = requests.post(url, timeout=_TIMEOUT,
                             headers={"Authorization": _auth_header(settings)})
        logger.info("CAI: stop agent %s → %s", agent_id, resp.status_code)
    except Exception:
        logger.exception("CAI: failed to stop agent %s", agent_id)
