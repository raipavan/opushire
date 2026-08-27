"""Optional system-instruction appendix for Gemini Live duplex / interruption behavior."""

from __future__ import annotations

from config import settings

_LIVE_VOICE_TURN_ADDENDUM = """

[REALTIME PHONE INTERACTION — TURN-TAKING]
This is a live voice call over the phone — be warm, natural, and conversational.
Speak in clear 1–2 sentence turns. Complete your question or thought before yielding.
If the callee speaks while you are talking, yield politely and listen to their response.
Always keep the conversation going naturally by asking a relevant follow-up question.
"""


def apply_live_voice_turn_addon(system_instruction: str) -> str:
    if not getattr(settings, "gemini_live_append_turn_instructions", True):
        return system_instruction or ""
    s = (system_instruction or "").rstrip()
    add = _LIVE_VOICE_TURN_ADDENDUM.strip()
    if not s:
        return add
    return f"{s}\n\n{add}"
