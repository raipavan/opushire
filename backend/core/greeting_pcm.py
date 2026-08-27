"""HTTP client for Gemini greeting pre-cache — prefer Live capture so opener matches the call."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

import httpx
from loguru import logger

from config import settings
from core.state import _CAMPAIGN_DATA

_TTS_HTTP_CLIENT: Optional[httpx.AsyncClient] = None


async def get_tts_client() -> httpx.AsyncClient:
    global _TTS_HTTP_CLIENT
    if _TTS_HTTP_CLIENT is None or _TTS_HTTP_CLIENT.is_closed:
        _TTS_HTTP_CLIENT = httpx.AsyncClient(timeout=15.0)
    return _TTS_HTTP_CLIENT


def _greetings_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "greetings"


# Live bridge assumes 24 kHz for ``greeting_{role}.pcm`` unless a sidecar meta gives ``sr``.
STORED_GREETING_DEFAULT_SR = 24000


def _text_hash(text: str) -> str:
    return hashlib.md5((text or "").strip().encode()).hexdigest()[:16]


def greeting_pcm_paths(role: str, variant: str = "") -> tuple[Path, Path]:
    """PCM + meta paths; ``variant='inbound'`` → ``greeting_{role}_inbound.pcm``."""
    r = (role or "data_edge").strip().lower()
    v = (variant or "").strip().lower()
    suffix = f"_{v}" if v else ""
    base = _greetings_base_dir()
    stem = f"greeting_{r}{suffix}"
    return base / f"{stem}.pcm", base / f"{stem}.pcm.meta"


def _greeting_meta_matches(meta: dict, text: str) -> bool:
    """If meta has text_hash, require it to match current greeting text."""
    want = _text_hash(text)
    if not want:
        return True
    stored = str(meta.get("text_hash") or "").strip()
    if not stored:
        return True
    return stored == want


def load_recorded_greeting_pcm(
    role: str,
    variant: str = "",
    *,
    greeting_text: str = "",
) -> Optional[Tuple[bytes, int]]:
    """Read ``greeting_{role}[_variant].pcm`` if present and (optionally) text still matches."""
    path, meta_path = greeting_pcm_paths(role, variant)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Invalid greeting meta {}: {}", meta_path, exc)
    if greeting_text.strip() and not _greeting_meta_matches(meta, greeting_text):
        logger.info(
            "Skipping greeting PCM for role={} — text changed (stored source={})",
            role,
            meta.get("source"),
        )
        return None
    sr = int(meta.get("sr", STORED_GREETING_DEFAULT_SR))
    try:
        pcm = path.read_bytes()
        if not pcm:
            return None
        label = f"{role}" + (f" ({variant})" if variant else "")
        logger.info(
            "Loaded recorded greeting for role={} ({} bytes, sr={}, source={})",
            label,
            len(pcm),
            sr,
            meta.get("source", "unknown"),
        )
        return pcm, sr
    except Exception as exc:
        logger.warning("Failed to read greeting PCM for role={}: {}", role, exc)
        return None


def _get_greeting_cache_path(role: str) -> Path:
    base_dir = _greetings_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"greeting_{role}_latest.pcm"


def _get_greeting_cache_metadata_path(role: str) -> Path:
    return _greetings_base_dir() / f"greeting_{role}_latest.meta"


def _write_greeting_cache_files(
    role: str,
    text: str,
    pcm: bytes,
    sr: int,
    *,
    source: str,
    voice: str,
) -> None:
    """Persist to both ``greeting_{role}.pcm`` (calls use this) and ``_latest`` cache."""
    h = _text_hash(text)
    meta = {
        "text_hash": h,
        "voice": voice,
        "sr": int(sr),
        "source": source,
        "model": settings.gemini_live_model if source == "gemini_live_capture" else settings.gemini_tts_model,
    }
    pcm_path, meta_path = greeting_pcm_paths(role)
    latest_pcm = _get_greeting_cache_path(role)
    latest_meta = _get_greeting_cache_metadata_path(role)
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    pcm_path.write_bytes(pcm)
    meta_path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    latest_pcm.write_bytes(pcm)
    latest_meta.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    logger.info(
        "Wrote greeting cache for role={} source={} voice={} ({} bytes)",
        role,
        source,
        voice,
        len(pcm),
    )


async def _generate_and_cache_greeting(role: str, text: str, voice: str) -> Optional[Tuple[bytes, int]]:
    """Cache opening audio — **Gemini Live capture** first (same voice as the call),
    then **REST TTS** fallback if Live capture fails."""
    text = (text or "").strip()
    if not text:
        return None

    live_voice = (settings.gemini_live_voice or "Leda").strip()

    # 1) Try Gemini Live capture (same voice as the call)
    try:
        from services.live_greeting_capture import capture_live_greeting_pcm

        logger.info("Capturing greeting via Gemini Live for role={} (matches call voice)", role)
        pcm, sr = await capture_live_greeting_pcm(role, text)
        _write_greeting_cache_files(role, text, pcm, sr, source="gemini_live_capture", voice=live_voice)
        return pcm, sr
    except Exception as live_exc:
        logger.warning(
            "Live greeting capture failed for role={} — trying REST TTS fallback: {}",
            role,
            live_exc,
        )

    # 2) Fallback: REST TTS (different voice engine but better than silence)
    try:
        from services.gemini_tts import gemini_synthesize_pcm, get_gemini_tts_httpx

        tts_client = await get_gemini_tts_httpx()
        tts_voice = (settings.gemini_tts_voice or voice or "Leda").strip()
        logger.info("Generating greeting via REST TTS fallback for role={} voice={}", role, tts_voice)
        pcm, sr = await gemini_synthesize_pcm(
            tts_client,
            text=text,
            voice=tts_voice,
            style_mode="opening",
        )
        _write_greeting_cache_files(role, text, pcm, sr, source="gemini_tts_rest", voice=tts_voice)
        logger.info("REST TTS fallback greeting generated for role={} ({} bytes, sr={})", role, len(pcm), sr)
        return pcm, sr
    except Exception as tts_exc:
        logger.error(
            "REST TTS fallback also failed for role={} — no greeting audio will be available: {}",
            role,
            tts_exc,
        )
        return None


def _load_cached_greeting(role: str, text: str) -> Optional[Tuple[bytes, int]]:
    """Prefer on-disk ``greeting_{role}.pcm``, then ``_latest`` cache."""
    on_disk = load_recorded_greeting_pcm(role, greeting_text=text)
    if on_disk:
        return on_disk

    try:
        cache_path = _get_greeting_cache_path(role)
        meta_path = _get_greeting_cache_metadata_path(role)
        if not cache_path.exists() or not meta_path.exists():
            return None
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if not _greeting_meta_matches(meta, text):
            logger.info("Greeting text changed for {}, will regenerate", role)
            return None
        with open(cache_path, "rb") as f:
            pcm = f.read()
        sr = int(meta.get("sr", 24000))
        if str(meta.get("source")) == "gemini_tts_rest":
            logger.warning(
                "Loaded REST-TTS cached greeting for {} — consider re-capturing with Live in Configuration",
                role,
            )
        return pcm, sr
    except Exception as e:
        logger.warning("Failed to load cached greeting for {}: {}", role, e)
        return None


async def prewarm_opening(call_id: str, text: str, voice: str) -> None:
    try:
        role = "data_edge"
        if call_id in _CAMPAIGN_DATA:
            role = _CAMPAIGN_DATA[call_id].get("_role", "data_edge")

        cached = _load_cached_greeting(role, text)
        if cached:
            pcm, sr = cached
            if call_id in _CAMPAIGN_DATA:
                _CAMPAIGN_DATA[call_id]["opening_pcm"] = (pcm, sr)
                logger.info("Pre-warmed opening for {} from cache: {} bytes", call_id, len(pcm))
            return

        result = await _generate_and_cache_greeting(role, text, voice)
        if result and call_id in _CAMPAIGN_DATA:
            pcm, sr = result
            _CAMPAIGN_DATA[call_id]["opening_pcm"] = (pcm, sr)
            logger.info("Pre-warmed opening for {} (newly cached): {} bytes", call_id, len(pcm))
    except Exception as e:
        logger.warning("Pre-warm failed for {}: {}", call_id, e)
