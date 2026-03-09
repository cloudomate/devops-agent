"""REST API for the UI sidebar data and configuration CRUD."""
from __future__ import annotations

import json
import os
import re

_GLOBAL_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import apply_db_overrides, get_settings
from ...database import (
    GLOBAL_PROJECT_NAME,
    add_project_member,
    count_deployment_requests,
    delete_system_setting,
    get_all_system_settings,
    get_session_history,
    create_project,
    delete_environment,
    delete_project,
    get_deployment_request,
    get_environment,
    get_or_create_global_project,
    get_project_by_name,
    get_user_by_id,
    is_project_member,
    list_deployment_requests,
    list_deployments,
    list_environments,
    list_environments_with_global,
    list_global_environments,
    list_project_members,
    list_projects,
    list_projects_for_user,
    list_users,
    remove_project_member,
    set_system_settings,
    update_deployment_request,
    update_project,
    update_user_role,
    upsert_environment,
)
from ..oidc import get_current_user

router = APIRouter(prefix="/api")


# ─── Projects (read) ──────────────────────────────────────────────────────────

@router.get("/projects")
def get_projects(user: dict = Depends(get_current_user)):
    return list_projects_for_user(user["id"], user["role"])


@router.get("/projects/{project_name}/environments")
def get_environments(project_name: str, user: dict = Depends(get_current_user)):
    project = get_project_by_name(project_name)
    if not project:
        return []
    return list_environments_with_global(project["id"])


@router.get("/deployments")
def get_deployments(limit: int = 20, user: dict = Depends(get_current_user)):
    if user["role"] in ("admin", "devops"):
        return list_deployments(limit=limit)
    # Developers: only deployments for their accessible projects
    projects = list_projects_for_user(user["id"], user["role"])
    rows = []
    for p in projects:
        rows.extend(list_deployments(p["id"], limit=limit))
    rows.sort(key=lambda d: d.get("started_at", ""), reverse=True)
    return rows[:limit]


@router.get("/deployments/{project_name}")
def get_project_deployments(project_name: str, limit: int = 20):
    project = get_project_by_name(project_name)
    if not project:
        return []
    return list_deployments(project["id"], limit=limit)


# ─── Projects (write) ─────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    github_repo: str = ""
    description: str = ""
    github_token: str = ""


class ProjectUpdate(BaseModel):
    github_repo: str | None = None
    description: str | None = None
    github_token: str | None = None  # empty string clears the token


@router.post("/projects", status_code=201)
def post_project(body: ProjectCreate, user: dict = Depends(get_current_user)):
    _require_devops(user)
    if get_project_by_name(body.name):
        raise HTTPException(400, f"Project '{body.name}' already exists")
    return create_project(body.name, body.github_repo, body.description, body.github_token)


