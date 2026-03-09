# DevOps Agent

An AI-powered deployment manager with a chat interface. Developers onboard their GitHub repos through conversation; DevOps engineers review and approve; ArgoCD handles deployments automatically via GitOps.

Built on any OpenAI-compatible LLM (Anthropic Claude, OpenAI, Ollama, etc.) with a React frontend, FastAPI backend, and WebSocket streaming.

---

## How It Works

Two personas interact through separate chat sessions:

```
Developer chat                 Agent                          DevOps chat
─────────────────────────────────────────────────────────────────────────
1. "Deploy github.com/org/app"

                               Silently clones repo, detects:
                               - Language / framework
                               - Dockerfile + exposed port
                               - Required env vars
                               - Health check path

                               Shows onboarding form →
                               (environments, domain, registry,
                                secrets, DB services)

2. Developer fills form
   and confirms

                               Saves deployment plan
                               (status: pending_review)
                               Notifies developer to wait

                                                               3. DevOps opens chat,
                                                                  sees pending requests

                                                               Agent runs security scan,
                                                               prepares Helm values +
                                                               ArgoCD Application CR,
                                                               shows plan for review

                                                               4. DevOps approves

                               Pushes to GitOps repo ─────────────────────────────→
                                                                              ArgoCD syncs
                                                                              app to cluster ✅

                               Generates GitHub Actions CI/CD workflow

5. Developer gets workflow
   → commit to .github/workflows/
   → every push to main auto-deploys
─────────────────────────────────────────────────────────────────────────
```

---

## Features

- **Automatic repo discovery** — clones GitHub repo, detects stack, Dockerfile, port, env vars, health path
- **Interactive onboarding form** — rendered in the UI, not plain text Q&A
- **Security scanning** — pre and post-deployment security checks
- **GitOps-first** — pushes Helm values + ArgoCD Application CRs; ArgoCD handles all syncing
- **Shared Helm chart** — one `charts/app/` chart for all projects; differences in per-project values files
- **Helm validation** — lints and templates values before pushing
- **CI/CD generation** — produces ready-to-commit GitHub Actions workflow with GHCR or custom registry support
- **Multi-environment** — staging, prod, or any custom environment per project
- **Cloudflare Tunnel support** — optional tunnel config per environment (no ingress needed)
- **Role-based access** — Admin / DevOps / Developer via Microsoft Entra ID SSO or no-auth dev mode
- **Deployment history** — all deployments, requests, and chat history stored in SQLite

---

## Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend build)
- An OpenAI-compatible LLM (Anthropic Claude, OpenAI, Ollama, etc.)
- GitHub token with `repo` scope
- Kubernetes cluster + ArgoCD (for GitOps deployments)

---

## Quick Start (Local Dev)

```bash
# 1. Clone and install
git clone https://github.com/cloudomate/devops-agent
cd devops-agent
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — minimum required: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, GITHUB_TOKEN

# 3. Build frontend
cd devops_agent/web/frontend && npm install && npm run build && cd ../../..

# 4. Start
python main.py serve --reload
# Open http://localhost:8000
```

No `ENTRA_CLIENT_ID` set → runs in dev mode, all requests treated as admin with full DevOps access.

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `LLM_BASE_URL` | OpenAI-compatible API base URL (e.g. `https://api.anthropic.com/v1`) |
| `LLM_API_KEY` | API key for the LLM provider |
| `LLM_MODEL` | Model name (e.g. `claude-opus-4-6`, `gpt-4o`, `llama3`) |
| `GITHUB_TOKEN` | GitHub PAT with `repo` scope — used for cloning and GitOps pushes |

### Authentication (Microsoft Entra ID / Azure AD)

Leave all `ENTRA_*` empty to run without auth (dev mode).

| Variable | Description |
|----------|-------------|
| `ENTRA_TENANT_ID` | Azure AD tenant ID |
| `ENTRA_CLIENT_ID` | App registration client ID |
| `ENTRA_CLIENT_SECRET` | Client secret value |
| `ENTRA_REDIRECT_URI` | OAuth callback, e.g. `https://your-domain/auth/callback` |
| `ENTRA_ADMIN_GROUP_ID` | Entra group whose members get admin role |
| `SECRET_KEY` | Secret for signing session cookies (required in production) |

### GitOps / ArgoCD (can also be set via Settings UI)

| Variable | Description |
|----------|-------------|
| `GITOPS_REPO` | GitOps repo slug, e.g. `myorg/gitops` |
| `GITOPS_TOKEN` | GitHub PAT with write access to GitOps repo (falls back to `GITHUB_TOKEN`) |
| `GITOPS_BRANCH` | Target branch (default: `main`) |
| `ARGOCD_URL` | ArgoCD server URL, e.g. `http://argocd.example.com` |
| `ARGOCD_TOKEN` | ArgoCD API token |

