"""JWT-based authentication for production use."""

from __future__ import annotations

import os
import secrets
import time
import base64
import json
import hashlib
import hmac
from pathlib import Path
from typing import Optional

import jwt
import bcrypt
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address


def _resolve_jwt_secret() -> str:
    """Prefer ``JWT_SECRET_KEY`` env; otherwise persist ``backend/data/.jwt_secret``.
    Restarting systemd/uvicorn used to regenerate a secret from `time`+pid, invalidating every
    Bearer token (`Invalid or expired token` on `/api/manual/call`, etc.)."""
    raw = os.getenv("JWT_SECRET_KEY", "").strip()
    if raw:
        return raw

    secrets_path = Path(__file__).resolve().parent.parent / "data" / ".jwt_secret"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if secrets_path.is_file():
            txt = secrets_path.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        new_secret = secrets.token_hex(32)
        fd = os.open(secrets_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (new_secret + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        logger.warning(
            "JWT_SECRET_KEY unset — wrote persistent secret {} (restart-safe; "
            "set JWT_SECRET_KEY in .env if you replicate servers).",
            secrets_path,
        )
        return new_secret
    except FileExistsError:
        txt = secrets_path.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    except OSError as e:
        ephemeral = hashlib.sha256(f"VERN_JWT_EPHEMERAL-{time.time()}".encode()).hexdigest()
        logger.error(
            "JWT_SECRET_KEY unset and cannot write {}; using ephemeral JWT secret (tokens break on restart): {}",
            secrets_path,
            e,
        )
        return ephemeral

    ephemeral = hashlib.sha256(f"VERN_JWT_EPHEMERAL-{time.time()}".encode()).hexdigest()
    logger.error(
        ".jwt_secret empty after race — using ephemeral JWT secret; set JWT_SECRET_KEY or retry."
    )
    return ephemeral


SECRET_KEY = _resolve_jwt_secret()

ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24

security = HTTPBearer(auto_error=False)

# Simple user store — move to database in production
_VALID_USERS = {
    "dataedge@pitchxai.com": {
        "password_hash": b"$2b$12$aCjuSY0XjABSS4DP.pFC7.r5nPUqNmColm5VtyheOyb21QbS8P5Ia",
        "role": "data_edge",
    },
    "admin@procucev.com": {
        "password_hash": b"$2b$12$oWaM3kYTRT1ufyAFdEzJueCADOkg8zJKgqyKqXXqXsIasOEA9LH5u",
        "role": "admin",
    },
    "factory@procucev.com": {
        "password_hash": b"$2b$12$N6kT7BiCxvuYr12XxcMM0OoL2EZQse/g5.DzMlE3U0IiyJd5ZH7XC",
        "role": "factory",
    },
}

# Procucev Gmail that should see Sellers campaign data (not Data Edge counselor).
_DASHBOARD_SELLERS_EMAILS = frozenset()

# Always Data Edge counselor dashboard — never Buyers/Sellers/RFQ toggle or datasets.
_DATA_EDGE_LOGIN_EMAILS = frozenset(
    {
        "dataedge@pitchxai.com",
    }
)

_CONSOLE_LOCKED_ROLES = frozenset({"data_edge", "admin"})


def dashboard_role_for_token(email: str | None, jwt_role: str | None) -> str:
    """Canonical console dataset role for this login (JWT + email overrides)."""

    from core.state import normalize_console_role

    em = (email or "").strip().lower()
    if em in _DATA_EDGE_LOGIN_EMAILS:
        pass # return "data_edge"

    role = normalize_console_role(jwt_role or "data_edge")
    return role


def console_session_meta(email: str | None, jwt_role: str | None) -> dict[str, object]:
    """Payload for ``GET /api/me`` — single source of truth for the operator UI."""
    from core.state import normalize_console_role

    dashboard_role = dashboard_role_for_token(email, jwt_role)
    locked = dashboard_role in _CONSOLE_LOCKED_ROLES
    return {
        "email": (email or "").strip().lower(),
        "jwt_role": normalize_console_role(jwt_role or ""),
        "dashboard_role": dashboard_role,
        "locked": locked,
        "can_switch_roles": not locked and dashboard_role in _CONSOLE_SWITCHABLE_ROLES,
    }


# Console UI can switch among these datasets without re-login (authenticated only).
_CONSOLE_SWITCHABLE_ROLES = frozenset()


def jwt_payload_from_request(request: Request) -> dict | None:
    """Decode Bearer JWT or ``access_token`` / ``token`` query param."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload:
            return payload
    for key in ("access_token", "token"):
        raw = (request.query_params.get(key) or "").strip()
        if raw:
            payload = _decode_jwt(raw)
            if payload:
                return payload
    return None


def console_role_from_request(request: Request, *, default: str = "data_edge") -> str:
    """Resolve campaign/console role: always returns data_edge."""
    from core.state import normalize_console_role

    payload = jwt_payload_from_request(request)
    jwt_role: str | None = None
    if payload:
        jwt_role = dashboard_role_for_token(
            payload.get("email"),
            payload.get("role"),
        )

    if jwt_role:
        return jwt_role
    header_role = normalize_console_role(request.headers.get("X-User-Role") or "")
    if header_role:
        return header_role
    return normalize_console_role(default)


limiter = Limiter(key_func=get_remote_address)


def _encode_jwt(payload: dict) -> str:
    """Encode payload as a JWT using PyJWT."""
    payload["exp"] = int(time.time()) + (TOKEN_EXPIRY_HOURS * 3600)
    payload["iat"] = int(time.time())
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_jwt(token: str) -> Optional[dict]:
    """Decode and verify a JWT; fallback to legacy hand-rolled decoder for compatibility."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        pass

    # Fallback: legacy hand-rolled HMAC decoder
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        signature_input = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(
            SECRET_KEY.encode(), signature_input, hashlib.sha256
        ).digest()

        sig_padded = signature_b64 + "=" * (4 - len(signature_b64) % 4)
        provided_sig = base64.urlsafe_b64decode(sig_padded)

        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        payload_padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload_data = json.loads(base64.urlsafe_b64decode(payload_padded))

        if payload_data.get("exp", 0) < time.time():
            return None

        return payload_data
    except Exception:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = _decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {
        "email": payload.get("email"),
        "role": payload.get("role"),
    }


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Optional[dict]:
    """Optional auth — returns None if no token (for public endpoints)."""
    if not credentials:
        return None
    payload = _decode_jwt(credentials.credentials)
    if not payload:
        return None
    return {"email": payload.get("email"), "role": payload.get("role")}


def create_token(email: str, role: str) -> dict:
    """Create a JWT token and return it."""
    token = _encode_jwt({"email": email, "role": role})
    return {"token": token, "expires_in": TOKEN_EXPIRY_HOURS * 3600}


def verify_password(email: str, password: str) -> Optional[str]:
    """Verify user credentials and return role, or None if invalid."""
    user = _VALID_USERS.get(email.lower())
    if not user:
        return None
    stored_hash = user["password_hash"]
    if isinstance(stored_hash, str):
        stored_hash = stored_hash.encode()
    if bcrypt.checkpw(password.encode(), stored_hash):
        return user["role"]
    return None


def require_role(role: str):
    """Dependency factory that validates the JWT role matches the requested role."""
    async def _require_role(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict:
        if not credentials:
            raise HTTPException(status_code=401, detail="Not authenticated")
        payload = _decode_jwt(credentials.credentials)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        user_role = payload.get("role")
        if user_role != role:
            raise HTTPException(
                status_code=403, detail=f"Role '{role}' required"
            )
        return {
            "email": payload.get("email"),
            "role": user_role,
        }

    return _require_role
