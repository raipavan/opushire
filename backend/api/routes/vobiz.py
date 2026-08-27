"""Vobiz answer URL + media WebSocket."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, WebSocket
from loguru import logger

from config import settings, validate_critical_config
from core.state import (
    role_has_active_vobiz_call,
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    get_state,
    normalize_console_role,
    parse_manual_camp_role_suffix,
)
from core.storage import record_inbound_callback
from services.vobiz_bridge import build_answer_xml, handle_vobiz_ws_live

router = APIRouter(tags=["vobiz"])

# Inbound DID → role (last 10 digits). Vobiz Answer URL may pass any ?role=; To number wins.
_INBOUND_DID_ROLE: dict[str, str] = {
    "8065481827": "data_edge",
}


def _phone_last10(value: Optional[str]) -> str:
    if not value:
        return ""
    digits = "".join(c for c in str(value) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def _parse_vobiz_request(request: Optional[Request]) -> tuple[dict, str, Optional[str], Optional[str]]:
    """Return (raw_data, from_phone, to_phone, call_uuid)."""
    from_phone = "unknown"
    to_phone = None
    call_uuid = None
    raw_data: dict = {}
    if request is None:
        return raw_data, from_phone, to_phone, call_uuid
    try:
        qp = dict(request.query_params)
        ct = (request.headers.get("content-type") or "").lower()
        body_data: dict = {}
        if "application/x-www-form-urlencoded" in ct:
            form = await request.form()
            body_data = dict(form)
        elif "application/json" in ct:
            try:
                body_data = await request.json()
            except Exception:
                pass
        raw_data = {**qp, **body_data}
        from_phone = (
            body_data.get("From")
            or body_data.get("from")
            or qp.get("From")
            or qp.get("from")
            or "unknown"
        )
        to_phone = body_data.get("To") or body_data.get("to") or qp.get("To") or qp.get("to")
        call_uuid = body_data.get("CallUUID") or qp.get("CallUUID")
    except Exception as exc:
        logger.warning("Vobiz answer: inbound parse failed: {}", exc)
    return raw_data, from_phone, to_phone, call_uuid


def _resolve_inbound_role(query_role: Optional[str], to_phone: Optional[str]) -> str:
    """Map Vobiz Answer URL ?role= and called DID to the console sandbox role."""
    explicit = normalize_console_role(query_role) if query_role else None
    mapped = _INBOUND_DID_ROLE.get(_phone_last10(to_phone))
    if mapped:
        if explicit and explicit != mapped:
            logger.info(
                "Vobiz inbound DID {} remapped role {} → {}",
                to_phone,
                explicit,
                mapped,
            )
        return mapped
    return explicit or "data_edge"


def _build_busy_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Reject reason="busy"/></Response>'
    )


async def _vobiz_answer_impl(
    camp_id: Optional[str] = None,
    role: Optional[str] = None,
    request: Optional[Request] = None,
) -> Response:
    is_inbound = bool(role) and not camp_id
    raw_data: dict = {}
    from_phone = "unknown"
    to_phone = None
    call_uuid = None
    if is_inbound and request is not None:
        raw_data, from_phone, to_phone, call_uuid = await _parse_vobiz_request(request)

    normalized_role = (
        _resolve_inbound_role(role, to_phone)
        if is_inbound
        else normalize_console_role(role) if role else None
    )

    is_busy = False
    if normalized_role:
        campaign_task = _CAMPAIGN_TASKS.get(normalized_role)
        if campaign_task and not campaign_task.done():
            is_busy = True
        if role_has_active_vobiz_call(normalized_role):
            is_busy = True

    if is_inbound and is_busy:
        try:
            await record_inbound_callback(
                normalized_role,
                from_phone,
                to_phone=to_phone,
                call_uuid=call_uuid,
                campaign_active=True,
                raw_start={"direction": "inbound", "busy_rejected": True, "raw": raw_data},
            )
        except Exception as exc:
            logger.warning("record_inbound_callback failed: {}", exc)

        return Response(content=_build_busy_xml(), media_type="application/xml")

    db_lead = None
    if camp_id and camp_id not in _CAMPAIGN_DATA:
        try:
            from core.storage import lead_row_by_call_id
            db_lead = await lead_row_by_call_id(camp_id)
        except Exception as e:
            logger.warning("Vobiz answer: failed to recover campaign lead from DB: {}", e)

    role_base = None
    if camp_id and (camp_id in _CAMPAIGN_DATA or db_lead):
        data = _CAMPAIGN_DATA[camp_id] if camp_id in _CAMPAIGN_DATA else db_lead
        camp_role = data.get("_role") or data.get("role")
        if camp_role:
            state = get_state(camp_role)
            role_base = state.get("vobiz", {}).get("public_url")
    elif normalized_role:
        try:
            state = get_state(normalized_role)
            role_base = state.get("vobiz", {}).get("public_url")
        except Exception:
            role_base = None

    dyn_base = None
    if request and not (settings.vobiz_stream_public_base_url or "").strip():
        req_host = request.headers.get("host") or request.url.netloc
        if req_host:
            proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
            dyn_base = f"{proto}://{req_host}"

    base = (dyn_base or role_base or settings.vobiz_public_base_url or "").rstrip("/")
    stream_base = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/") or base
    wss_url = stream_base.replace("https://", "wss://").replace("http://", "wss://") + "/ws/vobiz"

    # ── Build WSS URL ──────────────────────────────────────────────────
    # CRITICAL FIX: Vobiz's XML parser cannot decode ``&amp;`` entities inside
    # the <Stream> text node — any URL containing ``&`` (even when properly
    # XML-escaped) causes Vobiz to silently drop the WebSocket connection.
    # This was the root cause of calls never connecting after the first one.
    #
    # Solution: encode ALL session parameters in the URL PATH instead of
    # query strings — zero ampersands = no XML entity issues.
    #
    # Path formats:
    #   /ws/vobiz/c/{camp_id}                    — outbound campaign / manual call
    #   /ws/vobiz/i/{inbound_role}               — inbound PSTN call
    #   /ws/vobiz/a/{agent_id}                   — sandbox / factory agent
    #   /ws/vobiz/c/{camp_id}/a/{agent_id}       — campaign + agent
    #   /ws/vobiz                                — fallback (no params)
    agent_id = None
    if camp_id:
        if camp_id in _CAMPAIGN_DATA or db_lead:
            data = _CAMPAIGN_DATA[camp_id] if camp_id in _CAMPAIGN_DATA else db_lead
            agent_id = data.get("_agent_id") or data.get("agent_id")
        elif camp_id.startswith("sandbox-"):
            parts = camp_id.split("-")
            if len(parts) >= 2:
                agent_id = parts[1]
        # Outbound / manual call — camp_id goes in the path (no ampersands!)
        wss_url += f"/c/{camp_id}"
        if agent_id:
            wss_url += f"/a/{agent_id}"
    elif agent_id:
        # Sandbox agent without camp_id
        wss_url += f"/a/{agent_id}"
    elif normalized_role:
        # Inbound PSTN call — role goes in the path
        wss_url += f"/i/{normalized_role}"

    if stream_base and (
        "trycloudflare.com" in stream_base
        or "trycloudflare.dev" in stream_base
        or "cfargotunnel.com" in stream_base
    ):
        logger.warning(
            "Vobiz <Stream> URL uses a Cloudflare quick-tunnel host ({}…). "
            "If calls disconnect with no audio, set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http://IP:PORT (same FastAPI server).",
            stream_base.split("//")[-1][:48],
        )

    if request is not None:
        try:
            logger.info(
                "Vobiz answer: method={} qs={}",
                request.method, dict(request.query_params),
            )
        except Exception:
            pass

    logger.info(
        "Vobiz answer: camp={} inbound={} role={} to={} wss_url={}",
        camp_id,
        is_inbound,
        normalized_role,
        to_phone,
        wss_url,
    )
    status_callback_url = f"{base}/vobiz/stream-status"
    return Response(
        content=build_answer_xml(wss_url, inbound=is_inbound, status_callback_url=status_callback_url),
        media_type="application/xml",
    )


@router.get("/api/health/diagnostic")
async def health_diagnostic():
    """Diagnostic endpoint: checks all critical config for silent-call issues."""
    from pathlib import Path

    result = {
        "status": "ok",
        "checks": {},
    }

    # 1. Gemini API key
    gemini_key = settings.gemini_api_key
    result["checks"]["gemini_api_key"] = {
        "status": "ok" if gemini_key and (gemini_key.startswith("AIza") or gemini_key.startswith("AQ.")) else "error",
        "value": f"{gemini_key[:8]}…{gemini_key[-4:]}" if gemini_key and len(gemini_key) > 12 else "(empty or invalid)",
        "message": "Valid Google AI Studio key" if gemini_key and (gemini_key.startswith("AIza") or gemini_key.startswith("AQ.")) else "Missing or invalid key",
    }

    # 2. Gemini Live config
    result["checks"]["gemini_live"] = {
        "status": "ok",
        "model": settings.gemini_live_model,
        "voice": settings.gemini_live_voice,
        "language": settings.gemini_live_language_code,
        "first_opening": settings.gemini_live_first_opening,
        "aggressive_vad": settings.gemini_live_aggressive_activity_detection,
    }

    # 3. Vobiz config
    vobiz_ok = bool(settings.vobiz_public_base_url)
    stream_url = (settings.vobiz_stream_public_base_url or "").strip()
    result["checks"]["vobiz_base_url"] = {
        "status": "ok" if vobiz_ok else "error",
        "value": settings.vobiz_public_base_url or "(empty)",
        "message": "Configured" if vobiz_ok else "MISSING — calls cannot connect",
    }
    result["checks"]["vobiz_stream_url"] = {
        "status": "ok" if stream_url else "warning",
        "value": stream_url or "(empty — will use VOBIZ_PUBLIC_BASE_URL)",
        "message": (
            "Configured" if stream_url
            else "EMPTY — media WebSocket routes through VOBIZ_PUBLIC_BASE_URL. "
            "If your domain does NOT support WebSocket upgrades, calls will produce SILENCE."
        ),
    }

    # 4. Vobiz credentials
    result["checks"]["vobiz_data_edge_creds"] = {
        "status": "ok" if settings.vobiz_data_edge_auth_id else "warning",
        "auth_id": settings.vobiz_data_edge_auth_id or "(empty)",
        "from_number": settings.vobiz_data_edge_from_number or "(empty)",
        "message": "Configured" if settings.vobiz_data_edge_auth_id else "No role-specific credentials (using global fallback)",
    }

    # 5. Greeting PCM files
    greetings_dir = Path(__file__).resolve().parent.parent.parent / "data" / "greetings"
    greeting_status = {}
    for role_name in ("data_edge",):
        pcm_path = greetings_dir / f"greeting_{role_name}.pcm"
        meta_path = greetings_dir / f"greeting_{role_name}.pcm.meta"
        if pcm_path.is_file() and pcm_path.stat().st_size > 0:
            import json
            meta = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            greeting_status[role_name] = {
                "status": "ok",
                "size_bytes": pcm_path.stat().st_size,
                "sample_rate": meta.get("sr", "unknown"),
                "source": meta.get("source", "unknown"),
            }
        else:
            greeting_status[role_name] = {"status": "missing", "message": "No greeting PCM file"}
    result["checks"]["greetings"] = greeting_status

    # 6. Silence hangup config
    import os
    silence_sec = float(os.getenv("CALL_SILENCE_HANGUP_SEC", "30"))
    grace_sec = float(os.getenv("CALL_SILENCE_GRACE_SEC", "15"))
    result["checks"]["silence_config"] = {
        "status": "ok" if silence_sec >= 20 else "warning",
        "hangup_sec": silence_sec,
        "grace_sec": grace_sec,
        "message": f"Watchdog fires after {silence_sec}s silence, grace period {grace_sec}s",
    }

    # 7. Config validation problems
    problems = validate_critical_config()
    result["checks"]["config_validation"] = {
        "status": "error" if any("MISSING" in p or "invalid" in p.lower() for p in problems) else "warning" if problems else "ok",
        "problems": problems,
    }

    # Overall status
    statuses = []
    for k, v in result["checks"].items():
        if isinstance(v, dict):
            statuses.append(v.get("status", "ok"))
        elif isinstance(v, dict):
            for sk, sv in v.items():
                if isinstance(sv, dict):
                    statuses.append(sv.get("status", "ok"))
    if "error" in statuses:
        result["status"] = "error"
    elif "warning" in statuses:
        result["status"] = "warning"

    return result


@router.post("/vobiz/answer")
async def vobiz_answer_post(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.get("/vobiz/answer")
async def vobiz_answer_get(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.post("/vobiz/stream-status")
@router.get("/vobiz/stream-status")
async def vobiz_stream_status(request: Request):
    """Vobiz stream lifecycle callback: connected, stopped, timeout, failed, etc."""
    try:
        ct = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in ct:
            form = await request.form()
            data = dict(form)
        elif "application/json" in ct:
            data = await request.json()
        else:
            data = dict(request.query_params)
        event = data.get("Event") or data.get("event") or "unknown"
        call_uuid = data.get("CallUUID") or data.get("call_uuid") or data.get("callUuid") or "unknown"
        stream_id = data.get("StreamID") or data.get("stream_id") or data.get("streamId") or "unknown"
        reason = data.get("Reason") or data.get("reason") or ""
        logger.warning(
            "Vobiz stream-status callback: event={} call_uuid={} stream_id={} reason={} payload={}",
            event,
            call_uuid,
            stream_id,
            reason,
            data,
        )
    except Exception as exc:
        logger.warning("Vobiz stream-status callback parse failed: {}", exc)
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>", media_type="application/xml")


async def _parse_vobiz_callback(request: Request) -> dict:
    """Parse form/JSON/query params from Vobiz callback, merging query params with body."""
    qp = dict(request.query_params)
    ct = (request.headers.get("content-type") or "").lower()
    body_data: dict = {}
    if "application/x-www-form-urlencoded" in ct:
        form = await request.form()
        body_data = dict(form)
    elif "application/json" in ct:
        try:
            body_data = await request.json()
        except Exception:
            pass
    return {**qp, **body_data}


@router.post("/vobiz/ring")
@router.get("/vobiz/ring")
async def vobiz_ring_callback(request: Request):
    """Vobiz ring callback: called when the call starts ringing."""
    try:
        data = await _parse_vobiz_callback(request)
        camp_id = data.get("camp_id") or data.get("CampID") or ""
        call_uuid = data.get("CallUUID") or data.get("call_uuid") or data.get("RequestUUID") or ""
        from_num = data.get("From") or ""
        to_num = data.get("To") or ""
        logger.info(
            "Vobiz ring callback: camp_id={} call_uuid={} from={} to={} payload={}",
            camp_id, call_uuid, from_num, to_num, data,
        )
        if camp_id and camp_id in _CAMPAIGN_DATA:
            _CAMPAIGN_DATA[camp_id]["_vobiz_ringing_at"] = time.time()
    except Exception as exc:
        logger.warning("Vobiz ring callback parse failed: {}", exc)
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")


@router.post("/vobiz/hangup")
@router.get("/vobiz/hangup")
async def vobiz_hangup_callback(request: Request):
    """Vobiz hangup callback: called when the call ends."""
    try:
        data = await _parse_vobiz_callback(request)
        camp_id = data.get("camp_id") or data.get("CampID") or ""
        call_uuid = data.get("CallUUID") or data.get("call_uuid") or data.get("RequestUUID") or ""
        call_status = data.get("CallStatus") or ""
        event = data.get("Event") or ""
        logger.info(
            "Vobiz hangup callback: camp_id={} call_uuid={} event={} status={} payload={}",
            camp_id, call_uuid, event, call_status, data,
        )
    except Exception as exc:
        logger.warning("Vobiz hangup callback parse failed: {}", exc)
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")


@router.websocket("/ws/vobiz/{path_param:path}")
async def vobiz_ws_endpoint(
    websocket: WebSocket,
    path_param: str = "",
):
    """Vobiz media WebSocket — path-based parameter routing.

    Path formats (no query strings to avoid Vobiz XML entity issues):
      /ws/vobiz/c/{camp_id}                    — outbound campaign / manual
      /ws/vobiz/i/{inbound_role}               — inbound PSTN
      /ws/vobiz/a/{agent_id}                   — sandbox agent
      /ws/vobiz/c/{camp_id}/a/{agent_id}       — campaign + agent
      /ws/vobiz                                — legacy fallback (query params)
    """
    camp_id = None
    agent_id = None
    inbound_role = None
    manual_role = None

    if path_param:
        parts = path_param.strip("/").split("/")
        i = 0
        while i < len(parts):
            seg = parts[i]
            if seg == "c" and i + 1 < len(parts):
                camp_id = parts[i + 1]
                i += 2
            elif seg == "a" and i + 1 < len(parts):
                agent_id = parts[i + 1]
                i += 2
            elif seg == "i" and i + 1 < len(parts):
                inbound_role = parts[i + 1]
                i += 2
            else:
                i += 1

    # Legacy fallback: support old query-parameter style for any clients
    # that haven't migrated yet.
    if not camp_id and not agent_id and not inbound_role:
        qp = dict(websocket.query_params)
        camp_id = qp.get("camp_id")
        agent_id = qp.get("agent_id")
        inbound_role = qp.get("inbound_role")
        manual_role = qp.get("manual_role")

    logger.info(
        "Vobiz WS connect: path={!r} camp_id={} agent_id={} inbound_role={} manual_role={}",
        path_param, camp_id, agent_id, inbound_role, manual_role,
    )
    await handle_vobiz_ws_live(
        websocket,
        camp_id=camp_id,
        agent_id=agent_id,
        inbound_role=inbound_role,
        manual_role=manual_role,
    )
