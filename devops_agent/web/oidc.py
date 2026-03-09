"""OIDC helpers for Microsoft Entra ID SSO and session management."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import Cookie, HTTPException, Request

logger = logging.getLogger(__name__)

from ..config import get_settings
from ..database import create_or_update_user, create_session, get_session


def _auth_enabled() -> bool:
    s = get_settings()
    return bool(s.entra_client_id and s.entra_tenant_id and s.entra_client_secret)


def _entra_base() -> str:
    return f"https://login.microsoftonline.com/{get_settings().entra_tenant_id}/oauth2/v2.0"


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification — token came directly from HTTPS token endpoint."""
    try:
        parts = token.split('.')
        padding = '=' * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception:
        return {}


def _generate_pkce() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return code_verifier, code_challenge


def sign_state(data: dict) -> str:
    settings = get_settings()
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    sig = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_state(value: str) -> dict | None:
    try:
        payload, sig = value.rsplit('.', 1)
        settings = get_settings()
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def build_auth_url(redirect_uri: str) -> tuple[str, str]:
    """Returns (auth_url, signed_state_cookie_value)."""
    settings = get_settings()
    code_verifier, code_challenge = _generate_pkce()
    state_nonce = secrets.token_urlsafe(16)

    state_cookie = sign_state({"nonce": state_nonce, "cv": code_verifier})

    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": settings.entra_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email https://graph.microsoft.com/User.Read",
        "state": state_nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return f"{_entra_base()}/authorize?{params}", state_cookie


async def _resolve_role_from_groups(
    claims: dict,
    access_token: str,
    settings,
) -> str | None:
    """
    Check if user belongs to entra_admin_group_id.
    Returns "admin" if matched, None otherwise.
    Handles the groups-overage case (_claim_names.groups present) via Graph API.
    """
    admin_gid = settings.entra_admin_group_id
    if not admin_gid:
        return None

    groups: list[str] | None = claims.get("groups")
    if groups is not None:
        if admin_gid in groups:
            logger.info("Group match via token claim → admin")
            return "admin"
        return None

    # Groups overage indicator: Entra sends _claim_names when user has >150 groups
    if "_claim_names" in claims and "groups" in claims.get("_claim_names", {}):
        logger.info("Groups overage detected — calling Graph API")
        graph_groups = await _fetch_group_ids_from_graph(access_token)
        if admin_gid in graph_groups:
            logger.info("Group match via Graph API → admin")
            return "admin"
        return None

    return None


async def _fetch_group_ids_from_graph(access_token: str) -> list[str]:
    """Call MS Graph /me/memberOf and return a flat list of group object IDs."""
    ids: list[str] = []
    url = "https://graph.microsoft.com/v1.0/me/memberOf?$select=id&$top=100"
    async with httpx.AsyncClient() as client:
        while url:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("Graph API memberOf failed: %s %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            ids.extend(obj.get("id", "") for obj in data.get("value", []))
            url = data.get("@odata.nextLink")
    return ids


async def exchange_code(code: str, state_nonce: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange auth code → tokens → create/update DB user → return user dict."""
    settings = get_settings()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_entra_base()}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.entra_client_id,
                "client_secret": settings.entra_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Token exchange failed: {resp.text}")
        tokens = resp.json()

    id_token = tokens.get("id_token", "")
    access_token = tokens.get("access_token", "")
    claims = _decode_jwt_payload(id_token)

    logger.info(
        "Login: username=%s app_roles=%s has_groups_claim=%s",
        claims.get("preferred_username", claims.get("email")),
        claims.get("roles"),
        "groups" in claims,
    )

    # 1. App role assignment (highest priority)
    entra_roles = claims.get("roles", [])
    if "Admin" in entra_roles:
        role = "admin"
    elif "DevOps" in entra_roles:
        role = "devops"
    else:
        # 2. Group membership (entra_admin_group_id)
        group_role = await _resolve_role_from_groups(claims, access_token, settings)
        role = group_role or "developer"

    logger.info("Assigned role: %s", role)

    user = create_or_update_user(
        entra_oid=claims.get("oid", claims.get("sub", "")),
        username=claims.get("preferred_username", claims.get("email", "unknown")),
        display_name=claims.get("name", ""),
        role=role,
    )
    return user


def issue_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires_at = (datetime.utcnow() + timedelta(hours=8)).isoformat()
    create_session(token, user_id, expires_at)
    return token


# ─── FastAPI dependency ───────────────────────────────────────────────────────

_DEV_USER = {"id": 0, "username": "admin@local", "display_name": "Local Admin", "role": "admin"}


async def get_current_user(request: Request) -> dict[str, Any]:
    if not _auth_enabled():
        return _DEV_USER

    token = request.cookies.get("da_session")
    if not token:
        raise HTTPException(401, "Not authenticated")
    session = get_session(token)
    if not session:
        raise HTTPException(401, "Session expired — please sign in again")
    return session


async def get_current_user_ws(request: Request, token: str = "") -> dict[str, Any] | None:
    """For WebSocket — reads cookie or ?token= query param."""
    if not _auth_enabled():
        return _DEV_USER
    raw = request.cookies.get("da_session") or token
    if not raw:
        return None
    return get_session(raw)
