"""Central configuration. Every service imports settings from here.

Values come from environment variables (loaded from a .env file in dev).
Keeping this in one place means we never scatter os.getenv() calls around.
"""
from functools import lru_cache
from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # LiveKit Cloud or a self-hosted LiveKit server. Secrets never reach the browser.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_noise_cancellation: Literal["bvc", "nc", "off"] = "bvc"
    livekit_stt_model: str = "nova-3"
    livekit_stt_language: str = "en-US"
    livekit_endpointing_min_delay: float = Field(default=0.5, ge=0)
    livekit_endpointing_max_delay: float = Field(default=4.0, gt=0)
    deepgram_api_key: str = ""

    # Ask the Panel by phone. A LiveKit dispatch rule (media-worker/phone_setup.py)
    # sends calls to the panel's number to an agent of this name; the worker
    # registers under it at startup. Empty disables the phone line.
    phone_agent_name: str = "panel-phone"
    # Time zone the caller speaks in ("Vikram's 4:30 interview"), used to match
    # and read out interview times.
    local_timezone: str = "Asia/Kolkata"

    # Ask the Panel on WhatsApp, through Twilio (the sandbox for the demo). Twilio
    # reaches /whatsapp/webhook through the ngrok tunnel at ngrok_domain; nothing
    # else is served there. All three Twilio values are needed to enable it.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""            # e.g. whatsapp:+14155238886 (the sandbox)
    # Comma-separated numbers allowed to chat (+91…); empty = anyone who joined.
    whatsapp_allowed_numbers: str = ""
    ngrok_domain: str = ""

    # LLM. Every model call goes through the shared router in
    # shared/llm_router.py; never hardcode a model at a call site. Model ids may
    # carry a provider prefix ("ollama:" / "groq:" / "gemini:" / "openrouter:");
    # unprefixed ids default to OpenRouter. All providers are OpenAI-compatible.
    # Ollama Cloud (Pro plan) is the primary for both the fast path and the
    # reasoning model; Groq and Gemini are the fallbacks.
    ollama_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    # Optional pool of Groq keys (comma-separated) from separate accounts. The
    # router round-robins across them so the per-account tokens-per-minute limit
    # is shared out — same model/quality, ~N× the free budget. Falls back to the
    # single groq_api_key if unset.
    groq_api_keys: str = ""
    # Fast path (candidate is waiting): gpt-oss-120b on Ollama Cloud — no free-tier
    # tokens-per-minute cap, so no key pool needed. Falls back to the same model on
    # the Groq pool, then Gemini.
    llm_fast_model: str = "ollama:gpt-oss:120b"
    # Reasoning model (scoring, debate, conclusion, dossier): MiniMax M3 on Ollama
    # Cloud, called with thinking off. Benchmarked on the real prompts (Oct 2026):
    # clean JSON every run in ~2-10s and correct panel composition. GLM 5.3 /
    # 5.3-flash leak reasoning into the answer even with thinking off and blow
    # the token budget, so they're not used.
    llm_reasoning_model: str = "ollama:minimax-m3"
    # Fallbacks if the primary is down: Gemini 2.5 Flash, then gpt-oss (Groq pool).
    # Comma-separated.
    llm_reasoning_fallbacks: str = "gemini:gemini-2.5-flash,groq:openai/gpt-oss-120b"
    # Fast-path fallbacks, tried in order after llm_fast_model. Comma-separated.
    llm_fallback_chain: str = "groq:openai/gpt-oss-120b,gemini:gemini-2.5-flash"
    # Coding round (§8) vision models — gpt-oss is text-only, so these must be
    # image-capable. Two tiers: a FAST one for live screen reads mid-interview, and
    # a bigger one for the scoring pass at conclude (latency-insensitive). Both
    # verified free + vision-capable on OpenRouter (Gemma 4, no reasoning overhead).
    # No fallback chain (text models can't see) — coding degrades to verbal on fail.
    llm_vision_fast: str = "openrouter:google/gemma-4-31b-it:free"
    llm_vision_model: str = "openrouter:google/gemma-4-31b-it:free"
    # Gemini Live (§8): realtime screen-watch + voice for the coding round, run in
    # the browser via a server-minted ephemeral token. If the key is unset the
    # coding round falls back to the snapshot-vision path above.
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    # Free vision tiers rate-limit (429) often, so a vision read falls through this
    # chain before giving up (all verified free + image-capable on OpenRouter).
    llm_vision_fallbacks: str = "openrouter:google/gemma-4-31b-it:free"

    # Barge-in (interrupt the agent by talking over it). Needs the candidate on
    # headphones — otherwise the agent's own voice echoes into the mic and
    # false-triggers it. Set false for robust half-duplex on speakers: the agent
    # can't be interrupted, but echo can't derail the interview either.
    allow_bargein: bool = True

    # Floor control (ARCHITECTURE §5). λ (coverage weight) ramps from start→end
    # across the interview so late questions chase what we still don't know.
    interview_time_budget_s: int = 1200
    coverage_lambda_start: float = 0.5
    coverage_lambda_end: float = 1.5

    # Conviction (ARCHITECTURE §6): STRONG if |score-0.5| > margin AND
    # evidence_count >= min_evidence, else NEUTRAL. Computed in code, not asked.
    conviction_margin: float = 0.25
    conviction_min_evidence: int = 3

    # Phase 1 tunables
    media_worker_port: int = 8080

    # Phase 8: where locked interviews are persisted (one JSON per interview), so
    # the recruiter dashboard can list past candidates and rejoin. Mounted as a
    # volume in docker-compose so it survives container rebuilds.
    data_dir: str = "/app/data"

    model_config = SettingsConfigDict(
        # /app/.env is the mounted file inside the container (docker-compose);
        # ".env" is the local file for non-Docker runs. Reading the file at
        # startup (instead of baked env vars) means editing .env + a restart
        # applies changes — no container recreate needed.
        env_file=("/app/.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def fallback_models(self) -> list[str]:
        """Parsed fallback chain: primary tried first, then these in order."""
        return [m.strip() for m in self.llm_fallback_chain.split(",") if m.strip()]

    @model_validator(mode="after")
    def validate_voice(self):
        if self.livekit_url and not self.livekit_url.startswith(("ws://", "wss://")):
            raise ValueError("LIVEKIT_URL must start with ws:// or wss://")
        if self.livekit_endpointing_max_delay < self.livekit_endpointing_min_delay:
            raise ValueError("LiveKit maximum endpointing delay must be >= minimum delay")
        return self

    @property
    def reasoning_fallbacks(self) -> list[str]:
        """Reasoning models tried (in order) if the primary reasoning model fails,
        before the final gpt-oss safety net."""
        return [m.strip() for m in self.llm_reasoning_fallbacks.split(",") if m.strip()]

    @property
    def vision_fallbacks(self) -> list[str]:
        """Extra vision models tried (in order) if the primary vision model fails."""
        return [m.strip() for m in self.llm_vision_fallbacks.split(",") if m.strip()]

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token
                    and self.twilio_whatsapp_from)

    def whatsapp_allows(self, sender: str) -> bool:
        """Is this sender ("whatsapp:+91…") allowed to chat with the panel?"""
        allowed = {n.strip() for n in self.whatsapp_allowed_numbers.split(",") if n.strip()}
        return not allowed or sender.removeprefix("whatsapp:").strip() in allowed

    @property
    def groq_keys(self) -> list[str]:
        """All Groq keys to round-robin over (pool, else the single key)."""
        pool = [k.strip() for k in self.groq_api_keys.split(",") if k.strip()]
        if pool:
            return pool
        return [self.groq_api_key] if self.groq_api_key else []


@lru_cache
def get_settings() -> Settings:
    """Cached so we parse the environment only once per process."""
    return Settings()