### Other

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP port |
| `DATA_DIR` | `/data` | Directory for SQLite database |

---

## Settings UI

All GitOps, ArgoCD, registry, and database service settings can be configured through the web UI without restarting:

**Settings → Environments → Global** — set defaults for all projects:
- GitOps repo, token, branch
- ArgoCD URL and token
- Container registry
- Database service URLs (Postgres, Redis, MongoDB)
- Cloudflare tunnel config

**Settings → Environments → (per project)** — override any global setting for a specific project/environment.

---

## Docker

```bash
docker build --platform linux/arm64 -t devops-agent:latest .

docker run -p 8000:8000 \
  -v $(pwd)/data:/data \
  --env-file .env \
  devops-agent:latest

# or
docker compose up
```

---

## Kubernetes Deployment

Manifests in `k8s/`. Runs in namespace `devops-agent` with a PVC at `/data` for the SQLite DB.

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/secret.yaml        # edit with your values first
kubectl apply -f k8s/rbac-template.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

# Update a secret
kubectl patch secret devops-agent-env -n devops-agent \
  -p '{"data":{"KEY":"<base64>"}}'
kubectl rollout restart deployment/devops-agent -n devops-agent
```

---

## Authentication & Roles

When `ENTRA_CLIENT_ID` is set, SSO login is required via Microsoft Entra ID.

**Setup:**
1. Azure Portal → Entra ID → App registrations → New registration
2. Add redirect URI: `https://<your-domain>/auth/callback`
3. App roles → create: `Admin`, `DevOps`, `Developer`
4. Enterprise Applications → Users and groups → assign roles
5. Certificates & secrets → new client secret → set `ENTRA_CLIENT_SECRET`

**Role access:**

| Role | Capabilities |
|------|-------------|
| `Admin` | Everything + Settings page + user management |
| `DevOps` | Review/approve requests, manage environments, push GitOps files, run security scans |
| `Developer` | Onboard repos, submit deployment requests, view own projects |

---

## GitOps / ArgoCD Setup

See [`docs/gitops-setup.md`](docs/gitops-setup.md) for the full guide covering:
- Installing ArgoCD on Kubernetes
- Creating the shared Helm chart
- Connecting ArgoCD to the GitOps repo
- Generating ArgoCD API tokens
- Secrets management (Sealed Secrets / External Secrets Operator)

---

## Architecture

```
devops_agent/
├── agent.py              # Streaming agent loop — builds system prompt per persona,
│                         # calls LLM, dispatches tool calls, streams deltas over WS
├── config.py             # pydantic-settings — reads .env + DB overrides
├── database.py           # SQLite: projects, environments, deployments,
│                         # deployment_requests, chat_messages, users
├── tools/
│   ├── registry.py       # Tool schemas (OpenAI function format) + execute_tool()
│   ├── argocd.py         # push_shared_chart, push_project_values, register_argocd_app,
│   │                     # validate_helm_chart, generate_cicd_workflow, ArgoCDClient
│   ├── discovery.py      # clone repo via GitHub API, detect stack/port/Dockerfile/env vars
│   └── security.py       # pre/post-deploy security checks
├── deployers/
│   ├── factory.py        # picks backend from environment type field
│   ├── kubernetes.py     # direct kubectl deployer
│   ├── ssh.py            # SSH deployer (paramiko)
│   └── docker_compose.py # Docker Compose over SSH
└── web/
    ├── app.py            # FastAPI factory — init_db(), starts GitHub poll_loop()
    ├── oidc.py           # Microsoft Entra ID OIDC
    ├── routes/
    │   ├── chat.py       # WebSocket /ws/chat/{session_id} — streams agent responses
    │   └── api.py        # REST: projects, environments, deployment requests, chat history
    └── frontend/         # React + Vite — served from static/ after build
```

**Deployment backends** (resolved by environment `type` field):

| Backend | When used |
|---------|-----------|
| GitOps + ArgoCD | `GITOPS_REPO` is set — recommended for Kubernetes |
| Kubernetes direct | `type: kubernetes` without GitOps |
| SSH | `type: ssh` |
| Docker Compose | `type: docker-compose` |

---

## Development

```bash
# Backend with hot reload
python main.py serve --reload

# Frontend dev server (HMR on port 5173, proxies API to 8000)
cd devops_agent/web/frontend
npm run dev

# Rebuild frontend for production
npm run build   # output → devops_agent/web/static/

# Inspect database
sqlite3 data/devops_agent.db ".tables"
sqlite3 data/devops_agent.db "SELECT * FROM projects;"
```

---

## Useful Commands

```bash
# Logs
kubectl logs -n devops-agent deployment/devops-agent -f

# ArgoCD
kubectl get applications -n argocd
argocd app list
argocd app sync <project>-<env>
argocd app rollback <project>-<env> <revision>
```
