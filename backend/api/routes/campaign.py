"""Campaign management routes — SQLite-backed, production-ready."""

from __future__ import annotations

import asyncio
import csv
import datetime
import time
import io
import re

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel, Field

from config import settings
from core import storage as lead_storage
from core.state import (
    save_role_state, add_leads_bulk,
    update_lead_status, reset_leads, wipe_leads,
    export_leads_csv, _CAMPAIGN_TASKS, total_active_vobiz_calls,
    normalize_console_role,
    _ROLES,
)
from core.campaign_payload import (
    build_campaign_state_dashboard_fields,
    enrich_lead_for_console,
    slim_lead_for_api,
)
from core.campaign_hours import get_campaign_hours_status
from core.worker import (
    _campaign_worker_role,
    _analyze_and_update_lead,
    inter_call_gap_seconds_for_role,
    _read_transcript_jsonl,
    release_orphaned_dialing_leads,
)
from core.utils import _prewarm_opening
from core.phone_norm import norm_phone_str as _norm_phone_str
from services.call_recording import resolve_session_recording_path

# Global cache for campaign status response to protect event loop/CPU from 4-second frontend polling.
_STATE_CACHE: dict[str, tuple[float, dict]] = {}

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


def _jwt_payload_from_request(request: Request) -> dict | None:
    """Bearer header or ``access_token`` / ``token`` query (for ``<audio src>`` playback)."""
    from core.auth import _decode_jwt

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload:
            return payload
    for key in ("access_token", "token"):
        raw = (request.query_params.get(key) or "").strip()
        if raw:
            payload = _decode_jwt(raw)
            if payload:
                return payload
    return None


def _campaign_role(request: Request) -> str:
    from core.auth import console_role_from_request

    return console_role_from_request(request, default="data_edge")


def _sanitize_tabular_rows(rows: list[dict]) -> list[dict]:
    """Normalize CSV/XLS headers: strip BOM, trim keys and string cell values."""
    fixed: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        nr: dict = {}
        for k, v in r.items():
            nk = str(k).replace("\ufeff", "").strip() if k is not None else ""
            if not nk:
                nk = str(k)
            nv = "" if v is None else str(v).strip()
            nr[nk] = nv
        fixed.append(nr)
    return fixed


def _extract_phone_cell(row: dict, phone_hint: str | None, norm_phone) -> str:
    """Use auto-detected phone column first, then scan non-address cells for a dialable number."""
    keys = list(row.keys())
    order: list[str] = []
    if phone_hint:
        h = str(phone_hint).strip()
        if h in keys:
            order.append(h)
    
    # Avoid scanning columns that are highly likely to contain false-positive numbers like pincodes
    bad_fallbacks = {"address", "location", "city", "pincode", "zipcode", "website", "email", "name", "businessname", "clinic", "company"}
    for k in keys:
        if k not in order:
            kl = k.lower().strip()
            if not any(bf in kl for bf in bad_fallbacks):
                order.append(k)

    for k in order:
        cand = norm_phone(str(row.get(k, "") or "").strip())
        if cand:
            return cand
    return ""


def _looks_like_row_index_header(col: str) -> bool:
    """Headers such as ``S.No``, ``#``, ``ID`` — not person's name / company."""

    raw = str(col or "").strip()
    if not raw:
        return False
    hn = re.sub(r"[^\w+#]+", " ", raw.strip().lower()).strip().replace(".", "").replace("_", "")
    if not hn.replace("#", ""):
        return True
    if any(tok in hn for tok in ("name", "fullname", "first name", "person", "contact name", "customer name", "lead name")):
        return False
    compact = hn.replace(" ", "")
    needles = ("sno", "slno", "serialno", "linenumber", "lineno", "rownum", "rownumber")
    if any(n in compact for n in needles):
        return True
    if hn in {"id", "#", "sn", "sl", "index", "rank", "serial", "row"} or hn.endswith(" id"):
        return True
    if hn.startswith("unnamed"):
        return True
    if hn.startswith("col") and len(hn) > 3 and hn[3:].isdigit():
        return True
    return False


