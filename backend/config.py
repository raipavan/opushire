"""Central configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
# Resolved once at import — use for frontend paths (avoid counting Path.parents per route file).
REPO_ROOT = _REPO_ROOT
FRONTEND_DIR = REPO_ROOT / "frontend"

# Local dev often keeps secrets in repo-root `.env` while running `uvicorn` from `backend/`.
# Repo fills defaults; `backend/.env` overrides when both define the same key.
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_BACKEND_DIR / ".env", override=True)
load_dotenv(override=True)


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=False)
class Settings:
    """Runtime settings for Vernika AI voice agent."""

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    server_url: str = os.getenv("SERVER_URL", "http://localhost:8001")

    # When false: no RAG append and no live keyword RAG on Vobiz.
    rag_enabled: bool = _b("RAG_ENABLED", True)
    # When true: skip per-turn live RAG injection during Gemini Live calls.
    # Saves 50-100ms per turn by avoiding SQLite FTS lookups on the hot path.
    # The system prompt still contains the full KB via role prompt — this only
    # affects the per-turn dynamic RAG supplement.
    rag_live_low_latency: bool = _b("RAG_LIVE_LOW_LATENCY", False)
    rag_db_path: str = os.getenv("RAG_DB_PATH", str(_BACKEND_DIR / "data" / "rag.db"))
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "4"))
    rag_max_context_chars: int = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "3600"))

    # Call recordings
    call_recording_enabled: bool = _b("CALL_RECORDING_ENABLED", True)
    call_recording_dir: str = os.getenv(
        "CALL_RECORDING_DIR", str(_BACKEND_DIR / "data" / "call_recordings")
    )

    # Gemini API — Google AI Studio key (speech & text)
    gemini_api_key: str = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    # Post-call transcript analysis (SQLite / manual-call modal + campaign summaries).
    # Default: hosted Gemma 4 — 26B MoE instruct (REST generateContent). Override with GEMINI_CALL_ANALYSIS_MODEL.
    gemini_call_analysis_model: str = os.getenv(
        "GEMINI_CALL_ANALYSIS_MODEL", "gemini-2.5-flash"
    ).strip()
    # Separate API key for call analysis — isolates its quota from Live / TTS.
    gemini_call_analysis_api_key: str = (
        os.getenv("GEMINI_CALL_ANALYSIS_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()
    # Separate API key for TTS pre-warm (greeting generation) - allows using different quota
    gemini_tts_api_key: str = (os.getenv("GEMINI_TTS_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    gemini_tts_model: str = os.getenv("GEMINI_TTS_MODEL", "gemini-2.0-flash").strip()
    # Fallback TTS model when the primary hits 429 / RESOURCE_EXHAUSTED.
    # Must be a model with a *separate* quota bucket from the primary so the
    # daily limit of one doesn't block us. Set to "" to disable fallback.
    gemini_tts_fallback_model: str = os.getenv(
        "GEMINI_TTS_FALLBACK_MODEL", ""
    ).strip()
    gemini_tts_voice: str = os.getenv("GEMINI_TTS_VOICE", "Leda").strip()
    # In-call TTS style. Front-loaded with "Indian English accent" because
    # Gemini's voices default to American when accent guidance is buried.
    gemini_tts_style_prompt: str = os.getenv(
        "GEMINI_TTS_STYLE_PROMPT",
        "INDIAN ENGLISH ACCENT — speak like a young woman from Bangalore in her "
        "late twenties. This is Indian English, NOT American or British. Pronounce "
        "words the Indian way (e.g. 'better', 'water', 'really' as an Indian speaker "
        "would, not a US speaker). Voice is warm, friendly, conversational. Speak "
        "at a natural conversational pace, neither slow nor rushed. When Hindi or "
        "Hinglish appears, pronounce it naturally as a native Indian speaker.",
    ).strip()
    # Opening greeting style — extra explicit because the greeting is very short
    # (~1 sentence) and Gemini has little context to lean on. Without this the
    # opener can come out US-accented even when the in-call audio is Indian.
    gemini_tts_opening_style: str = os.getenv(
        "GEMINI_TTS_OPENING_STYLE",
        "INDIAN ENGLISH ACCENT, FEMALE, BANGALORE — read this opening greeting in a "
        "warm, friendly, natural Indian English accent (a young Indian woman in her "
        "late twenties from Bangalore). This is Indian English, NOT American, NOT "
        "British. Pronounce every word the Indian way. Speak at a natural "
        "conversational pace, neither slow nor rushed. Do not sound American.",
    ).strip()
    gemini_tts_min_emit_ms: int = int(os.getenv("GEMINI_TTS_MIN_EMIT_MS", "40"))
    tts_provider: str = "gemini"

    # IANA zone for interpreting recalled times from transcripts ("5pm", "tomorrow 9am").
    transcript_callback_tz: str = (os.getenv("TRANSCRIPT_CALLBACK_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata")

    # Outbound campaign quiet hours (hard block). Default: no dialing 20:30–09:30 local TZ.
    campaign_quiet_hours_enabled: bool = _b("CAMPAIGN_QUIET_HOURS_ENABLED", True)
    campaign_quiet_start: str = (os.getenv("CAMPAIGN_QUIET_START", "20:30").strip() or "20:30")
    campaign_quiet_end: str = (os.getenv("CAMPAIGN_QUIET_END", "09:30").strip() or "09:30")

    # Gemini Live API (native speech-to-speech for sub-800ms latency on phone calls)
    # Valid Google AI Studio Multimodal Live model: models/gemini-2.0-flash-exp
    gemini_live_model: str = (
        "models/gemini-2.0-flash-exp"
        if ("3.1" in os.getenv("GEMINI_LIVE_MODEL", "") or "3-1" in os.getenv("GEMINI_LIVE_MODEL", ""))
        else os.getenv("GEMINI_LIVE_MODEL", "models/gemini-2.0-flash-exp").strip()
    )
    gemini_live_voice: str = os.getenv("GEMINI_LIVE_VOICE", "Leda").strip()
    gemini_live_language_code: str = os.getenv("GEMINI_LIVE_LANGUAGE_CODE", "en-IN").strip()
    # When True: skip disk/primed PCM opener — Gemini Live speaks the greeting (same engine as the call).
    # When False (default): use greeting_{role}.pcm / REST TTS prewarm — more reliable first audio on some carriers.
    gemini_live_first_opening: bool = _b("GEMINI_LIVE_FIRST_OPENING", False)
    # Turn-taking / barge-in: HIGH sensitivity Activity Detection + optional tighter profile (default on).
    gemini_live_aggressive_activity_detection: bool = _b("GEMINI_LIVE_AGGRESSIVE_ACTIVITY_DETECTION", True)
    gemini_live_vad_prefix_padding_ms: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_MS", "150"))
    gemini_live_vad_silence_duration_ms: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_MS", "300"))
    gemini_live_vad_prefix_padding_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_ULTRA_MS", "100"))
    gemini_live_vad_silence_duration_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_ULTRA_MS", "200"))
    # Appended system text nudging concise turns + yield-on-overlap (phone calls).
    gemini_live_append_turn_instructions: bool = _b("GEMINI_LIVE_APPEND_TURN_INSTRUCTIONS", True)
    # When no scripted PCM opening: brief gate before forwarding callee mic → Gemini (avoids chopping first model syllable).
    # Lowered from 0.5s → 0.15s → 0.05s to reduce response delay.
    vobiz_gemini_live_forward_mute_seconds: float = float(
        os.getenv("VOBIZ_GEMINI_FORWARD_MUTE_SECONDS", "0.05")
    )

    # Playout jitter buffer safety margin in seconds (default: 0.06 = 60ms).
    # Lowered from 0.40s → 0.12s → 0.06s to reduce first-audio-chunk latency.
    # 60ms = 3 packets @ 20ms — sufficient for most network conditions.
    # Increase to 0.12-0.20 only if audio stuttering occurs on high-jitter networks.
    vobiz_playout_prebuffer_seconds: float = float(
        os.getenv("VOBIZ_PLAYOUT_PREBUFFER_SECONDS", "0.06")
    )
    # Browser voice test prebuffer (default: 0.150 = 150ms).
    # Accumulates this much audio before starting browser playout.
    # Higher values reduce stuttering but increase latency.
    browser_voice_prebuffer_seconds: float = float(
        os.getenv("BROWSER_VOICE_PREBUFFER_SECONDS", "0.150")
    )

    # Conversation logging
    conversation_log_enabled: bool = _b("CONVERSATION_LOG_ENABLED", True)
    conversation_log_dir: str = os.getenv(
        "CONVERSATION_LOG_DIR", str(_BACKEND_DIR / "data" / "conversation_logs")
    )

    # Optional outbound bed noise under voice — **off by default**. Set BACKGROUND_MUSIC_ENABLED=1
    # plus BACKGROUND_MUSIC_PATH / BACKGROUND_MUSIC_VOLUME to re-enable (see live_session mixer).
    background_music_enabled: bool = _b("BACKGROUND_MUSIC_ENABLED", False)
    background_music_path: str = os.getenv("BACKGROUND_MUSIC_PATH", "").strip()
    background_music_volume: float = float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0"))

    # Vobiz Telephony — Global Fallback
    vobiz_auth_id: str = os.getenv("VOBIZ_AUTH_ID", "").strip()
    vobiz_auth_token: str = os.getenv("VOBIZ_AUTH_TOKEN", "").strip()
    vobiz_from_number: str = os.getenv("VOBIZ_FROM_NUMBER", "").strip()
    vobiz_public_base_url: str = os.getenv("VOBIZ_PUBLIC_BASE_URL", "").strip()
    # Origin for Vobiz <Stream> WebSocket only (may differ from callback URL).
    # Quick tunnels often accept POST /vobiz/answer but fail WebSocket upgrades from carrier POPs.
    vobiz_stream_public_base_url: str = os.getenv("VOBIZ_STREAM_PUBLIC_BASE_URL", "").strip().rstrip("/")

    # Vobiz Telephony — Data Edge role (Priya / career counselor)
    vobiz_data_edge_auth_id: str = (
        os.getenv("VOBIZ_DATA_EDGE_AUTH_ID", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_AUTH_ID", "").strip()
    )
    vobiz_data_edge_auth_token: str = (
        os.getenv("VOBIZ_DATA_EDGE_AUTH_TOKEN", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_AUTH_TOKEN", "").strip()
    )
    vobiz_data_edge_from_number: str = (
        os.getenv("VOBIZ_DATA_EDGE_FROM_NUMBER", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_FROM_NUMBER", "").strip()
    )

    # Legacy alias (real_estate console role / old env names)
    vobiz_real_estate_auth_id: str = os.getenv("VOBIZ_REAL_ESTATE_AUTH_ID", "").strip()
    vobiz_real_estate_auth_token: str = os.getenv("VOBIZ_REAL_ESTATE_AUTH_TOKEN", "").strip()
    vobiz_real_estate_from_number: str = os.getenv("VOBIZ_REAL_ESTATE_FROM_NUMBER", "").strip()

    # Legacy multi-role fields (unused for data_edge, kept for attribute safety)
    vobiz_buyers_auth_id: str = os.getenv("VOBIZ_BUYERS_AUTH_ID", "").strip()
    vobiz_buyers_auth_token: str = os.getenv("VOBIZ_BUYERS_AUTH_TOKEN", "").strip()
    vobiz_buyers_from_number: str = os.getenv("VOBIZ_BUYERS_FROM_NUMBER", "").strip()

    # Opening/greeting line for outbound calls
    vobiz_opening_line_default: str = os.getenv("VOBIZ_OPENING_LINE_DEFAULT", "").strip()

    # Dariaan — auto book discovery call + WhatsApp Meet link (vernikaai / Interested only)
    whatsapp_proxy_enabled: bool = _b("WHATSAPP_PROXY_ENABLED", False)
    whatsapp_proxy_url: str = os.getenv("WHATSAPP_PROXY_URL", "http://127.0.0.1:3001").strip()
    whatsapp_proxy_secret: str = os.getenv("WHATSAPP_PROXY_SECRET", "").strip()

    # Meta WhatsApp Cloud API
    whatsapp_access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    whatsapp_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    whatsapp_inbound_leads_enabled: bool = _b("WHATSAPP_INBOUND_LEADS_ENABLED", True)
    whatsapp_auto_dial_dariaan: bool = _b("WHATSAPP_AUTO_DIAL_DARIAAN", False)
    dariaan_whatsapp_number: str = os.getenv("DARIAAN_WHATSAPP_NUMBER", "").strip()
    dariaan_whatsapp_qr_message: str = os.getenv("DARIAAN_WHATSAPP_QR_MESSAGE", "").strip()

    # OpenWA API Gateway (replaces old whatsapp-proxy sidecar)
    openwa_enabled: bool = _b("OPENWA_ENABLED", False)
    openwa_api_url: str = os.getenv("OPENWA_API_URL", "http://127.0.0.1:2785").strip()
    openwa_api_key: str = os.getenv("OPENWA_API_KEY", "").strip()
    openwa_session_id: str = os.getenv("OPENWA_SESSION_ID", "").strip()

    # WhatsApp auto-send on Interested disposition
    whatsapp_auto_send_enabled: bool = _b("WHATSAPP_AUTO_SEND_ON_INTERESTED", False)
    whatsapp_project_image_path: str = os.getenv("WHATSAPP_PROJECT_IMAGE_PATH", "").strip()
    whatsapp_project_brochure_path: str = os.getenv("WHATSAPP_PROJECT_BROCHURE_PATH", "").strip()
    # New: video + multiple documents (replaces legacy image/brochure when set)
    whatsapp_project_video_path: str = os.getenv("WHATSAPP_PROJECT_VIDEO_PATH", "").strip()
    whatsapp_project_doc_paths: str = os.getenv("WHATSAPP_PROJECT_DOC_PATHS", "").strip()
    whatsapp_project_details_body: str = os.getenv("WHATSAPP_PROJECT_DETAILS_BODY", "").strip()
    whatsapp_project_greeting_template: str = os.getenv(
        "WHATSAPP_PROJECT_GREETING_TEMPLATE",
        "Hi {name}, Thank you for your interest.",
    ).strip()


settings = Settings()


async def tts_engine_ready(provider: str, timeout: float = 2.0) -> bool:
    """Return whether a specific TTS engine can be used (only ``gemini``)."""
    p = (provider or "").strip().lower()
    if p == "gemini":
        return bool(settings.gemini_api_key)
    return False


async def tts_backend_healthy(timeout: float = 2.0) -> bool:
    """Voice UI gate for the configured default ``settings.tts_provider``."""
    return await tts_engine_ready(settings.tts_provider, timeout=timeout)


def server_url_to_ws(url: str, path: str = "/ws") -> str:
    """Turn https://host into wss://host/path for Vobiz stream."""
    u = url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://") :] + path
    if u.startswith("http://"):
        return "ws://" + u[len("http://") :] + path
    return u + path


