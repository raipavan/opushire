from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid
import wave
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Depends, BackgroundTasks
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from loguru import logger

from core.auth import get_current_user, _decode_jwt
from core.state import (
    get_state, save_role_state, normalize_console_role, resolved_greeting_text, _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    role_has_active_vobiz_call, acquire_vobiz_call_slot, release_vobiz_call_slot,
    try_recover_stale_vobiz_slot,
)
from core import storage as lead_storage
from config import settings
from core.outbound_numbers import resolve_outbound_from_number
from core.vobiz_credentials import resolve_vobiz_credentials

_MANUAL_SLOT_GUARDS: set[asyncio.Task] = set()


def _role_from_jwt(request: Request) -> str | None:
    """Extract role from JWT Authorization header, or None."""
    from core.auth import dashboard_role_for_token

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload and payload.get("role"):
            return dashboard_role_for_token(
                payload.get("email"),
                payload.get("role"),
            )
    return None


def _role_from_request(request: Request, default: str = "data_edge") -> str:
    """JWT role, or ``?role=buyers|sellers|rfqs`` when the console role toggle is used."""
    from core.auth import console_role_from_request

    return console_role_from_request(request, default=default)
from core.phone_norm import norm_phone_str
from core.greeting_pcm import load_recorded_greeting_pcm
from core.storage import insert_manual_call, mark_manual_call_failed
from core.utils import _build_opening_line
from core.worker import _prime_opening_audio
from services.call_recording import resolve_session_recording_path
from services.vobiz_bridge import make_vobiz_call

router = APIRouter(tags=["console"])


def _readable_transcript_lines(raw: str) -> tuple[str, list[str]]:
    """Return (joined readable text, list of lines) from JSONL or plain text."""
    lines_out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            role = obj.get("role") or obj.get("type", "")
            content = obj.get("content") or obj.get("text") or obj.get("message", "")
            if role in ("user", "assistant") and content:
                lines_out.append(f"{role.capitalize()}: {content.strip()}")
        except Exception:
            lines_out.append(line)
    if lines_out:
        return "\n".join(lines_out), lines_out
    return (raw or "").strip(), []


def _recommended_actions_from_analysis(analysis: dict) -> list[str]:
    bullets: list[str] = []
    disp = (analysis.get("disposition") or "").strip()
    if disp:
        bullets.append(f"Disposition: {disp}")
    ns = analysis.get("next_steps")
    if isinstance(ns, list):
        for x in ns:
            s = str(x).strip().lstrip("•-*").strip()
            if s:
                bullets.append(s)
        return bullets[:24]
    text = str(ns or "").strip()
    if text:
        parts = [p.strip().lstrip("•-*").strip() for p in re.split(r"[\n;]+", text)]
        parts = [p for p in parts if p]
        if parts:
            bullets.extend(parts)
        else:
            bullets.append(text)
    return bullets[:24]


def _manual_call_row_to_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "to_phone": row["to_phone"],
        "callee_name": row["callee_name"],
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "disposition": row.get("disposition") or "",
        "summary": (row.get("summary") or "")[:400],
    }


