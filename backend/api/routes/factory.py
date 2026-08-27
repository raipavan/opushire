"""Agent Factory (Sandbox) routes — SQLite-backed, production-ready."""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel

from config import settings
from services.sandbox_manager import (
    create_agent, get_agent, list_agents, delete_agent,
    update_agent, associate_file_with_agent, add_agent_lead,
    get_agent_leads, add_agent_knowledge_file,
)
from core.state import _CAMPAIGN_DATA, get_state
from core.phone_norm import norm_phone_str as _norm_phone_str
from core.utils import _prewarm_opening, _build_opening_line

router = APIRouter(prefix="/api/factory", tags=["factory"])


class AgentCreate(BaseModel):
    name: str
    prompt: Optional[str] = "You are a helpful AI agent."
    voice: str = "Puck"


class StartCallRequest(BaseModel):
    lead_index: Optional[int] = None
    lead: Optional[dict] = None



@router.get("/agents")
async def factory_list_agents(role: str = "factory"):
    try:
        agents = list_agents(role=role)
        return {"agents": agents, "total": len(agents)}
    except Exception as e:
        logger.error(f"Failed to list agents for role {role}: {e}")
        raise HTTPException(status_code=500, detail="Failed to list agents")


@router.post("/agents", status_code=201)
async def factory_create_agent(a: AgentCreate, role: str = "factory"):
    try:
        if not a.name or not a.prompt:
            raise HTTPException(status_code=400, detail="name and prompt are required")
        agent_id = create_agent(name=a.name, prompt=a.prompt, voice=a.voice, role=role)
        agent = get_agent(agent_id)
        return {"status": "created", "agent": agent}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create agent: {e}")
        raise HTTPException(status_code=500, detail="Failed to create agent")


@router.get("/agent/{agent_id}")
async def factory_get_agent(agent_id: str):
    try:
        agent = get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {"agent": agent}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve agent")


