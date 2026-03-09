# DevOps Agent

An AI-powered deployment manager that automates the end-to-end onboarding and deployment of applications to Kubernetes via a GitOps workflow. Developers chat with the agent to onboard a new app; DevOps engineers review and approve; ArgoCD deploys automatically.

---

## How It Works

```
Developer                     DevOps Agent                    GitOps Repo / ArgoCD
──────────────────────────────────────────────────────────────────────────────────
1. Pastes GitHub repo URL
   in Developer chat
                              2. Clones repo, detects stack,
                                 port, Dockerfile, env vars

                              3. Asks only what's needed:
                                 environments, domains, secrets

4. Developer answers

                              5. Shows deployment summary,
                                 asks developer to confirm

5. Developer clicks Confirm

                              6. Saves deployment plan
                                 (status: pending_review)

── DevOps engineer opens their chat ─────────────────────────────────────────────

                              7. Lists pending requests,
                                 runs security checks

                              8. Prepares GitOps files,
                                 shows DevOps for review

9. DevOps approves

                              10. Pushes Helm values + ArgoCD
                                  Application CR to GitOps repo
                                                                ← ArgoCD auto-syncs
                                                                   app to cluster

                              11. Generates GitHub Actions CI/CD
                                  workflow for developer

10. Developer commits workflow → every push to main auto-deploys via ArgoCD
──────────────────────────────────────────────────────────────────────────────────
```

---

## Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend build)
- An OpenAI-compatible LLM API (Anthropic, OpenAI, Ollama, etc.)
- GitHub token with `repo` scope (for cloning private repos)
- Kubernetes cluster with ArgoCD installed (for production GitOps deployments)

---

## Quick Start (Local Dev)

```bash
# 1. Clone and install dependencies
git clone <this-repo>
cd devops-agent
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, GITHUB_TOKEN

# 3. Build the frontend
cd devops_agent/web/frontend
npm install
npm run build
cd ../../..

# 4. Run
python main.py serve --reload
# Open http://localhost:8000
```

In local dev mode (no `ENTRA_CLIENT_ID` set), all requests run as admin with full DevOps access.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_BASE_URL` | ✅ | OpenAI-compatible API base URL |
| `LLM_API_KEY` | ✅ | API key for the LLM provider |
| `LLM_MODEL` | ✅ | Model name, e.g. `claude-opus-4-6` |
| `GITHUB_TOKEN` | ✅ | GitHub PAT for cloning repos and pushing GitOps files |
| `SECRET_KEY` | ✅ (prod) | Secret for signing OIDC session cookies |
| `ENTRA_TENANT_ID` | SSO only | Microsoft Entra (Azure AD) tenant ID |
| `ENTRA_CLIENT_ID` | SSO only | Entra app registration client ID |
| `ENTRA_CLIENT_SECRET` | SSO only | Entra client secret |
| `ENTRA_REDIRECT_URI` | SSO only | OAuth callback URL, e.g. `https://your-domain/auth/callback` |
| `ENTRA_ADMIN_GROUP_ID` | optional | Entra group whose members get admin role |
| `PORT` | optional | HTTP port (default: `8000`) |
| `DATA_DIR` | optional | Directory for SQLite database (default: `/data`) |

**GitOps/ArgoCD settings** (can also be set via the Settings UI):

| Variable | Description |
|----------|-------------|
| `GITOPS_REPO` | GitOps repo slug, e.g. `myorg/gitops` |
| `GITOPS_TOKEN` | GitHub PAT with write access to the GitOps repo (falls back to `GITHUB_TOKEN`) |
| `GITOPS_BRANCH` | Target branch (default: `main`) |
| `ARGOCD_URL` | ArgoCD server URL, e.g. `http://argocd.example.com` |
| `ARGOCD_TOKEN` | ArgoCD API token |

---

## Docker

```bash
# Build (ARM64 for Apple Silicon / GB10 DGX)
docker build --platform linux/arm64 -t devops-agent:latest .

# Run
docker run -p 8000:8000 \
  -v $(pwd)/data:/data \
  --env-file .env \
  devops-agent:latest

# Or with Docker Compose
docker compose up
```

---

## Kubernetes Deployment

Manifests are in `k8s/`. The app runs in namespace `devops-agent` and expects a PVC mounted at `/data` for the SQLite database.

