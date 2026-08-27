"""Runtime state management — uses SQLite for persistence, in-memory for active tracking."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from loguru import logger

# In-memory active tracking (not persisted)
_ACTIVE_VOBIZ_CALLS: int = 0
_ACTIVE_VOBIZ_CALLS_BY_ROLE: dict[str, int] = {}
_CAMPAIGN_DATA: dict[str, dict[str, Any]] = {}
_CAMPAIGN_TASKS: dict[str, Any] = {}
_OPENING_PCM_CACHE: dict[str, tuple[bytes, int]] = {}

_ROLES = (
    "data_edge",
)


from typing import Optional

def normalize_console_role(role: Optional[str]) -> str:
    """Ensure the role is valid, defaulting to 'data_edge'."""
    r = (role or "data_edge").lower().strip()
    return r if r in _ROLES else "data_edge"


def active_vobiz_calls_for_role(role: str) -> int:
    """Outbound/live legs currently active for one console role."""
    return int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(normalize_console_role(role), 0))


def role_has_active_vobiz_call(role: str) -> bool:
    """True when this role already has one active outbound leg (each role dials one-at-a-time)."""
    r = normalize_console_role(role)
    count = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
    logger.debug("role_has_active_vobiz_call role={} count={}", r, count)
    return count >= 1


def total_active_vobiz_calls() -> int:
    """Total live outbound legs across all roles (for dashboards)."""
    return int(sum(_ACTIVE_VOBIZ_CALLS_BY_ROLE.values()))


def acquire_vobiz_call_slot(role: str) -> None:
    """Reserve one telephony slot for ``role``; updates global active count for dashboards."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    prev = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
    _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = prev + 1
    _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()
    logger.debug("acquire_vobiz_call_slot role={} prev={} now={} global={}", r, prev, prev + 1, _ACTIVE_VOBIZ_CALLS)