def _manual_call_detail_response(row: dict) -> dict:
    from core.worker import _read_transcript_jsonl

    role = row["role"]
    log_id = row.get("log_id") or ""
    raw = _read_transcript_jsonl(role, log_id) if log_id else ""
    readable, line_list = _readable_transcript_lines(raw)
    recording_available = False
    if (log_id or "").strip():
        recording_available = bool(resolve_session_recording_path(log_id))
    aj: dict = {}
    try:
        if (row.get("analysis_json") or "").strip():
            parsed = json.loads(row["analysis_json"])
            if isinstance(parsed, dict):
                aj = parsed
    except Exception:
        aj = {}
    # Prefer flattened columns when present
    if not aj.get("summary") and row.get("summary"):
        aj = {**aj, "summary": row.get("summary")}
    if not aj.get("disposition") and row.get("disposition"):
        aj = {**aj, "disposition": row.get("disposition")}
    if not aj.get("next_steps") and row.get("next_steps"):
        aj = {**aj, "next_steps": row.get("next_steps")}
    if "next_action" not in aj:
        aj = {**aj, "next_action": None}
    if not aj.get("emotion_label") and row.get("emotion_label"):
        aj = {
            **aj,
            "emotion_label": row.get("emotion_label"),
            "emotion_rationale": row.get("emotion_rationale"),
            "emotion_confidence": row.get("emotion_confidence"),
        }
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "to_phone": row["to_phone"],
        "callee_name": row["callee_name"],
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "log_id": log_id,
        "transcript_raw": raw,
        "transcript_readable": readable,
        "transcript_lines": line_list,
        "summary": aj.get("summary")
        or row.get("summary")
        or ((row.get("error") or "") if (row.get("status") or "") == "failed" else "")
        or "",
        "disposition": aj.get("disposition") or row.get("disposition") or "",
        "next_steps": aj.get("next_steps") or row.get("next_steps") or "",
        "emotion_label": aj.get("emotion_label") or row.get("emotion_label") or "",
        "emotion_rationale": aj.get("emotion_rationale") or row.get("emotion_rationale") or "",
        "emotion_confidence": aj.get("emotion_confidence", row.get("emotion_confidence")),
        "recommended_actions": _recommended_actions_from_analysis(aj),
        "rating": aj.get("rating"),
        "analysis": aj,
        "error": row.get("error") or "",
        "recording_available": recording_available,
        "recording_url": f"/api/manual/calls/{row['id']}/recording?role={row['role']}" if recording_available else "",
    }


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@router.get("/api/tuning")
async def get_tuning(request: Request):
    role = _role_from_request(request)
    state = get_state(role)

    from core.opening_line import packaged_fallback_greeting
    from core.role_sandbox import coerce_role_prompt, coerce_role_rag, coerce_stored_greeting
    from prompts.priya import get_role_prompt_text, get_role_rag_source_text

    file_prompt = get_role_prompt_text(role)
    file_rag = get_role_rag_source_text(role)
    prompt = coerce_role_prompt(role, state.get("prompt", ""), file_prompt)
    rag = coerce_role_rag(role, state.get("rag", ""), file_rag)
    gt = coerce_stored_greeting(role, state.get("greeting_text"))
    greeting = gt if gt else packaged_fallback_greeting(role)

    return {
        "prompt": prompt,
        "rag": rag,
        "greeting_text": greeting
    }

class TuningUpdate(BaseModel):
    prompt: str = ""
    rag: str = ""
    greeting_text: str = ""

@router.post("/api/tuning")
async def update_tuning(data: TuningUpdate, request: Request):
    role = _role_from_request(request)

    from core.greeting_text_utils import coerce_stored_greeting
    from core.role_sandbox import validate_role_tuning

    tuning_err = validate_role_tuning(
        role,
        prompt=data.prompt or "",
        rag=data.rag or "",
        greeting=data.greeting_text or "",
    )
    if tuning_err:
        raise HTTPException(400, tuning_err)

    greeting_out = coerce_stored_greeting(role, data.greeting_text or "")
    save_role_state(role, prompt=data.prompt, rag=data.rag, greeting_text=greeting_out)

    # Keep prompt + KB files in sync — build_role_system_prompt() prefers non-empty DB,
    # then falls back to these files when the DB field is empty.
    from prompts.priya import set_role_prompt_text, set_role_rag_source_text

    set_role_prompt_text(role, data.prompt)
    set_role_rag_source_text(role, data.rag)

    return {"status": "ok"}


class GreetingTextBody(BaseModel):
    greeting_text: str = ""


