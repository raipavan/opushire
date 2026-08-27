"""Campaign worker — dials leads one-at-a-time per role; roles run in parallel."""

from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from loguru import logger
from core.state import (
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    acquire_vobiz_call_slot,
    release_vobiz_call_slot,
    role_has_active_vobiz_call,
    active_vobiz_calls_for_role,
    total_active_vobiz_calls,
    get_state,
    normalize_console_role,
)
from core.storage import (
    due_schedules,
    expired_running_schedules,
    mark_schedule_status,
    get_pending_callbacks,
    mark_callback_processed,
    promote_due_scheduled_callbacks,
    role_has_future_callback_scheduled,
    get_leads,
    update_lead_status,
    update_lead_call_info,
    save_role_state,
    reset_leads,
    wipe_leads,
    get_lead_counts,
    export_leads_csv,
    set_campaign_want_running,
)
from core.state import add_leads_bulk
from core.utils import _prewarm_opening, _build_opening_line
from core.greeting_pcm import load_recorded_greeting_pcm
from config import settings
from core.campaign_hours import is_campaign_quiet_hours, quiet_hours_block_message
from core.vobiz_credentials import resolve_vobiz_credentials

_background_tasks: set[asyncio.Task] = set()

# Once a lead is in ``dialing`` longer than this (process restart or hung WS), recycle it.
_STALE_DIALING_AFTER_SEC = 600
# Wait time when the queue becomes empty before exiting (gives the operator a chance to upload mid-run).
_EMPTY_QUEUE_GRACE_SEC = 30
# Fallback gap (seconds) between consecutive outbound calls if role_state.delay_sec is missing/invalid.
# Note: default gap is now random 120-180s in inter_call_gap_seconds_for_role();
# these are only used when an explicit delay_sec is set in role state.
_ENV_INTER_CALL_GAP_SEC = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_SEC", "5"))
_INTER_CALL_GAP_MIN = 0.0
_INTER_CALL_GAP_MAX = 1200.0  # 20 min cap


def inter_call_gap_seconds_for_role(role: str) -> float:
    """Pause after each dial before the next pending lead. Applies random ±20% jitter to configured gap."""
    import random as _random
    from core.state import get_state
    
    state = get_state(role)
    try:
        base_gap = float(state.get("delay_sec") if state.get("delay_sec") is not None else state.get("inter_call_gap_sec", 120))
    except (TypeError, ValueError):
        base_gap = 120.0

    from core.storage import is_strict_gap_core_role, STRICT_CORE_GAP_SEC
    if is_strict_gap_core_role(role):
        base_gap = max(base_gap, float(STRICT_CORE_GAP_SEC))

    jitter = base_gap * 0.20
    return _random.uniform(max(0, base_gap - jitter), base_gap + jitter)
# Callback processing interval: every 30 minutes (in seconds)
_CALLBACK_BATCH_INTERVAL_SEC = float(os.getenv("CALLBACK_BATCH_INTERVAL_SEC", "1800"))


async def _cancellable_sleep(role: str, total_seconds: float) -> bool:
    """Sleep in 0.5s slices but bail out as soon as the campaign is stopped.

    Returns True if the wait completed normally, False if the campaign was cancelled.
    """
    end = time.time() + max(0.0, total_seconds)
    while time.time() < end:
        if not _CAMPAIGN_TASKS.get(role):
            return False
        await asyncio.sleep(min(0.5, end - time.time()))
    return True


async def release_orphaned_dialing_leads(
    role: str,
    *,
    to_status: str = "failed",
    error: str = "Campaign stopped before call completed.",
) -> int:
    """Mark in-flight ``dialing`` rows terminal when the worker is not running (stop / quiet hours).

    Skips leads already marked ``in_progress`` — those have an active WebSocket and should
    finish naturally via the live_session finally block.
    """
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to release orphaned dialing leads role={}", role)
        return 0
    released = 0
    for r in rows:
        try:
            await update_lead_status(int(r["id"]), to_status, error=error)
            released += 1
        except Exception:
            logger.exception("release dialing lead id={}", r.get("id"))
    # Also check in_progress leads — if the WS is truly gone, they should be recovered
    try:
        in_prog = await get_leads(role, status="in_progress", limit=10000)
        for r in in_prog:
            try:
                # Only mark failed if there's no active WebSocket for this call
                call_id = r.get("_call_id") or ""
                from core.state import _CAMPAIGN_DATA
                mem = _CAMPAIGN_DATA.get(call_id, {})
                if not mem.get("_call_connected_at") or mem.get("_call_ended_at"):
                    await update_lead_status(int(r["id"]), to_status, error=error)
                    released += 1
            except Exception:
                logger.exception("release in_progress lead id={}", r.get("id"))
    except Exception:
        pass
    if released:
        logger.info(
            "Released {} orphaned lead(s) → {} for role={}",
            released,
            to_status,
            role,
        )
    return released


