"""Per-role campaign Schedules.

The operator uploads leads, then schedules the campaign to fire automatically
at a future date / time. A small background loop (see ``core.worker``) polls
the ``schedules`` table every 30 s and, when a row's ``run_at`` is reached and
its ``status`` is still ``scheduled``, kicks off the same campaign worker the
**Start Campaign** button does.

Endpoints
---------
- ``GET    /api/schedules?role=<role>``   — list scheduled / running / past runs
- ``POST   /api/schedules?role=<role>``   — create a schedule
                                             body: ``{run_at_iso, run_at?, name?}``
                                             ``run_at_iso`` is ISO-8601 (any
                                             timezone offset). ``run_at`` is
                                             epoch-seconds (UTC) — useful for
                                             scripts. One of the two is required.
- ``DELETE /api/schedules/{schedule_id}`` — cancel a not-yet-fired schedule

All endpoints respect ``normalize_console_role`` so a stale or invalid role
string never lands in the database.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from core.state import normalize_console_role
from core.storage import (
    add_schedule,
    cancel_schedule,
    get_schedule,
    list_schedules,
)


router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _role(request: Request, fallback: Optional[str] = None) -> str:
    from core.auth import console_role_from_request

    return console_role_from_request(request, default=fallback or "data_edge")


class ScheduleCreate(BaseModel):
    run_at_iso: Optional[str] = None
    run_at: Optional[float] = None
    stop_at_iso: Optional[str] = None
    stop_at: Optional[float] = None
    name: Optional[str] = ""


def _parse_iso_or_epoch(
    iso_value: Optional[str],
    epoch_value: Optional[float],
    field: str,
) -> Optional[float]:
    """Resolve an ISO-8601 string OR epoch number to epoch-seconds (UTC).

    Returns ``None`` if both inputs are empty (caller decides if that's OK).
    Raises HTTPException(400) for malformed input.
    """
    if epoch_value is not None:
        try:
            return float(epoch_value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be a number (epoch seconds)",
            )
    iso = (iso_value or "").strip()
    if not iso:
        return None
    # ``datetime.fromisoformat`` accepts ``2026-05-07T13:00:00+05:30`` and
    # naive ``2026-05-07T07:30:00``. It does NOT accept the trailing ``Z``
    # suffix that browsers sometimes emit, so we normalise that first.
    if iso.endswith("Z") or iso.endswith("z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {e}")
    if dt.tzinfo is None:
        # Treat naive as UTC. The browser always sends a tz so this branch
        # only fires for raw API callers / curl.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@router.get("")
async def list_role_schedules(request: Request):
    role = _role(request)
    items = await list_schedules(role, limit=100)
    return {"role": role, "schedules": items, "now": time.time()}


@router.post("")
async def create_role_schedule(payload: ScheduleCreate, request: Request):
    role = _role(request)
    run_at = _parse_iso_or_epoch(payload.run_at_iso, payload.run_at, "run_at")
    if run_at is None:
        raise HTTPException(status_code=400, detail="Provide run_at or run_at_iso.")
    stop_at = _parse_iso_or_epoch(payload.stop_at_iso, payload.stop_at, "stop_at")

    now = time.time()
    # Allow a small clock-skew window (15s) so a "schedule for now" still works.
    if run_at < now - 15:
        raise HTTPException(
            status_code=400,
            detail="Scheduled start time is in the past. Pick a future date and time.",
        )
    if stop_at is not None:
        if stop_at <= run_at:
            raise HTTPException(
                status_code=400,
                detail="Stop time must be after the start time.",
            )
        if stop_at < now - 15:
            raise HTTPException(
                status_code=400,
                detail="Stop time is in the past. Pick a future date and time.",
            )

    name = ((payload.name or "").strip())[:120]
    schedule_id = await add_schedule(role, run_at, name=name, stop_at=stop_at)
    window_log = (
        f"window={max(0, int(stop_at - run_at))}s" if stop_at else "window=open-ended"
    )
    logger.info(
        f"Scheduled campaign for role={role!r} at run_at={run_at:.0f} "
        f"(in {max(0, int(run_at - now))}s) {window_log} id={schedule_id} name={name!r}"
    )
    sched = await get_schedule(schedule_id)
    return {"status": "ok", "id": schedule_id, "schedule": sched}


@router.delete("/{schedule_id}")
async def remove_role_schedule(schedule_id: int):
    ok = await cancel_schedule(schedule_id)
    if not ok:
        # Either it never existed or it already fired/finished — give the UI a
        # precise error so it can refresh and stop showing a Cancel button.
        sched = await get_schedule(schedule_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="Schedule not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel — schedule is {sched.get('status')}.",
        )
    logger.info(f"Cancelled schedule id={schedule_id}")
    return {"status": "ok", "id": schedule_id}
