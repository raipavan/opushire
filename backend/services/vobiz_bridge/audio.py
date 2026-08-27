"""PCM helpers, optional background decode, and pacing outbound ``playAudio`` frames."""

from __future__ import annotations

import audioop
import base64
import json
from typing import Optional

import numpy as np
from fastapi import WebSocket
from loguru import logger

from .constants import OUT_CHUNK_BYTES, VOBIZ_CONTENT_TYPE, VOBIZ_SR

try:
    import miniaudio
except ImportError:
    miniaudio = None

from services.call_recording import CallRecorder


def resample_24k_to_16k_numpy(pcm_24k: bytes, state: dict | None = None) -> tuple[bytes, dict]:
    """Resample 24kHz mono s16le PCM to 16kHz using audioop.ratecv (high quality).

    Uses the same polyphase resampler as the greeting PCM pipeline for
    consistent audio quality. The ``state`` parameter carries the audioop
    rate converter state across chunk boundaries for smooth cross-chunk output.
    """
    if len(pcm_24k) < 2:
        return pcm_24k, state
    prev_state = state.get("ratecv") if isinstance(state, dict) else None
    out, new_state = audioop.ratecv(pcm_24k, 2, 1, 24000, 16000, prev_state)
    return out, {"ratecv": new_state}


def load_background_audio(path: str, target_sr: int = 16000) -> Optional[np.ndarray]:
    if miniaudio is None or not path or not __import__("os").path.exists(path):
        return None
    try:
        decoded = miniaudio.decode_file(path, sample_rate=target_sr, nchannels=1)
        return np.frombuffer(decoded.samples, dtype=np.int16)
    except Exception as e:
        logger.error(f"Failed to load background audio: {e}")
        return None


def pcm_rms_norm(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    x = pcm.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(x))))


def pcm_resample(pcm_bytes: bytes, in_sr: int, out_sr: int) -> bytes:
    if in_sr == out_sr:
        return pcm_bytes
    out, _ = audioop.ratecv(pcm_bytes, 2, 1, in_sr, out_sr, None)
    return out


def mix_voice_and_background_tick(
    voice_pcm16: bytes,
    bg_wave: Optional[np.ndarray],
    volume: float,
    bg_position: int,
    chunk_samples: int,
) -> tuple[bytes, int]:
    """One 16-bit mono tick: blend outbound voice with a looped bed (scripted PCM or Gemini).

    ``volume`` scales the bed linearly on float samples before clipping (e.g. 0.75 ≈ 75 %).
    """
    chunk_bytes = chunk_samples * 2
    bg_pcm = None
    vol = float(volume)
    if vol < 0.0:
        vol = 0.0
    if bg_wave is not None and vol > 0:
        end_pos = bg_position + chunk_samples
        if end_pos > len(bg_wave):
            part1 = bg_wave[bg_position:]
            part2 = bg_wave[: end_pos - len(bg_wave)]
            bg_chunk = np.concatenate((part1, part2))
            bg_position = end_pos - len(bg_wave)
        else:
            bg_chunk = bg_wave[bg_position:end_pos]
            bg_position = end_pos

        bg_chunk = (bg_chunk.astype(np.float32) * vol).clip(-32768, 32767).astype(np.int16)
        bg_pcm = bg_chunk.tobytes()

    if bg_pcm is None:
        return voice_pcm16, bg_position
    if not voice_pcm16:
        return bg_pcm, bg_position
    mixed = audioop.add(voice_pcm16, bg_pcm, 2)
    return mixed, bg_position


def pop_l16_chunk(queue: bytearray, chunk_bytes: int) -> bytes:
    if len(queue) >= chunk_bytes:
        out = bytes(queue[:chunk_bytes])
        del queue[:chunk_bytes]
        return out
    if len(queue) > 0:
        n = len(queue)
        out = bytes(queue) + b"\x00" * (chunk_bytes - n)
        queue.clear()
        return out
    return b"\x00" * chunk_bytes


_PLAY_TPL = (
    '{"event":"playAudio","media":{"contentType":"'
    + VOBIZ_CONTENT_TYPE
    + '","sampleRate":16000,"payload":"'
)
_PLAY_END = '"}}'


async def send_play_audio(
    ws: WebSocket,
    pcm16_bytes: bytes,
    sr: int = VOBIZ_SR,
    *,
    call_recorder: Optional[CallRecorder] = None,
) -> None:
    if not pcm16_bytes:
        return
    if call_recorder is not None:
        call_recorder.add_outbound(pcm16_bytes)
    view = memoryview(pcm16_bytes)
    for offset in range(0, len(view), OUT_CHUNK_BYTES):
        chunk = bytes(view[offset : offset + OUT_CHUNK_BYTES])
        if len(chunk) < 2:
            continue
        await ws.send_text(_PLAY_TPL + base64.b64encode(chunk).decode("ascii") + _PLAY_END)


async def send_play_audio_batched(
    ws: WebSocket,
    pcm16_bytes: bytes,
    sr: int = VOBIZ_SR,
) -> None:
    """Send PCM audio as a single WebSocket message.

    Unlike ``send_play_audio`` which splits into 640-byte chunks (one WS frame
    per 20 ms), this sends the entire buffer in one ``playAudio`` message.  On a
    2-core VPS where each ``ws.send_text()`` blocks for ~280 ms, batching 8
    frames (160 ms, 5120 bytes) into one send cuts outbound traffic by ~8x and
    keeps the mixer close to real-time.
    """
    if not pcm16_bytes:
        return
    await ws.send_text(
        _PLAY_TPL + base64.b64encode(pcm16_bytes).decode("ascii") + _PLAY_END
    )
