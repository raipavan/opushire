from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from core.auth import (
    console_session_meta,
    create_token,
    dashboard_role_for_token,
    get_current_user,
    limiter,
    verify_password,
)
from loguru import logger

router = APIRouter(tags=["auth"])

class LoginRequest(BaseModel):
    email: str
    password: str

@router.post("/api/login")
@limiter.limit("5/minute")
async def login(request: Request, data: LoginRequest):
    email = (data.email or "").strip().lower()
    logger.info(f"Login attempt for: {email}")
    role = verify_password(email, data.password)
    if not role:
        logger.warning(f"Invalid login for: {email}")
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token_data = create_token(email, role)
    dashboard_role = dashboard_role_for_token(email, role)
    return {
        "token": token_data["token"],
        "role": role,
        "dashboard_role": dashboard_role,
        "email": email,
        "locked": True,
    }


@router.get("/api/me")
async def session_me(user: dict = Depends(get_current_user)):
    """Tell the console which campaign dataset to load (authoritative; ignores stale ?role=)."""
    return console_session_meta(user.get("email"), user.get("role"))