@router.post("/api/tuning/preview-greeting")
async def preview_greeting(data: GreetingTextBody, request: Request):
    """Gemini Flash TTS WAV (~opening style). Uses GEMINI_LIVE_VOICE name when set — still REST, not identical to Live."""
    role = _role_from_request(request)
    text = (data.greeting_text or "").strip()
    if not text:
        raise HTTPException(400, "Enter a greeting line to preview")

    from config import settings
    from services.gemini_tts import gemini_synthesize_pcm, get_gemini_tts_httpx

    try:
        client = await get_gemini_tts_httpx()
        pcm, sr = await gemini_synthesize_pcm(
            client,
            text=text,
            voice=(settings.gemini_live_voice or settings.gemini_tts_voice),
            style_mode="opening",
        )
    except Exception as exc:
        logger.exception("preview-greeting TTS failed")
        raise HTTPException(503, f"TTS preview failed: {exc}") from exc

    wav = _pcm_s16le_to_wav(pcm, sr)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Sample-Rate": str(sr),
            "X-Role": role,
        },
    )


@router.post("/api/tuning/record-greeting")
async def record_greeting_tts(data: GreetingTextBody, request: Request):
    """Capture or generate PCM for greeting_{role}.pcm (Live first — same voice as the call)."""
    role = _role_from_request(request)
    text = (data.greeting_text or "").strip()
    if not text:
        raise HTTPException(400, "greeting_text is required")

    from config import settings
    from core.greeting_pcm import _generate_and_cache_greeting, greeting_pcm_paths

    try:
        result = await _generate_and_cache_greeting(
            role,
            text,
            settings.gemini_live_voice or settings.gemini_tts_voice,
        )
    except Exception as exc:
        logger.exception("record-greeting failed")
        raise HTTPException(503, f"Greeting generation failed: {exc}") from exc

    if not result:
        raise HTTPException(503, "Greeting generation failed")

    pcm, sr = result
    out_path, meta_path = greeting_pcm_paths(role)
    engine = "live"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        engine = "rest_tts" if meta.get("source") == "gemini_tts_rest" else "live"
    except Exception:
        pass

    return {
        "status": "ok",
        "path": str(out_path),
        "bytes": len(pcm),
        "sample_rate": sr,
        "engine": engine,
    }


@router.post("/api/tuning/capture-greeting-live")
async def capture_greeting_live(data: GreetingTextBody, request: Request):
    """Capture opening audio from Gemini Live (native voice) and save greeting_{role}.pcm.

    Returns WAV for immediate playback; PCM on disk is what calls use before Live connects.
    Query ``variant=inbound`` saves ``greeting_{role}_inbound.pcm`` (inbound DID legs).
    """
    role = _role_from_request(request)
    variant = (request.query_params.get("variant") or "").strip().lower()
    text = (data.greeting_text or "").strip()
    if not text:
        raise HTTPException(400, "greeting_text is required")

    from services.live_greeting_capture import capture_live_greeting_pcm, save_greeting_pcm_file

    logger.info(
        "capture-greeting-live: role={} variant={} text_len={}",
        role,
        variant or "(default)",
        len(text),
    )

    try:
        pcm, sr = await capture_live_greeting_pcm(role, text)
        if variant:
            out_path = save_greeting_pcm_file(
                role, pcm, sr, variant=variant, greeting_text=text
            )
        else:
            from config import settings
            from core.greeting_pcm import _write_greeting_cache_files, greeting_pcm_paths

            live_voice = (settings.gemini_live_voice or "Leda").strip()
            _write_greeting_cache_files(
                role, text, pcm, sr, source="gemini_live_capture", voice=live_voice
            )
            out_path, _ = greeting_pcm_paths(role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("capture-greeting-live failed role={}: {}", role, exc)
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("capture-greeting-live failed role={}", role)
        raise HTTPException(503, f"Live capture failed: {exc}") from exc

    wav = _pcm_s16le_to_wav(pcm, sr)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Sample-Rate": str(sr),
            "X-Role": role,
            "X-Greeting-Bytes": str(len(pcm)),
            "X-Greeting-Path": str(out_path),
            "X-Greeting-Source": "gemini_live",
        },
    )


