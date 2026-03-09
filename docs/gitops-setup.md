# GitOps Setup Guide

This guide covers how to set up the GitOps repository and ArgoCD so that the devops-agent can manage deployments across all projects using a single workflow.

---

## Architecture Overview

```
Developer  ──→  devops-agent (chat)
                  │  repo scan + questions + security check
                  │  deployment request submitted
                  ▼
DevOps     ──→  devops-agent (review & approve)
                  │  generates values file
                  │  git push → <org>/gitops repo
                  ▼
ArgoCD     ──→  detects change in gitops repo
                  │  syncs manifests to Kubernetes
                  ▼
Cluster    ──→  app is running ✅

Future deploys (CI/CD, no human needed):
  git push to app repo
    → GitHub Actions builds image, pushes to registry
    → Actions updates image.tag in gitops repo
    → ArgoCD auto-syncs → deployed ✅
```

---

## GitOps Repository Structure

Create a repository: `<org>/gitops`

```
<org>/gitops/
│
├── charts/
│   └── app/                         # Generic Helm chart for any web app
│       ├── Chart.yaml
│       ├── values.yaml              # Default values (safe fallbacks)
│       └── templates/
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── ingress.yaml
│           ├── hpa.yaml
│           ├── serviceaccount.yaml
│           └── externalsecret.yaml  # Optional: if using External Secrets Operator
│
├── projects/
│   ├── <project-a>/
│   │   ├── values-dev.yaml
│   │   ├── values-staging.yaml
│   │   └── values-prod.yaml
│   ├── <project-b>/
│   │   ├── values-staging.yaml
│   │   └── values-prod.yaml
│   └── ...
│
└── argocd/
    ├── <project-a>-dev.yaml         # ArgoCD Application CRs
    ├── <project-a>-staging.yaml
    ├── <project-a>-prod.yaml
    ├── <project-b>-staging.yaml
    └── ...
```

---

## 1. Install ArgoCD on the Cluster

```bash
kubectl create namespace argocd

kubectl apply -n argocd -f \
  https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for pods to be ready
kubectl wait --for=condition=available deployment -l app.kubernetes.io/name=argocd-server \
  -n argocd --timeout=120s
```

### Expose the ArgoCD UI

**Option A — Ingress (recommended if Traefik/nginx is already set up):**
```yaml
# argocd-ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: argocd
  namespace: argocd
  annotations:
    kubernetes.io/ingress.class: traefik
spec:
  rules:
    - host: argocd.<your-domain>
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: argocd-server
                port:
                  number: 80
```
```bash
kubectl apply -f argocd-ingress.yaml

# Disable TLS redirect inside ArgoCD (TLS handled upstream by reverse proxy)
kubectl patch configmap argocd-cmd-params-cm -n argocd \
  --patch '{"data":{"server.insecure":"true"}}'
kubectl rollout restart deployment argocd-server -n argocd
```

**Option B — Port-forward (local access only):**
```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Access at https://localhost:8080
```

### Get the initial admin password
```bash
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d && echo
```

### Install the ArgoCD CLI (optional but useful)
```bash
# macOS
brew install argocd

# Linux
curl -sSL -o /usr/local/bin/argocd \
  https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
chmod +x /usr/local/bin/argocd

argocd login argocd.<your-domain> --username admin --password <password>
```

---

## 2. Create the Generic Helm Chart

```bash
mkdir -p charts/app/templates
```

### `charts/app/Chart.yaml`
```yaml
apiVersion: v2
name: app
description: Generic web application chart
type: application
version: 1.0.0
appVersion: "1.0.0"
```

### `charts/app/values.yaml`
```yaml
# Required — set per project/environment
app: ""           # app name slug (e.g. "my-api")
environment: ""   # dev | staging | prod

image:
  repository: ""  # e.g. cr.example.com/org/my-api
  tag: latest
  pullPolicy: IfNotPresent

replicas: 1

service:
  port: 80
  targetPort: 8000

ingress:
  enabled: true
  host: ""        # e.g. my-api.example.com
  className: traefik

resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi

autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  targetCPUUtilizationPercentage: 70

env: {}           # key: value env vars (non-secret)
secrets: {}       # key: value — stored in a Secret, injected as env vars

serviceAccount:
  create: false

healthCheck:
  path: /health
  port: 8000
```

### `charts/app/templates/deployment.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Values.app }}
  namespace: {{ .Release.Namespace }}
  labels:
    app: {{ .Values.app }}
    env: {{ .Values.environment }}
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app: {{ .Values.app }}
  template:
    metadata:
      labels:
        app: {{ .Values.app }}
        env: {{ .Values.environment }}
    spec:
      containers:
        - name: {{ .Values.app }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ .Values.service.targetPort }}
          livenessProbe:
            httpGet:
              path: {{ .Values.healthCheck.path }}
              port: {{ .Values.healthCheck.port }}
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: {{ .Values.healthCheck.path }}
              port: {{ .Values.healthCheck.port }}
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          {{- if or .Values.env .Values.secrets }}
          envFrom:
            {{- if .Values.env }}
            - configMapRef:
                name: {{ .Values.app }}-config
            {{- end }}
            {{- if .Values.secrets }}
            - secretRef:
                name: {{ .Values.app }}-secrets
            {{- end }}
          {{- end }}