async def _recover_stale_dialing(role: str) -> int:
    """Worker startup: previous process may have crashed with leads stuck on ``dialing``.

    Reset them to ``pending`` so this run can pick them up. Returns count recovered.
    """
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to recover stale dialing leads")
        return 0
    now = time.time()
    recovered = 0
    for r in rows:
        st = r.get("start_time") or 0
        if not st or (now - float(st)) > _STALE_DIALING_AFTER_SEC:
            await update_lead_status(r["id"], "pending")
            recovered += 1
    if recovered:
        logger.info(f"Recovered {recovered} stale 'dialing' leads → 'pending' for role={role}")
    return recovered


def _prime_opening_audio(call_id: str, role: str, opening: str) -> None:
    """If ``data/greetings/greeting_{role}.pcm`` exists, load it synchronously before dial so
    playback is ready the instant the WebSocket opens. Otherwise schedule TTS prewarm."""
    if settings.gemini_live_first_opening:
        logger.debug(
            "Skip opening PCM prime for call_id={} — Gemini Live speaks first (GEMINI_LIVE_FIRST_OPENING)",
            call_id,
        )
        return
    if call_id not in _CAMPAIGN_DATA:
        return
    recorded = load_recorded_greeting_pcm(role, greeting_text=(opening or "").strip())
    if recorded:
        _CAMPAIGN_DATA[call_id]["opening_pcm"] = recorded
        logger.info(
            "Primed recorded greeting for call_id={} role={} ({} bytes @ {} Hz)",
            call_id,
            role,
            len(recorded[0]),
            recorded[1],
        )
        return
    task = asyncio.create_task(_prewarm_opening(call_id, opening, settings.gemini_live_voice))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _process_callback_batch(role: str) -> int:
    """Process pending callbacks: call them back and mark as processed.

    Returns number of callbacks processed.
    """
    from services.vobiz_bridge import make_vobiz_call, VobizCallError

    state = get_state(role)
    v_cfg = state.get("vobiz", {})
    v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)

    if not v_auth_id or not v_token or not v_base or not v_from:
        logger.warning(f"Cannot process callbacks for {role}: telephony not configured")
        return 0

    callbacks = await get_pending_callbacks(role, limit=50)
    if not callbacks:
        return 0

    logger.info(f"Processing {len(callbacks)} callbacks for {role}")
    processed = 0

    for cb in callbacks:
        cb_id = cb.get("id")
        cb_phone = cb.get("from_phone")
        if not cb_phone or cb_phone == "unknown":
            await mark_callback_processed(cb_id, role)
            continue

        # Mark as calling so we don't retry on next batch if this takes time
        from core.storage import mark_callback_calling
        await mark_callback_calling(cb_id, role)

        call_id = f"callback_{role}_{cb_id}_{uuid.uuid4().hex[:8]}"
        _CAMPAIGN_DATA[call_id] = {
            "name": cb.get("matched_name") or "Callback",
            "phone": cb_phone,
            "company": cb.get("matched_company") or "",
            "_role": role,
            "_callback_id": cb_id,
            "_is_callback": True,
        }

        try:
            # Pre-warm opening
            opening = _build_opening_line(_CAMPAIGN_DATA[call_id], role)
            _prime_opening_audio(call_id, role, opening)

            acquire_vobiz_call_slot(role)
            logger.info(
                f"Callback call initiated: {cb_phone} [role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
            )

            _cb_vobiz_resp = await make_vobiz_call(
                to=cb_phone,
                from_=v_from,
                answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}",
                auth_id=v_auth_id,
                auth_token=v_token,
                extra={
                    "ring_url": f"{v_base}/vobiz/ring?camp_id={call_id}",
                    "ring_method": "POST",
                    "hangup_url": f"{v_base}/vobiz/hangup?camp_id={call_id}",
                    "hangup_method": "POST",
                    "hangup_on_ring": "3600",
                },
            )
            _cb_uuid = _cb_vobiz_resp.get("request_uuid") or ""
            if _cb_uuid:
                _CAMPAIGN_DATA[call_id]["_vobiz_call_uuid"] = _cb_uuid

            # Wait for call to complete (similar to normal outbound)
            answered = False
            call_started_at = time.time()
            MAX_RING_WAIT = 60
            MAX_TOTAL_WAIT = 360

            while True:
                if not _CAMPAIGN_TASKS.get(role):
                    break

                info = _CAMPAIGN_DATA.get(call_id, {})
                if not answered and info.get("_call_connected_at"):
                    answered = True
                if answered and info.get("_call_ended_at"):
                    break

                elapsed = time.time() - call_started_at
                if not answered and elapsed >= MAX_RING_WAIT:
                    break
                if elapsed >= MAX_TOTAL_WAIT:
                    break

                await asyncio.sleep(2)

        except Exception:
            logger.exception(f"Callback call failed for {cb_phone}")
        finally:
            _CAMPAIGN_DATA.pop(call_id, None)
            release_vobiz_call_slot(role)
            await mark_callback_processed(cb_id, role)
            processed += 1

            if not _CAMPAIGN_TASKS.get(role):
                break

            # Human-like random gap between callbacks (2–3 minutes)
            import random as _random
            gap = _random.uniform(120, 180)
            logger.info(f"Callback gap: waiting {gap:.0f}s ({gap/60:.1f} min)")
            await _cancellable_sleep(role, gap)

    logger.info(f"Callback batch for {role} finished.")
    return processed


