"""Per-role campaign Cases.

The operator creates one or more named "Cases" (e.g. "April Steel Sheets
Push", "Diwali Discount Drive") for each role and activates **at most one**
per role. The active case description is injected into the AI's system
prompt (see ``services/vobiz_bridge.py``) so today's campaign instructions
take effect on the next call without editing the base persona prompt.

Endpoints
---------
- ``GET    /api/cases?role=<role>``                — list cases (active first)
- ``POST   /api/cases?role=<role>``                — create a new case
- ``PATCH  /api/cases/{case_id}``                  — rename / edit description
- ``DELETE /api/cases/{case_id}``                  — remove a case
- ``POST   /api/cases/{case_id}/activate?role=...``— mark this case active
                                                     (auto-deactivates the
                                                     previous one)
- ``POST   /api/cases/deactivate?role=<role>``     — turn all cases off

All endpoints respect ``normalize_console_role`` so a stale or invalid role
string never lands in the database.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel
from typing import Optional

from core.state import normalize_console_role
from core.storage import (
    add_case,
    delete_case,
    get_active_case,
    list_cases,
    set_active_case,
    update_case,
)


router = APIRouter(prefix="/api/cases", tags=["cases"])


def _role(request: Request, fallback: Optional[str] = None) -> str:
    raw = (
        request.query_params.get("role")
        or fallback
        or request.headers.get("X-User-Role")
        or "data_edge"
    )
    return normalize_console_role(raw)


class CaseCreate(BaseModel):
    name: str
    description: str = ""


class CaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@router.get("")
async def list_role_cases(request: Request):
    role = _role(request)
    cases = await list_cases(role)
    active = next((c for c in cases if c.get("active")), None)
    return {
        "role": role,
        "active_case_id": (active or {}).get("id"),
        "cases": cases,
    }


@router.post("")
async def create_role_case(payload: CaseCreate, request: Request):
    role = _role(request)
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Case name is required.")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="Case name is too long (max 120 chars).")
    description = (payload.description or "").strip()
    case_id = await add_case(role, name, description)
    logger.info(f"Created case {case_id} for role={role!r} name={name!r}")
    return {"status": "ok", "id": case_id, "role": role}


@router.patch("/{case_id}")
async def update_role_case(case_id: int, payload: CaseUpdate):
    name = payload.name.strip() if isinstance(payload.name, str) else None
    description = payload.description if isinstance(payload.description, str) else None
    if name is not None and not name:
        raise HTTPException(status_code=400, detail="Case name cannot be empty.")
    if name is None and description is None:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    ok = await update_case(case_id, name=name, description=description)
    if not ok:
        raise HTTPException(status_code=404, detail="Case not found.")
    return {"status": "ok", "id": case_id}


@router.delete("/{case_id}")
async def remove_role_case(case_id: int):
    ok = await delete_case(case_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Case not found.")
    return {"status": "ok", "id": case_id}


@router.post("/{case_id}/activate")
async def activate_role_case(case_id: int, request: Request):
    role = _role(request)
    ok = await set_active_case(role, case_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Case not found for this role.")
    active = await get_active_case(role)
    logger.info(f"Activated case {case_id} for role={role!r}")
    return {"status": "ok", "role": role, "active_case_id": case_id, "active_case": active}


@router.post("/deactivate")
async def deactivate_all_for_role(request: Request):
    role = _role(request)
    await set_active_case(role, None)
    logger.info(f"Deactivated all cases for role={role!r}")
    return {"status": "ok", "role": role, "active_case_id": None}
