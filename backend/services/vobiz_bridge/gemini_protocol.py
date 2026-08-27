"""Gemini Live WebSocket URL, setup payload, and small realtime-input helpers."""

from __future__ import annotations

import base64
import json
from typing import Any

from config import settings

from .constants import OUT_CHUNK_BYTES, VOBIZ_SR

GEMINI_LIVE_URL_TMPL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    "?key={api_key}"
)


def build_live_setup(
    *,
    model: str,
    system_instruction: str,
    voice: str,
    language_code: str,
    vad_ultra: bool = False,
) -> dict:
    """Build Gemini Live ``setup``. When aggressive VAD is enabled in settings,

    Uses ``HIGH`` sensitivity for start/end of speech plus tuneable silence/prefix
    (see ``GEMINI_LIVE_*`` env vars). VAD settings are consistent across all call types
    to ensure uniform audio quality.

    When ``vad_ultra=True``, uses tighter prefix padding and silence duration for
    lower-latency turn detection (outbound calls where callee responds quickly).
    """

    realtime_input_config: dict[str, Any] = {}
    if settings.gemini_live_aggressive_activity_detection:
        import os
        start_sens = os.getenv("GEMINI_LIVE_VAD_START_SENSITIVITY", "START_SENSITIVITY_BALANCED").strip()
        end_sens = os.getenv("GEMINI_LIVE_VAD_END_SENSITIVITY", "END_SENSITIVITY_BALANCED").strip()

        # Telephony-tuned timing: prevents phone speaker echo / micro-noise from cutting off AI mid-sentence
        if vad_ultra:
            prefix_ms = max(100, int(settings.gemini_live_vad_prefix_padding_ms_ultra))
            silence_ms = max(200, int(settings.gemini_live_vad_silence_duration_ms_ultra))
        else:
            prefix_ms = max(150, int(settings.gemini_live_vad_prefix_padding_ms))
            silence_ms = max(300, int(settings.gemini_live_vad_silence_duration_ms))

        realtime_input_config = {
            "automaticActivityDetection": {
                "startOfSpeechSensitivity": start_sens,
                "endOfSpeechSensitivity": end_sens,
                "prefixPaddingMs": prefix_ms,
                "silenceDurationMs": silence_ms,
            }
        }

    return {
        "setup": {
            "model": model if model.startswith("models/") else f"models/{model}",
            "generationConfig": {
                "responseModalities": ["audio"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice},
                    },
                    "languageCode": language_code,
                },
            },
            "systemInstruction": {
                "parts": [{"text": system_instruction}],
            },
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "end_call",
                            "description": (
                                "Disconnect the PSTN leg only when your system prompt allows it: "
                                "user clearly ended the conversation, justified abuse/teardown boundary, "
                                "or silence after exactly two unanswered check-ins asking if they're there."
                            ),
                        }
                    ]
                }
            ],
            "realtimeInputConfig": realtime_input_config,
        }
    }


async def gemini_send_live_rag(gem: Any, text: str, *, turn_complete: bool = True) -> None:
    del turn_complete
    t = (text or "").strip()
    if not t:
        return
    await gem.send(json.dumps({"realtimeInput": {"text": t}}))


async def gemini_send_live_opening_turn_nudge(gem: Any) -> None:
    """Prime Gemini Live to emit the first spoken assistant turn.

    Outbound PSTN legs often connect with callee silence; ``realtimeInput`` PCM silence
    alone does not reliably start native-audio generation. A minimal synthetic **user**
    turn with ``turnComplete`` matches Vertex / AI Studio Live docs for incremental
    ``clientContent`` updates.
    """

    await gem.send(
        json.dumps(
            {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        "[Outbound call connected — callee is on the line and silent.] "
                                        "Speak your opening line aloud now exactly as mandated in your instructions. "
                                        "One brief greeting only; then stop and listen for them."
                                    )
                                }
                            ],
                        }
                    ],
                    "turnComplete": True,
                }
            }
        )
    )


async def gemini_send_retry_nudge(gem: Any, *, attempt: int = 1) -> None:
    """Send a retry nudge when the model produced little or no audio.

    A more direct prompt that explicitly asks the model to speak its greeting.
    """
    await gem.send(
        json.dumps(
            {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        "[CRITICAL: You MUST speak your opening greeting NOW. "
                                        "The call has been connected and you are on the line. "
                                        "Say your introduction as instructed in your system prompt.]"
                                    )
                                }
                            ],
                        }
                    ],
                    "turnComplete": True,
                }
            }
        )
    )
    logger.info("Gemini retry nudge sent (attempt={})", attempt)


async def gemini_send_pcm_silence_kick(gem: Any, *, duration_ms: int = 120) -> None:
    n = max(OUT_CHUNK_BYTES, int(VOBIZ_SR * 2 * (duration_ms / 1000.0)))
    n = n & ~1
    silent_chunk = b"\x00" * n
    b64_silence = base64.b64encode(silent_chunk).decode()
    await gem.send(
        json.dumps(
            {
                "realtimeInput": {
                    "audio": {
                        "data": b64_silence,
                        "mimeType": "audio/pcm;rate=16000",
                    }
                }
            }
        )
    )


async def gemini_send_silence_prompt(gem: Any) -> None:
    """Inject a user turn prompting Gemini Live that the user is silent and it must ask if they are there."""
    await gem.send(
        json.dumps(
            {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        "[The user has been silent for 5 seconds. "
                                        "Immediately ask them: \"Hello, are you there?\" or equivalent in their language.]"
                                    )
                                }
                            ],
                        }
                    ],
                    "turnComplete": True,
                }
            }
        )
    )


# New AQ. auth keys (Google Auth Keys, June 2026+) use Bearer header not ?key=
GEMINI_LIVE_URL_BASE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)


def build_gemini_live_url_and_headers(api_key: str) -> tuple[str, dict]:
    """Return (wss_url, extra_headers) for the Gemini Live WebSocket.

    Always use the query parameter "?key=" for authentication as the 
    Google AI Studio Gemini Live WebSocket API requires this format for both
    legacy AIza... keys and new AQ... keys.
    """
    ak = (api_key or "").strip()
    return GEMINI_LIVE_URL_TMPL.format(api_key=ak), {}





