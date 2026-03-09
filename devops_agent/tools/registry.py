"""Tool definitions (OpenAI function-calling format) and dispatcher."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx

_GLOBAL_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None

from .security import post_deploy_security_check, pre_deploy_security_check, security_review_repo
from .discovery import discover_repo, clone_repo, all_files, find_files, grep_files, read_file, rel_path

# Session-level cache: github_repo → cloned Path (cleaned up on process exit)
_repo_cache: dict[str, Path] = {}


def _ensure_repo(project: dict[str, Any]) -> Path:
    """Return path to a shallow clone of the project's repo, cloning if not cached."""
    github_repo = project.get("github_repo") or ""
    if not github_repo:
        raise ValueError(f"Project '{project['name']}' has no github_repo configured.")
    if github_repo in _repo_cache and _repo_cache[github_repo].exists():
        return _repo_cache[github_repo]
    token = project.get("github_token") or _GLOBAL_GITHUB_TOKEN
    path, result = clone_repo(github_repo, token=token)
    if path is None:
        raise RuntimeError(f"Could not clone {github_repo}: {result}")
    _repo_cache[github_repo] = path
    return path
from ..database import (
    GLOBAL_PROJECT_NAME,
    create_deployment_request, create_project, delete_environment, delete_project,
    finish_deployment, get_deployment_request, get_environment, get_or_create_global_project,
    get_project_by_name,
    list_deployment_requests, list_deployments, list_environments, list_projects,
    start_deployment, update_deployment_request, update_environment_state,
    update_project, upsert_environment,
)
from ..deployers.factory import make_deployer
from ..config import get_settings
from .argocd import (
    ArgoCDClient, gitops_deploy, push_helm_chart,
    push_shared_chart, push_project_values, generate_cicd_workflow,
    validate_helm_chart,
)


def _argocd_client(env_values: dict | None = None) -> ArgoCDClient | None:
    """Return an ArgoCD client, preferring env-level overrides over global settings."""
    s = get_settings()
    url   = (env_values or {}).get("ARGOCD_URL")   or s.argocd_url
    token = (env_values or {}).get("ARGOCD_TOKEN") or s.argocd_token
    if url and token:
        return ArgoCDClient(url, token)
    return None


def _gitops_token(env_values: dict | None = None) -> str:
    """Return the GitOps repo token, preferring env-level overrides."""
    s = get_settings()
    return (
        (env_values or {}).get("GITOPS_TOKEN")
        or s.gitops_token
        or s.github_token
        or _GLOBAL_GITHUB_TOKEN
        or ""
    )


def _gitops_repo(env_values: dict | None = None) -> str:
    """Return the GitOps repo slug, preferring env-level overrides."""
    s = get_settings()
    return (env_values or {}).get("GITOPS_REPO") or s.gitops_repo or ""


def _gitops_branch(env_values: dict | None = None) -> str:
    s = get_settings()
    return (env_values or {}).get("GITOPS_BRANCH") or s.gitops_branch or "main"


def _is_gitops_enabled(env_values: dict | None = None) -> bool:
    return bool(_gitops_repo(env_values) and _gitops_token(env_values))


