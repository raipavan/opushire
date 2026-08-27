"""Vobiz REST dial, answer XML, and stream start metadata."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import httpx
from loguru import logger

from xml.sax.saxutils import escape

from .constants import VOBIZ_CONTENT_TYPE, VOBIZ_SR

async def close_vobiz_client() -> None:
    """No-op — each call now creates its own client.  Kept for lifespan API compatibility."""


def extract_vobiz_start_numbers(start: dict) -> tuple[str, str]:
    """Best-effort caller/callee numbers from Vobiz ``start`` JSON."""
    from_num, to_num = "", ""
    for k in (
        "From", "from", "callerId", "CallerId", "caller_id", "Caller",
        "caller", "remoteParty", "remoteIdentity", "fromNumber", "FromNumber",
        "CallerNumber", "callerNumber", "sipFrom", "SipFrom",
    ):
        v = start.get(k)
        if v is not None and str(v).strip():
            from_num = str(v).strip()
            break
    for k in (
        "To", "to", "called", "Called", "dialed", "Dialed", "toNumber", "ToNumber",
        "sipTo", "SipTo", "destination",
    ):
        v = start.get(k)
        if v is not None and str(v).strip():
            to_num = str(v).strip()
            break
    return from_num, to_num


def build_answer_xml(
    wss_stream_url: str,
    inbound: bool = False,
    status_callback_url: Optional[str] = None,
) -> str:
    del inbound  # routing is encoded in the WSS query string
    # ``&`` in query strings MUST be escaped in XML text — bare ``&manual_role`` breaks parsers and Vobiz never connects WS.
    # NOTE: audioTrack attribute removed - Vobiz docs state it should NOT be used with bidirectional="true"
    safe_url = escape(wss_stream_url, entities={'"': "&quot;", "'": "&apos;"})
    safe_status_url = ""
    if status_callback_url:
        safe_status_url = escape(status_callback_url, entities={'"': "&quot;", "'": "&apos;"})
    status_attr = f'statusCallbackUrl="{safe_status_url}" statusCallbackMethod="POST"' if safe_status_url else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Stream '
        'bidirectional="true" '
        'keepCallAlive="true" '
        f'contentType="{VOBIZ_CONTENT_TYPE};rate={VOBIZ_SR}" '
        'streamTimeout="3600"'
        f' {status_attr}'
        '>'
        f'{safe_url}'
        '</Stream>'
        '</Response>'
    )


class VobizCallError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any], message: Optional[str] = None):
        self.status = int(status)
        self.payload = payload or {}
        self.message = (message or self._derive_message()).strip()
        super().__init__(self.message)

    def _derive_message(self) -> str:
        p = self.payload or {}
        for key in ("error", "message", "detail", "reason", "raw"):
            v = p.get(key)
            if v:
                return str(v)
        return f"Vobiz HTTP {self.status}"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "message": self.message, "payload": self.payload}


async def make_vobiz_call(
    *,
    to: str,
    from_: str,
    answer_url: str,
    auth_id: str,
    auth_token: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    # Vobiz API expects E.164 WITHOUT the leading '+' for the 'from' field.
    # All documented examples show 'from': "14155551234" (no + prefix).
    # The '+' in the from number can cause carrier routing failures.
    cleaned_from = from_.lstrip("+") if from_ else from_
    # Vobiz enforces a max call duration via `time_limit` (seconds after answer).
    # If the Vobiz application/number has a low limit configured, calls are
    # auto-hung-up ("Scheduled Hangup"). Send a high per-call limit to override
    # it; the backend still caps calls via its own MAX_CALL_DURATION_SEC watchdog.
    call_time_limit = int(os.getenv("VOBIZ_CALL_TIME_LIMIT_SEC", "3600"))
    body: dict[str, Any] = {
        "from": cleaned_from,
        "to": to,
        "answer_url": answer_url,
        "answer_method": "POST",
        "time_limit": call_time_limit,
    }
    if extra:
        body.update(extra)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, json=body, headers=headers)
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = 1.5 * attempt
                logger.warning(
                    "Vobiz API connection attempt {} failed ({}), retrying in {:.1f}s",
                    attempt, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.error(
                "Vobiz API connection failed after {} attempts: {}",
                max_retries, exc,
            )
            raise
        try:
            data: dict[str, Any] = r.json()
        except Exception:
            data = {"raw": r.text}
        data["_http_status"] = r.status_code
        logger.info("Vobiz make_call {} -> HTTP {} {}", to, r.status_code, data)
        if r.status_code >= 400:
            raise VobizCallError(r.status_code, data)
        return data
