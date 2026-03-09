# GitOps Repository

This repository is the source of truth for all Kubernetes deployments managed by [DevOps Agent](https://github.com/cloudomate/devops-agent) and ArgoCD.

All files in this repository are **auto-generated** by the DevOps Agent — do not edit manually unless you know what you're doing.

---

## Repository Structure

```
├── charts/
│   └── app/                          # Shared Helm chart used by ALL projects
│       ├── Chart.yaml
│       ├── values.yaml               # Default values (safe fallbacks)
│       └── templates/
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── ingress.yaml
│           ├── configmap.yaml
│           ├── secret.yaml
│           └── hpa.yaml
│
├── projects/
│   ├── <project-slug>/
│   │   ├── values-staging.yaml       # Per-environment Helm values
│   │   └── values-prod.yaml
│   └── ...
│
└── argocd/
    ├── <project>-<env>.yaml          # ArgoCD Application CRs
    └── ...
```

---

## How It Works

1. **DevOps Agent** reviews a deployment request and pushes:
   - `projects/<project>/values-<env>.yaml` — Helm values for the app
   - `argocd/<project>-<env>.yaml` — ArgoCD Application CR

2. **ArgoCD** detects the new/updated files and syncs the application to the cluster automatically.

3. **CI/CD** (GitHub Actions in the app repo) updates `image.tag` in the values file on every push to `main` — ArgoCD picks up the change and re-deploys.

```
git push (app repo)
  → GitHub Actions builds & pushes Docker image
  → Actions updates image.tag in this repo
  → ArgoCD detects change → syncs to cluster ✅
```

---

## Shared Helm Chart

One chart (`charts/app/`) serves all projects. Per-project differences live only in `projects/<slug>/values-<env>.yaml`.

Key values:

```yaml
app: my-api                          # App name slug
environment: staging                 # dev | staging | prod

image:
  repository: ghcr.io/org/my-api
  tag: "abc1234"                     # Updated by CI on every deploy

replicas: 2
ingress:
  host: my-api.example.com

env:                                 # Non-secret env vars (ConfigMap)
  LOG_LEVEL: info

secrets:                             # Secret env vars (Kubernetes Secret)
  DATABASE_URL: postgres://...
  REDIS_URL: redis://...

resources:
  requests: { cpu: 100m, memory: 128Mi }
  limits:   { cpu: 500m, memory: 512Mi }

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 8
```

---

## ArgoCD Application CR

Each project+environment has its own `argocd/<project>-<env>.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-api-staging
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/cloudomate/gitops
    targetRevision: main
    path: charts/app
    helm:
      valueFiles:
        - ../../projects/my-api/values-staging.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: my-api-staging
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

---

## Namespace Strategy

Each project+environment gets its own namespace for isolation:

| Project | Environment | Namespace |
|---------|-------------|-----------|
| my-api | staging | `my-api-staging` |
| my-api | prod | `my-api-prod` |
| another-app | staging | `another-app-staging` |

---

## CI/CD Integration

After the first deployment, DevOps Agent generates a GitHub Actions workflow for the app repo. The workflow:

1. Builds and pushes the Docker image
2. Updates `image.tag` in this repo via a commit
3. ArgoCD detects the commit and auto-deploys

Required secrets in the app repo:

| Secret | Description |
|--------|-------------|
| `GITOPS_TOKEN` | GitHub PAT with write access to this repo |
| `REGISTRY_USERNAME` | Container registry username (not needed for GHCR) |
| `REGISTRY_PASSWORD` | Container registry password (not needed for GHCR) |

> If using GitHub Container Registry (`ghcr.io`), `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` are not needed — the workflow uses `GITHUB_TOKEN` automatically.

---

## ArgoCD Quick Reference

```bash
# View all managed apps
argocd app list

# Check sync status
argocd app get <project>-<env>

# Force sync
argocd app sync <project>-<env>

# View diff before sync
argocd app diff <project>-<env>

# Rollback to previous revision
argocd app rollback <project>-<env> <revision>

# View history
argocd app history <project>-<env>
```

---

## Setup

See the [DevOps Agent GitOps Setup Guide](https://github.com/cloudomate/devops-agent/blob/main/docs/gitops-setup.md) for full instructions on:

- Installing ArgoCD
- Connecting ArgoCD to this repository
- Generating ArgoCD API tokens
- Configuring the DevOps Agent to push here