# ─── Tool definitions ─────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    # Project management
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List all configured projects managed by this DevOps agent.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new project to manage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short slug identifier, e.g. 'my-app'"},
                    "github_repo": {"type": "string", "description": "GitHub repo in 'owner/repo' format"},
                    "description": {"type": "string", "description": "Human-readable description"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": "Update a project's metadata (name, github_repo, description).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "github_repo": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_project",
            "description": "Delete a project and all its environments.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        },
    },
    # Environment management
    {
        "type": "function",
        "function": {
            "name": "list_environments",
            "description": "List all environments for a project with their current deployment status.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_global_environment",
            "description": (
                "Create or update a GLOBAL environment (fallback for all projects). "
                "Use this to configure shared DevOps credentials: GitOps repo, ArgoCD, "
                "container registry, GitHub token, common DB services, and Cloudflare tunnel. "
                "Values are stored under config.values.* (e.g. GITOPS_REPO, ARGOCD_URL). "
                "These settings are inherited by all projects unless overridden at project level."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Environment name, e.g. 'prod', 'staging'"},
                    "type": {"type": "string", "enum": ["kubernetes", "ssh", "docker_compose"], "description": "Default: kubernetes"},
                    "values": {
                        "type": "object",
                        "description": (
                            "Credential and infra values stored as config.values.*. "
                            "Keys: GITHUB_TOKEN, GITOPS_REPO, GITOPS_BRANCH, GITOPS_TOKEN, "
                            "ARGOCD_URL, ARGOCD_TOKEN, REGISTRY, "
                            "POSTGRES_URL, REDIS_URL, MONGO_URL"
                        ),
                        "properties": {
                            "GITHUB_TOKEN": {"type": "string"},
                            "GITOPS_REPO": {"type": "string", "description": "owner/repo"},
                            "GITOPS_BRANCH": {"type": "string", "description": "default: main"},
                            "GITOPS_TOKEN": {"type": "string"},
                            "ARGOCD_URL": {"type": "string"},
                            "ARGOCD_TOKEN": {"type": "string"},
                            "REGISTRY": {"type": "string", "description": "e.g. cr.imys.in/hci or ghcr.io/myorg"},
                            "POSTGRES_URL": {"type": "string"},
                            "REDIS_URL": {"type": "string"},
                            "MONGO_URL": {"type": "string"},
                        },
                    },
                    "cloudflare": {
                        "type": "object",
                        "description": "Optional Cloudflare tunnel config",
                        "properties": {
                            "tunnel_enabled": {"type": "boolean"},
                            "api_token": {"type": "string"},
                            "zone_id": {"type": "string"},
                            "account_id": {"type": "string"},
                            "tunnel_id": {"type": "string"},
                        },
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_environment",
            "description": (
                "Add or update a project-specific environment override. "
                "Same config structure as global environments — set only the keys that differ "
                "from the global environment (e.g. a project-specific GITOPS_TOKEN, different REGISTRY, "
                "or project-specific DB URLs). Unset keys fall back to the global environment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string", "description": "e.g. staging, prod"},
                    "type": {"type": "string", "enum": ["kubernetes", "ssh", "docker_compose"], "description": "Default: kubernetes"},
                    "values": {
                        "type": "object",
                        "description": (
                            "Project-specific overrides for config.values.*. "
                            "Keys: GITHUB_TOKEN, GITOPS_REPO, GITOPS_BRANCH, GITOPS_TOKEN, "
                            "ARGOCD_URL, ARGOCD_TOKEN, REGISTRY, "
                            "POSTGRES_URL, REDIS_URL, MONGO_URL"
                        ),
                        "properties": {
                            "GITHUB_TOKEN": {"type": "string"},
                            "GITOPS_REPO": {"type": "string"},
                            "GITOPS_BRANCH": {"type": "string"},
                            "GITOPS_TOKEN": {"type": "string"},
                            "ARGOCD_URL": {"type": "string"},
                            "ARGOCD_TOKEN": {"type": "string"},
                            "REGISTRY": {"type": "string"},
                            "POSTGRES_URL": {"type": "string"},
                            "REDIS_URL": {"type": "string"},
                            "MONGO_URL": {"type": "string"},
                        },
                    },
                    "cloudflare": {
                        "type": "object",
                        "description": "Optional Cloudflare tunnel config override",
                        "properties": {
                            "tunnel_enabled": {"type": "boolean"},
                            "api_token": {"type": "string"},
                            "zone_id": {"type": "string"},
                            "account_id": {"type": "string"},
                            "tunnel_id": {"type": "string"},
                        },
                    },
                    "health_check_url": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_environment",
            "description": "Remove an environment from a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    # Deployment operations
    {
        "type": "function",
        "function": {
            "name": "deploy",
            "description": "Deploy a Docker image or git ref to a project environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                    "image_or_ref": {"type": "string", "description": "Docker image:tag or git SHA/branch"},
                    "reason": {"type": "string"},
                },
                "required": ["project", "environment", "image_or_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback",
            "description": "Roll back an environment to its previous deployment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Fetch recent logs from an environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                    "lines": {"type": "integer", "default": 100},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deployment_status",
            "description": "Get live runtime status of an environment (replica counts, running state).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_health_check",
            "description": "Hit the health-check URL of an environment and report the HTTP status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    # Repository discovery
    {
        "type": "function",
        "function": {
            "name": "discover_repo",
            "description": (
                "Analyze a GitHub repository and return a structured deployment brief: "
                "language/stack, Dockerfile details, Docker Compose services, Kubernetes/Helm manifests, "
                "required env vars, CI/CD workflows, and recommended deployment options. "
                "Run this first when a user wants to deploy a project or asks 'how should I deploy this?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (to resolve github_repo automatically)"},
                    "branch": {"type": "string", "description": "Branch to analyze (default: main)"},
                },
                "required": ["project"],
            },
        },
    },
    # Security tools
    {
        "type": "function",
        "function": {
            "name": "security_review_repo",
            "description": (
                "Scan a GitHub repository for hardcoded secrets, credentials, and dangerous "
                "code patterns (eval, shell injection, unsafe deserialization, etc.). "
                "Use this before deploying or when asked for a security review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name (to resolve github_repo automatically)"},
                    "branch": {"type": "string", "default": "main", "description": "Branch to scan"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pre_deploy_security_check",
            "description": (
                "Run pre-deployment security checks: secrets scan, committed .env detection, "
                "Dockerfile best-practice audit, and CVE scan (if trivy is available). "
                "Call this before every production deployment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                    "image_or_ref": {"type": "string", "description": "Docker image:tag or git ref being deployed"},
                    "branch": {"type": "string", "default": "main"},
                },
                "required": ["project", "environment", "image_or_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_deploy_security_check",
            "description": (
                "Run post-deployment security checks against the live service: "
                "HTTP reachability, security headers audit, sensitive path enumeration, "
                "and TLS certificate validity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_deployments",
            "description": "List recent deployment history, optionally filtered by project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Leave empty to see all projects"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        },
    },
    # Deployment request workflow (planning → review → execution)
    {
        "type": "function",
        "function": {
            "name": "submit_deployment_request",
            "description": (
                "Submit a deployment plan for DevOps review. Use this instead of deploy when acting as a developer. "
                "After gathering info from discover_repo, asking the user clarifying questions, and getting their "
                "confirmation, generate a clear Markdown plan and submit it here. DevOps will review and approve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "environment": {"type": "string"},
                    "image_or_ref": {"type": "string", "description": "Docker image:tag or git ref to deploy"},
                    "plan_markdown": {
                        "type": "string",
                        "description": (
                            "Human-readable Markdown deployment plan. Include: what's being deployed, "
                            "why, environment, key config changes, rollback plan."
                        ),
                    },
                    "plan_config": {
                        "type": "object",
                        "description": "Machine-readable deployment parameters (env vars, replicas, etc.)",
                    },
                    "session_id": {"type": "string", "description": "Current chat session ID"},
                },
                "required": ["project", "environment", "image_or_ref", "plan_markdown", "plan_config", "session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_deployment_requests",
            "description": "List deployment requests pending review or recently actioned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending_review", "approved", "rejected", "executing", "done", "failed"],
                        "description": "Filter by status. Omit to see all.",
                    },
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deployment_request",
            "description": "Get full details of a specific deployment request including plan and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "integer"},
                },
                "required": ["request_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_deployment_request",
            "description": (
                "Approve a pending deployment request and immediately execute the deployment. "
                "Only DevOps/admin users can approve. Optionally update the plan before approving."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "integer"},
                    "plan_markdown": {"type": "string", "description": "Updated plan (optional — keeps original if omitted)"},
                    "plan_config": {"type": "object", "description": "Updated config (optional)"},
                    "image_or_ref": {"type": "string", "description": "Override image/ref (optional)"},
                },
                "required": ["request_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject_deployment_request",
            "description": "Reject a pending deployment request with a reason. Only DevOps/admin users can reject.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "integer"},
                    "reason": {"type": "string", "description": "Why the request was rejected"},
                },
                "required": ["request_id", "reason"],
            },
        },
    },
    # Repo exploration tools (Claude Code-style, operate on a cached local clone)
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find files in the project's repository matching a glob pattern. "
                "The repo is cloned locally on first call and cached for subsequent calls. "
                "Use to explore structure: e.g. '**/*.py', 'src/**/*.ts', 'Dockerfile*', '*.yml'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'"},
                },
                "required": ["project", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": (
                "Search file contents in the project's repository using a regex pattern. "
                "Returns matching lines with file path and line number. "
                "Use to find env var usage, port definitions, config keys, imports, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "include": {"type": "string", "description": "File glob filter, e.g. '*.py' (default: all files)"},
                    "max_results": {"type": "integer", "description": "Max matches to return (default: 30)"},
                },
                "required": ["project", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a specific file from the project's repository. "
                "Use after glob_files to read a Dockerfile, config, source file, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                    "path": {"type": "string", "description": "Relative file path within the repo, e.g. 'src/main.rs'"},
                    "max_bytes": {"type": "integer", "description": "Max bytes to read (default: 32000)"},
                },
                "required": ["project", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": (
                "Run a shell command inside the project's cloned repository directory. "
                "Use for read-only exploration: git log, find, wc -l, ls, head, etc. "
                "Commands run inside the repo root. Timeout: 30 seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                    "command": {"type": "string", "description": "Shell command to run, e.g. 'git log --oneline -10'"},
                },
                "required": ["project", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "infra_setup_guide",
            "description": (
                "Generate a complete, platform-specific infrastructure setup guide for a project environment. "
                "Based on the environment config (platform, ingress_class, domain, tls, lb_type, etc.), "
                "produces step-by-step kubectl/Helm commands, Ingress manifests, DNS records, "
                "cert-manager ClusterIssuer YAML, MetalLB/ALB/AGIC setup, and CI/CD integration snippets. "
                "Call this after upsert_environment to give the DevOps team actionable setup instructions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                    "environment": {"type": "string", "description": "Environment name (e.g. prod, staging)"},
                },
                "required": ["project", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_helm_chart",
            "description": (
                "Validate and push a project-specific Helm chart to the GitOps repo. "
                "The agent generates chart file contents (Chart.yaml, values.yaml, "
                "templates/deployment.yaml, templates/service.yaml, templates/ingress.yaml, "
                "templates/secret.yaml, etc.) based on the app's discovered stack, then calls "
                "this tool to lint-validate and commit them. Must be called before approving "
                "a deployment request when no chart exists yet for the project. "
                "Paths in 'files' should be relative to charts/{project_slug}/ (e.g. 'Chart.yaml', "
                "'templates/deployment.yaml') or full repo paths ('charts/my-app/Chart.yaml')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string", "description": "Project slug (e.g. 'my-app')"},
                    "files": {
                        "type": "object",
                        "description": "Dict of {relative_path: file_content_string} for all chart files",
                        "additionalProperties": {"type": "string"},
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite chart if it already exists (default false)",
                    },
                },
                "required": ["project_slug", "files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_argocd_app",
            "description": (
                "Create or update an ArgoCD Application for a project and trigger a sync. "
                "Call this after push_helm_chart to wire the Helm chart to ArgoCD so the "
                "cluster stays in sync with the GitOps repo automatically. "
                "Also pushes the ArgoCD Application YAML to the GitOps repo under argocd/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string", "description": "Project slug (must match the chart path in the GitOps repo)"},
                    "environment": {"type": "string", "description": "Target environment name, e.g. 'prod' or 'staging'"},
                    "namespace": {"type": "string", "description": "Kubernetes namespace to deploy into (default: project_slug)"},
                },
                "required": ["project_slug", "environment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_shared_chart",
            "description": (
                "Ensure the shared generic Helm chart (charts/app/) exists in the GitOps repo. "
                "This chart is used by ALL projects — push it once when setting up a new GitOps repo. "
                "Idempotent: skips if already present unless force=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force": {"type": "boolean", "description": "Overwrite existing chart (default false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_project_values",
            "description": (
                "Generate and push projects/<project>/values-<env>.yaml for one or more environments. "
                "This is the per-project GitOps configuration — each env gets its own values file "
                "with image, replicas, domain, env vars, secrets, resources, and health check. "
                "Call this after push_shared_chart for each new project being onboarded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string", "description": "Project slug (e.g. 'sre-agent')"},
                    "environments": {
                        "type": "object",
                        "description": (
                            "Dict of {env_name: config}. Each config: "
                            "{image, port, domain, replicas, env_vars, secrets, health_path, "
                            "ingress_class, resources, autoscaling}"
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "image": {"type": "string", "description": "Full image ref e.g. cr.imys.in/hci/sre-agent:latest"},
                                "port": {"type": "integer", "description": "Container port"},
                                "domain": {"type": "string", "description": "Ingress hostname"},
                                "replicas": {"type": "integer"},
                                "env_vars": {"type": "object", "description": "Non-secret env vars {KEY: value}"},
                                "secrets": {"type": "object", "description": "Secret env vars {KEY: value}"},
                                "health_path": {"type": "string", "description": "Health check path (default /health)"},
                                "ingress_class": {"type": "string", "description": "Ingress class (default traefik)"},
                            },
                        },
                    },
                },
                "required": ["project_slug", "environments"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_helm_chart",
            "description": (
                "Validate a Helm values file against the shared chart in the GitOps repo. "
                "Runs helm lint --strict and helm template to render manifests. "
                "Call this before push_project_values to verify the config is correct. "
                "Returns lint output and rendered Kubernetes manifests for DevOps review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "values_content": {
                        "type": "string",
                        "description": "Full YAML content of the values file to validate",
                    },
                },
                "required": ["values_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_cicd_workflow",
            "description": (
                "Generate a GitHub Actions CI/CD workflow YAML for a project. "
                "The workflow: builds Docker image → pushes to registry → updates image tag "
                "in GitOps repo → ArgoCD auto-syncs. Return this to the developer to commit "
                "to their repo at .github/workflows/deploy.yml."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string", "description": "Project slug"},
                    "environments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of environments to deploy to, e.g. ['staging', 'prod']",
                    },
                    "registry": {
                        "type": "string",
                        "description": "Container registry prefix, e.g. 'cr.imys.in/hci'",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch that triggers deployment (default: main)",
                    },
                },
                "required": ["project_slug", "environments", "registry"],
            },
        },
    },
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_project(name: str) -> dict[str, Any]:
    project = get_project_by_name(name)
    if not project:
        raise ValueError(f"Project '{name}' not found. Use list_projects to see available projects.")
    return project


def _resolve_env(project_name: str, env_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    project = _resolve_project(project_name)
    env = get_environment(project["id"], env_name)
    if not env:
        # Fall back to global environment
        global_proj = get_or_create_global_project()
        env = get_environment(global_proj["id"], env_name)
        if env:
            env = dict(env)
            env["is_global"] = True
        else:
            raise ValueError(
                f"Environment '{env_name}' not found in project '{project_name}' "
                "or in global environments. Use list_environments to see configured environments."
            )
    return project, env


# ─── Infrastructure setup guide generator ────────────────────────────────────

def _find_k8s_artifacts(repo_path: Path) -> dict[str, Any]:
    """Scan repo for existing Helm charts, kustomize, or raw K8s manifests."""
    result: dict[str, Any] = {
        "helm_chart": None,      # Path to Chart.yaml if found
        "kustomize": None,       # Path to kustomization.yaml if found
        "raw_manifests": [],     # List of yaml files containing K8s kinds
    }
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "vendor")]
        rp = Path(root)
        if "Chart.yaml" in files:
            result["helm_chart"] = rp / "Chart.yaml"
        if "kustomization.yaml" in files or "kustomization.yml" in files:
            result["kustomize"] = rp / "kustomization.yaml"
        for f in files:
            if f.endswith((".yaml", ".yml")):
                fp = rp / f
                try:
                    text = fp.read_text(errors="ignore")
                    if any(k in text for k in ("kind: Deployment", "kind: Ingress", "kind: Service")):
                        result["raw_manifests"].append(fp)
                except OSError:
                    pass
    return result