def _column_values_mostly_row_numbers(values: list[str], threshold: float = 0.7) -> bool:
    """True when cells look like spreadsheet row counters (``11.0``, ``10``…) not people."""

    nonempty: list[str] = []
    for v in values:
        t = str(v or "").strip().replace(",", "").replace(" ", "")
        if t:
            nonempty.append(t)
    if len(nonempty) < 3:
        return False
    pat = re.compile(r"^-?\d+(?:\.(?:0+|00+))?$")
    hits = sum(1 for t in nonempty if pat.fullmatch(t))
    return hits / len(nonempty) >= threshold


@router.post("/upload")
async def upload_leads(file: UploadFile = File(...), request: Request = None):
    try:
        role = _campaign_role(request) if request else "data_edge"
        content = await file.read()
        filename = (file.filename or "").lower()

        EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

        def _is_phone(val: str) -> bool:
            v = val.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "").replace(".", "")
            return v.isdigit() and 7 <= len(v) <= 15

        def _is_email(val: str) -> bool:
            return bool(EMAIL_RE.match(val.strip()))

        def _score_column(values: list, check_fn) -> float:
            if not values:
                return 0.0
            hits = sum(1 for v in values if v and check_fn(str(v)))
            return hits / len(values)

        def _is_product_like_column(col: str) -> bool:
            """RFQ sheets often label product/subject columns — never use as contact name."""
            cl = col.strip().lower()
            product_keys = (
                "product", "subject", "rfq", "specification", "spec", "category",
                "item", "description", "requirement", "material", "commodity",
                "goods", "particulars", "enquiry", "inquiry",
            )
            return any(kw in cl for kw in product_keys)

        def _detect_columns(rows: list[dict], upload_role: str = "data_edge") -> dict:
            if not rows:
                return {}
            cols = list(rows[0].keys())
            sample = rows[:30]
            col_values = {c: [str(r.get(c, "") or "") for r in sample] for c in cols}

            phone_scores = {c: _score_column(col_values[c], _is_phone) for c in cols}
            email_scores = {c: _score_column(col_values[c], _is_email) for c in cols}

            # Prioritize columns that explicitly mention phone / whatsapp / mobile if they have active phone values
            phone_col = None
            phone_candidates = sorted(
                [(c, phone_scores[c]) for c in cols if phone_scores[c] > 0],
                key=lambda x: (
                    any(tok in x[0].lower() for tok in ("whatsapp", "phone", "mobile", "contact")),
                    x[1]
                ),
                reverse=True
            )
            if phone_candidates:
                phone_col = phone_candidates[0][0]

            email_col = max(email_scores, key=email_scores.get) if email_scores else None
            if phone_col and phone_scores[phone_col] < 0.2:
                phone_col = None
            if email_col and email_scores[email_col] < 0.3:
                email_col = None

            text_cols = [c for c in cols if c not in (phone_col, email_col)]
            NAME_KEYWORDS = ['name', 'person', 'client', 'buyer', 'seller', 'agent', 'contact', 'lead', 'customer']
            COMPANY_KEYWORDS = ['company', 'business', 'organization', 'org', 'firm', 'brand', 'employer', 'shop', 'store', 'enterprise']

            def _col_matches(col: str, keywords: list) -> bool:
                cl = col.strip().lower()
                return any(kw in cl for kw in keywords)

            def _bad_for_contact_field(c: str) -> bool:
                cl = c.strip().lower()
                if cl in ('city', 'location', 'state', 'district', 'town', 'address', 'region', 'zone', 'branch', 'pincode', 'zip', 'country') or any(tok in cl for tok in ('city', 'address', 'district')):
                    return True
                return bool(
                    _looks_like_row_index_header(c)
                    or _column_values_mostly_row_numbers(col_values.get(c, []))
                )

            product_cols = set()

            name_col = company_col = None
            for c in text_cols:
                if _bad_for_contact_field(c):
                    continue
                if c.strip().lower() in ('name', 'full name', 'first name', 'contact name', 'customer name'):
                    name_col = c
                    break
            for c in text_cols:
                if _bad_for_contact_field(c):
                    continue
                if c.strip().lower() in ('company', 'company name', 'business', 'organization'):
                    company_col = c
                    break
            if not name_col:
                for c in text_cols:
                    if c == company_col or _bad_for_contact_field(c):
                        continue
                    if _col_matches(c, NAME_KEYWORDS):
                        name_col = c
                        break
            if not company_col:
                for c in text_cols:
                    if c == name_col or _bad_for_contact_field(c):
                        continue
                    if _col_matches(c, COMPANY_KEYWORDS):
                        company_col = c
                        break

            remaining = [c for c in text_cols if c not in (name_col, company_col)]

            def _pick_fallback(candidates: list[str]):
                for c in candidates:
                    if _bad_for_contact_field(c):
                        continue
                    return c
                return None

            if not name_col:
                name_col = _pick_fallback([c for c in remaining if c not in product_cols])
                if name_col:
                    remaining = [c for c in remaining if c != name_col]
            if not company_col:
                company_col = _pick_fallback(remaining)

            logger.info(
                f"Auto-detected columns for {upload_role} → phone:{phone_col}, name:{name_col}, "
                f"email:{email_col}, company:{company_col}"
            )
            return {
                "phone": phone_col,
                "name": name_col,
                "email": email_col,
                "company": company_col,
                "product_cols": list(product_cols) if product_cols else [],
            }

        rows = []
        headers = []
        try:
            if filename.endswith('.xlsx'):
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                ws = wb.active
                all_rows = list(ws.iter_rows(values_only=True))
                if all_rows:
                    headers = [str(c or f"col{i}").strip() for i, c in enumerate(all_rows[0])]
                    for row in all_rows[1:]:
                        rows.append({headers[i]: str(v or "") for i, v in enumerate(row) if i < len(headers)})
            elif filename.endswith('.xls'):
                import xlrd
                wb = xlrd.open_workbook(file_contents=content)
                ws = wb.sheet_by_index(0)
                if ws.nrows > 0:
                    headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
                    for r in range(1, ws.nrows):
                        rows.append({headers[c]: str(ws.cell_value(r, c)) for c in range(ws.ncols)})
            else:
                decoded = None
                for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
                    try:
                        decoded = content.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if not decoded:
                    decoded = content.decode('latin-1', errors='replace')
                decoded = decoded.replace('\r\n', '\n').replace('\r', '\n')
                reader = csv.DictReader(io.StringIO(decoded))
                rows = list(reader)
                headers = reader.fieldnames or (list(rows[0].keys()) if rows else [])
        except Exception as e:
            logger.error(f"File parse error: {e}")
            raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")

        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        rows = _sanitize_tabular_rows(rows)
        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        col_map = _detect_columns(rows, upload_role=role)
        phone_col = col_map.get("phone")
        name_col = col_map.get("name")
        email_col = col_map.get("email")
        company_col = col_map.get("company")

        segment_col = None
        for col in headers:
            cl = col.strip().lower()
            if cl in ("type", "segment", "role", "sub-role", "subrole", "user role", "lead role"):
                segment_col = col
                break
        if not segment_col:
            for col in headers:
                cl = col.strip().lower()
                if "segment" in cl or "subrole" in cl or ("type" in cl and cl != "content-type" and cl != "content type"):
                    segment_col = col
                    break

        mapped_cols = {c for c in (phone_col, name_col, email_col, company_col, segment_col) if c}

        clean_leads = []
        for r in rows:
            ph = _extract_phone_cell(r, phone_col, _norm_phone_str)
            if not ph:
                continue
            raw_name = str(r.get(name_col, "") if name_col else "").strip()
            
            raw_segment = "rfq"
            if segment_col:
                seg_val = str(r.get(segment_col, "")).strip().lower()
                if "seller" in seg_val or "sell" in seg_val:
                    raw_segment = "seller"
                elif "rfq" in seg_val or "buy" in seg_val:
                    raw_segment = "rfq"

            entry = {
                "name": raw_name or "Unknown",
                "phone": ph,
                "email": str(r.get(email_col, "") if email_col else "").strip(),
                "company": str(r.get(company_col, "") if company_col else "").strip(),
                "details": "",
                "segment": raw_segment,
            }
            for col, val in r.items():
                if col in mapped_cols:
                    continue
                sv = str(val or "").strip()
                if sv:
                    entry[col] = sv
            clean_leads.append(entry)

        count = add_leads_bulk(role, clean_leads)
        logger.info(f"Upload complete for role '{role}': {count} leads saved to database.")
        recent: list = []
        if count:
            n = min(150, max(int(count), 1))
            recent_raw = await lead_storage.get_leads(role, limit=n)
            recent = [enrich_lead_for_console(dict(x)) for x in recent_raw]
        return {
            "status": "ok",
            "count": count,
            "recent": recent,
            "leads": clean_leads[:50],
            "headers": headers,
            "column_map": col_map,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lead upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload leads")


@router.post("/toggle")
async def toggle_campaign(request: Request):
    """Legacy alternating start/stop. Prefer ``POST /api/campaign/start`` and ``/stop``.

    Mirrors intent flags used for auto-resume after restarts (see ``START``).
    """
    try:
        role = _campaign_role(request)
        _STATE_CACHE.pop(role, None)

        if _CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done():
            await lead_storage.set_campaign_want_running(role, False)
            await lead_storage.set_campaign_globally_paused(True)
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
            await release_orphaned_dialing_leads(role)
            logger.info(f"Stopped campaign for {role} (toggle).")
            return {"status": "stopped", "active": False, "campaign_paused": True}
        else:
            from core.worker import _schedule_preflight

            await lead_storage.set_campaign_globally_paused(False)
            err = await _schedule_preflight(role)
            if err:
                raise HTTPException(status_code=400, detail=err)
            await lead_storage.set_campaign_want_running(role, True)
            _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
            logger.info(f"Started campaign for {role} (toggle).")
            return {"status": "started", "active": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Toggle campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle campaign")


@router.post("/start")
async def start_campaign(request: Request):
    """Start the dialer for this role (**idempotent** — never stops an already-running worker).

    Historically `/start` mistakenly called toggle logic that would **stop** the campaign when a
    task was already alive, which confused the dashboard and halted runs on double-clicks/resync.
    """
    try:
        role = _campaign_role(request)
        _STATE_CACHE.pop(role, None)
        run = _CAMPAIGN_TASKS.get(role)
        if run and not run.done():
            try:
                await lead_storage.set_campaign_globally_paused(False)
            except Exception:
                pass
            c = await lead_storage.get_lead_counts(role)
            return {
                "status": "already_running",
                "active": True,
                "pending": c.get("pending", 0),
                "dialing": c.get("dialing", 0),
                "campaign_paused": False,
            }
        from core.worker import _schedule_preflight

        try:
            await lead_storage.set_campaign_globally_paused(False)
        except Exception as e:
            logger.error(f"Failed to clear global pause flag: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to clear campaign pause state: {e}")

        err = await _schedule_preflight(role)
        if err:
            raise HTTPException(status_code=400, detail=err)

        try:
            await lead_storage.set_campaign_want_running(role, True)
        except Exception as e:
            logger.error(f"Failed to set want_running: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to set campaign run state: {e}")

        _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))

        try:
            c = await lead_storage.get_lead_counts(role)
        except Exception as e:
            logger.error(f"Failed to get lead counts: {e}")
            c = {}

        return {
            "status": "started",
            "active": True,
            "pending": c.get("pending", 0),
            "dialing": c.get("dialing", 0),
            "campaign_paused": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Start campaign failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start campaign: {e}")


@router.post("/stop")
async def stop_campaign(request: Request):
    try:
        role = _campaign_role(request)
        _STATE_CACHE.pop(role, None)
        await lead_storage.set_campaign_want_running(role, False)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        if role in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[role]["running"] = False
        released = await release_orphaned_dialing_leads(role)
        return {"status": "stopped", "active": False, "released_dialing": released, "campaign_paused": False}
    except Exception as e:
        logger.error(f"Stop campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to stop campaign")


@router.post("/stop-all")
async def stop_all_campaigns(request: Request):
    """Stop outbound dialers for every console role and clear orphaned dialing rows."""
    caller = _campaign_role(request)
    if caller != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to stop all campaigns")

    _STATE_CACHE.clear()
    await lead_storage.set_campaign_globally_paused(True)
    stopped: list[str] = []
    for r in _ROLES:
        await lead_storage.set_campaign_want_running(r, False)
        task = _CAMPAIGN_TASKS.get(r)
        if task and not task.done():
            task.cancel()
        _CAMPAIGN_TASKS[r] = None
        released = await release_orphaned_dialing_leads(r)
        if r in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[r]["running"] = False
        stopped.append(r)
        logger.info("stop-all: role={} released_dialing={}", r, released)

    return {
        "status": "stopped_all",
        "roles": stopped,
        "active_campaigns": 0,
        "campaign_paused": True,
    }


@router.post("/reset")
async def reset_campaign(request: Request):
    try:
        role = _campaign_role(request)
        _STATE_CACHE.pop(role, None)
        reset_leads(role)
        counts = await lead_storage.get_lead_counts(role)
        return {"status": "reset", "count": counts.get("total", 0)}
    except Exception as e:
        logger.error(f"Reset campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset campaign")


@router.post("/wipe")
async def wipe_campaign(request: Request):
    try:
        role = _campaign_role(request)
        await lead_storage.set_campaign_want_running(role, False)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        wipe_leads(role)
        logger.info(f"Wipe complete for role: {role}")
        return {"status": "wiped"}
    except Exception as e:
        logger.error(f"Wipe campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to wipe campaign")


@router.post("/lead/{lead_id}/status")
async def update_lead_status_route(lead_id: int, request: Request):
    try:
        role = _campaign_role(request)
        data = await request.json()
        new_status = data.get("status", "")
        VALID = {"pending", "completed", "failed", "not_interested", "callback_scheduled"}
        if new_status not in VALID:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID}")
        await update_lead_status(lead_id, new_status)
        logger.info(f"Lead {lead_id} marked as {new_status}")
        return {"status": "ok", "lead_id": lead_id, "new_status": new_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update lead status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to update lead status")


@router.post("/lead/{lead_id}/disposition")
async def update_lead_disposition_route(lead_id: int, request: Request):
    try:
        data = await request.json()
        new_dispo = (data.get("disposition") or "").strip()
        if new_dispo not in lead_storage.VALID_DISPOSITIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid disposition. Must be one of: {lead_storage.VALID_DISPOSITIONS}",
            )
        ok = await lead_storage.update_lead_disposition(lead_id, new_dispo)
        if not ok:
            raise HTTPException(status_code=404, detail="Lead not found")
        logger.info(f"Lead {lead_id} disposition overridden → {new_dispo}")
        return {"status": "ok", "lead_id": lead_id, "disposition": new_dispo}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update lead disposition failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to update lead disposition")


