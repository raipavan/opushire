"""Resolve Vobiz auth + CLI per console role (env overrides stale DB for dedicated trunks)."""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from config import settings
from core.outbound_numbers import resolve_outbound_from_number
from core.state import normalize_console_role


def _data_edge_env_configured() -> bool:
    return bool(
        (settings.vobiz_data_edge_auth_id or "").strip()
        and (settings.vobiz_data_edge_auth_token or "").strip()
        and (settings.vobiz_data_edge_from_number or "").strip()
    )


def resolve_vobiz_credentials(
    role: str,
    vobiz_cfg: Optional[Mapping[str, object]] = None,
) -> Tuple[str, str, str, str]:
    """
    Return (auth_id, auth_token, from_number, public_url) for outbound dial.
    """
    r = normalize_console_role(role)
    vc = dict(vobiz_cfg or {})

    public_url = (
        str(vc.get("public_url") or settings.vobiz_public_base_url or "")
        .strip()
        .rstrip("/")
    )

    if r == "data_edge" and _data_edge_env_configured():
        return (
            settings.vobiz_data_edge_auth_id.strip(),
            settings.vobiz_data_edge_auth_token.strip(),
            settings.vobiz_data_edge_from_number.strip(),
            public_url,
        )

    # Dedicated role must NOT fall back to the global fallback account.
    if r == "data_edge":
        return "", "", "", public_url

    auth_id = str(vc.get("auth_id") or settings.vobiz_auth_id or "").strip()
    auth_token = str(vc.get("auth_token") or settings.vobiz_auth_token or "").strip()
    from_number = resolve_outbound_from_number(role, vc)
    return auth_id, auth_token, from_number, public_url