@router.post("/api/tuning/upload-doc")
async def upload_doc(request: Request, file: UploadFile = File(...)):
    role = _role_from_request(request)
    # extract text
    content = await file.read()
    filename = file.filename.lower()
    text = ""
    try:
        if filename.endswith(".txt"):
            text = content.decode("utf-8", errors="replace")
        elif filename.endswith(".pdf"):
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            for page in reader.pages:
                text += page.extract_text() + "\n"
        elif filename.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(content))
            for para in doc.paragraphs:
                text += para.text + "\n"
        else:
            raise HTTPException(400, "Unsupported file type")
    except Exception as e:
        logger.error(f"Failed to extract document: {e}")
        raise HTTPException(500, f"Extraction failed: {e}")
        
    from prompts.priya import get_role_rag_source_text, set_role_rag_source_text

    state = get_state(role)
    current_rag = get_role_rag_source_text(role) or state.get("rag", "")
    new_rag = current_rag + "\n\n" + text if current_rag else text
    save_role_state(role, rag=new_rag)
    set_role_rag_source_text(role, new_rag)

    return {"status": "ok", "filename": file.filename, "extracted_length": len(text)}

class VobizUpdate(BaseModel):
    auth_id: str
    auth_token: str
    from_number: str
    public_url: str

@router.post("/api/settings/vobiz")
async def update_vobiz(data: VobizUpdate, request: Request):
    role = _role_from_request(request)
    state = get_state(role)
    vobiz_config = state.get("vobiz", {})
    vobiz_config.update({
        "auth_id": data.auth_id,
        "auth_token": data.auth_token,
        "from_number": data.from_number,
        "public_url": data.public_url
    })
    save_role_state(role, vobiz_config=vobiz_config)
    return {"status": "ok"}

class ManualCallReq(BaseModel):
    to: str
    callee_name: str