```

### `charts/app/templates/service.yaml`
```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ .Values.app }}
  namespace: {{ .Release.Namespace }}
spec:
  selector:
    app: {{ .Values.app }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: {{ .Values.service.targetPort }}
```

### `charts/app/templates/ingress.yaml`
```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Values.app }}
  namespace: {{ .Release.Namespace }}
  annotations:
    kubernetes.io/ingress.class: {{ .Values.ingress.className }}
spec:
  rules:
    - host: {{ .Values.ingress.host }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .Values.app }}
                port:
                  number: {{ .Values.service.port }}
{{- end }}
```

### `charts/app/templates/configmap.yaml`
```yaml
{{- if .Values.env }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Values.app }}-config
  namespace: {{ .Release.Namespace }}
data:
  {{- range $k, $v := .Values.env }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
{{- end }}
```

### `charts/app/templates/secret.yaml`
```yaml
{{- if .Values.secrets }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Values.app }}-secrets
  namespace: {{ .Release.Namespace }}
type: Opaque
stringData:
  {{- range $k, $v := .Values.secrets }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
{{- end }}
```

### `charts/app/templates/hpa.yaml`
```yaml
{{- if .Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .Values.app }}
  namespace: {{ .Release.Namespace }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ .Values.app }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
```

---

## 3. Add a Project

When the devops-agent approves a deployment request, it creates:

### `projects/<project>/values-<env>.yaml`
```yaml
# projects/my-api/values-staging.yaml
app: my-api
environment: staging

image:
  repository: cr.example.com/org/my-api
  tag: "a3f2c1d"       # ← updated by CI on every deploy

replicas: 2

ingress:
  host: my-api-staging.example.com

env:
  LOG_LEVEL: info
  PORT: "8000"

secrets:
  DATABASE_URL: "postgresql://user:pass@db-host:5432/myapi_staging"
  REDIS_URL: "redis://:secret@redis-host:6379"
  API_KEY: "sk-..."

resources:
  requests: { cpu: 100m, memory: 128Mi }
  limits:   { cpu: 500m, memory: 512Mi }

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 8
  targetCPUUtilizationPercentage: 70

healthCheck:
  path: /health
  port: 8000
```

### `argocd/<project>-<env>.yaml`
```yaml
# argocd/my-api-staging.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-api-staging
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/<org>/gitops
    targetRevision: main
    path: charts/app
    helm:
      valueFiles:
        - ../../projects/my-api/values-staging.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: my-api-staging    # one namespace per project+env
  syncPolicy:
    automated:
      prune: true       # remove resources deleted from git
      selfHeal: true    # revert manual kubectl changes
    syncOptions:
      - CreateNamespace=true
```

Apply once to register the app with ArgoCD:
```bash
kubectl apply -f argocd/my-api-staging.yaml
```

ArgoCD then manages all future syncs automatically.

---

## 4. Connect ArgoCD to the GitOps Repo

```bash
# If the repo is private, add credentials first
argocd repo add https://github.com/<org>/gitops \
  --username git \
  --password <github-pat>

# Or via SSH
argocd repo add git@github.com:<org>/gitops.git \
  --ssh-private-key-data "$(cat ~/.ssh/id_ed25519)"
```

---

## 5. CI/CD — Automated Image Updates

Add this step to the project's GitHub Actions workflow after `docker push`:

```yaml
# .github/workflows/deploy.yml (in the app repo)
- name: Update image tag in GitOps repo
  env:
    GITOPS_TOKEN: ${{ secrets.GITOPS_TOKEN }}   # PAT with repo write access
    IMAGE_TAG: ${{ github.sha }}
  run: |
    git clone https://x-access-token:${GITOPS_TOKEN}@github.com/<org>/gitops.git
    cd gitops

    # Update the image tag for the target environment
    sed -i "s/tag: .*/tag: \"${IMAGE_TAG}\"/" \
      projects/${{ env.APP_NAME }}/values-staging.yaml

    git config user.email "ci@example.com"
    git config user.name "CI"
    git add .
    git commit -m "deploy: ${{ env.APP_NAME }} → ${IMAGE_TAG}"
    git push
```

ArgoCD picks up the commit within seconds and syncs.

---

## 6. Namespace Strategy

Each project+environment gets its own namespace:

| Project | Environment | Namespace |
|---------|-------------|-----------|
| my-api | dev | my-api-dev |
| my-api | staging | my-api-staging |
| my-api | prod | my-api-prod |
| objectio | staging | objectio-staging |

This provides isolation between projects and environments with no risk of name collisions.

---

## 7. Secrets Management (Production)

For production, avoid storing secrets in the GitOps repo as plaintext. Options:

**Option A — Sealed Secrets (simplest):**
```bash
# Install Sealed Secrets controller
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/latest/download/controller.yaml

# Seal a secret
kubeseal --format yaml < my-secret.yaml > my-sealed-secret.yaml
# Commit my-sealed-secret.yaml to the GitOps repo — safe to store in git
```

**Option B — External Secrets Operator + Vault/AWS Secrets Manager:**
```yaml
# externalsecret.yaml stored in gitops — references secrets by name, not value
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: my-api-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: my-api-secrets
  data:
    - secretKey: DATABASE_URL
      remoteRef:
        key: my-api/staging
        property: database_url
```

---

## 8. Configuring devops-agent Settings

The devops-agent reads ArgoCD and GitOps settings from three sources, in priority order (most specific wins):

```text
Environment config values  →  App environment variables  →  k8s Secret
```

### Option A — Settings UI (recommended)

Open **Settings → Global Environments**, edit an environment, and fill in:

| Section | Field | Description |
| --- | --- | --- |
| **GitOps Repository** | Repo | `org/repo` slug, e.g. `myorg/gitops` |
| | Token | GitHub PAT with `repo` write scope (falls back to GitHub PAT if blank) |
| | Branch | Target branch (default: `main`) |
| **ArgoCD** | URL | ArgoCD server URL, e.g. `http://argocd.example.com` |
| | Token | ArgoCD API token (see §Generating an ArgoCD API Token below) |

Values set here are stored in the environment's config and take effect immediately on the next deployment approval.

You can also override these **per-environment** — e.g. point staging at a different ArgoCD instance — by editing the specific environment's config rather than the global one.

### Option B — Kubernetes Secret (cluster-wide defaults)

```bash
# Patch the devops-agent secret directly
kubectl patch secret devops-agent-env -n devops-agent -p "{\"data\":{
  \"ARGOCD_URL\":\"$(echo -n 'http://argocd.example.com' | base64 -w0)\",
  \"ARGOCD_TOKEN\":\"$(echo -n '<token>' | base64 -w0)\",
  \"GITOPS_REPO\":\"$(echo -n 'myorg/gitops' | base64 -w0)\",
  \"GITOPS_TOKEN\":\"$(echo -n 'ghp_...' | base64 -w0)\",
  \"GITOPS_BRANCH\":\"$(echo -n 'main' | base64 -w0)\"
}}"

kubectl rollout restart deployment/devops-agent -n devops-agent
```

### Option C — `.env` file (local dev)

```env
ARGOCD_URL=http://localhost:8080
ARGOCD_TOKEN=eyJ...
GITOPS_REPO=myorg/gitops
GITOPS_TOKEN=ghp_...
GITOPS_BRANCH=main
```

### Generating an ArgoCD API Token

```bash
# 1. Enable apiKey capability for the admin account
kubectl patch configmap argocd-cm -n argocd \
  --patch '{"data":{"accounts.admin":"apiKey,login"}}'

# 2. Get a session token
SESSION=$(curl -sk http://<argocd-cluster-ip>/api/v1/session \
  -d '{"username":"admin","password":"<password>"}' \
  -H 'Content-Type: application/json' | jq -r .token)

# 3. Generate a permanent API token
curl -sk http://<argocd-cluster-ip>/api/v1/account/admin/token \
  -X POST \
  -H "Authorization: Bearer $SESSION" \
  -H 'Content-Type: application/json' \
  -d '{"name":"devops-agent"}' | jq -r .token
```

### Settings Priority

| Setting | Per-env UI | k8s Secret / `.env` | Fallback |
| --- | --- | --- | --- |
| `ARGOCD_URL` | ✅ overrides | ✅ global default | — |
| `ARGOCD_TOKEN` | ✅ overrides | ✅ global default | — |
| `GITOPS_REPO` | ✅ overrides | ✅ global default | — |
| `GITOPS_TOKEN` | ✅ overrides | ✅ global default | `GITHUB_TOKEN` |
| `GITOPS_BRANCH` | ✅ overrides | ✅ global default | `main` |

If `ARGOCD_URL` / `ARGOCD_TOKEN` are not set, the agent skips the ArgoCD sync step (GitOps files are still pushed to the repo and ArgoCD will auto-detect them if configured with auto-sync).

If `GITOPS_REPO` / `GITOPS_TOKEN` are not set, the agent falls back to the legacy direct deployer (kubectl / Docker).

---

## Quick Reference

| Action | Command |
|--------|---------|
| Install ArgoCD | `kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml` |
| Get admin password | `kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" \| base64 -d` |
| Enable apiKey capability | `kubectl patch configmap argocd-cm -n argocd --patch '{"data":{"accounts.admin":"apiKey,login"}}'` |
| Generate API token | See §8 above |
| Register new app | `kubectl apply -f argocd/<project>-<env>.yaml` |
| Force sync | `argocd app sync <project>-<env>` |
| Check app status | `argocd app get <project>-<env>` |
| View all apps | `argocd app list` |
| Rollback | `argocd app rollback <project>-<env> <revision>` |
| Diff (what would change) | `argocd app diff <project>-<env>` |
| Patch devops-agent secret | `kubectl patch secret devops-agent-env -n devops-agent -p '{"data":{...}}'` |
| Restart devops-agent | `kubectl rollout restart deployment/devops-agent -n devops-agent` |
