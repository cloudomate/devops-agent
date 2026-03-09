# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run locally (dev mode, auto-reload)
python main.py serve --reload

# Run with Docker Compose
docker compose up

# Build and push Docker image
docker build -t cr.imys.in/hci/devops-agent:latest .
docker push cr.imys.in/hci/devops-agent:latest

# Deploy to k3s on gb10dgx01
ssh gb10dgx01 "kubectl rollout restart deployment/devops-agent -n devops-agent && kubectl rollout status deployment/devops-agent -n devops-agent --timeout=90s"

# Patch the k8s secret (then restart to pick up)
kubectl patch secret devops-agent-env -n devops-agent -p '{"data":{"KEY":"<base64>"}}'
```

There are no automated tests. Manual testing is done via the UI or curl against `localhost:8000`.

## Architecture

### Entry Point and Startup

`main.py` is a Typer CLI app. `serve` is the only command — it calls `uvicorn` with the factory `devops_agent.web.app:create_app`. On startup the factory runs `init_db()` (SQLite schema + migrations) and starts the background GitHub `poll_loop()`.

### Personas and Agent Loop

`devops_agent/agent.py` contains `run_agent_streaming()` — the core async generator that drives the agent. It builds a system prompt based on `persona` (`developer` or `devops`), then loops: stream LLM response → execute any tool calls → append results → repeat until no tool calls remain. Text deltas and tool events are yielded to the WebSocket handler.

**Developer persona** — onboarding new apps. Can discover repos, ask questions, and submit deployment requests. Cannot approve, reject, or manage infrastructure.

**DevOps persona** — reviews deployment requests, manages environments and infrastructure, approves/rejects. Has access to all tools.

The active project is injected into the system prompt so tool calls default to the right project.

### Tools

All tools live in `devops_agent/tools/registry.py` — ~1500 lines. It contains both the tool schema definitions (OpenAI function-calling format) returned to the LLM and the dispatcher `execute_tool(name, args)` that runs them. The file also holds small helpers like `_resolve_project()`, `_resolve_env()`, `_argocd_client()`, `_gitops_token()`.

Helpers for specialized tool logic:
- `devops_agent/tools/discovery.py` — clones repos via GitHub API, scans files, detects DB types, generates structured question lists
- `devops_agent/tools/security.py` — pre/post-deploy security checks
- `devops_agent/tools/argocd.py` — GitOps file push (GitHub API, no git CLI), ArgoCD Application CR generation, `ArgoCDClient` class, `gitops_deploy()` orchestration

### Deployment Backends

`devops_agent/deployers/factory.py` reads the environment config (`type` field) and returns the right `BaseDeployer` subclass: `KubernetesDeployer`, `SSHDeployer`, or `DockerComposeDeployer`. When `GITOPS_REPO` is set, `approve_deployment_request` bypasses the deployer entirely and calls `gitops_deploy()` instead.

### GitOps / ArgoCD Priority

Settings are resolved in order: **per-environment config values → k8s Secret env vars → `.env` file**. Helpers `_gitops_repo(env_vals)`, `_gitops_token(env_vals)`, `_argocd_client(env_vals)` all accept an optional `env_vals` dict that takes precedence over global settings.

If `GITOPS_REPO` is not set → legacy deployer. If `ARGOCD_URL`/`ARGOCD_TOKEN` are not set but `GITOPS_REPO` is set → files are pushed to the GitOps repo but ArgoCD sync is skipped (ArgoCD auto-detects via its repo watch).

### Database

Single SQLite file at `$DATA_DIR/devops_agent.db` (default `/data`). All DB access is synchronous via `sqlite3`. Key tables: `projects`, `environments` (per-project + global fallback), `deployments`, `deployment_requests`, `chat_messages`, `users`, `project_members`.

Environment `config` column is a JSON blob — all service credentials (Postgres, Redis, GitHub token, ArgoCD token, etc.) live inside it as `config.values.<KEY>`.

### Web Layer

`devops_agent/web/routes/chat.py` — WebSocket endpoint at `/ws/chat/{session_id}`. Reads `project` from the WS payload, calls `run_agent_streaming()`, streams `{type:"delta",text:"..."}` frames to the browser, saves completed messages via `save_message()`.

`devops_agent/web/routes/api.py` — REST endpoints for the settings UI: CRUD for projects, environments, deployment requests. Also exposes `/api/chat-history/{session_id}` (strips tool-call markup before returning).

### Frontend

Vanilla JS SPA in `devops_agent/web/static/`. No build step — files are served directly.

`app.js` key patterns:
- Per-project chat sessions stored in `localStorage` as `session_project_<name>`, switched by reconnecting the WebSocket
- Agent emits a `deploy-confirm` fenced code block; `renderMarkdown()` intercepts it and renders a confirmation card (defined in `_buildConfirmCard()`)
- `__SUBMIT_CONFIRMED__` hidden message triggers the `submit_deployment_request` tool
- Settings form uses `_gefBuildConfig()` / `_gefLoadConfig()` / `_gefResetForm()` — all field IDs prefixed `gef-`

### Authentication

`devops_agent/web/oidc.py` handles Entra ID OIDC. When `ENTRA_CLIENT_ID` is empty the app runs in no-auth dev mode (all requests treated as admin). Role hierarchy: `admin > devops > developer`. Roles come from Entra App Role assignments; members of `ENTRA_ADMIN_GROUP_ID` also get admin.

### Infrastructure

Production k8s manifests are in `k8s/`. The app runs in namespace `devops-agent`. Secrets are in `devops-agent-env`. The container expects `/data` as a PVC mount for the SQLite DB and kubeconfig.

ArgoCD runs in namespace `argocd` on the same cluster. The GitOps repo structure is documented in `docs/gitops-setup.md`.
