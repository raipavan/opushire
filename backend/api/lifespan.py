"""Application lifespan (DB init, shutdown)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from config import FRONTEND_DIR, settings
from core.state import _CAMPAIGN_TASKS, init_state
from core.storage import (
    close_db,
    init_db,
    roles_with_campaign_run_wanted,
    set_campaign_want_running,
)
from services.vobiz_bridge import close_vobiz_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    logger.info("Starting bridge server…")
    data_root = (os.environ.get("VERN_DATA_DIR") or "").strip()
    if data_root:
        data_dir = os.path.abspath(data_root)
    else:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    init_db(data_dir)
    init_state()

    # Per-role sandbox: refresh packaged prompt/RAG and coerce cross-role greetings.
    try:
        from core.role_sandbox import sync_all_role_sandboxes_on_startup

        sync_all_role_sandboxes_on_startup()
    except Exception as exc:
        logger.warning("Role sandbox startup sync skipped: {}", exc)

    # ── Startup Diagnostics ──────────────────────────────────────────────
    _diag_errors: list[str] = []
    _diag_warnings: list[str] = []

    if not settings.gemini_api_key:
        _diag_errors.append("GEMINI_API_KEY not set — Gemini Live/TTS will fail")
    elif not settings.gemini_api_key.startswith("AIza") and not settings.gemini_api_key.startswith("AQ."):
        _diag_warnings.append(
            f"GEMINI_API_KEY looks unusual (starts with {settings.gemini_api_key[:6]}…) "
            "— ensure it is a valid Google AI Studio key"
        )
    elif settings.gemini_api_key.startswith("AQ."):
        logger.info("GEMINI_API_KEY uses Google Auth Key format (AQ.) — standard query parameter auth")


    if not settings.vobiz_public_base_url:
        _diag_errors.append("VOBIZ_PUBLIC_BASE_URL not set — Vobiz cannot reach this host")
    else:
        _stream_url = (settings.vobiz_stream_public_base_url or "").strip()
        if not _stream_url:
            _diag_warnings.append(
                "VOBIZ_STREAM_PUBLIC_BASE_URL is empty — media WebSocket will use VOBIZ_PUBLIC_BASE_URL "
                f"({settings.vobiz_public_base_url}). If your domain does NOT support WebSocket upgrades, "
                "calls will connect but produce SILENCE. Set VOBIZ_STREAM_PUBLIC_BASE_URL to "
                "http://YOUR_VPS_IP:8001 for direct WS access."
            )
        if "trycloudflare.com" in settings.vobiz_public_base_url or "trycloudflare.dev" in settings.vobiz_public_base_url:
            _diag_warnings.append(
                "VOBIZ_PUBLIC_BASE_URL uses Cloudflare quick tunnel — media WebSockets often fail. "
                "Set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS direct IP."
            )

    # Check greeting PCM files
    from pathlib import Path
    _greetings_dir = Path(__file__).resolve().parent.parent / "data" / "greetings"
    for _role_name in ("data_edge",):
        _pcm = _greetings_dir / f"greeting_{_role_name}.pcm"
        _meta = _greetings_dir / f"greeting_{_role_name}.pcm.meta"
        if _pcm.is_file() and _pcm.stat().st_size > 0:
            logger.info("Greeting PCM OK: role={} size={} bytes", _role_name, _pcm.stat().st_size)
        else:
            _diag_warnings.append(f"No greeting PCM for role={_role_name} — opening may be delayed")

    # Check Vobiz credentials
    if settings.vobiz_data_edge_auth_id and settings.vobiz_data_edge_auth_token:
        logger.info("Vobiz Data Edge credentials: OK (auth_id={})", settings.vobiz_data_edge_auth_id)
    elif settings.vobiz_auth_id and settings.vobiz_auth_token:
        logger.info("Vobiz global credentials: OK (auth_id={}) — no role-specific credentials", settings.vobiz_auth_id)
    else:
        _diag_errors.append("No Vobiz credentials configured (neither global nor data_edge)")

    # Log Gemini Live model config
    logger.info(
        "Gemini Live config: model={} voice={} lang={} first_opening={} aggressive_vad={}",
        settings.gemini_live_model,
        settings.gemini_live_voice,
        settings.gemini_live_language_code,
        settings.gemini_live_first_opening,
        settings.gemini_live_aggressive_activity_detection,
    )

    for w in _diag_warnings:
        logger.warning("DIAG: {}", w)
    for e in _diag_errors:
        logger.error("DIAG: {}", e)
    if not _diag_errors and not _diag_warnings:
        logger.info("Startup diagnostics: ALL OK")
    elif not _diag_errors:
        logger.info("Startup diagnostics: {} warnings, 0 errors", len(_diag_warnings))
    else:
        logger.error("Startup diagnostics: {} errors, {} warnings — calls may fail", len(_diag_errors), len(_diag_warnings))

    fe_index = FRONTEND_DIR / "index.html"
    if fe_index.is_file():
        logger.info(
            "Operator UI: http://127.0.0.1:{}/  (file {})",
            settings.port,
            fe_index,
        )
    else:
        logger.error(
            "Frontend index missing at {} — GET / will show a stub. "
            "Keep a sibling ``frontend/`` next to ``backend/`` (or rebuild Docker).",
            fe_index,
        )
    logger.info("OpenAPI / Swagger: http://127.0.0.1:{}/docs", settings.port)

    # ── Startup: clean up orphaned manual call slots from previous crash ──
    # After a server restart, in-memory slot counters reset to 0, but SQLite
    # may still have manual_calls rows stuck in 'in_progress' or 'dialing'.
    # Mark them as failed so the UI doesn't show stale state.
    try:
        from core.storage import _get_conn, _commit_with_retry
        conn = _get_conn()
        stale = conn.execute(
            "SELECT camp_id FROM manual_calls WHERE status IN ('in_progress', 'dialing')"
        ).fetchall()
        if stale:
            conn.execute(
                "UPDATE manual_calls SET status = 'failed', error = 'Server restarted — orphaned call cleaned up', updated_at = datetime('now') WHERE status IN ('in_progress', 'dialing')"
            )
            _commit_with_retry(conn)
            logger.info("Startup: cleaned up {} orphaned manual call(s) from previous session", len(stale))
    except Exception as exc:
        logger.warning("Startup orphaned call cleanup skipped: {}", exc)

    try:
        from core.state import get_lead_counts as _gcd
        from core.worker import (
            _campaign_worker_role,
            _schedule_preflight,
            release_orphaned_dialing_leads,
        )

        from core.storage import is_campaign_globally_paused

        if await is_campaign_globally_paused():
            logger.info("Global campaign pause is active — outbound dialers will not auto-resume.")
            resume_roles = []
        else:
            resume_roles = await roles_with_campaign_run_wanted()
        for r_role in resume_roles:
            ct = _gcd(r_role)
            if int(ct.get("pending", 0) or 0) <= 0 and int(ct.get("dialing", 0) or 0) <= 0:
                await set_campaign_want_running(r_role, False)
                continue
            why = await _schedule_preflight(r_role)
            if why:
                logger.warning("Campaign runner resume deferred role={}: {}", r_role, why)
                try:
                    from core.campaign_hours import is_campaign_quiet_hours

                    if is_campaign_quiet_hours():
                        await set_campaign_want_running(r_role, False)
                        await release_orphaned_dialing_leads(
                            r_role,
                            error="Campaign stopped: outside calling hours (9:30 AM – 8:30 PM IST).",
                        )
                except Exception:
                    pass
                continue
            existing = _CAMPAIGN_TASKS.get(r_role)
            if existing and not existing.done():
                continue
            _CAMPAIGN_TASKS[r_role] = asyncio.create_task(_campaign_worker_role(r_role))
            logger.info(
                "Resumed outbound dialer role={} (operator had Start before last restart)",
                r_role,
            )
    except Exception as exc:
        logger.warning("Campaign runner auto-resume skipped: {}", exc)

    scheduler_task = None
    try:
        from core.worker import _scheduler_loop
        scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Campaign scheduler loop started.")
    except Exception as exc:
        logger.warning("Campaign scheduler loop failed to start: {}", exc)

    sip_bridge = None
    if os.getenv("SIP_BRIDGE_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        try:
            from services.sip_bridge.server import SIPBridgeServer
            sip_bridge = SIPBridgeServer()
            await sip_bridge.start()
        except Exception as exc:
            logger.warning("SIP Bridge failed to start: {}", exc)

    logger.info("Bridge ready on {}:{}", settings.host, settings.port)
    yield

    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Campaign scheduler loop stopped.")

    for role, task in list(_CAMPAIGN_TASKS.items()):
        if task and not task.done():
            task.cancel()
            logger.info("Cancelled task for {}", role)
    if sip_bridge:
        await sip_bridge.stop()
    await close_vobiz_client()
    close_db()
    logger.info("Shutdown complete")