def _parse_log_id_date(log_id: str) -> str | None:
    """Extract YYYY-MM-DD from log_id patterns like camp-xxx-20260513T07291 or vobiz-live-20260518T161022-xxx."""
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", log_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _read_transcript_jsonl(role: str, log_id: str) -> str:
    """Locate the JSONL transcript for a log_id and return its raw text.

    Scans the per-role ``data/<role>/logs/`` tree in both current and legacy
    systems. Parses the date from the log_id for exact-day lookup, then falls
    back to recent days. Returns empty string if nothing is found.
    """
    from datetime import datetime, timedelta, timezone

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate_dirs: list[str] = []

    def _add_log_dir(base: str, day: str) -> None:
        # Check the explicitly requested role
        dirs_to_check = [os.path.join(base, role, "logs", day), os.path.join(base, "logs", day)]
        # Also check all other subdirectories in base (which correspond to roles)
        if os.path.isdir(base):
            for d in os.listdir(base):
                sub = os.path.join(base, d, "logs", day)
                if sub not in dirs_to_check:
                    dirs_to_check.append(sub)
                    
        for sub in dirs_to_check:
            if sub not in candidate_dirs:
                candidate_dirs.append(sub)

    # Date-prefixed lookup: extract date from log_id like camp-xxx-20260513T07291
    date_hint = _parse_log_id_date(log_id)
    if date_hint:
        _add_log_dir(os.path.join(backend_dir, "data"), date_hint)
        for legacy_base in (
            "/root/vernika/backend/data",
            "/root/vernika/agent/data",
            "/root/DataEdge/backend/data",
        ):
            _add_log_dir(legacy_base, date_hint)

    # Fallback: scan recent days across all known log trees (60d for older campaigns)
    today = datetime.now(timezone.utc).date()
    for delta in range(0, 60):
        d = (today - timedelta(days=delta)).isoformat()
        _add_log_dir(os.path.join(backend_dir, "data"), d)
        for legacy_base in (
            "/root/vernika/backend/data",
            "/root/vernika/agent/data",
        ):
            _add_log_dir(legacy_base, d)

    for d in candidate_dirs:
        for ext in ("jsonl", "txt"):
            p = os.path.join(d, f"{log_id}.{ext}")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    continue
    return ""


def _disposition_to_status(disposition: str) -> str:
    """Map analyzer disposition → lead status the dashboard expects.

    Dispositions are normalised via ``canonical_disposition`` so punctuation,
    synonyms, and minor model rephrasings map deterministically.
    """
    from services.call_analyzer import canonical_disposition

    canon = canonical_disposition(disposition)
    if canon == "Interested":
        return "completed"
    if canon == "Not Interested":
        return "not_interested"
    if canon == "Wrong Number":
        return "failed"
    # Call Later, Busy, Answered, anything unknown → successful connection bucket
    return "completed"


async def _analyze_and_update_lead(role: str, lead_id: int, log_id: str, duration_sec: float | None = None):
    """Read the call's JSONL transcript, analyze it, and finalize the lead status.

    Writes terminal statuses — including ``callback_scheduled`` when the callee asks
    to be recalled at a specific future time parsed from QA (campaign promotes to
    ``pending`` when that moment passes).
    """
    if not log_id:
        logger.warning(f"Analyze: no log_id for lead {lead_id}; marking completed.")
        await update_lead_status(lead_id, "completed", duration_sec=duration_sec)
        return

    transcript = _read_transcript_jsonl(role, log_id)
    if not transcript.strip():
        logger.info(f"No transcript for lead {lead_id} (log_id={log_id}) — attempting Whisper transcription...")
        transcribed = None
        try:
            from services.transcriber import transcribe_audio

            transcribed = await transcribe_audio(log_id, role)
        except ImportError as e:
            logger.warning("Whisper transcription unavailable: {}", e)
        if transcribed:
            transcript = transcribed
            logger.info("Transcription successful, proceeding with analysis")
        else:
            logger.info(f"No transcript and no transcribable recording for lead {lead_id}")
            await update_lead_status(
                lead_id,
                status="completed",
                analysis={"summary": "Call connected; transcript unavailable.", "rating": 0, "disposition": "Answered"},
                duration_sec=duration_sec,
            )
            return

    # Count how many turns are from the lead vs the AI
    lead_turns = 0
    for line in transcript.strip().splitlines():
        try:
            obj = json.loads(line)
            role_label = obj.get("role") or obj.get("type", "")
            content = (obj.get("content") or obj.get("text") or obj.get("message", "")).strip()
            if role_label == "user" and content and len(content) > 1:
                lead_turns += 1
        except Exception:
            pass

    if lead_turns < 1:
        logger.info(f"Lead {lead_id} had no verbal response — marking as no-conversation.")
        await update_lead_status(
            lead_id,
            status="completed",
            analysis={
                "summary": "Call connected but lead did not speak / no conversation.",
                "rating": 0,
                "disposition": "Answered",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured from the lead.",
                "emotion_confidence": None,
                "requested_callback_datetime_iso": None,
            },
            duration_sec=duration_sec,
        )
        return

    try:
        from services.call_analyzer import analyze_call_transcript, canonical_disposition
        from services.callback_time import annotate_analysis_callback_epoch
 
        analysis = await analyze_call_transcript(transcript)
        annotate_analysis_callback_epoch(analysis, tz_name=settings.transcript_callback_tz)
        from services.transcript_interest import apply_interest_disposition_override

        analysis = apply_interest_disposition_override(analysis, transcript)
    except Exception as e:
        logger.exception(f"Analyzer call failed for lead {lead_id}")
        from services.transcript_interest import apply_interest_disposition_override

        fallback_analysis = {
            "summary": f"Analyzer error: {e}",
            "rating": 0,
            "disposition": "Answered",
            "requested_callback_datetime_iso": None,
            "emotion_label": "Unknown",
            "emotion_rationale": "",
            "emotion_confidence": None,
        }
        fallback_analysis = apply_interest_disposition_override(fallback_analysis, transcript)
        await update_lead_status(
            lead_id,
            status="completed",
            analysis=fallback_analysis,
            duration_sec=duration_sec,
        )
        return

    try:
        rem_f = float(analysis.get("callback_reminder_epoch"))
    except (TypeError, ValueError):
        rem_f = None

    canon_disp = canonical_disposition(analysis.get("disposition"))
    now_t = time.time()

    if canon_disp in ("Call Later", "Busy") and rem_f is not None:
        new_status = "callback_scheduled" if rem_f > now_t else "pending"
    else:
        new_status = _disposition_to_status(analysis.get("disposition", ""))

    await update_lead_status(lead_id, status=new_status, analysis=analysis, duration_sec=duration_sec)
    logger.info(
        f"Analysis updated for lead {lead_id}: status={new_status} disposition={analysis.get('disposition')!r} "
        f"rating={analysis.get('rating')} callback_epoch={analysis.get('callback_reminder_epoch')!r} "
        f"duration={duration_sec}"
    )

    # Auto-send WhatsApp when disposition is Interested
    if canon_disp == "Interested":
        try:
            from services.whatsapp_auto_sender import maybe_send_interested_whatsapp
            _wa_task = asyncio.create_task(
                maybe_send_interested_whatsapp(
                    lead_id=lead_id,
                    phone="",  # will be fetched from DB inside the function
                    role=role,
                    analysis=analysis,
                )
            )
            _background_tasks.add(_wa_task)
            _wa_task.add_done_callback(_background_tasks.discard)
        except Exception as e:
            logger.warning("Failed to trigger WhatsApp auto-send for lead {}: {}", lead_id, e)




async def _finalize_manual_call_leg(
    role: str, camp_id: str, live_log_id: str, duration_sec: float | None = None
) -> None:
    """Post-call analyzer + SQLite row for console **Make a Call** legs (no lead row)."""
    from core.storage import finalize_manual_call_record, manual_call_row_by_camp_id

    try:
        if not await manual_call_row_by_camp_id(camp_id):
            logger.warning("Manual call finalize: no manual_calls row for camp_id={}", camp_id)
            return

        transcript = _read_transcript_jsonl(role, live_log_id)
        analysis: dict
        if not (transcript or "").strip():
            analysis = {
                "summary": "Call ended; transcript unavailable.",
                "rating": 0,
                "next_steps": "N/A",
                "disposition": "Answered",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured in transcript.",
                "emotion_confidence": None,
            }
        else:
            try:
                from services.call_analyzer import analyze_call_transcript

                analysis = await analyze_call_transcript(transcript)
            except Exception as e:
                logger.exception("Manual call analyzer failed: {}", e)
                analysis = {
                    "summary": f"Analyzer error: {e}",
                    "rating": 0,
                    "next_steps": "Retry later",
                    "disposition": "Answered",
                    "emotion_label": "Unknown",
                    "emotion_rationale": "",
                    "emotion_confidence": None,
                }

        from services.transcript_interest import apply_interest_disposition_override
        analysis = apply_interest_disposition_override(analysis, transcript)

        await finalize_manual_call_record(camp_id, live_log_id, duration_sec, analysis)
        logger.info(
            "Manual call outcome saved camp_id={} disposition={!r}",
            camp_id,
            analysis.get("disposition"),
        )

        # Auto-send WhatsApp for manual calls when disposition is Interested
        from services.call_analyzer import canonical_disposition
        canon_disp = canonical_disposition(analysis.get("disposition"))
        logger.info(
            "Manual call auto-send check camp_id={} raw_disposition={!r} canonical={!r}",
            camp_id, analysis.get("disposition"), canon_disp,
        )
        if canon_disp == "Interested":
            logger.info("Manual call Interested, preparing WhatsApp auto-send for camp_id={}", camp_id)
            try:
                from core.storage import manual_call_row_by_camp_id
                manual_row = await manual_call_row_by_camp_id(camp_id)
                logger.info("Manual call auto-send row lookup camp_id={} row_found={}", camp_id, bool(manual_row))
                if manual_row:
                    manual_phone = (manual_row.get("to_phone") or "").strip()
                    manual_name = (manual_row.get("callee_name") or "").strip()
                    logger.info("Manual call auto-send phone camp_id={} phone={!r} name={!r}", camp_id, manual_phone, manual_name)
                    if manual_phone:
                        from services.whatsapp_auto_sender import maybe_send_interested_whatsapp
                        _wa_task = asyncio.create_task(
                            maybe_send_interested_whatsapp(
                                lead_id=0,  # manual calls have no lead row
                                phone=manual_phone,
                                role=role,
                                analysis={**analysis, "_callee_name": manual_name},
                            )
                        )
                        _background_tasks.add(_wa_task)
                        _wa_task.add_done_callback(_background_tasks.discard)
                        logger.info("Manual call auto-send task created camp_id={} phone={!r}", camp_id, manual_phone)
            except Exception as e:
                logger.warning("Failed to trigger WhatsApp auto-send for manual call {}: {}", camp_id, e)
    finally:
        release_vobiz_call_slot(role)


async def _campaign_worker_role(role: str):
    """Worker task that dials leads for a specific role (one leg at a time per role)."""
    from core.campaign_hours import is_campaign_quiet_hours, quiet_hours_block_message
    _in_quiet = is_campaign_quiet_hours()
    logger.info(
        "Worker for {} started (quiet_hours={}, time={}).",
        role,
        _in_quiet,
        time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )
    if _in_quiet:
        logger.warning(
            "Worker for {} is starting DURING quiet hours — it will stop on first loop iteration. {}",
            role,
            quiet_hours_block_message(),
        )
    await _recover_stale_dialing(role)

    empty_since: float | None = None
    campaign_started_at = time.time()
    next_callback_batch_at = campaign_started_at + _CALLBACK_BATCH_INTERVAL_SEC

    while True:
        try:
            if not _CAMPAIGN_TASKS.get(role):
                logger.warning("EXIT {}: task slot cleared externally (cancelled by user or shutdown).", role)
                break

            if is_campaign_quiet_hours():
                _msg = quiet_hours_block_message()
                logger.warning(
                    "EXIT {}: quiet hours active — stopping campaign. {}",
                    role,
                    _msg,
                )
                try:
                    await set_campaign_want_running(role, False)
                except Exception:
                    pass
                await release_orphaned_dialing_leads(
                    role,
                    error="Campaign stopped: outside calling hours (9:30 AM – 8:30 PM IST).",
                )
                _CAMPAIGN_TASKS[role] = None
                break

            try:
                await promote_due_scheduled_callbacks(time.time())
            except Exception as e:
                logger.exception("promote_due_scheduled_callbacks failed")

            now = time.time()
            if now >= next_callback_batch_at:
                logger.info(f"30-minute mark reached for {role} - processing callbacks")
                await _process_callback_batch(role)
                next_callback_batch_at = now + _CALLBACK_BATCH_INTERVAL_SEC
                logger.info(f"Next callback batch scheduled at {next_callback_batch_at}")

            state = get_state(role)

            pending = await get_leads(role, status="pending", limit=1000)
            if not pending:
                try:
                    now_t = time.time()
                    if await role_has_future_callback_scheduled(role, now_t):
                        empty_since = None
                        await _cancellable_sleep(role, 15.0)
                        continue
                except Exception as e:
                    logger.exception("Deferred callback idle check failed")

                if empty_since is None:
                    empty_since = time.time()
                    logger.info(f"Queue empty for {role}; waiting up to {_EMPTY_QUEUE_GRACE_SEC}s for new leads.")
                if time.time() - empty_since >= _EMPTY_QUEUE_GRACE_SEC:
                    logger.warning("EXIT {}: no pending leads after {}s grace — stopping campaign.", role, _EMPTY_QUEUE_GRACE_SEC)
                    try:
                        from core.storage import set_campaign_want_running
                        await set_campaign_want_running(role, False)
                    except Exception:
                        pass
                    _CAMPAIGN_TASKS[role] = None
                    break
                await asyncio.sleep(2)
                continue
            empty_since = None

            if role_has_active_vobiz_call(role):
                logger.debug(
                    f"Role slot busy: {role} waiting (role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()})."
                )
                await asyncio.sleep(2)
                continue

            lead = pending[0]
            lead_id = lead["id"]
            lead_phone = lead["phone"]
            lead_name = lead.get("name", "Unknown")

            call_id = str(uuid.uuid4())
            await update_lead_status(lead_id, "dialing")
            await update_lead_call_info(lead_id, start_time=time.time(), call_id=call_id)

            _CAMPAIGN_DATA[call_id] = {
                **lead,
                "_lead_id": lead_id,
                "_leadIndex": -1,
                "_role": role,
                "_call_id": call_id,
            }

            v_cfg = state.get("vobiz", {}) or {}
            v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)

            if not v_auth_id or not v_token or not v_base or not v_from:
                _missing = []
                if not v_auth_id: _missing.append("auth_id")
                if not v_token: _missing.append("token")
                if not v_base: _missing.append("base_url")
                if not v_from: _missing.append("from_number")
                logger.error(
                    "EXIT {}: telephony not configured — missing [{}]. Stopping campaign.",
                    role,
                    ", ".join(_missing),
                )
                await update_lead_status(lead_id, "failed", error="Telephony not configured")
                _CAMPAIGN_DATA.pop(call_id, None)
                try:
                    from core.storage import set_campaign_want_running
                    await set_campaign_want_running(role, False)
                except Exception:
                    pass
                _CAMPAIGN_TASKS[role] = None
                break

            from services.vobiz_bridge import make_vobiz_call, VobizCallError
            slot_acquired = False
            try:
                try:
                    from services.campaign_live import set_active_campaign_call, clear_transcript_session
                    set_active_campaign_call(call_id)
                    clear_transcript_session(call_id)
                except Exception as _ce:
                    logger.exception("campaign_live setup skipped: {}", _ce)

                active_role = role
                _CAMPAIGN_DATA[call_id]["_role"] = active_role
                _CAMPAIGN_DATA[call_id]["_campaign_role"] = role

                opening = _build_opening_line(lead, active_role)
                _prime_opening_audio(call_id, active_role, opening)

                acquire_vobiz_call_slot(role)
                slot_acquired = True
                logger.info(
                    f"Call initiated: {lead_name} ({lead_phone}) "
                    f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                )

                try:
                    _vobiz_resp = await make_vobiz_call(
                        to=lead_phone, from_=v_from,
                        answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}",
                        auth_id=v_auth_id, auth_token=v_token,
                        extra={
                            "ring_url": f"{v_base}/vobiz/ring?camp_id={call_id}",
                            "ring_method": "POST",
                            "hangup_url": f"{v_base}/vobiz/hangup?camp_id={call_id}",
                            "hangup_method": "POST",
                            "hangup_on_ring": "3600",
                        },
                    )
                    _call_uuid = _vobiz_resp.get("request_uuid") or ""
                    if _call_uuid:
                        _CAMPAIGN_DATA[call_id]["_vobiz_call_uuid"] = _call_uuid
                except VobizCallError as ve:
                    logger.error(
                        f"Vobiz refused call to {lead_phone} for {role}: HTTP {ve.status} — {ve.message}"
                    )
                    await update_lead_status(lead_id, "failed", error=f"Vobiz {ve.status}: {ve.message}")
                    if ve.status in (401, 402, 403):
                        logger.error(
                            "EXIT {}: non-recoverable Vobiz HTTP {} — halting campaign. "
                            "Check Vobiz auth token / balance.",
                            role,
                            ve.status,
                        )
                        try:
                            from core.storage import set_campaign_want_running
                            await set_campaign_want_running(role, False)
                        except Exception:
                            pass
                        _CAMPAIGN_TASKS[role] = None
                        raise asyncio.CancelledError()
                    continue

                answered = False
                call_started_at = time.time()
                MAX_RING_WAIT = 60
                MAX_TOTAL_WAIT = 360
                _ws_connected_at = None  # Track when Vobiz WS connects (answer callback received)

                while True:
                    if not _CAMPAIGN_TASKS.get(role):
                        break

                    info = _CAMPAIGN_DATA.get(call_id, {})
                    if not answered and info.get("_call_connected_at"):
                        answered = True
                        _ws_connected_at = info["_call_connected_at"]
                        logger.info(
                            f"Call connected with {lead_name} ({lead_phone}) "
                            f"[ws_connect_latency={(_ws_connected_at - call_started_at):.1f}s]"
                        )
                    if answered and info.get("_call_ended_at"):
                        logger.info(f"Call ended naturally with {lead_name}")
                        break

                    elapsed = time.time() - call_started_at
                    # Diagnostic: log progress every 15s while waiting for connection
                    if not answered and int(elapsed) % 15 == 0 and int(elapsed) > 0:
                        _prev_log = getattr(_campaign_worker_role, '_last_progress_log', {})
                        if _prev_log.get(call_id) != int(elapsed):
                            logger.info(
                                f"Waiting for answer from {lead_name} ({lead_phone}): "
                                f"{int(elapsed)}s elapsed, ws_connected={info.get('_call_connected_at') is not None}"
                            )
                            _campaign_worker_role._last_progress_log = {call_id: int(elapsed)}
                    if not answered and elapsed >= MAX_RING_WAIT:
                        logger.warning(
                            f"No answer for {lead_name} ({lead_phone}) after {MAX_RING_WAIT}s — "
                            f"Vobiz answer callback may not have fired. "
                            f"Check: (1) VOBIZ_PUBLIC_BASE_URL is reachable, "
                            f"(2) WebSocket upgrade works on your domain, "
                            f"(3) camp_id={call_id[:12]}... was passed correctly."
                        )
                        break
                    if elapsed >= MAX_TOTAL_WAIT:
                        logger.warning(f"Call to {lead_name} exceeded {MAX_TOTAL_WAIT}s — forcing next.")
                        break

                    lead_finalized = False
                    try:
                        rows = await get_leads(role, limit=2000)
                        for l in rows:
                            if l["id"] == lead_id and l["status"] in ("completed", "not_interested", "failed"):
                                logger.info(f"Lead {lead_name} status finalized as {l['status']}")
                                lead_finalized = True
                                break
                    except Exception:
                        logger.exception("Lead status check failed")
                    if lead_finalized:
                        break

                    await asyncio.sleep(2)

                if not answered:
                    # Only mark "failed" if status is still "dialing" — if it's already
                    # "in_progress", the call is active and the worker should not interfere.
                    current_status = "dialing"
                    try:
                        rows_check = await get_leads(role, limit=2000)
                        for lc in rows_check:
                            if lc["id"] == lead_id:
                                current_status = lc.get("status", "dialing")
                                break
                    except Exception:
                        pass
                    if current_status == "in_progress":
                        logger.info(
                            f"Lead {lead_name} is in_progress (call active) — not marking failed despite no _call_connected_at"
                        )
                    else:
                        logger.info(f"Lead {lead_name} did not connect — marking failed.")
                        await update_lead_status(lead_id, "failed", error="No answer / Timeout")

                log_id = (_CAMPAIGN_DATA.get(call_id, {}) or {}).get("_log_id")
                if log_id:
                    try:
                        await update_lead_call_info(lead_id, log_id=log_id, call_id=call_id)
                    except Exception as exc:
                        logger.exception(f"Persist log_id failed for lead {lead_id}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Call trigger failed for {lead_phone}")
                await update_lead_status(lead_id, "failed", error=str(e))
            finally:
                if slot_acquired:
                    release_vobiz_call_slot(role)
                    logger.info(
                        f"Call slot released for {role} "
                        f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                    )
                _CAMPAIGN_DATA.pop(call_id, None)

            if not _CAMPAIGN_TASKS.get(role):
                logger.warning("EXIT {}: task slot cleared after call completion.", role)
                break

            gap = inter_call_gap_seconds_for_role(role)
            if not await _cancellable_sleep(role, gap):
                logger.warning("EXIT {}: inter-call sleep cancelled (shutdown or role change).", role)
                break

        except asyncio.CancelledError:
            logger.warning("EXIT {}: outer loop CancelledError (task.cancel() called).", role)
            break
        except Exception as e:
            logger.exception(f"Worker error for {role}")
            await asyncio.sleep(10)

    logger.info(f"Worker for {role} finished.")


# ─── Campaign Scheduler ───────────────────────────────────────────────
# Polls the ``schedules`` table every ``_SCHEDULER_POLL_SEC`` seconds and,
# for each row whose ``run_at`` has been reached and ``status='scheduled'``,
# starts the same per-role campaign worker the **Start Campaign** button
# triggers — so a user can upload a CSV in the morning and have it dial out
# automatically at, say, 5 PM.

_SCHEDULER_POLL_SEC = float(os.getenv("CAMPAIGN_SCHEDULER_POLL_SEC", "30"))


async def _run_scheduled_campaign(
    role: str,
    schedule_id: int,
    stop_at: float | None = None,
):
    """Wrapper that ties a schedule row's lifecycle to a campaign worker run.

    Uses the same ``_CAMPAIGN_TASKS[role]`` slot the manual toggle uses so the
    Stop button, status endpoint, and dashboard pill all reflect the run
    correctly without any extra plumbing.

    If ``stop_at`` (epoch-UTC seconds) is provided, the campaign is forcibly
    stopped at that moment by cancelling the worker task. The schedule row is
    marked ``completed`` (not ``cancelled``) because reaching the end of the
    operator-defined window is the intended terminal state, not a failure.
    """
    stop_watcher: asyncio.Task | None = None
    stopped_by_window = False

    async def _window_stop_watcher() -> None:
        """Sleep until ``stop_at`` then cancel the campaign worker."""
        nonlocal stopped_by_window
        if stop_at is None:
            return
        delay = max(0.0, float(stop_at) - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        active = _CAMPAIGN_TASKS.get(role)
        if active and not active.done():
            stopped_by_window = True
            logger.info(
                f"Scheduled campaign id={schedule_id} role={role!r}: "
                f"stop window reached — cancelling worker."
            )
            active.cancel()

    try:
        await mark_schedule_status(schedule_id, "running", started_at=time.time())
        task = asyncio.create_task(_campaign_worker_role(role))
        _CAMPAIGN_TASKS[role] = task
        if stop_at is not None:
            stop_watcher = asyncio.create_task(_window_stop_watcher())

        try:
            await task
        except asyncio.CancelledError:
            # If we cancelled the worker because the stop window expired, treat
            # it as a clean completion. Otherwise (Stop button / process
            # shutdown), surface as cancelled.
            if stopped_by_window:
                await mark_schedule_status(schedule_id, "completed")
                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} "
                    f"→ completed (auto-stopped at end of window)"
                )
                return
            raise
        # Worker exited naturally (queue empty + grace period).
        await mark_schedule_status(schedule_id, "completed")
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → completed")
    except asyncio.CancelledError:
        await mark_schedule_status(
            schedule_id, "cancelled", error="Run cancelled before completion."
        )
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → cancelled")
        raise
    except Exception as e:
        await mark_schedule_status(schedule_id, "failed", error=str(e)[:300])
        logger.exception(f"Scheduled campaign id={schedule_id} role={role!r} failed")
    finally:
        if stop_watcher and not stop_watcher.done():
            stop_watcher.cancel()