@router.delete("/agent/{agent_id}")
async def factory_delete_agent(agent_id: str):
    try:
        ok = delete_agent(agent_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete agent")


@router.post("/agent/{agent_id}/update")
async def factory_update_agent(agent_id: str, data: dict):
    try:
        agent = get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        update_agent(
            agent_id,
            name=data.get("name"),
            prompt=data.get("prompt"),
            voice=data.get("voice"),
        )
        return get_agent(agent_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update agent")


@router.post("/agent/{agent_id}/upload-doc")
async def factory_upload_doc(agent_id: str, file: UploadFile = File(...)):
    try:
        agent = get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        content = await file.read()
        result = associate_file_with_agent(agent_id, content, file.filename or "upload")
        if not result:
            raise HTTPException(status_code=404, detail="Agent not found")
        if result.get("extracted_text", "").startswith("[Error"):
            raise HTTPException(status_code=400, detail=result["extracted_text"])
        return {
            "status": "synced",
            "file_id": result["file_id"],
            "filename": result["filename"],
            "extracted_length": len(result.get("extracted_text", "")),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload doc for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload document")


@router.post("/agent/{agent_id}/leads")
async def factory_upload_leads(agent_id: str, file: UploadFile = File(...)):
    try:
        agent = get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        content = await file.read()
        filename = (file.filename or "").lower()
        rows = []
        headers = []

        if filename.endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            if all_rows:
                headers = [str(c or f"col{i}").strip() for i, c in enumerate(all_rows[0])]
                for row in all_rows[1:]:
                    rows.append({headers[i]: str(v or "") for i, v in enumerate(row) if i < len(headers)})
        elif filename.endswith(".xls"):
            import xlrd
            wb = xlrd.open_workbook(file_contents=content)
            ws = wb.sheet_by_index(0)
            if ws.nrows > 0:
                headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
                for r in range(1, ws.nrows):
                    rows.append({headers[c]: str(ws.cell_value(r, c)) for c in range(ws.ncols)})
        elif filename.endswith(".json"):
            decoded = content.decode("utf-8")
            data = json.loads(decoded)
            if isinstance(data, list):
                rows = data
                if rows:
                    headers = list(rows[0].keys())
            elif isinstance(data, dict):
                rows = [data]
                headers = list(data.keys())
            else:
                raise ValueError("JSON must be an array of objects or a single object")
        else:
            decoded = None
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    decoded = content.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if not decoded:
                decoded = content.decode("latin-1", errors="replace")
            decoded = decoded.replace("\r\n", "\n").replace("\r", "\n")
            reader = csv.DictReader(io.StringIO(decoded))
            rows = list(reader)
            headers = reader.fieldnames or (list(rows[0].keys()) if rows else [])

        EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

        def _is_phone(val: str) -> bool:
            v = val.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "").replace(".", "")
            return v.isdigit() and 7 <= len(v) <= 15

        def _is_email(val: str) -> bool:
            return bool(EMAIL_RE.match(val.strip()))

        def _score_column(values, check_fn) -> float:
            if not values:
                return 0.0
            hits = sum(1 for v in values if v and check_fn(str(v)))
            return hits / len(values)

        def _detect_columns(rows_list):
            if not rows_list:
                return {}
            cols = list(rows_list[0].keys())
            sample = rows_list[:30]
            col_values = {c: [str(r.get(c, "") or "") for r in sample] for c in cols}
            phone_scores = {c: _score_column(col_values[c], _is_phone) for c in cols}
            email_scores = {c: _score_column(col_values[c], _is_email) for c in cols}
            phone_col = max(phone_scores, key=phone_scores.get) if phone_scores else None
            email_col = max(email_scores, key=email_scores.get) if email_scores else None
            if phone_col and phone_scores[phone_col] < 0.3:
                phone_col = None
            if email_col and email_scores[email_col] < 0.3:
                email_col = None
            text_cols = [c for c in cols if c not in (phone_col, email_col)]
            NAME_KEYWORDS = ["name", "person", "client", "buyer", "seller", "agent", "contact", "lead", "customer"]
            COMPANY_KEYWORDS = ["company", "business", "organization", "org", "firm", "brand", "employer", "shop", "store"]

            def _col_matches(col, keywords):
                cl = col.strip().lower()
                return any(kw in cl for kw in keywords)

            name_col = company_col = None
            for c in text_cols:
                if c.strip().lower() in ("name", "full name", "first name", "contact name"):
                    name_col = c
                    break
            for c in text_cols:
                if c.strip().lower() in ("company", "company name", "business", "organization"):
                    company_col = c
                    break
            if not name_col:
                for c in text_cols:
                    if c not in (company_col,) and _col_matches(c, NAME_KEYWORDS):
                        name_col = c
                        break
            if not company_col:
                for c in text_cols:
                    if c not in (name_col,) and _col_matches(c, COMPANY_KEYWORDS):
                        company_col = c
                        break
            GEOGRAPHIC_KEYWORDS = {'city', 'location', 'state', 'district', 'town', 'address', 'region', 'zone', 'branch', 'pincode', 'zip', 'country'}
            def _is_geo(c):
                cl = c.strip().lower()
                return cl in GEOGRAPHIC_KEYWORDS or any(tok in cl for tok in ('city', 'address', 'district'))

            remaining = [c for c in text_cols if c not in (name_col, company_col) and not _is_geo(c)]
            if not name_col and remaining:
                name_col = remaining.pop(0)
            if not company_col and remaining:
                company_col = remaining.pop(0)
            return {"phone": phone_col, "name": name_col, "email": email_col, "company": company_col}

        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": headers}

        col_map = _detect_columns(rows)
        phone_col = col_map.get("phone")
        name_col = col_map.get("name")
        email_col = col_map.get("email")
        company_col = col_map.get("company")

        added = []
        for r in rows:
            raw_phone = str(r.get(phone_col, "") if phone_col else "").strip()
            ph = _norm_phone_str(raw_phone)
            if not ph:
                continue
            lead = {
                "name": str(r.get(name_col, "") if name_col else "").strip() or "Unknown",
                "phone": ph,
                "email": str(r.get(email_col, "") if email_col else "").strip(),
                "company": str(r.get(company_col, "") if company_col else "").strip(),
            }
            lead_id = add_agent_lead(agent_id, lead)
            if lead_id:
                added.append({"lead_id": lead_id, **lead})

        return {"status": "ok", "count": len(added), "leads": added[:50], "column_map": col_map}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload leads for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload leads: {e}")


@router.post("/agent/{agent_id}/start-call")
async def factory_start_call(agent_id: str, req: StartCallRequest):
    try:
        agent = get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        lead = None
        if req.lead_index is not None:
            leads = get_agent_leads(agent_id)
            if req.lead_index < 0 or req.lead_index >= len(leads):
                raise HTTPException(status_code=404, detail="Lead index out of range")
            lead = leads[req.lead_index]
        elif req.lead:
            lead = req.lead
        else:
            raise HTTPException(status_code=400, detail="lead_index or lead object required")

        ph = _norm_phone_str(lead.get("phone", ""))
        if not ph:
            raise HTTPException(status_code=400, detail="Invalid phone number in lead")

        call_id = f"sandbox-{agent_id[:8]}-{uuid.uuid4()}"

        full_prompt = agent.get("prompt", "")
        knowledge_files = agent.get("knowledge_files", [])
        if knowledge_files:
            kb_text = "\n\n[Knowledge Base]\n"
            for kf in knowledge_files:
                kb_text += f"\n--- Source: {kf['filename']} ---\n{kf['extracted_text']}\n"
            full_prompt += kb_text

        opening = _build_opening_line(lead, "factory")

        v_cfg = get_state("factory").get("vobiz", {})
        v_auth_id = v_cfg.get("auth_id") or settings.vobiz_auth_id
        v_token = v_cfg.get("auth_token") or settings.vobiz_auth_token
        v_from = v_cfg.get("from_number") or settings.vobiz_from_number
        v_base = (v_cfg.get("public_url") or settings.vobiz_public_base_url or "").rstrip("/")

        if not v_auth_id or not v_token or not v_base:
            raise HTTPException(status_code=400, detail="Telephony not configured")

        _CAMPAIGN_DATA[call_id] = {
            "name": lead.get("name", "Unknown"),
            "phone": ph,
            "company": lead.get("company", ""),
            "email": lead.get("email", ""),
            "details": "Sandbox agent call",
            "_role": "factory",
            "_leadIndex": -1,
            "_agent_id": agent_id,
            "_sandbox_prompt": full_prompt,
            "_sandbox_voice": agent.get("voice", settings.gemini_live_voice),
            "opening_pcm": None,
        }

        asyncio.create_task(_prewarm_opening(call_id, opening, _CAMPAIGN_DATA[call_id]["_sandbox_voice"]))

        answer_url = f"{v_base}/vobiz/answer?camp_id={call_id}"

        from services.vobiz_bridge import make_vobiz_call

        async def _do_dial():
            try:
                _f_resp = await make_vobiz_call(
                    to=ph, from_=v_from, answer_url=answer_url,
                    auth_id=v_auth_id, auth_token=v_token,
                    extra={
                        "ring_url": f"{v_base}/vobiz/ring?camp_id={call_id}",
                        "ring_method": "POST",
                        "hangup_url": f"{v_base}/vobiz/hangup?camp_id={call_id}",
                        "hangup_method": "POST",
                        "hangup_on_ring": "3600",
                    },
                )
                _f_uuid = _f_resp.get("request_uuid") or ""
                if _f_uuid:
                    _CAMPAIGN_DATA[call_id]["_vobiz_call_uuid"] = _f_uuid
            except Exception as e:
                logger.error(f"Sandbox call failed for {call_id}: {e}")

        asyncio.create_task(_do_dial())

        return {"status": "dialing", "call_id": call_id, "agent_id": agent_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start call for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to start call")
