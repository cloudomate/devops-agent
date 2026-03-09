"""OIDC auth routes — login, callback, logout, me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..oidc import (
    _auth_enabled, build_auth_url, exchange_code, get_current_user,
    issue_session, verify_state,
)
from ...database import delete_session
from ...config import get_settings

router = APIRouter(prefix="/auth")


def _redirect_uri(request: Request) -> str:
    """Return the configured redirect URI, falling back to the request base URL.

    When the app runs behind a TLS-terminating reverse proxy the request
    arrives as plain HTTP, so request.base_url gives the wrong scheme.
    ENTRA_REDIRECT_URI should always be set explicitly in production.
    """
    configured = get_settings().entra_redirect_uri
    if configured and not configured.startswith("http://localhost"):
        return configured
    return str(request.base_url).rstrip("/") + "/auth/callback"


@router.get("/login")
async def login(request: Request):
    if not _auth_enabled():
        return RedirectResponse("/")
    auth_url, state_cookie = build_auth_url(_redirect_uri(request))
    resp = RedirectResponse(auth_url)
    resp.set_cookie("da_oidc_state", state_cookie, httponly=True, samesite="lax", max_age=600)
    return resp


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/?auth_error={error}")

    state_cookie = request.cookies.get("da_oidc_state", "")
    state_data = verify_state(state_cookie)
    if not state_data or state_data.get("nonce") != state:
        return RedirectResponse("/?auth_error=invalid_state")

    try:
        user = await exchange_code(code, state, state_data["cv"], _redirect_uri(request))
    except Exception as exc:
        return RedirectResponse(f"/?auth_error=token_exchange_failed")

    session_token = issue_session(user["id"])

    resp = RedirectResponse("/")
    resp.set_cookie(
        "da_session", session_token,
        httponly=True, samesite="lax",
        max_age=8 * 3600,
    )
    resp.delete_cookie("da_oidc_state")
    return resp


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("da_session")
    if token:
        delete_session(token)
    resp = RedirectResponse("/")
    resp.delete_cookie("da_session")
    return resp


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user["role"],
        "auth_enabled": _auth_enabled(),
    }