def _norm_phone_digits(phone: object) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


async def _resolve_lead_session_log_id(role: str, row: dict) -> str:
    """``_log_id`` on this row, or the same phone+role duplicate that has a session log."""
    log_id = str(row.get("_log_id") or row.get("log_id") or "").strip()
    if log_id:
        return log_id
    phone = _norm_phone_digits(row.get("phone"))
    if not phone:
        return ""
    for sibling in await lead_storage.get_leads(role, limit=20_000):
        if _norm_phone_digits(sibling.get("phone")) != phone:
            continue
        sid = str(sibling.get("_log_id") or sibling.get("log_id") or "").strip()
        if sid:
            return sid
    return ""


@router.get("/lead/{lead_id}/transcript")
async def campaign_lead_transcript(
    lead_id: int,
    request: Request,
):
    """Raw JSONL (same folder layout as analyzer / manual calls)."""
    role = _campaign_role(request)
    row = await lead_storage.get_lead(role, lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    log_id = await _resolve_lead_session_log_id(role, row)
    if not log_id:
        raise HTTPException(status_code=404, detail="No transcript session for this lead")
    raw = _read_transcript_jsonl(role, log_id)
    if not (raw or "").strip():
        raise HTTPException(status_code=404, detail="Transcript file missing or empty")
    return Response(content=raw, media_type="text/plain; charset=utf-8")


@router.get("/lead/{lead_id}/recording")
async def campaign_lead_recording(
    lead_id: int,
    request: Request,
):
    """Mixed 16 kHz WAV when CallRecorder captured this session."""
    role = _campaign_role(request)
    row = await lead_storage.get_lead(role, lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    log_id = await _resolve_lead_session_log_id(role, row)
    if not log_id:
        raise HTTPException(status_code=404, detail="No session log for recording lookup")
    rec = resolve_session_recording_path(log_id)
    if not rec or not rec.is_file():
        raise HTTPException(status_code=404, detail="Recording not found")
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return FileResponse(
        rec,
        media_type=media_type,
        filename=rec.name,
        headers={"Accept-Ranges": "bytes"},
    )


@router.post("/lead/{lead_id}/analyze")
async def retrigger_analysis(
    lead_id: int,
    request: Request,
):
    try:
        role = _campaign_role(request)
        lead_row = await lead_storage.get_lead(role, lead_id)
        if not lead_row:
            raise HTTPException(status_code=404, detail="Lead not found")
        log_id = lead_row.get("_log_id")
        if not log_id:
            raise HTTPException(status_code=400, detail="No log ID found for this lead")
        await _analyze_and_update_lead(role, lead_id, log_id)
        refreshed = await lead_storage.get_lead(role, lead_id)
        if not refreshed:
            raise HTTPException(status_code=500, detail="Lead missing after analyze")
        return {"status": "ok", "lead": slim_lead_for_api(dict(refreshed), role=role)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Retrigger analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyze call")


# ── Re-analyze All ────────────────────────────────────────────────────

_REANALYZE_ALL_PROGRESS: dict[str, dict] = {}

@router.post("/reanalyze-all")
async def campaign_reanalyze_all(request: Request):
    """Re-analyze every completed lead that has a log_id and recording."""
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS and _REANALYZE_ALL_PROGRESS[role].get("running"):
        raise HTTPException(status_code=409, detail="Re-analyze already running for this role")

    leads = await lead_storage.get_leads(role, limit=20000)
    eligible = [l for l in leads if l.get("status") in ("completed", "failed", "not_interested") and l.get("_log_id")]
    if not eligible:
        raise HTTPException(status_code=400, detail="No eligible leads found (need completed/failed status + log_id)")

    total = len(eligible)
    _REANALYZE_ALL_PROGRESS[role] = {
        "running": True,
        "total": total,
        "completed": 0,
        "current": "",
        "errors": [],
    }

    async def _run():
        try:
            for idx, lead in enumerate(eligible):
                if not _REANALYZE_ALL_PROGRESS.get(role, {}).get("running"):
                    break
                lid = lead["id"]
                log_id = lead.get("_log_id", "")
                name = lead.get("name", f"#{lid}")
                _REANALYZE_ALL_PROGRESS[role]["current"] = f"{name} ({lead.get('phone','')})"
                try:
                    await _analyze_and_update_lead(role, lid, log_id)
                except Exception as e:
                    _REANALYZE_ALL_PROGRESS[role]["errors"].append(f"#{lid} {name}: {e}")
                _REANALYZE_ALL_PROGRESS[role]["completed"] = idx + 1
                await asyncio.sleep(4.0)
        finally:
            if role in _REANALYZE_ALL_PROGRESS:
                _REANALYZE_ALL_PROGRESS[role]["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "total": total}


@router.get("/reanalyze-all/progress")
async def campaign_reanalyze_all_progress(request: Request):
    role = _campaign_role(request)
    state = _REANALYZE_ALL_PROGRESS.get(role)
    if not state:
        return {"running": False, "total": 0, "completed": 0, "current": "", "errors": []}
    return {
        "running": state.get("running", False),
        "total": state.get("total", 0),
        "completed": state.get("completed", 0),
        "current": state.get("current", ""),
        "errors": state.get("errors", []),
    }


@router.post("/reanalyze-all/cancel")
async def campaign_reanalyze_all_cancel(request: Request):
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS:
        _REANALYZE_ALL_PROGRESS[role]["running"] = False
    return {"status": "cancelled"}


@router.get("/manifest")
async def campaign_manifest_preview(
    request: Request,
    limit: int = Query(0, ge=0, description="Max rows for dashboard Lead Manifest + call list (0 = unlimited)"),
):
    """Lightweight full-row fetch for UI tables — avoids oversized ``/state`` payloads."""
    role = _campaign_role(request)
    rows = await lead_storage.get_leads(
        role, limit=int(limit) if int(limit) > 0 else 9_999_999_999, order="activity"
    )
    enriched = [slim_lead_for_api(dict(r), role=role) for r in rows]
    return {"role": role, "returned": len(enriched), "leads": enriched}



@router.get("/state")
async def get_campaign_status(
    request: Request,
    chart_sample_limit: int = Query(2000, ge=50, le=10000, description="Sample size for donut/callback charts embedded in state"),
):
    try:
        role = _campaign_role(request)
        now = time.time()
        if role in _STATE_CACHE:
            cached_time, cached_val = _STATE_CACHE[role]
            if now - cached_time < 3.0:
                return cached_val

        counts = await lead_storage.get_lead_counts(role)
        sample_cap = min(int(chart_sample_limit), 10000)
        chart_rows = await lead_storage.get_leads(role, limit=sample_cap)
        dash = build_campaign_state_dashboard_fields(role, chart_rows)
        chart_leads = [
            slim_lead_for_api(l, role=role) for l in dash.pop("leads_enriched", [])
        ]
        total_in_db = int(counts.get("total") or 0)
        dash["called_count"] = await lead_storage.count_leads_with_outbound_attempt(role)
        from core.storage import (
            is_strict_gap_core_role,
            STRICT_CORE_GAP_MIN_SEC,
            STRICT_CORE_GAP_MAX_SEC,
        )

        gap_strict = is_strict_gap_core_role(role)
        res = {
            "active": bool(_CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done()),
            "inter_call_gap_sec": inter_call_gap_seconds_for_role(role),
            "inter_call_gap_strict": gap_strict,
            "inter_call_gap_min_sec": int(STRICT_CORE_GAP_MIN_SEC) if gap_strict else None,
            "inter_call_gap_max_sec": int(STRICT_CORE_GAP_MAX_SEC) if gap_strict else None,
            **counts,
            **dash,
            
            "chart_sample": chart_leads,
            "leads": chart_leads,
            "manifest_fetch_hint": {"endpoint": "/api/campaign/manifest", "suggested_limit": 0},
            "lead_list_truncated": total_in_db > len(chart_leads),
            "leads_returned": len(chart_leads),
            "active_calls": total_active_vobiz_calls(),
            "campaign_hours": get_campaign_hours_status(),
            "campaign_paused": await lead_storage.is_campaign_globally_paused(),
        }
        _STATE_CACHE[role] = (now, res)
        return res
    except Exception as e:
        logger.error(f"Get campaign status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get campaign status")


class InterCallGapBody(BaseModel):
    """Seconds to wait after each outbound leg before dialing the next lead (same role)."""
    seconds: float = Field(..., ge=0, le=1200, description="0 = back-to-back; max 20 minutes")


@router.post("/inter-call-gap")
async def set_inter_call_gap(body: InterCallGapBody, request: Request):
    """Persist pause between consecutive campaign calls for this role (``role_state.delay_sec``)."""
    try:
        from core.storage import (
            is_strict_gap_core_role,
            STRICT_CORE_GAP_SEC,
            STRICT_CORE_GAP_MIN_SEC,
            STRICT_CORE_GAP_MAX_SEC,
        )

        role = _campaign_role(request)
        if is_strict_gap_core_role(role):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Sellers, Buyers, RFQs, and Dariaan use a fixed {int(STRICT_CORE_GAP_SEC)}s pause "
                    f"({int(STRICT_CORE_GAP_MIN_SEC)}–{int(STRICT_CORE_GAP_MAX_SEC)}s carrier safety); "
                    "it cannot be changed."
                ),
            )
        sec = float(body.seconds)
        save_role_state(role, delay_sec=sec)
        logger.info(f"inter_call_gap_sec={sec} saved for role={role}")
        return {"status": "ok", "inter_call_gap_sec": sec}
    except Exception as e:
        logger.error(f"Set inter-call gap failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save inter-call gap")


@router.get("/download")
async def download_leads(request: Request, filter: str = "all"):
    try:
        role = _campaign_role(request)
        leads = export_leads_csv(role, filter)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Name", "Phone", "Email", "Company", "Status", "Called At", "Error"])
        for l in leads:
            called_at = ""
            if l.get("start_time"):
                called_at = datetime.datetime.fromtimestamp(l["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([
                l.get("name", ""),
                l.get("phone", ""),
                l.get("email", ""),
                l.get("company", ""),
                l.get("status", ""),
                called_at,
                l.get("error", ""),
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        filename = f"leads_{role}_{filter}.csv"
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"Download leads failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to download leads")


@router.get("/inbound-callbacks")
async def list_inbounds(role: str = Query(...)):
    from core.storage import list_inbound_callbacks

    role = normalize_console_role(role)
    try:
        items = await list_inbound_callbacks(role)
        return {"items": items}
    except Exception as e:
        logger.error(f"List inbounds failed: {e}")
        raise HTTPException(status_code=500, detail="Could not load inbound callbacks") from e


@router.post("/inbound-callbacks/{row_id}/dismiss")
async def dismiss_inbound(row_id: int, role: str = Query(...)):
    from core.storage import dismiss_inbound_callback

    role = normalize_console_role(role)
    try:
        ok = await dismiss_inbound_callback(row_id, role)
        return {"status": "ok" if ok else "not_found"}
    except Exception as e:
        logger.error(f"Dismiss inbound failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class RescheduleOutcomesBody(BaseModel):
    from_time_epoch: float | None = None
    to_time_epoch: float | None = None
    categories: list[str] = Field(default_factory=list)
    reschedule_time_epoch: float


@router.post("/reschedule-outcomes")
async def reschedule_outcomes_route(body: RescheduleOutcomesBody, request: Request):
    role = _campaign_role(request)
    try:
        from core.storage import reschedule_leads_by_outcome
        count = await reschedule_leads_by_outcome(
            role=role,
            from_time=body.from_time_epoch,
            to_time=body.to_time_epoch,
            categories=body.categories,
            reschedule_time=body.reschedule_time_epoch
        )
        return {"status": "ok", "rescheduled_count": count}
    except Exception as e:
        logger.error(f"Reschedule outcomes failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