```bash
# Apply manifests
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/secret.yaml       # fill in your secrets first
kubectl apply -f k8s/rbac-template.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

# Update a secret value
kubectl patch secret devops-agent-env -n devops-agent \
  -p '{"data":{"KEY":"<base64-value>"}}'

# Restart to pick up secret changes
kubectl rollout restart deployment/devops-agent -n devops-agent
kubectl rollout status deployment/devops-agent -n devops-agent
```

---

## Authentication (Microsoft Entra ID / Azure AD)

When `ENTRA_CLIENT_ID` is set, the app requires SSO login via Microsoft Entra ID.

**Setup steps:**
1. Azure Portal → Entra ID → App registrations → New registration
2. Add redirect URI: `https://<your-domain>/auth/callback`
3. App roles → create three roles: `Admin`, `DevOps`, `Developer`
4. Enterprise Applications → Users and groups → assign roles to users/groups
5. Certificates & secrets → create a client secret → copy to `ENTRA_CLIENT_SECRET`

**Role mapping:**

| Entra App Role | Access |
|----------------|--------|
| `Admin` | Full access + user management + Settings page |
| `DevOps` | Full access: review requests, manage environments, push GitOps files |
| `Developer` | Onboard repos, submit deployment requests, view own projects |

Members of `ENTRA_ADMIN_GROUP_ID` also receive admin access regardless of app role.

---

## GitOps / ArgoCD Setup

See [`docs/gitops-setup.md`](docs/gitops-setup.md) for the full guide, including:

- Installing ArgoCD on Kubernetes
- Creating the shared Helm chart (`charts/app/`)
- Generating an ArgoCD API token
- Configuring ArgoCD to watch the GitOps repo
- Secrets management options (Sealed Secrets, External Secrets Operator)

**Quick config via Settings UI:**

Open **Settings → Environments → Global**, set:
- GitOps Repo, Token, Branch
- ArgoCD URL and Token
- Container Registry

These can be overridden per-environment for multi-cluster setups.

---

## Architecture

```
devops_agent/
├── agent.py              # Core streaming agent loop (LLM + tool dispatch)
├── config.py             # Settings (pydantic-settings, reads .env)
├── database.py           # SQLite: projects, environments, deployments, users
├── tools/
│   ├── registry.py       # All tool definitions + dispatcher (~1500 lines)
│   ├── argocd.py         # GitOps file push, ArgoCD CR generation, CI/CD workflow
│   ├── discovery.py      # Repo cloning, stack detection, question generation
│   └── security.py       # Pre/post-deploy security checks
├── deployers/
│   ├── kubernetes.py     # Direct kubectl deployer (no GitOps)
│   ├── ssh.py            # SSH deployer
│   └── docker_compose.py # Docker Compose deployer
└── web/
    ├── app.py            # FastAPI factory, startup (DB init + GitHub poll loop)
    ├── oidc.py           # Microsoft Entra ID OIDC auth
    ├── routes/
    │   ├── chat.py       # WebSocket endpoint — streams agent responses
    │   └── api.py        # REST API for sidebar data, CRUD for projects/envs
    └── frontend/         # React (Vite) — built output served from static/
```

**Two agent personas:**
- **Developer** — onboards new repos, submits deployment requests; cannot approve or manage infrastructure
- **DevOps** — reviews requests, manages environments, pushes GitOps files, approves deployments

**Deployment backends (in priority order):**
1. GitOps + ArgoCD — when `GITOPS_REPO` is configured (recommended)
2. Direct Kubernetes — `kubectl apply` via in-cluster kubeconfig
3. SSH — deploys over SSH to a remote host
4. Docker Compose — deploys via `docker compose up` over SSH

---

## Development

```bash
# Hot-reload local server
python main.py serve --reload

# Frontend dev server (with HMR)
cd devops_agent/web/frontend
npm run dev

# Rebuild frontend for production
npm run build
# Output goes to devops_agent/web/static/

# Inspect the database
sqlite3 data/devops_agent.db ".tables"
sqlite3 data/devops_agent.db "SELECT * FROM projects;"
```

---

## Useful Commands

```bash
# View logs
kubectl logs -n devops-agent deployment/devops-agent -f

# Check ArgoCD applications
kubectl get applications -n argocd

# Force ArgoCD sync
argocd app sync <project>-<env>

# Rollback via ArgoCD
argocd app rollback <project>-<env> <revision>
```