@router.post("/api/manual/call")
async def manual_call(
    data: ManualCallReq,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    role = _role_from_request(request)
    state = get_state(role)
    vobiz_config = state.get("vobiz", {})

    auth_id, auth_token, from_number, v_base = resolve_vobiz_credentials(role, vobiz_config)

    if not auth_id or not auth_token:
        raise HTTPException(400, "Vobiz credentials not configured")
    if not from_number.strip():
        raise HTTPException(400, "Outbound caller ID (from_number) is not configured for this role")
    if not v_base:
        raise HTTPException(400, "VOBIZ_PUBLIC_BASE_URL is not configured")

    to_norm = norm_phone_str((data.to or "").strip())
    if not to_norm:
        raise HTTPException(400, "Invalid phone number — enter 10 digits (after +91), or a full number starting with + (e.g. +971…).")

    if role_has_active_vobiz_call(role):
        logger.info(
            "Manual call: campaign has active slot for role={}, proceeding anyway (parallel mode)",
            role,
        )

    camp_id = f"manual_{role}_{uuid.uuid4()}"
    import time as _time
    manual_row: dict = {
        "_role": role,
        "_manual_leg": True,
        "_inserted_at": _time.time(),
        "phone": to_norm,
        "name": (data.callee_name or "").strip() or "Unknown",
    }
    from core.greeting_text_utils import coerce_stored_greeting

    gt = coerce_stored_greeting(role, (state.get("greeting_text") or "").strip())
    if gt:
        manual_row["greeting_text"] = gt

    manual_call_id = await insert_manual_call(
        role,
        camp_id,
        to_norm,
        (data.callee_name or "").strip() or "Unknown",
    )
    _CAMPAIGN_DATA[camp_id] = manual_row

    opening_text = gt or _build_opening_line(
        {"name": manual_row["name"], "phone": to_norm},
        role,
    )
    _prime_opening_audio(camp_id, role, opening_text)

    try:
        auth_tail = auth_id[-6:] if auth_id else ""
        logger.info(
            "Manual Vobiz dial context: role={} auth_id_tail={!r} from_number={!r} base_url={!r} camp_id={}",
            role,
            auth_tail,
            from_number.strip(),
            v_base or "",
            camp_id,
        )
        _vobiz_resp = await make_vobiz_call(
            to=to_norm,
            from_=from_number,
            answer_url=f"{v_base}/vobiz/answer?camp_id={camp_id}",
            auth_id=auth_id,
            auth_token=auth_token,
            extra={
                "ring_url": f"{v_base}/vobiz/ring?camp_id={camp_id}",
                "ring_method": "POST",
                "hangup_url": f"{v_base}/vobiz/hangup?camp_id={camp_id}",
                "hangup_method": "POST",
                "hangup_on_ring": "3600",
            },
        )
        _call_uuid = _vobiz_resp.get("request_uuid") or ""
        if _call_uuid:
            _CAMPAIGN_DATA[camp_id]["_vobiz_call_uuid"] = _call_uuid

        # Safety-net: if Vobiz never connects the WS, release the slot after
        # a timeout so the campaign can resume.  ``_finalize_manual_call_leg``
        # releases it on normal WS close — this covers the case where the WS
        # never connected at all (e.g. callee didn't answer, or Vobiz couldn't
        # reach our WSS endpoint).
        async def _slot_guard(r: str, cid: str, mid: int) -> None:
            await asyncio.sleep(45)
            mem = _CAMPAIGN_DATA.get(cid, {})
            connected = bool(mem.get("_call_connected_at"))
            if not connected:
                logger.warning(
                    "Manual call slot guard: WS never connected for camp_id={} role={}",
                    cid, r,
                )
                from core.storage import mark_manual_call_failed as _mcf
                await _mcf(cid, "WebSocket never connected (timeout)")

        _guard = asyncio.create_task(_slot_guard(role, camp_id, manual_call_id))
        # detach from request lifetime — but still prevent GC
        _MANUAL_SLOT_GUARDS.add(_guard)
        _guard.add_done_callback(_MANUAL_SLOT_GUARDS.discard)

        return {"status": "ok", "camp_id": camp_id, "manual_call_id": manual_call_id}
    except Exception as e:
        logger.exception(f"Manual call failed")
        await mark_manual_call_failed(camp_id, str(e))
        _CAMPAIGN_DATA.pop(camp_id, None)
        raise HTTPException(500, str(e))


@router.get("/api/manual/calls/recent")
async def manual_calls_recent(
    request: Request,
    _user: dict = Depends(get_current_user),
    limit: int = 15,
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "data_edge"
    )
    from core.storage import list_recent_manual_calls

    rows = await list_recent_manual_calls(role, limit=max(1, min(int(limit), 50)))
    return {"items": [_manual_call_row_to_summary(r) for r in rows]}


@router.get("/api/manual/calls/{call_id}")
async def manual_call_detail(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "data_edge"
    )
    from core.storage import get_manual_call_by_id

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")
    return _manual_call_detail_response(row)


@router.post("/api/manual/calls/{call_id}/reanalyze")
async def manual_call_reanalyze(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    """Re-run post-call Gemini/Gemma QA on the saved JSONL transcript and update SQLite."""
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "data_edge"
    )
    from core.storage import get_manual_call_by_id, update_manual_call_analysis_by_id
    from core.worker import _read_transcript_jsonl
    from services.call_analyzer import analyze_call_transcript

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")

    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(400, "Call has no log_id transcript yet")

    transcript = _read_transcript_jsonl(role, log_id)

    if not (transcript or "").strip():
        logger.info("No transcript JSONL found — attempting transcription from recording...")
        from services.transcriber import transcribe_audio
        transcribed = await transcribe_audio(log_id, role)
        if transcribed:
            transcript = transcribed
            logger.info("Transcription successful, proceeding with analysis")
        else:
            raise HTTPException(400, "No transcript and no transcribable recording for this call")

    analysis = await analyze_call_transcript(transcript)
    if not await update_manual_call_analysis_by_id(call_id, analysis):
        raise HTTPException(500, "Could not persist analysis")

    refreshed = await get_manual_call_by_id(call_id)
    if not refreshed:
        raise HTTPException(500, "Row missing after update")
    return _manual_call_detail_response(refreshed)


class ManualCallDispositionReq(BaseModel):
    disposition: str


@router.post("/api/manual/calls/{call_id}/disposition")
async def manual_call_set_disposition(
    call_id: int,
    data: ManualCallDispositionReq,
    request: Request,
    background_tasks: BackgroundTasks,
    _user: dict = Depends(get_current_user),
):
    """Manually mark a manual call as Interested or Not Interested.

    When disposition is 'Interested', the configured WhatsApp project details
    (video + text + PDFs) are automatically sent to the callee.
    """
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "data_edge"
    )
    from core.storage import get_manual_call_by_id, update_manual_call_disposition
    from services.whatsapp_auto_sender import maybe_send_interested_whatsapp

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")

    disposition = (data.disposition or "").strip()
    if disposition not in ("Interested", "Not Interested"):
        raise HTTPException(400, "disposition must be 'Interested' or 'Not Interested'")

    if not await update_manual_call_disposition(call_id, disposition):
        raise HTTPException(500, "Could not update disposition")

    # Trigger WhatsApp auto-send only for Interested
    if disposition == "Interested":
        manual_phone = (row.get("to_phone") or "").strip()
        manual_name = (row.get("callee_name") or "").strip()
        if manual_phone:
            # Build a minimal analysis dict from the existing row
            existing_analysis: dict[str, Any] = {"_callee_name": manual_name}
            try:
                if (row.get("analysis_json") or "").strip():
                    parsed = json.loads(row["analysis_json"])
                    if isinstance(parsed, dict):
                        existing_analysis.update(parsed)
            except Exception:
                pass
            existing_analysis["disposition"] = disposition
            background_tasks.add_task(
                maybe_send_interested_whatsapp,
                lead_id=0,
                phone=manual_phone,
                role=role,
                analysis=existing_analysis,
            )

    refreshed = await get_manual_call_by_id(call_id)
    if not refreshed:
        raise HTTPException(500, "Row missing after update")
    return _manual_call_detail_response(refreshed)


