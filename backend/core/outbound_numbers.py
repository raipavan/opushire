"""Resolve outbound Vobiz ``from`` number per role without duplicating branching logic."""

from __future__ import annotations

import re
from typing import Mapping, Optional

from config import settings
from core.state import normalize_console_role


def _digits(s: object) -> str:
    """Keep only digits for CLI comparison."""

    return re.sub(r"\D", "", str(s or ""))


def _cli_same_number(a: str, b: str) -> bool:
    """Cheap E164-ish equality using last ten digits when both long enough."""

    da = _digits(a)
    db = _digits(b)
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    if da and db:
        return da == db
    return (str(a or "").strip() == str(b or "").strip())


def resolve_outbound_from_number(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> str:
    """Pick CLI: stored ``vobiz.from_number`` unless polluted; then per-role env; then global fallback."""

    vc = dict(vobiz_cfg or {})
    explicit = str(vc.get("from_number") or "").strip()

    r = normalize_console_role(role)

    fb_global = (settings.vobiz_from_number or "").strip()

    if explicit:
        return explicit

    if r == "data_edge":
        per_role_raw = settings.vobiz_data_edge_from_number
    else:
        per_role_raw = ""
    per_role = str(per_role_raw or "").strip()
    if per_role:
        return per_role
    return ""

