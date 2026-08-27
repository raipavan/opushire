"""Post-call booking for Dariaan (vernikaai): Google Calendar event + WhatsApp confirmation.

Uses OAuth refresh token — NOT Gmail password. Configure via .env (see .env.example).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from config import settings


def dariaan_meeting_booking_configured() -> bool:
    return bool(
        settings.dariaan_meeting_booking_enabled
        and settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.google_calendar_refresh_token
        and settings.whatsapp_access_token
        and settings.whatsapp_phone_number_id
    )


def _norm_whatsapp_to(phone: str) -> Optional[str]:
    """Meta Cloud API expects digits only, country code included (no +)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11:
        return None
    return digits


async def _google_access_token() -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "refresh_token": settings.google_calendar_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = (data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"Google token exchange failed: {data}")
        return token


def _default_meeting_start(tz: ZoneInfo) -> datetime:
    """Next weekday 11:00 local if callee did not specify a time."""
    now = datetime.now(tz)
    candidate = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _parse_meeting_start(analysis: dict, tz: ZoneInfo) -> datetime:
    raw = analysis.get("requested_callback_datetime_iso") or analysis.get("meeting_datetime_iso")
    if raw:
        try:
            s = str(raw).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.astimezone(tz)
            if dt > datetime.now(tz):
                return dt
        except (TypeError, ValueError):
            pass
    return _default_meeting_start(tz)


async def _create_calendar_event(
    *,
    access_token: str,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
    attendee_email: Optional[str],
    attendee_name: str,
) -> dict[str, Any]:
    tz_name = settings.transcript_callback_tz.strip() or "Asia/Kolkata"
    cal_id = settings.google_calendar_id.strip() or "primary"
    request_id = secrets.token_hex(8)

    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    attendees = []
    if attendee_email and "@" in attendee_email:
        attendees.append({"email": attendee_email, "displayName": attendee_name or attendee_email})
    if attendees:
        body["attendees"] = attendees

    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
    params = {"conferenceDataVersion": "1", "sendUpdates": "all" if attendees else "none"}

    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Calendar API {resp.status_code}: {resp.text[:500]}")
        return resp.json()


async def _send_whatsapp_text(to_digits: str, body: str) -> dict[str, Any]:
    pid = settings.whatsapp_phone_number_id.strip()
    url = f"https://graph.facebook.com/v21.0/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "text",
        "text": {"preview_url": True, "body": body[:4096]},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json=payload,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"WhatsApp API {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _extract_meet_link(event: dict) -> str:
    conf = event.get("conferenceData") or {}
    for ep in conf.get("entryPoints") or []:
        if (ep.get("entryPointType") or "").lower() == "video":
            uri = (ep.get("uri") or "").strip()
            if uri:
                return uri
    return (event.get("hangoutLink") or event.get("htmlLink") or "").strip()


async def maybe_book_dariaan_discovery_meeting(
    role: str,
    lead_id: int,
    analysis: dict,
) -> Optional[dict[str, Any]]:
    """Book 30-min discovery on Google Calendar and WhatsApp the Meet link. Returns booking metadata."""

    # Role removed — function is now a no-op
    return None
    if not dariaan_meeting_booking_configured():
        logger.debug("Dariaan meeting booking skipped — not fully configured in .env")
        return None

    from services.call_analyzer import canonical_disposition

    if canonical_disposition(analysis.get("disposition")) != "Interested":
        return None

    from core.storage import get_lead, update_lead_status

    lead = await get_lead("vernikaai", lead_id)
    if not lead:
        logger.warning("Dariaan booking: lead {} not found", lead_id)
        return None

    phone = (lead.get("phone") or "").strip()
    wa_to = _norm_whatsapp_to(phone)
    if not wa_to:
        logger.warning("Dariaan booking: invalid phone for lead {}", lead_id)
        return None

    name = (lead.get("name") or "Founder").strip() or "Founder"
    email = (lead.get("email") or "").strip()
    tz = ZoneInfo(settings.transcript_callback_tz.strip() or "Asia/Kolkata")
    start = _parse_meeting_start(analysis, tz)
    minutes = max(15, int(settings.dariaan_discovery_meeting_minutes or 30))
    end = start + timedelta(minutes=minutes)

    summary = settings.dariaan_meeting_title.strip() or "Dariaan — 30-min Discovery Call"
    desc = (
        f"Discovery call with {name} (Meta ad enquiry).\n"
        f"Phone: {phone}\n"
        f"Lead id: {lead_id}\n\n"
        f"{analysis.get('summary') or ''}"
    ).strip()

    result: dict[str, Any] = {
        "booking_attempted": True,
        "meeting_start_iso": start.isoformat(),
        "whatsapp_to": wa_to,
    }

    try:
        token = await _google_access_token()
        event = await _create_calendar_event(
            access_token=token,
            summary=summary,
            description=desc,
            start=start,
            end=end,
            attendee_email=email or None,
            attendee_name=name,
        )
        meet_link = _extract_meet_link(event)
        event_link = (event.get("htmlLink") or "").strip()
        result["calendar_event_id"] = event.get("id")
        result["meet_link"] = meet_link
        result["calendar_html_link"] = event_link

        when_str = start.strftime("%a %d %b %Y, %I:%M %p %Z")
        msg = (
            f"Hi {name},\n\n"
            f"Thank you for your interest in *Dariaan* (fashion & retail accelerator).\n\n"
            f"Your *30-minute discovery call* is booked for:\n"
            f"📅 {when_str}\n\n"
        )
        if meet_link:
            msg += f"Join Google Meet:\n{meet_link}\n\n"
        elif event_link:
            msg += f"Calendar invite:\n{event_link}\n\n"
        msg += (
            "Profile & details: www.dariaan.in\n\n"
            "— Vernika, Dariaan"
        )

        wa_resp = await _send_whatsapp_text(wa_to, msg)
        result["whatsapp_message_id"] = (wa_resp.get("messages") or [{}])[0].get("id")
        result["whatsapp_sent"] = True
        logger.info(
            "Dariaan booking OK lead={} meet={} wa_to={}",
            lead_id,
            meet_link[:60] if meet_link else "n/a",
            wa_to,
        )
    except Exception as e:
        result["booking_error"] = str(e)
        result["whatsapp_sent"] = False
        logger.exception("Dariaan meeting booking failed for lead {}: {}", lead_id, e)

    merged = dict(analysis)
    merged["meeting_booking"] = result
    await update_lead_status(lead_id, status=lead.get("status") or "completed", analysis=merged)
    return result