@router.get("/api/manual/calls/{call_id}/recording")
async def manual_call_recording_download(
    call_id: int,
    request: Request,
):
    """Mixed WAV/MP3 with streaming support. Bearer auth or ``?access_token=`` for <audio src>."""
    from loguru import logger
    from core.auth import _decode_jwt
    auth = (request.headers.get("Authorization") or "").strip()
    payload = None
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        logger.info(f"recording auth: Bearer header, payload={payload}")
    if not payload:
        for key in ("access_token", "token"):
            raw = (request.query_params.get(key) or "").strip()
            logger.info(f"recording auth: trying query key={key}, raw_len={len(raw)}")
            if raw:
                payload = _decode_jwt(raw)
                if payload:
                    logger.info(f"recording auth: query param {key} decoded, payload={payload}")
                    break
                else:
                    logger.warning(f"recording auth: query param {key} failed to decode")
    if not payload:
        logger.warning(f"recording auth: NO payload. auth_header={auth[:30]}, qparams={dict(request.query_params)}")
        raise HTTPException(401, "Not authenticated")

    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "data_edge"
    )
    from core.storage import get_manual_call_by_id

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")
    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(404, "No session log for recording lookup")
    rec = resolve_session_recording_path(log_id)
    if not rec or not rec.is_file():
        raise HTTPException(404, "Recording not found — check CALL_RECORDING_ENABLED and retention")
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return FileResponse(
        rec,
        media_type=media_type,
        filename=rec.name,
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/api/conversation-logs/{date}/{log_id}")
async def get_conversation_log(date: str, log_id: str):
    log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs", date, f"{log_id}.txt")
    if os.path.exists(log_path):
        return FileResponse(log_path)
    raise HTTPException(404, "Log not found")

@router.get("/api/recordings/{date}/{filename}")
async def get_recording(date: str, filename: str):
    rec_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs", date, filename)
    if os.path.exists(rec_path):
        media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
        return FileResponse(rec_path, media_type=media_type)
    raise HTTPException(404, "Recording not found")
