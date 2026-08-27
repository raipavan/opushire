"""Browser WebSocket ↔ Gemini Live (no Vobiz). Used by /ws/web-demo and /ws/voice-test."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import audioop
from pathlib import Path

import websockets as ws_client
from fastapi import WebSocket
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from config import settings
from core.state import get_state, normalize_console_role
from prompts.priya import build_role_system_prompt

from services.vobiz_bridge.audio import pcm_resample, resample_24k_to_16k_numpy
from services.vobiz_bridge.constants import GEMINI_OUT_SR, VOBIZ_SR
from services.vobiz_bridge.gemini_protocol import (
    GEMINI_LIVE_URL_TMPL,
    build_live_setup,
    gemini_send_pcm_silence_kick,
    gemini_send_silence_prompt,
)
from services.vobiz_bridge.turn_taking_addon import apply_live_voice_turn_addon


def make_wav_header(data_len: int, sample_rate: int = 16000) -> bytes:
    header = bytearray(44)
    header[0:4] = b"RIFF"
    header[4:8] = (data_len + 36).to_bytes(4, "little")
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    header[16:20] = (16).to_bytes(4, "little")
    header[20:22] = (1).to_bytes(2, "little")
    header[22:24] = (1).to_bytes(2, "little")
    header[24:28] = sample_rate.to_bytes(4, "little")
    header[28:32] = (sample_rate * 2).to_bytes(4, "little")
    header[32:34] = (2).to_bytes(2, "little")
    header[34:36] = (16).to_bytes(2, "little")
    header[36:40] = b"data"
    header[40:44] = data_len.to_bytes(4, "little")
    return bytes(header)


async def handle_browser_voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    query_role = normalize_console_role(websocket.query_params.get("role") or "data_edge")
    role = query_role
    api_key = settings.gemini_api_key
    if not api_key:
        logger.error("Browser voice WS: missing gemini_api_key in settings")
        await websocket.close(code=1011)
        return

    voice = settings.gemini_live_voice
    model = settings.gemini_live_model
    language_code = settings.gemini_live_language_code
    lead_id = websocket.query_params.get("lead_id")
    lead = None
    if lead_id:
        from core.storage import _get_lead_sync
        try:
            lead = _get_lead_sync(role, int(lead_id))
        except Exception:
            pass
    if not lead:
        segment = websocket.query_params.get("segment") or "rfq"
        lead = {"segment": segment}

    role_config = get_state(role)
    system_prompt = build_role_system_prompt(role, role_config, lead)
    system_prompt = apply_live_voice_turn_addon(system_prompt)

    gemini_url = GEMINI_LIVE_URL_TMPL.format(api_key=api_key)
    vad_ultra = role == "data_edge"
    setup = build_live_setup(
        model=model,
        system_instruction=system_prompt,
        voice=voice,
        language_code=language_code,
        vad_ultra=vad_ultra,
    )

    # Audio Recording Buffers
    start_time = time.perf_counter()
    user_pcm_buf = bytearray()
    ai_pcm_buf = bytearray()

    # Generate log_id and SQLite record for Voice History console
    from datetime import datetime
    timestamp_str = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_id = f"voice-test-{role}-{timestamp_str}"
    lead_id = None
    try:
        from core.storage import _get_conn
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO leads (role, name, phone, company, status, extra, start_time, _log_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                role,
                "Browser Voice Test",
                "Browser Call",
                "PitchX Live Test",
                "dialing",
                "{}",
                time.time(),
                log_id
            )
        )
        lead_id = cursor.lastrowid
        conn.commit()
        logger.info("Browser voice: created test lead id={} log_id={}", lead_id, log_id)
    except Exception as e:
        logger.warning("Browser voice: failed to insert test lead: {}", e)

    try:
        async with ws_client.connect(
            gemini_url,
            max_size=2 * 1024 * 1024,
            ping_interval=10,
            close_timeout=2,
        ) as gem:
            await gem.send(json.dumps(setup))
            logger.info("Browser voice: Gemini setup sent (role={})", role)
            try:
                await gemini_send_pcm_silence_kick(gem, duration_ms=200)
            except Exception as exc:
                logger.warning("Browser voice: silence kick failed: {}", exc)

            pending_audio_24k = bytearray()
            browser_16k_queue = bytearray()  # Jitter buffer for browser playout
            flush_bytes = int(GEMINI_OUT_SR * 2 * 0.04)  # 40ms @ 24kHz
            chunk_16k_bytes = int(16000 * 2 * 0.020)  # 20ms @ 16kHz = 640 bytes
            last_meaningful_t = time.perf_counter()
            is_generating = False
            resample_state = None
            first_audio_chunk_logged = False
            first_user_audio_t = None
            is_playing_to_browser = False
            # Prebuffer: accumulate configurable duration before starting playout
            PREBUFFER_BYTES = int(16000 * 2 * settings.browser_voice_prebuffer_seconds)
            _first_response = True

            async def flush_pending_to_browser() -> None:
                """Resample 24kHz -> 16kHz and feed into the jitter buffer."""
                nonlocal pending_audio_24k, resample_state
                while len(pending_audio_24k) >= flush_bytes:
                    chunk = bytes(pending_audio_24k[:flush_bytes])
                    del pending_audio_24k[:flush_bytes]
                    pcm_16k, resample_state = resample_24k_to_16k_numpy(
                        chunk, resample_state
                    )
                    
                    # Record AI Output (mono s16le PCM 16kHz)
                    elapsed = time.perf_counter() - start_time
                    expected_bytes = int(elapsed * 16000 * 2)
                    expected_bytes = (expected_bytes // 2) * 2
                    if expected_bytes > len(ai_pcm_buf):
                        ai_pcm_buf.extend(b"\x00" * (expected_bytes - len(ai_pcm_buf)))
                    ai_pcm_buf.extend(pcm_16k)

                    browser_16k_queue.extend(pcm_16k)

            async def playout_loop() -> None:
                """Send audio from the jitter buffer to the browser at 20ms ticks."""
                nonlocal is_playing_to_browser, _first_response
                next_wakeup = time.perf_counter()
                while True:
                    q_len = len(browser_16k_queue)
                    if not is_playing_to_browser:
                        # Start playout when prebuffer is filled or generation ended
                        if _first_response and q_len > 0:
                            # First response: start immediately (low latency)
                            is_playing_to_browser = True
                            _first_response = False
                            logger.info("Browser voice: first-response playout ({} bytes) — skipping prebuffer", q_len)
                        elif q_len >= PREBUFFER_BYTES or (not is_generating and q_len > 0):
                            is_playing_to_browser = True
                            _first_response = False
                            logger.info("Browser voice: jitter buffer filled ({} bytes, gen={}) — starting playout", q_len, is_generating)

                    if is_playing_to_browser and q_len >= chunk_16k_bytes:
                        chunk = bytes(browser_16k_queue[:chunk_16k_bytes])
                        del browser_16k_queue[:chunk_16k_bytes]
                        out_b64 = base64.b64encode(chunk).decode("ascii")
                        try:
                            await websocket.send_text(json.dumps({"type": "audio", "data": out_b64}))
                        except Exception:
                            return
                    elif is_playing_to_browser and q_len > 0 and not is_generating:
                        # End of turn — drain remaining audio
                        remaining = bytes(browser_16k_queue)
                        browser_16k_queue.clear()
                        out_b64 = base64.b64encode(remaining).decode("ascii")
                        try:
                            await websocket.send_text(json.dumps({"type": "audio", "data": out_b64}))
                        except Exception:
                            return
                        is_playing_to_browser = False
                    elif not is_generating and q_len == 0:
                        is_playing_to_browser = False

                    # Pace at 20ms ticks
                    next_wakeup += 0.020
                    sleep_time = next_wakeup - time.perf_counter()
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
                    else:
                        next_wakeup = time.perf_counter()

            async def pump_browser_to_gemini() -> None:
                while True:
                    try:
                        raw = await websocket.receive_text()
                    except WebSocketDisconnect:
                        return
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "audio":
                        continue
                    b64 = obj.get("data") or ""
                    if not b64:
                        continue
                    
                    # Record User Input
                    try:
                        raw_pcm = base64.b64decode(b64)
                        elapsed = time.perf_counter() - start_time
                        expected_bytes = int(elapsed * 16000 * 2)
                        expected_bytes = (expected_bytes // 2) * 2
                        if expected_bytes > len(user_pcm_buf):
                            user_pcm_buf.extend(b"\x00" * (expected_bytes - len(user_pcm_buf)))
                        user_pcm_buf.extend(raw_pcm)
                    except Exception:
                        pass

                    await gem.send(
                        json.dumps(
                            {
                                "realtimeInput": {
                                    "audio": {
                                        "data": b64,
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }
                        )
                    )

            async def pump_gemini_to_browser() -> None:
                nonlocal pending_audio_24k, last_meaningful_t, is_generating, resample_state
                async for raw in gem:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    if obj.get("error"):
                        logger.error("Browser voice: Gemini upstream error: {}", obj.get("error"))

                    sc = obj.get("serverContent") or {}

                    it = sc.get("inputTranscription") or {}
                    if it.get("text"):
                        last_meaningful_t = time.perf_counter()
                        if first_user_audio_t is None:
                            first_user_audio_t = time.perf_counter()

                    if sc.get("interrupted"):
                        logger.info("Browser voice: user barge-in (interrupted)")
                        pending_audio_24k.clear()
                        browser_16k_queue.clear()
                        # Keep resample_state for smooth audio continuity
                        # (prevents clicks/pops at turn boundaries)
                        is_generating = False
                        last_meaningful_t = time.perf_counter()
                        try:
                            await websocket.send_text(json.dumps({"type": "interrupted"}))
                        except Exception:
                            return

                    if sc.get("modelTurn"):
                        is_generating = True

                    mt = sc.get("modelTurn") or {}
                    for part in mt.get("parts") or []:
                        inline = part.get("inlineData") or part.get("inline_data")
                        if not inline:
                            continue
                        mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                        if not mime.startswith("audio/"):
                            continue
                        b64_in = inline.get("data") or ""
                        if not b64_in:
                            continue
                        try:
                            pcm = base64.b64decode(b64_in)
                        except Exception:
                            continue
                        pending_audio_24k.extend(pcm)
                        # Log first audio chunk latency
                        if not first_audio_chunk_logged:
                            trigger_t = first_user_audio_t or start_time
                            dt_ms = (time.perf_counter() - trigger_t) * 1000.0
                            logger.info(
                                "Browser voice: first model-audio chunk — {:.0f} ms since first user audio, {} bytes",
                                dt_ms,
                                len(pcm),
                            )
                            first_audio_chunk_logged = True
                        try:
                            await flush_pending_to_browser()
                        except Exception:
                            return

                    if sc.get("turnComplete") or sc.get("generationComplete"):
                        is_generating = False
                        last_meaningful_t = time.perf_counter()
                        # Flush any remaining 24kHz audio
                        if pending_audio_24k:
                            chunk = bytes(pending_audio_24k)
                            pending_audio_24k.clear()
                            pcm_16k, resample_state = resample_24k_to_16k_numpy(
                                chunk, resample_state
                            )
                            browser_16k_queue.extend(pcm_16k)

                            # Record Final Outbound Chunk
                            elapsed = time.perf_counter() - start_time
                            expected_bytes = int(elapsed * 16000 * 2)
                            expected_bytes = (expected_bytes // 2) * 2
                            if expected_bytes > len(ai_pcm_buf):
                                ai_pcm_buf.extend(b"\x00" * (expected_bytes - len(ai_pcm_buf)))
                            ai_pcm_buf.extend(pcm_16k)

            async def user_silence_watchdog() -> None:
                # Disabled — sleep forever so it does not trigger nudges (irritating "are you there" loop)
                while True:
                    await asyncio.sleep(3600)

            in_task = asyncio.create_task(pump_browser_to_gemini())
            out_task = asyncio.create_task(pump_gemini_to_browser())
            playout_task = asyncio.create_task(playout_loop())
            silence_task = asyncio.create_task(user_silence_watchdog())
            _, pending = await asyncio.wait(
                {in_task, out_task, playout_task, silence_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            # Compile, mix, save, and trigger analysis for database
            duration_sec = time.perf_counter() - start_time
            try:
                max_len = max(len(user_pcm_buf), len(ai_pcm_buf))
                if max_len > 0:
                    if len(user_pcm_buf) < max_len:
                        user_pcm_buf.extend(b"\x00" * (max_len - len(user_pcm_buf)))
                    if len(ai_pcm_buf) < max_len:
                        ai_pcm_buf.extend(b"\x00" * (max_len - len(ai_pcm_buf)))

                    mixed_pcm = audioop.add(bytes(user_pcm_buf), bytes(ai_pcm_buf), 2)
                    wav_data = make_wav_header(len(mixed_pcm)) + mixed_pcm
                    wav_b64 = base64.b64encode(wav_data).decode("ascii")

                    # Write mixed WAV to filesystem
                    from datetime import datetime, timezone
                    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    rec_base = Path(settings.call_recording_dir)
                    if not rec_base.is_absolute():
                        backend_dir = Path(__file__).resolve().parent.parent
                        rec_base = backend_dir / rec_base
                    rec_dir = rec_base / date_str
                    rec_dir.mkdir(parents=True, exist_ok=True)
                    wav_path = rec_dir / f"{log_id}_mixed.wav"
                    with open(str(wav_path), "wb") as f:
                        f.write(wav_data)
                    logger.info("Browser voice: saved mixed WAV to {}", wav_path)

                    # Trigger transcription and post-call analysis in the background
                    if lead_id:
                        from core.worker import _analyze_and_update_lead
                        asyncio.create_task(_analyze_and_update_lead(role, lead_id, log_id, duration_sec))

                    await websocket.send_text(json.dumps({
                        "type": "recording",
                        "data": wav_b64
                    }))
                    logger.info("Browser voice: sent test call recording ({} bytes)", len(wav_data))
            except Exception as e:
                logger.warning("Browser voice: failed to compile/send recording: {}", e)

    except WebSocketDisconnect:
        logger.info("Browser voice: client disconnected")
        # Handle case where user disconnected before WebSocket loops could join/finalize
        duration_sec = time.perf_counter() - start_time
        try:
            if lead_id:
                from core.worker import _analyze_and_update_lead
                asyncio.create_task(_analyze_and_update_lead(role, lead_id, log_id, duration_sec))
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Browser voice session failed: {}", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