def release_vobiz_call_slot(role: str) -> None:
    """Release a telephony slot for ``role``."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    cur = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
    if cur > 0:
        nxt = cur - 1
        if nxt <= 0:
            _ACTIVE_VOBIZ_CALLS_BY_ROLE.pop(r, None)
        else:
            _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = nxt
        _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()
        logger.debug("release_vobiz_call_slot role={} prev={} now={} global={}", r, cur, nxt, _ACTIVE_VOBIZ_CALLS)
    else:
        logger.debug("release_vobiz_call_slot role={} prev=0 (already released, no-op)", r)


def try_recover_stale_vobiz_slot(role: str) -> bool:
    """Detect and release a stale telephony slot for ``role``.

    A slot is considered stale when:
      - The in-memory counter says a call is active for this role, BUT
      - No entry in ``_CAMPAIGN_DATA`` for this role has ``_call_connected_at`` set
        (meaning the WebSocket never actually connected).

    Returns True if a stale slot was recovered, False otherwise.
    """
    import time
    r = normalize_console_role(role)
    cur = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
    if cur <= 0:
        return False

    # Scan _CAMPAIGN_DATA for entries belonging to this role
    now = time.time()
    stale_camp_id = None
    found_any_for_role = False
    for cid, cdata in list(_CAMPAIGN_DATA.items()):
        if not isinstance(cdata, dict):
            continue
        if cdata.get("_role") != r:
            continue
        found_any_for_role = True
        connected = cdata.get("_call_connected_at")
        ended = cdata.get("_call_ended_at")
        if connected is not None and ended is None:
            # This campaign has a live WS — check if it's been connected too long (stale)
            age_since_connect = now - connected if isinstance(connected, (int, float)) else 0
            if age_since_connect > 120:  # 2 minutes — call should have ended
                logger.warning(
                    "try_recover_stale: camp_id={} connected {:.0f}s ago (>120s) — treating as stale",
                    cid, age_since_connect,
                )
                stale_camp_id = cid
                break
            logger.debug("try_recover_stale: camp_id={} has _call_connected_at — slot valid", cid)
            return False
        if connected is not None and ended is not None:
            # Call already ended but _call_connected_at wasn't cleaned up — treat as stale
            logger.debug("try_recover_stale: camp_id={} has _call_connected_at but also _call_ended_at — stale", cid)
            stale_camp_id = cid
            break

        # No connection — check if the entry has been around long enough to be stale
        # CRITICAL FIX: Also handle campaign calls (not just manual legs).
        # A campaign call with no _call_connected_at after 60s is definitely stuck.
        started = cdata.get("_inserted_at") or cdata.get("started_at") or 0
        age_sec = now - started if isinstance(started, (int, float)) else 999
        if age_sec >= 60:
            logger.warning(
                "try_recover_stale: camp_id={} age={:.0f}s, connected={} ended={} — "
                "treating as stale (no WS connected after >=60s)",
                cid, age_sec, connected, ended,
            )
            stale_camp_id = cid
            break

    if stale_camp_id:
        logger.warning(
            "Recovered stale vobiz slot for role={} camp_id={} (no WS connected after >=60s) — releasing",
            r, stale_camp_id,
        )
        release_vobiz_call_slot(r)
        _CAMPAIGN_DATA.pop(stale_camp_id, None)
        return True

    # Fallback: counter says active but no campaign data found at all for this role — slot is definitely stale
    if not found_any_for_role:
        logger.warning(
            "try_recover_stale: role={} counter={} but no campaign data found — releasing stale slot",
            r, cur,
        )
        release_vobiz_call_slot(r)
        return True

    return False


def parse_manual_camp_role_suffix(suffix: str) -> tuple[str, str]:
    """Parse ``role`` and optional per-attempt token from camp_id after the ``manual_`` prefix.

    Formats:
      - ``{role}`` — legacy single shared manual leg id
      - ``{role}_{token}`` — unique id per manual dial (``token`` may contain underscores)
    """
    suf = (suffix or "").strip()
    if not suf:
        return "data_edge", ""
    for r in sorted(_ROLES, key=len, reverse=True):
        if suf == r:
            return r, ""
        # ``manual_{role}_{uuid}`` (current) and legacy ``manual_{role}-{token}`` both map to ``role``.
        for sep in ("_", "-"):
            prefix = r + sep
            if suf.startswith(prefix):
                return r, suf[len(prefix) :]
    return normalize_console_role(suf), ""


def resolved_greeting_text(role: str) -> str:
    """Gre stored in SQLite (coerced); if missing or invalidated, packaged role opener."""
    from core.greeting_text_utils import coerce_stored_greeting

    state = get_state(role)
    raw = state.get("greeting_text") or ""
    text = coerce_stored_greeting(role, raw).strip()
    if text:
        return text
    from core.opening_line import packaged_fallback_greeting

    return packaged_fallback_greeting(role)


def init_state():
    """Initialize campaign tasks for all roles."""
    for role in _ROLES:
        _CAMPAIGN_TASKS[role] = None

def get_state(role: str) -> dict:
    """Get in-memory state for a role (prompt, rag, vobiz config, etc.)."""
    try:
        from core.storage import _get_role_state_sync
        return _get_role_state_sync(role or "data_edge")
    except Exception as e:
        logger.warning(f"Storage not available, using fallback: {e}")
        from core.storage import default_inter_call_gap_sec

        r = (role or "data_edge").strip().lower()
        return {
            "role": r,
            "prompt": "",
            "rag": "",
            "delay_sec": default_inter_call_gap_sec(r),
            "vobiz": {},
        }

def save_role_state(
    role: str,
    prompt: str = None,
    rag: str = None,
    vobiz_config: dict = None,
    delay_sec: float = None,
    greeting_text: str = None,
):
    """Persist role state to SQLite."""
    try:
        from core.storage import _save_role_state_sync

        _save_role_state_sync(
            role,
            prompt=prompt,
            rag=rag,
            vobiz_config=vobiz_config,
            delay_sec=delay_sec,
            greeting_text=greeting_text,
        )
    except Exception as e:
        logger.error(f"Failed to save state for {role}: {e}")

def get_leads(role: str, status: str = None, limit: int = 500) -> list[dict]:
    try:
        from core.storage import _get_leads_sync
        return _get_leads_sync(role, status=status, limit=limit)
    except Exception:
        logger.exception("get_leads failed for role={!r}", role)
        return []

def add_leads_bulk(role: str, leads: list[dict]) -> int:
    from core.storage import _bulk_add_leads_sync

    return _bulk_add_leads_sync(role, leads)

def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None, duration_sec: float = None):
    try:
        from core.storage import _update_lead_status_sync
        _update_lead_status_sync(lead_id, status, error=error, analysis=analysis, duration_sec=duration_sec)
    except Exception as e:
        logger.error(f"Failed to update lead status: {e}")

def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None):
    try:
        from core.storage import _update_lead_call_info_sync
        _update_lead_call_info_sync(lead_id, log_id=log_id, call_id=call_id, start_time=start_time)
    except Exception as e:
        logger.error(f"Failed to update lead call info: {e}")

def reset_leads(role: str):
    try:
        from core.storage import _reset_leads_sync
        _reset_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to reset leads: {e}")

def wipe_leads(role: str):
    try:
        from core.storage import _wipe_leads_sync
        _wipe_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to wipe leads: {e}")

def get_lead_counts(role: str) -> dict:
    try:
        from core.storage import _get_lead_counts_sync
        return _get_lead_counts_sync(role)
    except Exception:
        logger.exception("get_lead_counts failed for role={!r}", role)
        return {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0}

def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    try:
        from core.storage import _export_leads_csv_sync
        return _export_leads_csv_sync(role, status_filter)
    except Exception:
        return []

from pathlib import Path

def _get_role_path(role: str, subpath: str = None) -> Path:
    from config import settings
    # Assuming standard data directory layout
    base_dir = Path("data") / role
    if subpath:
        base_dir = base_dir / subpath
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir
