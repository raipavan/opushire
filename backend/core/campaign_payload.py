"""Dashboard campaign JSON helpers: enrich leads & chart fields consumed by ``/api/campaign/state``."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from core.state import normalize_console_role


def _parse_analysis_blob(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def lead_is_called_console(lead: dict) -> bool:
    """Match frontend ``isCalled``: any sign the lead has been touched on the phone."""
    return bool(lead.get("start_time") or lead.get("_log_id") or lead.get("called_at_iso"))


def effective_disposition_console(lead: dict) -> str:
    """Mirror ``effectiveDispo`` in ``frontend/static/js/api_utils.js``."""
    s = str(lead.get("status") or "").strip().lower()
    if s in ("failed", "error"):
        return "Failed"
    from services.call_analyzer import canonical_disposition
    aj = _parse_analysis_blob(lead.get("analysis"))
    if aj.get("disposition_overridden"):
        d = canonical_disposition(str(aj.get("disposition") or "").strip())
        return d or "Answered"
    d = canonical_disposition(
        str(lead.get("disposition") or aj.get("disposition") or "")
        .strip()
    )
    if d and d not in ("Answered", ""):
        return d
    if aj.get("outcome_from_transcript"):
        return "Interested"
    try:
        from services.transcript_interest import soft_interest_in_text

        if soft_interest_in_text(
            lead.get("summary"),
            aj.get("summary"),
            lead.get("next_steps"),
            aj.get("next_steps"),
        ):
            return "Interested"
    except Exception:
        pass
    if d:
        return d
    s = str(lead.get("status") or "").strip().lower()
    status_map = {
        "not_interested": "Not Interested",
        "completed": "Completed",
        "failed": "Failed",
        "pending": "Pending",
        "dialing": "Dialing…",
        "no answer": "No answer",
    }
    return status_map.get(s, s[:1].upper() + s[1:] if s else "")


def _sqlite_row_ts_to_utc_date(txt: object) -> date | None:
    """Parse ``YYYY-MM-DD HH:MM:SS`` SQLite timestamps as UTC."""
    if txt is None or not str(txt).strip():
        return None
    s = str(txt).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def _dashboard_tz() -> ZoneInfo:
    try:
        return ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _sqlite_row_ts_to_ist_date(txt: object, tz: ZoneInfo) -> date | None:
    """SQLite ``updated_at`` style string → calendar date in ``tz`` (server rows are UTC wall time)."""

    if txt is None or not str(txt).strip():
        return None
    s = str(txt).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt_utc = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt_utc.astimezone(tz).date()
        except ValueError:
            continue
    return None


def _lead_anchor_dashboard_date(lead: dict, tz: ZoneInfo) -> date | None:
    """Calendar day in the dashboard TZ (IST by default) for timeline buckets."""

    try:
        st = lead.get("start_time")
        if st is not None:
            f = float(st)
            if f > 0:
                return datetime.fromtimestamp(f, tz=timezone.utc).astimezone(tz).date()
    except (TypeError, ValueError, OSError):
        pass

    iso = lead.get("called_at_iso")
    if isinstance(iso, str) and iso.strip():
        txt = iso.strip()
        try:
            if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt and len(txt) >= 16:
                txt = txt + "Z"
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            return dt.astimezone(tz).date()
        except (ValueError, TypeError):
            pass

    return _sqlite_row_ts_to_ist_date(lead.get("updated_at"), tz)


def _lead_anchor_utc_date(lead: dict) -> date | None:
    """UTC calendar day (legacy / tests). Prefer ``_lead_anchor_dashboard_date`` for charts."""

    try:
        st = lead.get("start_time")
        if st is not None:
            f = float(st)
            if f > 0:
                return datetime.fromtimestamp(f, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        pass

    iso = lead.get("called_at_iso")
    if isinstance(iso, str) and iso.strip():
        txt = iso.strip()
        try:
            if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt and len(txt) >= 16:
                txt = txt + "Z"
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).date()
        except (ValueError, TypeError):
            pass

    u = _sqlite_row_ts_to_utc_date(lead.get("updated_at"))
    return u


def _stored_name_looks_like_row_counter(name: str) -> bool:
    """Match Excel row indices stored as ``name`` (``11.0``, ``7``…) — wrong column mapping."""

    t = str(name or "").strip().replace(",", "").replace(" ", "")
    if not t:
        return False
    return bool(re.fullmatch(r"-?\d+(?:\.(?:0+|00+))?$", t))


def enrich_lead_for_console(lead: dict) -> dict:
    """Expose ``disposition``, ``summary``, ``rating``, ``called_at_iso`` for dashboard rows & charts."""
    out = dict(lead)
    log_id_raw = str(out.get("_log_id") or out.get("log_id") or "").strip()
    if log_id_raw:
        out["log_id"] = log_id_raw
        try:
            from services.call_recording import resolve_session_recording_path

            rp = resolve_session_recording_path(log_id_raw)
            out["recording_available"] = bool(rp and rp.is_file())
            if out["recording_available"]:
                role_key = normalize_console_role(str(out.get("role") or "data_edge"))
                out["recording_url"] = (
                    f"/api/campaign/lead/{out['id']}/recording?role={role_key}"
                )
        except Exception:
            out["recording_available"] = False
    else:
        out["recording_available"] = False
    aj = _parse_analysis_blob(out.get("analysis"))
    disp = (
        str(out.get("disposition") or aj.get("disposition") or "").strip()
    )
    out["disposition"] = disp
    if "summary" not in out or not out["summary"]:
        out["summary"] = str(aj.get("summary") or "")
    if aj.get("rating") is not None:
        try:
            out["rating"] = int(aj.get("rating"))
        except (ValueError, TypeError):
            out["rating"] = 0

    ns = aj.get("next_steps")
    if ns is not None and not str(out.get("next_steps") or "").strip():
        out["next_steps"] = ns if isinstance(ns, str) else "; ".join(str(x) for x in ns)

    na = aj.get("next_action")
    if na is not None:
        out["next_action"] = na

    if aj.get("emotion_label") and not out.get("emotion_label"):
        out["emotion_label"] = str(aj.get("emotion_label") or "").strip()
    if aj.get("emotion_rationale") and not out.get("emotion_rationale"):
        out["emotion_rationale"] = str(aj.get("emotion_rationale") or "").strip()
    if aj.get("emotion_confidence") is not None and out.get("emotion_confidence") is None:
        try:
            out["emotion_confidence"] = float(aj.get("emotion_confidence"))
        except (TypeError, ValueError):
            pass

    if out.get("start_time") and not out.get("called_at_iso"):
        try:
            st = float(out["start_time"])
            out["called_at_iso"] = datetime.utcfromtimestamp(st).replace(tzinfo=None).isoformat() + "Z"
        except (ValueError, TypeError, OSError):
            pass
    # Post-call QA from transcript flags (manifest / tooling)
    if aj.get("outcome_from_transcript"):
        out["outcome_from_transcript"] = bool(aj["outcome_from_transcript"])

    cre = aj.get("callback_reminder_epoch")
    if cre is not None:
        try:
            out["callback_reminder_at_iso"] = (
                datetime.fromtimestamp(float(cre), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError, OSError):
            pass

    nm_raw = str(out.get("name") or "").strip()
    co_raw = str(out.get("company") or "").strip()
    co_lines = [x.strip() for x in co_raw.splitlines() if str(x).strip()]
    if co_lines:
        out["contact_display_primary"] = co_lines[0]
        tail = co_lines[1:]
        if nm_raw and nm_raw.lower() != "unknown" and nm_raw != co_lines[0]:
            out["contact_display_secondary"] = nm_raw + (" · " + " · ".join(tail) if tail else "")
        else:
            out["contact_display_secondary"] = " · ".join(tail) if tail else ""
    elif nm_raw and nm_raw.lower() != "unknown":
        out["contact_display_primary"] = nm_raw
        out["contact_display_secondary"] = ""
    else:
        out["contact_display_primary"] = "Unknown"
        out["contact_display_secondary"] = ""
    role_key = normalize_console_role(str(out.get("role") or "data_edge"))
    lid = out.get("id")
    log_ref = log_id_raw or str(out.get("log_id") or "").strip()
    if lid is not None and log_ref:
        out["transcript_url"] = f"/api/campaign/lead/{lid}/transcript?role={role_key}"
        if out.get("recording_available"):
            out["recording_url"] = f"/api/campaign/lead/{lid}/recording?role={role_key}"
    # Align manifest/filter disposition with soft-interest rules (email / send details / will check).
    effective = effective_disposition_console(out)
    if effective:
        out["disposition"] = effective
    return out


# Fields safe to send to the browser (omit multi-KB ``analysis`` blobs).
_SLIM_LEAD_KEYS = (
    "id",
    "role",
    "name",
    "phone",
    "email",
    "company",
    "segment",
    "status",
    "duration_sec",
    "disposition",
    "summary",
    "rating",
    "start_time",
    "called_at_iso",
    "_log_id",
    "log_id",
    "recording_available",
    "recording_url",
    "transcript_url",
    "outcome_from_transcript",
    "next_steps",
    "emotion_label",
    "emotion_rationale",
    "emotion_confidence",
    "failure_title",
    "failure_detail",
    "failure_reason",
    "failure_severity",
    "contact_display_primary",
    "contact_display_secondary",
    "callback_reminder_at_iso",
    "error",
)


def slim_lead_for_api(lead: dict, *, role: str | None = None) -> dict:
    """Enrich then drop heavy columns so ``/state`` and ``/manifest`` stay small and reliable."""

    enriched = enrich_lead_for_console(dict(lead))
    role_key = normalize_console_role(role or enriched.get("role") or "data_edge")
    out: dict[str, Any] = {}
    for key in _SLIM_LEAD_KEYS:
        if key in enriched and enriched[key] is not None:
            out[key] = enriched[key]
    out["id"] = enriched.get("id")
    out["role"] = role_key
    if enriched.get("_log_id"):
        out["_log_id"] = enriched["_log_id"]
    if enriched.get("log_id"):
        out["log_id"] = enriched["log_id"]
    lid = enriched.get("id")
    log_ref = str(enriched.get("log_id") or enriched.get("_log_id") or "").strip()
    if lid is not None and log_ref:
        out["transcript_url"] = f"/api/campaign/lead/{lid}/transcript?role={role_key}"
        if enriched.get("recording_available"):
            out["recording_url"] = f"/api/campaign/lead/{lid}/recording?role={role_key}"
    return out


def disposition_counts_for_dashboard(leads_enriched: list[dict]) -> dict[str, int]:
    """Bucket QA dispositions for Outcome Distribution (all outbound-touched leads)."""

    keys = (
        "Interested",
        "Not Interested",
        "Call Later",
        "Busy",
        "Callback",
        "Answered",
        "Failed",
    )
    buckets: dict[str, int] = {k: 0 for k in keys}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        status = str(lead.get("status") or "").strip().lower()
        if status in ("failed", "error"):
            buckets["Failed"] += 1
            continue
        if status == "not_interested":
            buckets["Not Interested"] += 1
            continue

        ed = effective_disposition_console(lead)
        el = ed.lower()

        if ed == "Interested" or ("interested" in el and "not interested" not in el):
            buckets["Interested"] += 1
        elif ed == "Not Interested" or "not interested" in el:
            buckets["Not Interested"] += 1
        elif ed in ("Wrong Number", "Not Available", "Voicemail"):
            buckets["Failed"] += 1
        elif ed == "Call Later":
            buckets["Call Later"] += 1
        elif ed == "Busy":
            buckets["Busy"] += 1
        elif ed == "Callback":
            buckets["Callback"] += 1
        elif ed in ("Failed", "No answer", "No Answer", "Error"):
            buckets["Failed"] += 1
        elif status == "completed":
            buckets["Answered"] += 1
        else:
            buckets["Answered"] += 1
    return buckets


def progress_counts_for_dashboard(leads_enriched: list[dict]) -> dict[str, int]:
    """Status breakdown for Campaign Progress bar (full outbound cohort)."""
    out = {"connected": 0, "failed": 0, "no_answer": 0, "pending": 0, "other": 0}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        s = str(lead.get("status") or "").strip().lower()
        if s == "completed":
            out["connected"] += 1
        elif s in ("failed", "error"):
            out["failed"] += 1
        elif s in ("no answer", "busy"):
            out["no_answer"] += 1
        elif s in ("pending", "dialing", ""):
            out["pending"] += 1
        else:
            out["other"] += 1
    return out


def weekday_counts_for_dashboard(
    leads_enriched: list[dict], tz: ZoneInfo
) -> list[int]:
    """Calls by weekday (Mon=0 … Sun=6) in dashboard TZ — matches frontend chartWeekday."""
    counts = [0] * 7
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        day_anchor = _lead_anchor_dashboard_date(lead, tz)
        if day_anchor is None:
            continue
        counts[day_anchor.weekday()] += 1
    return counts


def campaign_called_count(leads_enriched: list[dict]) -> int:
    return sum(1 for l in leads_enriched if lead_is_called_console(l))


def hourly_counts_for_dashboard(leads_enriched: list[dict], tz: ZoneInfo) -> list[int]:
    """Calls by hour of day (0-23) in dashboard TZ — for the Hourly Distribution chart."""
    counts = [0] * 24
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        try:
            st = lead.get("start_time")
            if st is not None:
                f = float(st)
                if f > 0:
                    hour = datetime.fromtimestamp(f, tz=timezone.utc).astimezone(tz).hour
                    counts[hour] += 1
                    continue
        except (TypeError, ValueError, OSError):
            pass
        iso = lead.get("called_at_iso")
        if isinstance(iso, str) and iso.strip():
            try:
                txt = iso.strip()
                if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt:
                    txt = txt + "Z"
                dt = datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(tz)
                counts[dt.hour] += 1
            except (ValueError, TypeError):
                pass
    return counts


_DAYS_JS_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _js_weekday_label(d: date) -> str:
    """Local calendar date ``d`` → same short label as JS ``days[d.getDay()]``."""
    weekday_py = d.weekday()  # Monday=0 .. Sunday=6
    js_get_day = (weekday_py + 1) % 7  # JS: Sunday=0
    return _DAYS_JS_LABELS[js_get_day]


def last_seven_dashboard_axis() -> tuple[list[str], list[date]]:
    """Rolling 7 calendar days (oldest → newest) in ``TRANSCRIPT_CALLBACK_TZ`` — default IST."""

    tz = _dashboard_tz()
    today = datetime.now(tz).date()
    dates = [today - timedelta(days=(6 - i)) for i in range(7)]
    labels = [_js_weekday_label(day) for day in dates]
    return labels, dates


def build_dashboard_timelines(
    leads_enriched: list[dict], dates: list[date], tz: ZoneInfo
) -> tuple[list[int], list[int]]:
    """Per‑day outbound counts aligned with ``dates`` (dashboard TZ calendar days)."""

    totals = [0] * len(dates)
    interested = [0] * len(dates)
    idx = {day: i for i, day in enumerate(dates)}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        day_anchor = _lead_anchor_dashboard_date(lead, tz)
        if day_anchor is None or day_anchor not in idx:
            continue
        i = idx[day_anchor]
        totals[i] += 1
        if effective_disposition_console(lead) == "Interested":
            interested[i] += 1
    return totals, interested


def build_campaign_state_dashboard_fields(role: str, leads: list[dict]) -> dict[str, Any]:
    """Chart payload + enriched leads (parsed analysis surfaced for dashboard rows)."""

    role = normalize_console_role(role)
    enriched = [enrich_lead_for_console(dict(l)) for l in leads]
    tz = _dashboard_tz()
    labels, dates = last_seven_dashboard_axis()

    from core.storage import (
        _count_open_inbounds_for_role_sync,
        _get_leads_with_outbound_activity_sync,
        _inbound_counts_on_calendar_dates_sync,
    )

    outbound_rows = _get_leads_with_outbound_activity_sync(role)
    enriched_for_timeline = [enrich_lead_for_console(dict(l)) for l in outbound_rows]

    open_inbounds = _count_open_inbounds_for_role_sync(role)
    date_keys = [d.isoformat() for d in dates]
    per_day = _inbound_counts_on_calendar_dates_sync(role, date_keys)

    cb_by_label: dict[str, int] = {lab: 0 for lab in labels}
    for dt, lbl in zip(dates, labels):
        key = dt.isoformat()
        cb_by_label[lbl] = cb_by_label.get(lbl, 0) + int(per_day.get(key, 0) or 0)

    ttl, tins = build_dashboard_timelines(enriched_for_timeline, dates, tz)
    inbound_per_bucket = [int(per_day.get(d.isoformat(), 0) or 0) for d in dates]
    disposition = disposition_counts_for_dashboard(enriched_for_timeline)
    called_total = campaign_called_count(enriched_for_timeline)

    return {
        "called_count": called_total,
        "inbound_callbacks": open_inbounds,
        "disposition_counts": disposition,
        "callback_counts_by_date": cb_by_label,
        "timeline_dates_iso": date_keys,
        "timeline_inbound_per_day": inbound_per_bucket,
        "timeline_total_calls": ttl,
        "timeline_interested": tins,
        "timeline_week_labels": labels,
        "progress_counts": progress_counts_for_dashboard(enriched_for_timeline),
        "weekday_counts": weekday_counts_for_dashboard(enriched_for_timeline, tz),
        "hourly_counts": hourly_counts_for_dashboard(enriched_for_timeline, tz),
        "chart_interested_total": int(disposition.get("Interested") or 0),
        "leads_enriched": enriched,
    }