async def _schedule_preflight(role: str) -> str | None:
    """Mirror of the checks in ``/api/campaign/toggle``. Returns an error string
    if the run cannot be started right now, else ``None``.
    """
    from core.storage import is_campaign_globally_paused

    if await is_campaign_globally_paused():
        return (
            "Campaign is paused. Outbound dialing will not run until you click "
            "Start during calling hours (9:30 AM – 8:30 PM IST)."
        )
    if is_campaign_quiet_hours():
        return quiet_hours_block_message()
    running = _CAMPAIGN_TASKS.get(role)
    if running and not running.done():
        return "A campaign is already running for this role."
    counts = await get_lead_counts(role)
    if counts.get("pending", 0) <= 0 and counts.get("dialing", 0) <= 0:
        return "No pending leads at scheduled time. Upload a list before the schedule fires."
    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    auth_id, auth_token, _from_num, base_url = resolve_vobiz_credentials(role, v_cfg)
    missing = []
    if not auth_id:
        missing.append("Auth ID")
    if not auth_token:
        missing.append("Auth Token")
    if not base_url:
        missing.append("Public URL")
    if not _from_num:
        missing.append("From Number")
    if missing:
        return f"Telephony bridge not configured ({', '.join(missing)} missing)."
    return None


async def _enforce_window_stop(sched: dict) -> None:
    """Force-stop a scheduled run whose stop window has expired.

    Two cases:
      a) The campaign worker is still running in this process → cancel it.
         The wrapper's CancelledError handler will mark the schedule as
         ``completed`` (because ``stopped_by_window`` is True after we cancel).
         Actually — the wrapper only flips ``stopped_by_window`` inside its
         own watcher. Since this enforcement path comes from the polling loop
         (e.g. after a server restart that lost the inline watcher), we mark
         the row directly and rely on the worker's CancelledError path to
         exit cleanly.
      b) No worker is running for this role (process restart, manual Stop) →
         just close out the row.
    """
    schedule_id = int(sched.get("id") or 0)
    role = normalize_console_role(sched.get("role") or "data_edge")
    if not schedule_id:
        return
    active = _CAMPAIGN_TASKS.get(role)
    if active and not active.done():
        logger.info(
            f"Scheduler: stop window reached for id={schedule_id} role={role!r} "
            f"after restart — cancelling worker."
        )
        active.cancel()
    await mark_schedule_status(
        schedule_id, "completed",
        error=None,
    )


