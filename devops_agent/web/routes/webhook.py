"""GitHub webhook handler."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from ...agent import handle_webhook_event
from ...config import get_settings
from ...database import get_project_by_name, list_projects

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return not secret  # if no secret configured, allow all
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _find_project_by_repo(repo_full_name: str) -> dict[str, Any] | None:
    for p in list_projects():
        if p.get("github_repo") == repo_full_name:
            return p
    return None


async def _process_webhook(event: str, payload: dict[str, Any]) -> None:
    repo = payload.get("repository", {}).get("full_name", "")
    project = _find_project_by_repo(repo)
    project_hint = project["name"] if project else ""

    if event == "push":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        sha = payload.get("after", "")[:8]
        pusher = payload.get("pusher", {}).get("name", "unknown")
        summary = f"push to branch '{branch}' in {repo} — sha={sha} by {pusher}"

    elif event == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        merged = pr.get("merged", False)
        if action == "closed" and merged:
            base = pr.get("base", {}).get("ref", "")
            title = pr.get("title", "")
            author = pr.get("user", {}).get("login", "")
            pr_number = payload.get("number", "")
            summary = f"PR #{pr_number} merged into '{base}' in {repo} — '{title}' by {author}"
        else:
            return  # ignore non-merged PRs

    elif event == "issue_comment":
        comment = payload.get("comment", {}).get("body", "")
        if not comment.strip().startswith("/"):
            return  # only handle slash commands
        issue_number = payload.get("issue", {}).get("number", "")
        author = payload.get("comment", {}).get("user", {}).get("login", "")
        summary = f"slash command in {repo} issue/PR #{issue_number} by {author}: {comment.strip()}"

    else:
        logger.debug("Ignoring unhandled GitHub event: %s", event)
        return

    logger.info("Handling webhook: %s", summary)
    response = await handle_webhook_event(summary, project_hint=project_hint)
    logger.info("Agent response: %s", response[:500])


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
):
    settings = get_settings()
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = x_github_event or "unknown"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    background_tasks.add_task(_process_webhook, event, payload)
    return {"status": "accepted"}