@router.patch("/projects/{project_name}")
def patch_project(project_name: str, body: ProjectUpdate, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    # Include fields that were explicitly set (not None means field was provided)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    # Allow clearing github_token by sending empty string
    if body.github_token == "":
        kwargs["github_token"] = None
    update_project(project["id"], **kwargs)
    return get_project_by_name(project_name)


@router.delete("/projects/{project_name}", status_code=204)
def del_project(project_name: str, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    delete_project(project["id"])


# ─── Environments (write) ─────────────────────────────────────────────────────

class EnvUpsert(BaseModel):
    name: str
    type: str                    # 'kubernetes' | 'ssh'
    config: dict                 # type-specific fields
    health_check_url: str = ""


@router.post("/projects/{project_name}/environments", status_code=201)
def post_environment(project_name: str, body: EnvUpsert, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    return upsert_environment(
        project["id"], body.name, body.type, body.config,
        body.health_check_url or None,
    )


@router.put("/projects/{project_name}/environments/{env_name}")
def put_environment(project_name: str, env_name: str, body: EnvUpsert, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    return upsert_environment(
        project["id"], body.name, body.type, body.config,
        body.health_check_url or None,
    )


@router.delete("/projects/{project_name}/environments/{env_name}", status_code=204)
def del_environment(project_name: str, env_name: str, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    if not get_environment(project["id"], env_name):
        raise HTTPException(404, "Environment not found")
    delete_environment(project["id"], env_name)


# ─── Global environments ──────────────────────────────────────────────────────

@router.get("/environments/global")
def get_global_environments(user: dict = Depends(get_current_user)):
    return list_global_environments()


@router.post("/environments/global", status_code=201)
def post_global_environment(body: EnvUpsert, user: dict = Depends(get_current_user)):
    _require_devops(user)
    global_proj = get_or_create_global_project()
    return upsert_environment(
        global_proj["id"], body.name, body.type, body.config,
        body.health_check_url or None,
    )


@router.put("/environments/global/{env_name}")
def put_global_environment(env_name: str, body: EnvUpsert, user: dict = Depends(get_current_user)):
    _require_devops(user)
    global_proj = get_or_create_global_project()
    return upsert_environment(
        global_proj["id"], body.name, body.type, body.config,
        body.health_check_url or None,
    )


@router.delete("/environments/global/{env_name}", status_code=204)
def del_global_environment(env_name: str, user: dict = Depends(get_current_user)):
    _require_devops(user)
    global_proj = get_or_create_global_project()
    if not get_environment(global_proj["id"], env_name):
        raise HTTPException(404, "Global environment not found")
    delete_environment(global_proj["id"], env_name)


# ─── Repo value discovery ──────────────────────────────────────────────────────

_CANDIDATE_PATHS = [
    "values.yaml",
    "values.yml",
    "deploy/values.yaml",
    "deploy/values.yml",
    "charts/values.yaml",
    "helm/values.yaml",
    ".env.example",
    ".env.sample",
    ".env.template",
]

def _gh_raw(repo: str, branch: str, path: str, token: str | None) -> str | None:
    headers = {"Accept": "application/vnd.github.raw+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{repo}/contents/{path}",
            headers=headers, params={"ref": branch}, timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("encoding") == "base64":
                import base64
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return r.text
    except Exception:
        pass
    return None

def _parse_values_yaml(text: str) -> dict[str, str]:
    """Extract top-level scalar keys from a YAML file (no dep on PyYAML)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^([A-Za-z][A-Za-z0-9_\-]*):\s*(.*)', line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip('"\'')
            # Skip nested blocks (next char would be indented) and comments
            if val and not val.startswith('#') and not val.startswith('{') and not val.startswith('['):
                values[key] = val
    return values

def _parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip('"\'')
    return values

@router.get("/projects/{project_name}/repo-values")
def get_repo_values(project_name: str, branch: str = "main"):
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = project.get("github_repo", "")
    if not repo:
        raise HTTPException(400, "Project has no github_repo configured")
    token = project.get("github_token") or _GLOBAL_GITHUB_TOKEN

    found: dict[str, str] = {}
    sources: list[str] = []

    for path in _CANDIDATE_PATHS:
        text = _gh_raw(repo, branch, path, token)
        if text is None:
            continue
        if path.endswith((".yaml", ".yml")):
            parsed = _parse_values_yaml(text)
        else:
            parsed = _parse_dotenv(text)
        if parsed:
            # Don't overwrite already-found keys — first file wins
            for k, v in parsed.items():
                if k not in found:
                    found[k] = v
            sources.append(path)

    return {"values": found, "sources": sources}


# ─── Auth guards ──────────────────────────────────────────────────────────────

def _require_devops(user: dict) -> None:
    if user["role"] not in ("admin", "devops"):
        raise HTTPException(403, "DevOps or Admin role required")


def _require_project_access(user: dict, project: dict) -> None:
    if user["role"] in ("admin", "devops"):
        return
    if not is_project_member(project["id"], user["id"]):
        raise HTTPException(403, "You don't have access to this project")


# ─── Admin: system settings ───────────────────────────────────────────────────

# Settings that are sensitive — values are masked on GET
_SENSITIVE = {"LLM_API_KEY", "ENTRA_CLIENT_SECRET", "ARGOCD_TOKEN", "GITOPS_TOKEN", "GITHUB_TOKEN"}
_ALLOWED_KEYS = {
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET",
    "ENTRA_REDIRECT_URI", "ENTRA_ADMIN_GROUP_ID",
    "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET",
    "ARGOCD_URL", "ARGOCD_TOKEN",
    "GITOPS_REPO", "GITOPS_TOKEN", "GITOPS_BRANCH",
}


@router.get("/admin/settings")
def get_admin_settings(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    s = get_settings()
    db = get_all_system_settings()
    result = {}
    for key in _ALLOWED_KEYS:
        # DB value takes precedence; fall back to env-var default
        val = db.get(key) or getattr(s, key.lower(), "")
        result[key] = "••••••••" if (key in _SENSITIVE and val) else (val or "")
    return result


class AdminSettingsUpdate(BaseModel):
    settings: dict[str, str]


@router.put("/admin/settings")
def put_admin_settings(body: AdminSettingsUpdate, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    to_save = {}
    for key, val in body.settings.items():
        key = key.upper()
        if key not in _ALLOWED_KEYS:
            continue
        if val == "••••••••":
            continue  # placeholder — user didn't change it
        if val:
            to_save[key] = val
        else:
            delete_system_setting(key)  # clear override → fall back to env var
    if to_save:
        set_system_settings(to_save)
    # Reload settings singleton with new DB overrides
    apply_db_overrides(get_all_system_settings())
    return {"ok": True, "updated": list(to_save.keys())}


# ─── User management ──────────────────────────────────────────────────────────

@router.get("/users")
def get_users(user: dict = Depends(get_current_user)):
    _require_devops(user)
    return list_users()


class UserRoleUpdate(BaseModel):
    role: str


@router.patch("/users/{user_id}/role")
def patch_user_role(user_id: int, body: UserRoleUpdate, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    if body.role not in ("admin", "devops", "developer"):
        raise HTTPException(400, "Invalid role")
    update_user_role(user_id, body.role)
    return {"ok": True}


@router.delete("/users/{user_id}", status_code=204)
def delete_user_route(user_id: int, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete yourself")
    from ...database import get_user_by_id as _get
    if not _get(user_id):
        raise HTTPException(404, "User not found")
    from ...database import _conn
    with _conn() as con:
        con.execute("DELETE FROM users WHERE id=?", (user_id,))


# ─── Project member management ────────────────────────────────────────────────

@router.get("/projects/{project_name}/members")
def get_project_members(project_name: str, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    return list_project_members(project["id"])


class MemberAdd(BaseModel):
    user_id: int


@router.post("/projects/{project_name}/members", status_code=201)
def add_member(project_name: str, body: MemberAdd, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    target = get_user_by_id(body.user_id)
    if not target:
        raise HTTPException(404, "User not found")
    add_project_member(project["id"], body.user_id)
    return {"ok": True}


@router.delete("/projects/{project_name}/members/{user_id}", status_code=204)
def remove_member(project_name: str, user_id: int, user: dict = Depends(get_current_user)):
    _require_devops(user)
    project = get_project_by_name(project_name)
    if not project:
        raise HTTPException(404, "Project not found")
    remove_project_member(project["id"], user_id)


# ─── Deployment requests ──────────────────────────────────────────────────────

@router.get("/deployment-requests")
def get_deployment_requests(
    status: str | None = None,
    project: str | None = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    project_id = None
    if project:
        p = get_project_by_name(project)
        if p:
            project_id = p["id"]
    # Developers only see their own requests; devops/admin see all
    requested_by = None
    if user["role"] == "developer":
        requested_by = user["id"]
    return list_deployment_requests(status=status, project_id=project_id, requested_by=requested_by, limit=limit)


@router.get("/deployment-requests/count")
def get_deployment_requests_count(user: dict = Depends(get_current_user)):
    """Badge count of pending_review requests (devops/admin only)."""
    if user["role"] not in ("admin", "devops"):
        return {"count": 0}
    return {"count": count_deployment_requests("pending_review")}


@router.get("/deployment-requests/{request_id}")
def get_deployment_request_detail(request_id: int, user: dict = Depends(get_current_user)):
    req = get_deployment_request(request_id)
    if not req:
        raise HTTPException(404, "Deployment request not found")
    # Developers can only see their own requests
    if user["role"] == "developer" and req.get("requested_by") != user["id"]:
        raise HTTPException(403, "Access denied")
    return req


class DeploymentRequestPatch(BaseModel):
    status: str | None = None
    plan_markdown: str | None = None
    reject_reason: str | None = None


@router.patch("/deployment-requests/{request_id}")
def patch_deployment_request(
    request_id: int,
    body: DeploymentRequestPatch,
    user: dict = Depends(get_current_user),
):
    """Safety-hatch endpoint for UI-driven status updates (devops/admin only)."""
    _require_devops(user)
    req = get_deployment_request(request_id)
    if not req:
        raise HTTPException(404, "Deployment request not found")
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if kwargs:
        kwargs["reviewed_by"] = user["id"]
        update_deployment_request(request_id, **kwargs)
    return get_deployment_request(request_id)


# ─── Chat history ─────────────────────────────────────────────────────────────

@router.get("/chat-history/{session_id}")
async def get_chat_history(session_id: str, user=Depends(get_current_user)):
    """Return chat history for a session so the frontend can restore it on project switch."""
    msgs = get_session_history(session_id, limit=100)
    # Strip internal tool-call markers from assistant messages before sending to UI
    import re as _re
    _tool_re = _re.compile(r"\n\n⚙️ \*[^*]+\*\n```[\s\S]*?```\n?")
    cleaned = []
    for m in msgs:
        content = m["content"]
        if m["role"] == "assistant":
            content = _tool_re.sub("", content).strip()
        if content:
            cleaned.append({"role": m["role"], "content": content})
    return cleaned