def validate_critical_config() -> list[str]:
    """Return list of human-readable configuration problems (empty if OK)."""
    problems: list[str] = []
    if not settings.gemini_api_key:
        problems.append("GEMINI_API_KEY / GOOGLE_API_KEY is not set")
    elif not settings.gemini_api_key.startswith("AIza") and not settings.gemini_api_key.startswith("AQ."):
        problems.append(
            f"GEMINI_API_KEY starts with '{settings.gemini_api_key[:6]}…' — expected 'AIza…' or 'AQ.' format. "
            "Ensure this is a valid Google AI Studio API key."
        )
    vb = (
        settings.vobiz_auth_id
        and settings.vobiz_auth_token
        and settings.vobiz_from_number
    )
    if vb and not settings.vobiz_public_base_url:
        problems.append(
            "Vobiz is partially configured (auth/from set) but VOBIZ_PUBLIC_BASE_URL is empty — "
            "outbound calls cannot deliver answer XML or media WebSocket."
        )
    if vb and settings.vobiz_public_base_url and "proxy.runpod.net" in settings.vobiz_public_base_url:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL uses RunPod HTTP proxy, which may not work externally. "
            "Consider switching to a Cloudflare tunnel or direct domain."
        )
    ts = settings.vobiz_stream_public_base_url or ""
    pub = settings.vobiz_public_base_url or ""
    if vb and pub and ("trycloudflare.com" in pub or "trycloudflare.dev" in pub) and not ts:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL looks like a Cloudflare quick tunnel — media WebSockets often never "
            "reach your server (calls ring then drop). Set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http(s) origin with port (e.g. http://YOUR_IP:8001) while keeping callbacks on the tunnel "
            "if needed, or switch fully to a stable domain."
        )
    # Check for Hostinger proxy which blocks WebSockets
    if (pub and "hstgr.cloud" in pub.lower()) or (ts and "hstgr.cloud" in ts.lower()):
        if not ts or "hstgr.cloud" in ts.lower():
            problems.append(
                "Hostinger shared domain (.hstgr.cloud) detected for Vobiz media streaming! "
                "Hostinger's proxy blocks WebSocket upgrade requests (101 Switching Protocols), causing PITCH SILENCE on calls. "
                "Set VOBIZ_PUBLIC_BASE_URL=http://31.97.186.20:8001 and VOBIZ_STREAM_PUBLIC_BASE_URL=http://31.97.186.20:8001 to bypass the proxy."
            )
    # Warn about empty stream URL (non-fatal but important)
    elif vb and pub and not ts:
        problems.append(
            "VOBIZ_STREAM_PUBLIC_BASE_URL is empty — media WebSocket will route through VOBIZ_PUBLIC_BASE_URL. "
            "If your domain/hosting does NOT support WebSocket upgrades, calls will connect but produce SILENCE. "
            "Set VOBIZ_STREAM_PUBLIC_BASE_URL=http://31.97.186.20:8001 for direct WS media."
        )
    # Silence hangup too short
    silence_sec = float(os.getenv("CALL_SILENCE_HANGUP_SEC", "30"))
    if silence_sec < 20:
        problems.append(
            f"CALL_SILENCE_HANGUP_SEC={silence_sec}s is very short — may hang up before AI can respond. "
            "Recommended: 30-60s."
        )
    return problems