def _write_helm_chart(chart_dir: Path, proj_name: str, deployment: str,
                      namespace: str, ingress_class: str, domain: str,
                      tls: bool, cert_issuer: str, ann: dict[str, str],
                      lb_annotations: dict[str, str]) -> list[Path]:
    """Write a minimal but complete Helm chart to chart_dir. Returns written file paths."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    tmpl_dir = chart_dir / "templates"
    tmpl_dir.mkdir(exist_ok=True)
    written: list[Path] = []

    # Chart.yaml
    chart_yaml = chart_dir / "Chart.yaml"
    chart_yaml.write_text(
        f"apiVersion: v2\n"
        f"name: {proj_name}\n"
        f"description: Helm chart for {proj_name}\n"
        f"type: application\n"
        f"version: 0.1.0\n"
        f"appVersion: \"latest\"\n"
    )
    written.append(chart_yaml)

    # values.yaml
    lb_ann_yaml = "\n".join(f"    {k}: \"{v}\"" for k, v in lb_annotations.items())
    ing_ann_yaml = "\n".join(f"    {k}: \"{v}\"" for k, v in ann.items())
    tls_yaml = (
        f"  tls:\n    - hosts:\n        - {domain}\n      secretName: {deployment}-tls\n"
        if tls else "  tls: []\n"
    )
    values_yaml = chart_dir / "values.yaml"
    values_yaml.write_text(
        f"replicaCount: 1\n\n"
        f"image:\n"
        f"  repository: YOUR_REGISTRY/{proj_name}\n"
        f"  tag: latest\n"
        f"  pullPolicy: IfNotPresent\n\n"
        f"service:\n"
        f"  type: ClusterIP\n"
        f"  port: 80\n"
        f"  targetPort: 8080\n"
        f"  annotations:\n{lb_ann_yaml or '    {}'}\n\n"
        f"ingress:\n"
        f"  enabled: true\n"
        f"  className: \"{ingress_class}\"\n"
        f"  annotations:\n{ing_ann_yaml or '    {}'}\n"
        f"  hosts:\n"
        f"    - host: {domain or 'app.example.com'}\n"
        f"      paths:\n"
        f"        - path: /\n"
        f"          pathType: Prefix\n"
        f"{tls_yaml}\n"
        f"resources:\n"
        f"  requests:\n"
        f"    cpu: 100m\n"
        f"    memory: 128Mi\n"
        f"  limits:\n"
        f"    cpu: 500m\n"
        f"    memory: 512Mi\n\n"
        f"env: []\n"
        f"# env:\n"
        f"#   - name: DATABASE_URL\n"
        f"#     valueFrom:\n"
        f"#       secretKeyRef:\n"
        f"#         name: {proj_name}-secrets\n"
        f"#         key: database-url\n"
    )
    written.append(values_yaml)

    # templates/_helpers.tpl
    helpers = tmpl_dir / "_helpers.tpl"
    helpers.write_text(
        f"{{{{- define \"{proj_name}.fullname\" -}}}}\n"
        f"{{{{- .Release.Name | trunc 63 | trimSuffix \"-\" }}}}\n"
        f"{{{{- end }}}}\n\n"
        f"{{{{- define \"{proj_name}.labels\" -}}}}\n"
        f"helm.sh/chart: {{{{ .Chart.Name }}}}-{{{{ .Chart.Version }}}}\n"
        f"app.kubernetes.io/name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"app.kubernetes.io/managed-by: {{{{ .Release.Service }}}}\n"
        f"{{{{- end }}}}\n"
    )
    written.append(helpers)

    # templates/deployment.yaml
    deploy_tmpl = tmpl_dir / "deployment.yaml"
    deploy_tmpl.write_text(
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"  namespace: {namespace}\n"
        f"  labels:\n"
        f"    {{{{- include \"{proj_name}.labels\" . | nindent 4 }}}}\n"
        f"spec:\n"
        f"  replicas: {{{{ .Values.replicaCount }}}}\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f"      app.kubernetes.io/name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app.kubernetes.io/name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"    spec:\n"
        f"      containers:\n"
        f"        - name: {{{{ .Chart.Name }}}}\n"
        f"          image: \"{{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag }}}}\"\n"
        f"          imagePullPolicy: {{{{ .Values.image.pullPolicy }}}}\n"
        f"          ports:\n"
        f"            - containerPort: {{{{ .Values.service.targetPort }}}}\n"
        f"          env:\n"
        f"            {{{{- toYaml .Values.env | nindent 12 }}}}\n"
        f"          resources:\n"
        f"            {{{{- toYaml .Values.resources | nindent 12 }}}}\n"
    )
    written.append(deploy_tmpl)

    # templates/service.yaml
    svc_tmpl = tmpl_dir / "service.yaml"
    svc_tmpl.write_text(
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n"
        f"  name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"  namespace: {namespace}\n"
        f"  {{{{- with .Values.service.annotations }}}}\n"
        f"  annotations:\n"
        f"    {{{{- toYaml . | nindent 4 }}}}\n"
        f"  {{{{- end }}}}\n"
        f"spec:\n"
        f"  type: {{{{ .Values.service.type }}}}\n"
        f"  ports:\n"
        f"    - port: {{{{ .Values.service.port }}}}\n"
        f"      targetPort: {{{{ .Values.service.targetPort }}}}\n"
        f"  selector:\n"
        f"    app.kubernetes.io/name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
    )
    written.append(svc_tmpl)

    # templates/ingress.yaml
    ing_tmpl = tmpl_dir / "ingress.yaml"
    ing_tmpl.write_text(
        f"{{{{- if .Values.ingress.enabled }}}}\n"
        f"apiVersion: networking.k8s.io/v1\n"
        f"kind: Ingress\n"
        f"metadata:\n"
        f"  name: {{{{ include \"{proj_name}.fullname\" . }}}}\n"
        f"  namespace: {namespace}\n"
        f"  {{{{- with .Values.ingress.annotations }}}}\n"
        f"  annotations:\n"
        f"    {{{{- toYaml . | nindent 4 }}}}\n"
        f"  {{{{- end }}}}\n"
        f"spec:\n"
        f"  ingressClassName: {{{{ .Values.ingress.className }}}}\n"
        f"  {{{{- if .Values.ingress.tls }}}}\n"
        f"  tls:\n"
        f"    {{{{- toYaml .Values.ingress.tls | nindent 4 }}}}\n"
        f"  {{{{- end }}}}\n"
        f"  rules:\n"
        f"    {{{{- range .Values.ingress.hosts }}}}\n"
        f"    - host: {{{{ .host }}}}\n"
        f"      http:\n"
        f"        paths:\n"
        f"          {{{{- range .paths }}}}\n"
        f"          - path: {{{{ .path }}}}\n"
        f"            pathType: {{{{ .pathType }}}}\n"
        f"            backend:\n"
        f"              service:\n"
        f"                name: {{{{ include \"{proj_name}.fullname\" $ }}}}\n"
        f"                port:\n"
        f"                  number: {{{{ $.Values.service.port }}}}\n"
        f"          {{{{- end }}}}\n"
        f"    {{{{- end }}}}\n"
        f"{{{{- end }}}}\n"
    )
    written.append(ing_tmpl)

    return written


def _infra_setup_guide(project: dict[str, Any], env: dict[str, Any]) -> str:  # noqa: C901
    """Check repo for existing K8s/Helm artifacts, generate missing ones locally."""
    config = env.get("config") or {}
    platform = (config.get("platform") or "").lower()
    ingress_class = config.get("ingress_class") or "nginx"
    domain = config.get("domain") or ""
    namespace = config.get("namespace") or "default"
    deployment = config.get("deployment") or project["name"]
    tls = bool(config.get("tls"))
    cert_issuer = config.get("cert_manager_issuer") or "letsencrypt-prod"
    cert_challenge = config.get("cert_manager_challenge") or "http01"
    lb_type = (config.get("lb_type") or "").lower()
    lb_annotations = config.get("lb_annotations") or {}
    acm_arn = config.get("acm_certificate_arn") or ""
    external_dns = bool(config.get("external_dns"))

    if not platform:
        ann_str = str(lb_annotations).lower()
        if ingress_class == "alb" or "aws" in ann_str or lb_type in ("alb", "nlb"):
            platform = "eks"
        elif ingress_class == "azure/application-gateway" or "azure" in ann_str:
            platform = "aks"
        else:
            platform = "self-hosted"

    proj_name = project["name"]
    env_name = env["name"]
    lines = [
        f"# Infrastructure: {proj_name} / {env_name}",
        f"\n**Platform:** {platform.upper()}  |  **Ingress:** {ingress_class}  |  **Namespace:** {namespace}",
    ]
    if domain:
        lines.append(f"**Domain:** {domain}  |  **TLS:** {'Yes (' + cert_issuer + ')' if tls else 'No'}")

    # ── Build ingress annotations ──
    ann: dict[str, str] = {}
    if ingress_class == "alb":
        ann["alb.ingress.kubernetes.io/scheme"] = "internet-facing"
        ann["alb.ingress.kubernetes.io/target-type"] = "ip"
        if tls:
            ann["alb.ingress.kubernetes.io/certificate-arn"] = acm_arn or "arn:aws:acm:REGION:ACCOUNT:certificate/ID"
            ann["alb.ingress.kubernetes.io/ssl-redirect"] = "443"
    elif ingress_class == "azure/application-gateway":
        if tls:
            ann["cert-manager.io/cluster-issuer"] = cert_issuer
    elif ingress_class == "traefik":
        if tls:
            ann["cert-manager.io/cluster-issuer"] = cert_issuer
    else:  # nginx
        if tls:
            ann["cert-manager.io/cluster-issuer"] = cert_issuer

    # ── Step 1: Scan repo for existing artifacts ──
    lines.append("\n## 1. Repository Scan")
    try:
        repo_path = _ensure_repo(project)
        artifacts = _find_k8s_artifacts(repo_path)
    except Exception as exc:
        artifacts = {"helm_chart": None, "kustomize": None, "raw_manifests": []}
        lines.append(f"> Could not clone repo: {exc}. Generating charts based on config only.")

    if artifacts["helm_chart"]:
        helm_dir = artifacts["helm_chart"].parent
        lines.append(f"Found **Helm chart** at `{artifacts['helm_chart'].relative_to(repo_path)}`")
        lines.append(
            f"\nExisting chart detected -- generating a **values override file** for `{env_name}` "
            f"rather than a new chart."
        )
        # Generate an env-specific values override only
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        staging_dir.mkdir(parents=True, exist_ok=True)

        ann_values = "\n".join(f"    {k}: \"{v}\"" for k, v in ann.items())
        lb_ann_values = "\n".join(f"    {k}: \"{v}\"" for k, v in lb_annotations.items())
        tls_values = (
            f"  tls:\n    - hosts:\n        - {domain}\n      secretName: {deployment}-tls\n"
            if tls else "  tls: []\n"
        )
        override_path = staging_dir / f"values-{env_name}.yaml"
        override_path.write_text(
            f"# Values override for environment: {env_name}\n"
            f"# Apply with: helm upgrade --install {deployment} <chart-path> -n {namespace} -f {override_path.name}\n\n"
            f"image:\n"
            f"  tag: \"$IMAGE_TAG\"  # replace or inject via CI\n\n"
            f"service:\n"
            f"  annotations:\n{lb_ann_values or '    {}'}\n\n"
            f"ingress:\n"
            f"  enabled: true\n"
            f"  className: \"{ingress_class}\"\n"
            f"  annotations:\n{ann_values or '    {}'}\n"
            f"  hosts:\n"
            f"    - host: {domain or 'app.example.com'}\n"
            f"      paths:\n"
            f"        - path: /\n"
            f"          pathType: Prefix\n"
            f"{tls_values}"
        )
        lines.append(f"\nGenerated `{override_path}`:")
        lines.append(f"```yaml\n{override_path.read_text()}\n```")

        lines.append("\n## 2. Apply to Cluster")
        lines.append(
            "```bash\n"
            f"# Check existing release\n"
            f"helm list -n {namespace}\n\n"
            f"# Install or upgrade\n"
            f"helm upgrade --install {deployment} {helm_dir} \\\n"
            f"  --namespace {namespace} --create-namespace \\\n"
            f"  -f {override_path}\n"
            "```"
        )
        chart_ref = str(helm_dir)

    elif artifacts["kustomize"]:
        kust_dir = artifacts["kustomize"].parent
        lines.append(f"Found **Kustomize** config at `{artifacts['kustomize'].relative_to(repo_path)}`")
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Generate an overlay
        overlay_dir = staging_dir / "overlay"
        overlay_dir.mkdir(exist_ok=True)

        # Build patch for ingress
        ann_patch = "\n".join(f"        {k}: \"{v}\"" for k, v in ann.items())
        tls_patch = (
            f"  tls:\n  - hosts:\n    - {domain}\n    secretName: {deployment}-tls\n"
            if tls else ""
        )
        patch_file = overlay_dir / "ingress-patch.yaml"
        patch_file.write_text(
            f"apiVersion: networking.k8s.io/v1\n"
            f"kind: Ingress\n"
            f"metadata:\n"
            f"  name: {deployment}\n"
            f"  annotations:\n{ann_patch or '    {}'}\n"
            f"spec:\n"
            f"  ingressClassName: {ingress_class}\n"
            f"{tls_patch}"
        )
        kust_file = overlay_dir / "kustomization.yaml"
        kust_file.write_text(
            f"apiVersion: kustomize.config.k8s.io/v1beta1\n"
            f"kind: Kustomization\n"
            f"resources:\n"
            f"  - {kust_dir}\n"
            f"namespace: {namespace}\n"
            f"patches:\n"
            f"  - path: ingress-patch.yaml\n"
            f"    target:\n"
            f"      kind: Ingress\n"
            f"      name: {deployment}\n"
        )
        lines.append(f"\nGenerated Kustomize overlay at `{overlay_dir}`")
        lines.append("\n## 2. Apply to Cluster")
        lines.append(
            "```bash\n"
            f"kubectl apply -k {overlay_dir} --dry-run=client\n"
            f"kubectl apply -k {overlay_dir}\n"
            "```"
        )
        chart_ref = str(overlay_dir)

    elif artifacts["raw_manifests"]:
        found = [str(f.relative_to(repo_path)) for f in artifacts["raw_manifests"][:5]]
        lines.append(f"Found raw K8s manifests: {', '.join(found)}")
        lines.append(
            "No Helm chart or Kustomize found. Generating a Helm chart locally "
            "so you can version-control and reuse it."
        )
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        written = _write_helm_chart(
            staging_dir / "chart", proj_name, deployment, namespace,
            ingress_class, domain, tls, cert_issuer, ann, lb_annotations,
        )
        lines.append(f"\nGenerated Helm chart at `{staging_dir / 'chart'}`:")
        for f in written:
            lines.append(f"  - `{f.relative_to(staging_dir)}`")
        lines.append("\n## 2. Apply to Cluster")
        lines.append(
            "```bash\n"
            f"helm lint {staging_dir / 'chart'}\n"
            f"helm upgrade --install {deployment} {staging_dir / 'chart'} \\\n"
            f"  --namespace {namespace} --create-namespace\n"
            "```"
        )
        chart_ref = str(staging_dir / "chart")

    else:
        lines.append("No Helm chart, Kustomize, or K8s manifests found in repo. Generating a Helm chart locally.")
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        written = _write_helm_chart(
            staging_dir / "chart", proj_name, deployment, namespace,
            ingress_class, domain, tls, cert_issuer, ann, lb_annotations,
        )
        lines.append(f"\nGenerated Helm chart at `{staging_dir / 'chart'}`:")
        for f in written:
            lines.append(f"  - `{f.relative_to(staging_dir)}`")
        lines.append("\n## 2. Apply to Cluster")
        lines.append(
            "```bash\n"
            f"helm lint {staging_dir / 'chart'}\n"
            f"helm upgrade --install {deployment} {staging_dir / 'chart'} \\\n"
            f"  --namespace {namespace} --create-namespace\n"
            "```"
        )
        chart_ref = str(staging_dir / "chart")

    # ── cert-manager ClusterIssuer (if TLS and not EKS/ACM) ──
    if tls and ingress_class != "alb":
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        staging_dir.mkdir(parents=True, exist_ok=True)

        if cert_challenge == "dns01":
            solver = (
                "    - dns01:\n"
                "        cloudflare:\n"
                "          email: YOUR_EMAIL@example.com\n"
                "          apiTokenSecretRef:\n"
                "            name: cloudflare-api-token\n"
                "            key: api-token"
            )
        else:
            solver = (
                "    - http01:\n"
                f"        ingress:\n"
                f"          ingressClassName: {ingress_class}"
            )

        acme_server = (
            "https://acme-v02.api.letsencrypt.org/directory"
            if "prod" in cert_issuer
            else "https://acme-staging-v02.api.letsencrypt.org/directory"
        )
        issuer_content = (
            f"apiVersion: cert-manager.io/v1\n"
            f"kind: ClusterIssuer\n"
            f"metadata:\n"
            f"  name: {cert_issuer}\n"
            f"spec:\n"
            f"  acme:\n"
            f"    server: {acme_server}\n"
            f"    email: YOUR_EMAIL@example.com\n"
            f"    privateKeySecretRef:\n"
            f"      name: {cert_issuer}-key\n"
            f"    solvers:\n"
            f"{solver}\n"
        )
        issuer_path = staging_dir / "clusterissuer.yaml"
        issuer_path.write_text(issuer_content)

        lines.append(f"\n## 3. cert-manager ClusterIssuer")
        lines.append(
            f"Check if cert-manager is already installed:\n"
            f"```bash\n"
            f"kubectl get namespace cert-manager 2>/dev/null && helm list -n cert-manager\n"
            f"```\n"
            f"If not installed:\n"
            f"```bash\n"
            f"helm repo add jetstack https://charts.jetstack.io && helm repo update\n"
            f"helm upgrade --install cert-manager jetstack/cert-manager \\\n"
            f"  --namespace cert-manager --create-namespace --set installCRDs=true\n"
            f"```\n"
            f"Apply ClusterIssuer (generated at `{issuer_path}`):\n"
            f"```yaml\n{issuer_content}```\n"
            f"```bash\n"
            f"# Check if issuer already exists\n"
            f"kubectl get clusterissuer {cert_issuer} 2>/dev/null\n"
            f"# Apply\n"
            f"kubectl apply -f {issuer_path}\n"
            f"```"
        )
        if cert_challenge == "dns01":
            lines.append(
                "\n### Cloudflare API Token Secret\n"
                "```bash\n"
                "# Check if secret already exists\n"
                "kubectl get secret cloudflare-api-token -n cert-manager 2>/dev/null\n"
                "# Create if missing (token needs Zone:DNS:Edit permission)\n"
                "kubectl create secret generic cloudflare-api-token \\\n"
                "  --from-literal=api-token=YOUR_CF_API_TOKEN \\\n"
                "  -n cert-manager\n"
                "```"
            )

    # ── MetalLB IP pool (self-hosted + metallb) ──
    if platform == "self-hosted" and (lb_type == "metallb" or not lb_type):
        staging_dir = Path.home() / ".devops-agent" / "generated" / proj_name / env_name
        staging_dir.mkdir(parents=True, exist_ok=True)
        pool_content = (
            "apiVersion: metallb.io/v1beta1\n"
            "kind: IPAddressPool\n"
            "metadata:\n"
            "  name: default-pool\n"
            "  namespace: metallb-system\n"
            "spec:\n"
            "  addresses:\n"
            "  - 192.168.1.100-192.168.1.110  # TODO: replace with your IP range\n"
            "---\n"
            "apiVersion: metallb.io/v1beta1\n"
            "kind: L2Advertisement\n"
            "metadata:\n"
            "  name: default\n"
            "  namespace: metallb-system\n"
        )
        pool_path = staging_dir / "metallb-pool.yaml"
        pool_path.write_text(pool_content)
        lines.append(
            f"\n## MetalLB IP Pool\n"
            f"Check if MetalLB is installed:\n"
            f"```bash\n"
            f"kubectl get namespace metallb-system 2>/dev/null && helm list -n metallb-system\n"
            f"```\n"
            f"If not installed:\n"
            f"```bash\n"
            f"helm repo add metallb https://metallb.github.io/metallb && helm repo update\n"
            f"helm upgrade --install metallb metallb/metallb --namespace metallb-system --create-namespace\n"
            f"```\n"
            f"Apply IP pool (edit the address range first in `{pool_path}`):\n"
            f"```yaml\n{pool_content}```\n"
            f"```bash\nkubectl apply -f {pool_path}\n```"
        )

    # ── DNS records ──
    if domain:
        lines.append(f"\n## DNS Records")
        if platform == "eks":
            lines.append(
                "After applying, get ALB hostname:\n"
                "```bash\n"
                f"kubectl get ingress -n {namespace} -o jsonpath='{{.items[0].status.loadBalancer.ingress[0].hostname}}'\n"
                "```\n"
                f"Create DNS record:\n"
                "```\n"
                f"{domain}  CNAME  <alb-hostname>.elb.amazonaws.com  TTL=300\n"
                "```"
            )
            if external_dns:
                lines.append(
                    "\nexternal-dns values (check if already deployed: `helm list -n kube-system`):\n"
                    "```bash\n"
                    "helm upgrade --install external-dns bitnami/external-dns -n kube-system \\\n"
                    "  --set provider=aws --set aws.zoneType=public --set txtOwnerId=YOUR_CLUSTER\n"
                    "```"
                )
        elif platform == "aks":
            lines.append(
                "After applying, get LB IP:\n"
                "```bash\n"
                f"kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{{.status.loadBalancer.ingress[0].ip}}'\n"
                "```\n"
                f"Create DNS record:\n"
                "```\n"
                f"{domain}  A  <EXTERNAL-IP>  TTL=300\n"
                "```"
            )
        else:
            lines.append(
                "After applying, get LB IP:\n"
                "```bash\n"
                f"kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{{.status.loadBalancer.ingress[0].ip}}'\n"
                "```\n"
                f"Create DNS record:\n"
                "```\n"
                f"{domain}  A  <EXTERNAL-IP>  TTL=300\n"
                "```"
            )

    # ── Verify ──
    lines.append(f"\n## Verify")
    lines.append(
        "```bash\n"
        f"kubectl get all -n {namespace}\n"
        f"kubectl get ingress -n {namespace}\n"
    )
    if tls and ingress_class != "alb":
        lines.append(f"kubectl get certificate -n {namespace}")
    lines.append("```")

    # ── CI/CD snippet ──
    lines.append("\n## CI/CD (GitHub Actions snippet)")
    lines.append(
        "```yaml\n"
        "- name: Deploy\n"
        "  env:\n"
        "    KUBECONFIG: ${{ secrets.KUBECONFIG }}\n"
        "  run: |\n"
        f"    helm upgrade --install {deployment} {chart_ref} \\\n"
        f"      --namespace {namespace} --create-namespace \\\n"
        f"      --set image.tag=${{{{ github.sha }}}}\n"
        f"    kubectl rollout status deployment/{deployment} -n {namespace} --timeout=120s\n"
        "```"
    )

    return "\n".join(lines)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

def execute_tool(name: str, arguments: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    """Execute a tool call and return a string result."""
    try:
        return _dispatch(name, arguments, user=user)
    except Exception as exc:
        return f"Error: {exc}"


def _dispatch(name: str, args: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    # ── Project management ──
    if name == "list_projects":
        projects = list_projects()
        if not projects:
            return "No projects configured yet. Use create_project to add one."
        lines = ["Configured projects:"]
        for p in projects:
            lines.append(f"  • {p['name']}  repo={p['github_repo'] or 'none'}  {p['description'] or ''}")
        return "\n".join(lines)

    if name == "create_project":
        existing = get_project_by_name(args["name"])
        if existing:
            # Update github_repo/description if provided, then return existing
            kwargs: dict[str, Any] = {}
            if args.get("github_repo"):
                kwargs["github_repo"] = args["github_repo"]
            if args.get("description"):
                kwargs["description"] = args["description"]
            if kwargs:
                update_project(existing["id"], **kwargs)
            p = get_project_by_name(args["name"])
            return f"Project '{p['name']}' already exists (id={p['id']}) — updated with provided fields."
        p = create_project(
            name=args["name"],
            github_repo=args.get("github_repo", ""),
            description=args.get("description", ""),
        )
        return f"Created project '{p['name']}' (id={p['id']})."

    if name == "update_project":
        project = _resolve_project(args["project"])
        update_project(
            project["id"],
            github_repo=args.get("github_repo"),
            description=args.get("description"),
        )
        return f"Updated project '{args['project']}'."

    if name == "delete_project":
        project = _resolve_project(args["project"])
        delete_project(project["id"])
        return f"Deleted project '{args['project']}' and all its environments."

    # ── Environment management ──
    if name == "list_environments":
        project = _resolve_project(args["project"])
        envs = list_environments(project["id"])
        if not envs:
            return f"No environments configured for '{args['project']}'."
        lines = [f"Environments for '{args['project']}':"]
        for e in envs:
            current = e.get("current_image") or "never deployed"
            lines.append(
                f"  • {e['name']}  type={e['type']}  current={current}  "
                f"health={e.get('health_check_url') or 'none'}"
            )
        return "\n".join(lines)

    if name == "upsert_global_environment":
        from ..database import get_or_create_global_project
        global_proj = get_or_create_global_project()
        config: dict = {"values": args.get("values") or {}}
        if args.get("cloudflare"):
            config["cloudflare"] = args["cloudflare"]
        upsert_environment(
            project_id=global_proj["id"],
            name=args["name"],
            type_=args.get("type", "kubernetes"),
            config=config,
            health_check_url=None,
        )
        return f"Global environment '{args['name']}' saved."

    if name == "upsert_environment":
        project = _resolve_project(args["project"])
        # Support new values/cloudflare schema; fall back to legacy config dict
        if "values" in args or "cloudflare" in args:
            config: dict = {"values": args.get("values") or {}}
            if args.get("cloudflare"):
                config["cloudflare"] = args["cloudflare"]
        else:
            config = args.get("config") or {}
        upsert_environment(
            project_id=project["id"],
            name=args["environment"],
            type_=args.get("type", "kubernetes"),
            config=config,
            health_check_url=args.get("health_check_url"),
        )
        return f"Environment '{args['environment']}' saved for project '{args['project']}'."

    if name == "delete_environment":
        project, _ = _resolve_env(args["project"], args["environment"])
        delete_environment(project["id"], args["environment"])
        return f"Deleted environment '{args['environment']}' from '{args['project']}'."

    # ── Deployment operations ──
    if name == "deploy":
        project, env = _resolve_env(args["project"], args["environment"])
        image_or_ref = args["image_or_ref"]
        reason = args.get("reason", "")

        deploy_id = start_deployment(
            project["id"], args["environment"], image_or_ref, reason, triggered_by="agent"
        )
        deployer = make_deployer(env)
        result = deployer.deploy(image_or_ref)
        status = "success" if result.success else "failed"
        finish_deployment(deploy_id, status, result.output)
        if result.success:
            update_environment_state(project["id"], args["environment"], image_or_ref)
        return f"[{status.upper()}] {result.message}\n{result.output[:1000]}"

    if name == "rollback":
        project, env = _resolve_env(args["project"], args["environment"])
        previous = env.get("previous_image")
        if not previous:
            return "No previous deployment found to roll back to."

        reason = args.get("reason", "manual rollback")
        deploy_id = start_deployment(
            project["id"], args["environment"], previous, reason, triggered_by="agent"
        )
        deployer = make_deployer(env)
        result = deployer.rollback(previous)
        status = "success" if result.success else "failed"
        finish_deployment(deploy_id, status, result.output)
        if result.success:
            update_environment_state(project["id"], args["environment"], previous)
        return f"[{status.upper()}] Rolled back to {previous}\n{result.output[:1000]}"

    if name == "get_logs":
        project_name = args["project"]
        environment  = args["environment"]
        lines        = args.get("lines", 100)
        # ArgoCD path
        _, env = _resolve_env(project_name, environment)
        _env_vals = (env.get("config") or {}).get("values") if env else None
        argocd = _argocd_client(_env_vals)
        if argocd and _is_gitops_enabled(_env_vals):
            app_name  = f"{project_name}-{environment}"
            namespace = f"{project_name}-{environment}"
            return argocd.get_logs(app_name, namespace, tail_lines=lines)
        # Legacy path
        deployer = make_deployer(env)
        logs = deployer.get_logs(lines=lines)
        return logs[:3000] if logs else "(no logs)"

    if name == "get_deployment_status":
        project_name = args["project"]
        environment  = args["environment"]
        # ArgoCD path
        _, env = _resolve_env(project_name, environment)
        _env_vals = (env.get("config") or {}).get("values") if env else None
        argocd = _argocd_client(_env_vals)
        if argocd and _is_gitops_enabled(_env_vals):
            app_name = f"{project_name}-{environment}"
            try:
                status = argocd.get_status(app_name)
                return json.dumps(status, indent=2)
            except Exception as e:
                return f"ArgoCD status error: {e}"
        # Legacy path
        deployer = make_deployer(env)
        status = deployer.get_status()
        return json.dumps(status, indent=2)

    if name == "run_health_check":
        _, env = _resolve_env(args["project"], args["environment"])
        url = env.get("health_check_url")
        if not url:
            return "No health_check_url configured for this environment."
        try:
            resp = httpx.get(url, timeout=10)
            return f"HTTP {resp.status_code} — {'OK' if resp.is_success else 'UNHEALTHY'}"
        except Exception as exc:
            return f"Health check failed: {exc}"

    # ── Repository discovery ──
    if name == "discover_repo":
        project = _resolve_project(args["project"])
        github_repo = project.get("github_repo", "")
        if not github_repo:
            return f"Project '{args['project']}' has no github_repo configured. Update the project with a github_repo first."
        return discover_repo(
            github_repo=github_repo,
            branch=args.get("branch", "main"),
            github_token=project.get("github_token") or _GLOBAL_GITHUB_TOKEN,
        )

    # ── Security tools ──
    if name == "security_review_repo":
        project = _resolve_project(args["project"])
        github_repo = project.get("github_repo", "")
        if not github_repo:
            return f"Project '{args['project']}' has no github_repo configured."
        return security_review_repo(
            github_repo,
            branch=args.get("branch", "main"),
            github_token=project.get("github_token") or _GLOBAL_GITHUB_TOKEN,
        )

    if name == "pre_deploy_security_check":
        project, env = _resolve_env(args["project"], args["environment"])
        github_repo = project.get("github_repo", "")
        if not github_repo:
            return f"Project '{args['project']}' has no github_repo configured."
        return pre_deploy_security_check(
            github_repo=github_repo,
            image_or_ref=args["image_or_ref"],
            branch=args.get("branch", "main"),
            github_token=project.get("github_token") or _GLOBAL_GITHUB_TOKEN,
        )

    if name == "post_deploy_security_check":
        _, env = _resolve_env(args["project"], args["environment"])
        url = env.get("health_check_url", "")
        if not url:
            return "No health_check_url configured for this environment — cannot run post-deploy security check."
        return post_deploy_security_check(url=url, environment=args["environment"])

    if name == "list_deployments":
        project_name = args.get("project")
        project_id = None
        if project_name:
            project = _resolve_project(project_name)
            project_id = project["id"]
        deployments = list_deployments(project_id, limit=args.get("limit", 10))
        if not deployments:
            return "No deployments found."
        lines = ["Recent deployments:"]
        for d in deployments:
            lines.append(
                f"  #{d['id']} {d['project_name']}/{d['environment']}  "
                f"{d['image_or_ref']}  [{d['status']}]  {d['started_at'][:19]}"
            )
        return "\n".join(lines)

    # ── Deployment request workflow ──
    if name == "submit_deployment_request":
        project = _resolve_project(args["project"])
        requested_by = (user or {}).get("id")
        request_id = create_deployment_request(
            project_id=project["id"],
            environment=args["environment"],
            image_or_ref=args["image_or_ref"],
            plan_markdown=args["plan_markdown"],
            plan_config=args.get("plan_config", {}),
            requested_by=requested_by,
            session_id=args.get("session_id", ""),
        )
        return (
            f"✅ Deployment request #{request_id} submitted for DevOps review.\n"
            f"Project: {args['project']} → {args['environment']}\n"
            f"Image/ref: {args['image_or_ref']}\n"
            f"A DevOps engineer will review your plan and approve or request changes."
        )

    if name == "list_deployment_requests":
        project_id = None
        project_name = args.get("project")
        if project_name:
            project = _resolve_project(project_name)
            project_id = project["id"]
        requests = list_deployment_requests(
            status=args.get("status"),
            project_id=project_id,
            limit=args.get("limit", 20),
        )
        if not requests:
            return "No deployment requests found."
        lines = ["Deployment requests:"]
        for r in requests:
            lines.append(
                f"  #{r['id']} [{r['status'].upper()}]  "
                f"{r['project_name']}/{r['environment']}  "
                f"{r['image_or_ref']}  "
                f"by {r['requester_name'] or 'unknown'}  {r['created_at'][:19]}"
            )
        return "\n".join(lines)

    if name == "get_deployment_request":
        req = get_deployment_request(args["request_id"])
        if not req:
            return f"Deployment request #{args['request_id']} not found."
        lines = [
            f"## Deployment Request #{req['id']}",
            f"**Status:** {req['status']}",
            f"**Project:** {req['project_name']} → {req['environment']}",
            f"**Image/ref:** {req['image_or_ref']}",
            f"**Requested by:** {req['requester_name'] or 'unknown'}  ({req['created_at'][:19]})",
        ]
        if req.get("reviewer_name"):
            lines.append(f"**Reviewed by:** {req['reviewer_name']}  ({req['updated_at'][:19]})")
        if req.get("reject_reason"):
            lines.append(f"**Reject reason:** {req['reject_reason']}")
        lines.append(f"\n### Plan\n{req['plan_markdown']}")
        if req.get("plan_config"):
            try:
                cfg = json.loads(req["plan_config"]) if isinstance(req["plan_config"], str) else req["plan_config"]
                lines.append(f"\n### Config\n```json\n{json.dumps(cfg, indent=2)}\n```")
            except Exception:
                pass
        return "\n".join(lines)

    if name == "approve_deployment_request":
        req = get_deployment_request(args["request_id"])
        if not req:
            return f"Deployment request #{args['request_id']} not found."
        if req["status"] != "pending_review":
            return f"Request #{args['request_id']} is '{req['status']}' — only pending_review requests can be approved."

        # Apply any edits before executing
        updates: dict[str, Any] = {}
        if "plan_markdown" in args:
            updates["plan_markdown"] = args["plan_markdown"]
        if "plan_config" in args:
            updates["plan_config"] = args["plan_config"]
        if "image_or_ref" in args:
            updates["image_or_ref"] = args["image_or_ref"]
        updates["status"] = "executing"
        updates["reviewed_by"] = (user or {}).get("id")
        update_deployment_request(args["request_id"], **updates)

        # Execute the deployment
        image_or_ref = args.get("image_or_ref") or req["image_or_ref"]
        try:
            project, env = _resolve_env(req["project_name"], req["environment"])
            plan_config = req.get("plan_config") or {}
            if isinstance(plan_config, str):
                try:
                    plan_config = json.loads(plan_config)
                except Exception:
                    plan_config = {}

            deploy_id = start_deployment(
                project["id"], req["environment"], image_or_ref,
                f"Approved deployment request #{req['id']}",
                triggered_by="agent",
            )

            # ── GitOps / ArgoCD path ──────────────────────────────────────────
            _env_vals = (env.get("config") or {}).get("values") if env else None
            if _is_gitops_enabled(_env_vals):
                try:
                    gr = gitops_deploy(
                        project_slug=req["project_name"],
                        environment=req["environment"],
                        image_or_ref=image_or_ref,
                        plan_config=plan_config,
                        env_config=env.get("config") if env else None,
                        gitops_repo=_gitops_repo(_env_vals),
                        gitops_token=_gitops_token(_env_vals),
                        gitops_branch=_gitops_branch(_env_vals),
                        argocd_client=_argocd_client(_env_vals),
                    )
                    finish_deployment(deploy_id, "success", json.dumps(gr))
                    update_environment_state(project["id"], req["environment"], image_or_ref)
                    update_deployment_request(args["request_id"], status="done", deployment_id=deploy_id)
                    argocd_status = gr.get("argocd_status", {})
                    return (
                        f"[SUCCESS] Deployment request #{req['id']} pushed to GitOps.\n"
                        f"  GitOps repo: {gr['gitops_repo']}\n"
                        f"  Values file: {gr['values_path']}\n"
                        f"  ArgoCD app:  {gr['app_name']}\n"
                        f"  Namespace:   {gr['namespace']}\n"
                        + (f"  ArgoCD sync status: {argocd_status.get('sync_status', 'triggered')}\n" if argocd_status else "  ArgoCD sync triggered.\n")
                        + f"\nArgoCD will sync the cluster automatically. Monitor at {s.argocd_url}/applications/{gr['app_name']}"
                    )
                except Exception as exc:
                    finish_deployment(deploy_id, "failed", str(exc))
                    update_deployment_request(args["request_id"], status="failed")
                    return f"GitOps deploy failed: {exc}"

            # ── Legacy direct deployer path (no GitOps configured) ───────────
            deployer = make_deployer(env)
            result = deployer.deploy(image_or_ref)
            status = "success" if result.success else "failed"
            finish_deployment(deploy_id, status, result.output)
            if result.success:
                update_environment_state(project["id"], req["environment"], image_or_ref)
            req_status = "done" if result.success else "failed"
            update_deployment_request(args["request_id"], status=req_status, deployment_id=deploy_id)
            return (
                f"[{status.upper()}] Deployment request #{req['id']} executed.\n"
                f"{result.message}\n{result.output[:1000]}"
            )
        except Exception as exc:
            update_deployment_request(args["request_id"], status="failed")
            return f"Deployment failed: {exc}"

    if name == "reject_deployment_request":
        req = get_deployment_request(args["request_id"])
        if not req:
            return f"Deployment request #{args['request_id']} not found."
        if req["status"] != "pending_review":
            return f"Request #{args['request_id']} is '{req['status']}' — only pending_review requests can be rejected."
        update_deployment_request(
            args["request_id"],
            status="rejected",
            reviewed_by=(user or {}).get("id"),
            reject_reason=args.get("reason", ""),
        )
        return (
            f"❌ Deployment request #{args['request_id']} rejected.\n"
            f"Reason: {args.get('reason', '')}"
        )

    # ── Repo exploration (Claude Code-style) ──
    if name == "glob_files":
        project = _resolve_project(args["project"])
        repo_path = _ensure_repo(project)
        files = all_files(repo_path)
        pattern = args["pattern"]
        matched = find_files(files, repo_path, pattern)
        if not matched:
            return f"No files matched '{pattern}' in {project['github_repo']}."
        lines = [f"Matched {len(matched)} file(s) for pattern '{pattern}':"]
        for f in matched[:100]:
            lines.append(f"  {rel_path(f, repo_path)}")
        if len(matched) > 100:
            lines.append(f"  ... and {len(matched) - 100} more")
        return "\n".join(lines)

    if name == "grep_files":
        project = _resolve_project(args["project"])
        repo_path = _ensure_repo(project)
        files = all_files(repo_path)
        pattern = args["pattern"]
        include = args.get("include", "*")
        max_results = args.get("max_results", 30)
        hits = grep_files(files, pattern, include=include, max_results=max_results)
        if not hits:
            return f"No matches for '{pattern}' in {project['github_repo']}."
        lines = [f"{len(hits)} match(es) for '{pattern}':"]
        for f, lineno, text in hits:
            lines.append(f"  {rel_path(f, repo_path)}:{lineno}: {text}")
        return "\n".join(lines)

    if name == "read_file":
        project = _resolve_project(args["project"])
        repo_path = _ensure_repo(project)
        target = repo_path / args["path"]
        # Security: ensure path stays within repo
        try:
            target = target.resolve()
            target.relative_to(repo_path.resolve())
        except ValueError:
            return f"Path '{args['path']}' is outside the repository."
        if not target.exists():
            return f"File not found: {args['path']}"
        content = read_file(target, max_bytes=args.get("max_bytes", 32_000))
        if not content:
            return f"File is empty or unreadable: {args['path']}"
        return f"```\n{content}\n```"

    if name == "bash_exec":
        project = _resolve_project(args["project"])
        repo_path = _ensure_repo(project)
        command = args["command"]
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                return f"[exit {result.returncode}]\n{output[:2000]}"
            return output[:3000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30 seconds."

    if name == "infra_setup_guide":
        project, env = _resolve_env(args["project"], args["environment"])
        if env["type"] != "kubernetes":
            return (
                f"infra_setup_guide only applies to kubernetes environments. "
                f"'{args['environment']}' is type '{env['type']}'."
            )
        return _infra_setup_guide(project, env)

    if name == "push_helm_chart":
        s = get_settings()
        _env_vals: dict | None = None  # use global gitops settings
        gitops_repo = _gitops_repo(_env_vals)
        gitops_token = _gitops_token(_env_vals)
        gitops_branch = _gitops_branch(_env_vals)
        if not gitops_repo:
            return "GitOps not configured: GITOPS_REPO is not set. Configure it via admin settings."
        result = push_helm_chart(
            project_slug=args["project_slug"],
            files=args["files"],
            gitops_repo=gitops_repo,
            gitops_token=gitops_token,
            gitops_branch=gitops_branch,
            force=args.get("force", False),
        )
        if result.get("error"):
            return f"[ERROR] {result['error']}\n\nHelm lint output:\n{result.get('lint', '')}"
        pushed = result.get("pushed", [])
        skipped = result.get("skipped", [])
        lint = result.get("lint", "")
        lines = [f"[OK] Pushed {len(pushed)} chart files to {gitops_repo}:"]
        lines += [f"  {p}" for p in pushed]
        if skipped:
            lines.append(f"Skipped (already exist): {', '.join(skipped)}")
        lines.append(f"Helm lint: {lint}")
        return "\n".join(lines)

    if name == "register_argocd_app":
        s = get_settings()
        _env_vals: dict | None = None
        gitops_repo = _gitops_repo(_env_vals)
        gitops_token = _gitops_token(_env_vals)
        gitops_branch = _gitops_branch(_env_vals)
        if not gitops_repo:
            return "GitOps not configured: GITOPS_REPO is not set."
        project_slug = args["project_slug"]
        environment  = args.get("environment", "prod")
        namespace    = args.get("namespace") or project_slug
        app_name     = f"{project_slug}-{environment}" if environment != "prod" else project_slug

        from .argocd import build_argocd_app_yaml, push_gitops_file, ArgoCDClient, _ensure_argocd_repo
        argocd_yaml = build_argocd_app_yaml(
            app_name=app_name,
            gitops_repo=gitops_repo,
            project_slug=project_slug,
            environment=environment,
            namespace=namespace,
        )
        # Push the ArgoCD Application YAML to the GitOps repo
        argocd_path = f"argocd/{app_name}.yaml"
        try:
            push_gitops_file(
                repo=gitops_repo,
                path=argocd_path,
                content=argocd_yaml,
                token=gitops_token,
                branch=gitops_branch,
                commit_message=f"argocd({app_name}): register application",
            )
        except Exception as exc:
            return f"[WARN] Could not push ArgoCD YAML to GitOps repo: {exc}\nYou can apply it manually."

        # Create/update via ArgoCD API if configured
        argocd_client = _argocd_client(_env_vals)
        if argocd_client:
            try:
                import yaml as _yaml
                _ensure_argocd_repo(argocd_client, gitops_repo, gitops_token)
                argocd_client.upsert_app(_yaml.safe_load(argocd_yaml))
                argocd_client.sync(app_name)
                status = argocd_client.get_status(app_name)
                return (
                    f"[OK] ArgoCD Application '{app_name}' created and sync triggered.\n"
                    f"  Repo: {gitops_repo} → charts/{project_slug}\n"
                    f"  Namespace: {namespace}\n"
                    f"  Health: {status.get('health')}, Sync: {status.get('sync_status')}\n"
                    f"  Monitor: {s.argocd_url}/applications/{app_name}"
                )
            except Exception as exc:
                return (
                    f"[PARTIAL] ArgoCD YAML pushed to GitOps repo at {argocd_path}, "
                    f"but ArgoCD API call failed: {exc}\n"
                    f"Apply manually: kubectl apply -f (from GitOps repo)"
                )
        return (
            f"[OK] ArgoCD Application YAML pushed to {gitops_repo}/{argocd_path}.\n"
            f"ArgoCD API not configured — apply manually or configure ARGOCD_URL + ARGOCD_TOKEN."
        )

    if name == "push_shared_chart":
        _ev: dict | None = None
        gitops_repo = _gitops_repo(_ev)
        gitops_token = _gitops_token(_ev)
        gitops_branch = _gitops_branch(_ev)
        if not gitops_repo:
            return "GitOps not configured: GITOPS_REPO is not set."
        result = push_shared_chart(
            gitops_repo=gitops_repo,
            gitops_token=gitops_token,
            gitops_branch=gitops_branch,
            force=args.get("force", False),
        )
        if result.get("error"):
            return f"[ERROR] {result['error']}"
        status = result.get("status", "pushed")
        pushed = result.get("pushed", [])
        skipped = result.get("skipped", [])
        if status == "already_exists":
            return f"[OK] charts/app/ already exists in {gitops_repo} — skipped (use force=true to overwrite)."
        return f"[OK] Pushed shared chart ({len(pushed)} files) to {gitops_repo}/charts/app/"

    if name == "push_project_values":
        _ev = None
        gitops_repo = _gitops_repo(_ev)
        gitops_token = _gitops_token(_ev)
        gitops_branch = _gitops_branch(_ev)
        if not gitops_repo:
            return "GitOps not configured: GITOPS_REPO is not set."
        result = push_project_values(
            project_slug=args["project_slug"],
            environments=args["environments"],
            gitops_repo=gitops_repo,
            gitops_token=gitops_token,
            gitops_branch=gitops_branch,
        )
        if result.get("error"):
            return f"[ERROR] {result['error']}"
        pushed = result.get("pushed", [])
        lines = [f"[OK] Pushed values files to {gitops_repo}:"]
        lines += [f"  {p}" for p in pushed]
        lines.append(f"\nArgoCD will auto-sync once the ArgoCD Applications are registered.")
        return "\n".join(lines)

    if name == "validate_helm_chart":
        _ev = None
        gitops_repo = _gitops_repo(_ev)
        gitops_token = _gitops_token(_ev)
        gitops_branch = _gitops_branch(_ev)
        if not gitops_repo:
            return "GitOps not configured: GITOPS_REPO is not set."
        result = validate_helm_chart(
            gitops_repo=gitops_repo,
            gitops_token=gitops_token,
            values_content=args["values_content"],
            gitops_branch=gitops_branch,
        )
        if result.get("error"):
            return f"[INVALID] Helm validation failed: {result['error']}\n\nLint output:\n{result.get('lint', '')}"
        manifests = result.get("rendered_manifests", "")
        lint = result.get("lint", "")
        return (
            f"[VALID] Helm chart validation passed.\n\n"
            f"Lint output:\n{lint}\n\n"
            f"Rendered manifests (preview):\n```yaml\n{manifests[:4000]}"
            f"{'...(truncated)' if len(manifests) > 4000 else ''}\n```"
        )

    if name == "generate_cicd_workflow":
        s = get_settings()
        gitops_repo = _gitops_repo(None)
        registry = args.get("registry") or "cr.imys.in/hci"
        workflow = generate_cicd_workflow(
            project_slug=args["project_slug"],
            gitops_repo=gitops_repo or "<org>/gitops",
            registry=registry,
            environments=args.get("environments", ["prod"]),
            branch=args.get("branch", "main"),
        )
        return (
            f"GitHub Actions workflow for {args['project_slug']}:\n\n"
            f"Commit this to `.github/workflows/deploy.yml` in the app repo:\n\n"
            f"```yaml\n{workflow}\n```"
        )

    return f"Unknown tool: {name}"