async def _scheduler_loop():
    """Long-running task that fires due schedules. Cancel-safe.

    Two responsibilities every poll:
      1. Start any ``scheduled`` rows whose ``run_at`` has passed.
      2. Force-stop any ``running`` rows whose ``stop_at`` has passed (the
         inline stop watcher handles the happy path; this is the safety net
         for process restarts).
    """
    logger.info(f"Campaign scheduler started (poll every {_SCHEDULER_POLL_SEC:.0f}s).")
    while True:
        try:
            now = time.time()

            try:
                await promote_due_scheduled_callbacks(now)
            except Exception as e:
                logger.exception("Scheduler: promote_due_scheduled_callbacks failed")

            # ── 1. Fire due schedules ──
            try:
                due = await due_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: due_schedules query failed")
                due = []

            for sched in due:
                schedule_id = int(sched.get("id") or 0)
                role = normalize_console_role(sched.get("role") or "data_edge")
                stop_at = sched.get("stop_at")
                if not schedule_id:
                    continue

                err = await _schedule_preflight(role)
                if err:
                    await mark_schedule_status(schedule_id, "failed", error=err)
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} skipped — {err}"
                    )
                    continue

                # Edge case: stop_at already passed before we even fired (clock
                # skew / very short window). Don't bother starting.
                if stop_at is not None and float(stop_at) <= now:
                    await mark_schedule_status(
                        schedule_id, "failed",
                        error="Stop time passed before the campaign could start.",
                    )
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} "
                        f"stop_at already past — not starting."
                    )
                    continue

                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} firing now "
                    f"(name={sched.get('name')!r}, stop_at={stop_at})"
                )
                # Don't await — let it run in the background while we keep polling.
                task = asyncio.create_task(
                    _run_scheduled_campaign(role, schedule_id, stop_at=stop_at)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

            # ── 2. Enforce stop windows (safety net for restarts) ──
            try:
                expired = await expired_running_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: expired_running query failed")
                expired = []
            for sched in expired:
                await _enforce_window_stop(sched)

        except asyncio.CancelledError:
            logger.info("Campaign scheduler cancelled.")
            raise
        except Exception as e:
            logger.exception("Scheduler loop iteration error")

        # Sleep in slices so cancellation is responsive even if poll interval is large.
        slept = 0.0
        while slept < _SCHEDULER_POLL_SEC:
            await asyncio.sleep(min(1.0, _SCHEDULER_POLL_SEC - slept))
            slept += 1.0
