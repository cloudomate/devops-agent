"""GitHub polling — periodically checks repos for new commits, tags, and merged PRs."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import get_settings
from .database import list_projects, update_project

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds between polls per project


def _gh_headers(token: str | None = None) -> dict:
    settings = get_settings()
    t = token or settings.github_token
    h = {"Accept": "application/vnd.github+json"}
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def _fetch(client: httpx.Client, url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = client.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        logger.debug("Poll fetch error %s: %s", url, exc)
    return None


async def _check_project(project: dict[str, Any]) -> None:
    repo = project.get("github_repo", "")
    if not repo:
        return

    token = project.get("github_token") or None
    headers = _gh_headers(token)
    base = f"https://api.github.com/repos/{repo}"
    name = project["name"]
    project_id = project["id"]
    known_sha = project.get("last_commit_sha")

    events: list[str] = []

    with httpx.Client(headers=headers) as client:
        # ── Latest commit on default branch ──────────────────────────────────
        commits = _fetch(client, f"{base}/commits", {"per_page": 1})
        if commits and isinstance(commits, list):
            latest_sha = commits[0].get("sha", "")
            short_sha = latest_sha[:8]
            if known_sha is None:
                # First poll — just record, don't fire
                update_project(project_id, last_commit_sha=latest_sha)
                return
            if latest_sha and latest_sha != known_sha:
                commit = commits[0].get("commit", {})
                author = commit.get("author", {}).get("name", "unknown")
                message = commit.get("message", "").split("\n")[0][:80]
                events.append(
                    f"new commit on {repo} default branch — "
                    f"sha={short_sha} by {author}: {message}"
                )
                update_project(project_id, last_commit_sha=latest_sha)

        # ── Recently merged PRs ───────────────────────────────────────────────
        prs = _fetch(client, f"{base}/pulls", {"state": "closed", "per_page": 5, "sort": "updated", "direction": "desc"})
        if prs and isinstance(prs, list):
            for pr in prs:
                if not pr.get("merged_at"):
                    continue
                # Only fire if merged after the commit we just saw (avoids duplicate on first run)
                if known_sha is None:
                    break
                pr_sha = pr.get("merge_commit_sha", "")
                if pr_sha == known_sha or not pr_sha:
                    continue
                # Check if this merge is the commit we just detected
                if events and pr_sha[:8] in events[0]:
                    base_branch = pr.get("base", {}).get("ref", "")
                    title = pr.get("title", "")
                    author = pr.get("user", {}).get("login", "")
                    pr_num = pr.get("number", "")
                    events[0] = (
                        f"PR #{pr_num} merged into '{base_branch}' on {repo} "
                        f"— '{title}' by {author}"
                    )
                    break

        # ── New tags / releases ───────────────────────────────────────────────
        tags = _fetch(client, f"{base}/tags", {"per_page": 1})
        if tags and isinstance(tags, list) and known_sha is not None:
            latest_tag = tags[0].get("name", "")
            tag_sha = tags[0].get("commit", {}).get("sha", "")
            if tag_sha and tag_sha == (commits[0].get("sha", "") if commits else ""):
                events.append(f"new tag '{latest_tag}' published on {repo}")

    for summary in events:
        logger.info("Poll detected: %s", summary)
        # Don't invoke the agent for poll events — the local model hallucinates
        # tool results. Events are logged; a future task can act on them.


async def poll_loop() -> None:
    """Background loop: poll all projects every POLL_INTERVAL seconds."""
    logger.info("GitHub poller started (interval=%ds)", POLL_INTERVAL)
    while True:
        projects = list_projects()
        for project in projects:
            if project.get("github_repo"):
                try:
                    await _check_project(project)
                except Exception as exc:
                    logger.error("Poller error for %s: %s", project.get("name"), exc)
        await asyncio.sleep(POLL_INTERVAL)
