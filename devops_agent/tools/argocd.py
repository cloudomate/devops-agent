"""ArgoCD + GitOps integration.

Provides:
  - GitOps repo management via GitHub API (no git CLI needed)
  - ArgoCD Application create / sync / status via ArgoCD REST API
  - Value file generation from plan_config
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import yaml


# ─── GitOps repo helpers ──────────────────────────────────────────────────────

def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def push_gitops_file(
    repo: str,
    path: str,
    content: str,
    commit_message: str,
    token: str,
    branch: str = "main",
) -> dict[str, Any]:
    """Create or update a file in the GitOps repo via GitHub API.

    Returns the GitHub API response dict.
    Raises httpx.HTTPError on failure.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = _github_headers(token)

    # Check if file exists (need SHA to update)
    sha = None
    r = httpx.get(url, headers=headers, params={"ref": branch}, timeout=10)
    if r.status_code == 200:
        sha = r.json().get("sha")

    body: dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    r = httpx.put(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def get_gitops_file(repo: str, path: str, token: str, branch: str = "main") -> str | None:
    """Return decoded content of a file from the GitOps repo, or None if not found."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = httpx.get(url, headers=_github_headers(token), params={"ref": branch}, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode()


# ─── Values YAML generation ───────────────────────────────────────────────────

# Scaling profiles keyed by expected concurrent users bracket.
_SCALING_PROFILES: dict[str, dict[str, Any]] = {
    "small": {   # < 100 concurrent users
        "replicaCount": 1,
        "autoscaling": {"enabled": False, "minReplicas": 1, "maxReplicas": 1},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
    },
    "medium": {  # 100 – 1 000 concurrent users
        "replicaCount": 2,
        "autoscaling": {"enabled": True, "minReplicas": 2, "maxReplicas": 5,
                        "targetCPUUtilizationPercentage": 70, "targetMemoryUtilizationPercentage": 80},
        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"cpu": "1000m", "memory": "1Gi"}},
    },
    "large": {   # 1 000 – 10 000 concurrent users
        "replicaCount": 3,
        "autoscaling": {"enabled": True, "minReplicas": 3, "maxReplicas": 10,
                        "targetCPUUtilizationPercentage": 70, "targetMemoryUtilizationPercentage": 80},
        "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "2000m", "memory": "2Gi"}},
    },
    "xlarge": {  # > 10 000 concurrent users
        "replicaCount": 5,
        "autoscaling": {"enabled": True, "minReplicas": 5, "maxReplicas": 20,
                        "targetCPUUtilizationPercentage": 65, "targetMemoryUtilizationPercentage": 75},
        "resources": {"requests": {"cpu": "1000m", "memory": "1Gi"}, "limits": {"cpu": "4000m", "memory": "4Gi"}},
    },
}

_SECRET_PAT = re.compile(r"PASSWORD|SECRET|_KEY|_TOKEN|API_KEY|PRIVATE|ACCESS_KEY|AUTH_", re.I)


def build_values_yaml(
    project: str,
    environment: str,
    image_or_ref: str,
    plan_config: dict[str, Any],
    env_config: dict[str, Any] | None = None,
    service_name: str = "",
    scaling_profile: str = "small",
) -> str:
    """Generate a values.yaml for the shared Helm chart (charts/app/).

    Args:
        project:         Project slug, e.g. "my-app".
        environment:     Environment name, e.g. "staging" or "prod".
        image_or_ref:    Full image ref with tag, e.g. "cr.io/org/app:abc123".
        plan_config:     Dict from deployment plan — env_vars, secrets, health_check,
                         domain, replicas, autoscaling, resources, etc.
        env_config:      Environment-level config from DB (cloudflare, ingress_class, …).
        service_name:    For multi-service projects: "frontend", "backend", etc.
                         Empty string = single-service (fullname = project slug).
        scaling_profile: "small" | "medium" | "large" | "xlarge"
    """
    if ":" in image_or_ref and "/" in image_or_ref:
        repo_part, tag = image_or_ref.rsplit(":", 1)
    else:
        repo_part, tag = image_or_ref, "latest"

    pc       = plan_config or {}
    env_cfg  = env_config or {}
    profile  = _SCALING_PROFILES.get(scaling_profile, _SCALING_PROFILES["small"])

    hc       = pc.get("health_check") or env_cfg.get("health_check") or {}
    domain   = pc.get("domain") or env_cfg.get("domain") or ""
    cf_cfg   = env_cfg.get("cloudflare") or {}
    tunnel   = cf_cfg.get("tunnel_enabled", False)

    # Resources / scaling: plan_config overrides profile
    resources  = pc.get("resources") or profile["resources"]
    autoscaling = {**profile["autoscaling"], **(pc.get("autoscaling") or {})}
    replica_count = pc.get("replicas") or profile["replicaCount"]

    # Separate plain env vars from secrets
    all_env = {**(pc.get("env_vars") or {})}
    secrets = {**(pc.get("secrets") or {})}
    for k, v in (pc.get("values") or {}).items():
        if _SECRET_PAT.search(k):
            secrets[k] = v
        else:
            all_env[k] = v

    values: dict[str, Any] = {
        "app": project,
        "environment": environment,
        "image": {
            "repository": repo_part,
            "tag": tag,
            "pullPolicy": "IfNotPresent",
        },
        "replicaCount": replica_count,
        "containerPort": hc.get("port", 8000),
        "livenessProbe": {
            "httpGet": {"path": hc.get("path", "/health"), "port": "http"},
            "initialDelaySeconds": 10, "periodSeconds": 15, "failureThreshold": 3, "timeoutSeconds": 5,
        },
        "readinessProbe": {
            "httpGet": {"path": hc.get("path", "/health"), "port": "http"},
            "initialDelaySeconds": 5, "periodSeconds": 10, "failureThreshold": 3, "timeoutSeconds": 3,
        },
        "resources": resources,
        "autoscaling": autoscaling,
        "serviceConfig": {"type": "ClusterIP", "port": 80},
        "cloudflare": {"tunnelEnabled": tunnel},
        "ingress": {
            "enabled": bool(domain) and not tunnel,
            "className": env_cfg.get("ingress_class", "nginx"),
            "host": domain,
            "tls": {
                "enabled": bool(domain) and not tunnel,
                "clusterIssuer": env_cfg.get("cert_issuer", "letsencrypt-prod"),
            },
        },
    }

    if service_name:
        values["serviceName"] = service_name
    if all_env:
        values["env"] = all_env
    if secrets:
        values["secrets"] = secrets

    return yaml.dump(values, default_flow_style=False, sort_keys=False, allow_unicode=True)


def build_argocd_app_yaml(
    app_name: str,
    gitops_repo: str,
    project_slug: str,
    environment: str,
    namespace: str,
    gitops_branch: str = "main",
    service_name: str = "",
) -> str:
    """Generate an ArgoCD Application CR YAML.

    For multi-service projects, pass service_name (e.g. "frontend") — the values
    file will be at projects/<slug>/<service>/values-<env>.yaml and the ArgoCD app
    name should include the service (e.g. "myapp-frontend-staging").
    """
    if service_name:
        values_path = f"../../projects/{project_slug}/{service_name}/values-{environment}.yaml"
    else:
        values_path = f"../../projects/{project_slug}/values-{environment}.yaml"

    app: dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": app_name,
            "namespace": "argocd",
            "labels": {
                "app.kubernetes.io/part-of": project_slug,
                "environment": environment,
            },
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": f"https://github.com/{gitops_repo}",
                "targetRevision": gitops_branch,
                "path": "charts/app",
                "helm": {
                    "valueFiles": [values_path],
                },
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": namespace,
            },
            "syncPolicy": {
                "automated": {
                    "prune": True,
                    "selfHeal": True,
                },
                "syncOptions": [
                    "CreateNamespace=true",
                    "ServerSideApply=true",
                ],
            },
        },
    }
    return yaml.dump(app, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ─── Shared generic Helm chart (charts/app/) ─────────────────────────────────
#
# Single chart serves all projects. Per-project/env differences live only in
# projects/<slug>/[<service>/]values-<env>.yaml
#
# Multi-service: set `.Values.serviceName` (e.g. "frontend") → fullname becomes
#   "<app>-<serviceName>". Single-service: leave serviceName empty → fullname = app.
#
# Routing modes:
#   cloudflare.tunnelEnabled=true  → ClusterIP only, no Ingress. Tunnel configured
#                                    externally via Cloudflare API.
#   cloudflare.tunnelEnabled=false → nginx Ingress + cert-manager TLS (default).

SHARED_CHART_FILES: dict[str, str] = {
    "charts/app/Chart.yaml": """\
apiVersion: v2
name: app
description: >
  Shared Helm chart for all projects managed by devops-agent.
  Per-project values live in projects/<slug>/[<service>/]values-<env>.yaml.
type: application
version: 1.2.0
appVersion: "1.0.0"
keywords:
  - app
  - devops-agent
maintainers:
  - name: devops-agent
""",

    "charts/app/values.yaml": """\
# ── App identity ──────────────────────────────────────────────────────────────
# Required — override in projects/<slug>/values-<env>.yaml
app: ""          # project slug, e.g. "my-app"
serviceName: ""  # service within project, e.g. "frontend" (empty = single-service)
environment: ""  # staging | prod

# ── Image ─────────────────────────────────────────────────────────────────────
image:
  repository: ""
  tag: "latest"
  pullPolicy: IfNotPresent
  pullSecrets: []  # list of k8s secret names for private registries

# ── Scaling ───────────────────────────────────────────────────────────────────
replicaCount: 1

autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80

# ── Container ─────────────────────────────────────────────────────────────────
containerPort: 8000

resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"

# ── Health checks ─────────────────────────────────────────────────────────────
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3
  timeoutSeconds: 5

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
  timeoutSeconds: 3

# ── Security ──────────────────────────────────────────────────────────────────
podSecurityContext:
  runAsNonRoot: true
  runAsUser: 1000
  fsGroup: 1000

securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: false
  capabilities:
    drop: ["ALL"]

# ── Service ───────────────────────────────────────────────────────────────────
serviceConfig:
  type: ClusterIP  # ClusterIP | LoadBalancer | NodePort
  port: 80

# ── Ingress (nginx + cert-manager) ────────────────────────────────────────────
ingress:
  enabled: false
  className: "nginx"
  host: ""
  tls:
    enabled: true
    clusterIssuer: "letsencrypt-prod"  # cert-manager ClusterIssuer name
  annotations: {}
    # nginx.ingress.kubernetes.io/proxy-body-size: "50m"
    # nginx.ingress.kubernetes.io/proxy-read-timeout: "60"

# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────
# When tunnelEnabled=true: Ingress is suppressed, service type forced to ClusterIP.
# Tunnel ingress route and DNS must be configured via the Cloudflare API separately.
cloudflare:
  tunnelEnabled: false

# ── Config & Secrets ──────────────────────────────────────────────────────────
# Mounted via envFrom (ConfigMap and Secret respectively).
# For production, prefer ExternalSecrets or Vault injection over inline secrets.
env: {}      # NON-sensitive env vars: KEY: value
secrets: {}  # Sensitive env vars:    KEY: value  (stored in k8s Secret)

# ── Pod extras ────────────────────────────────────────────────────────────────
podAnnotations: {}

nodeSelector: {}
tolerations: []
affinity: {}
""",

    "charts/app/templates/_helpers.tpl": """\
{{/*
Full name: "app" for single-service, "app-service" for multi-service.
Truncated to 63 chars (k8s DNS label limit).
*/}}
{{- define "app.fullname" -}}
{{- if .Values.serviceName -}}
{{- printf "%s-%s" .Values.app .Values.serviceName | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Values.app | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{/*
Standard Kubernetes recommended labels.
*/}}
{{- define "app.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "app.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ .Values.app }}
environment: {{ .Values.environment }}
{{- end }}

{{/*
Selector labels — stable, used by Deployment selector and Service selector.
Changing these would require a delete+recreate of the Deployment.
*/}}
{{- define "app.selectorLabels" -}}
app.kubernetes.io/name: {{ include "app.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Effective service type — always ClusterIP when Cloudflare tunnel is enabled.
*/}}
{{- define "app.serviceType" -}}
{{- if .Values.cloudflare.tunnelEnabled -}}ClusterIP
{{- else -}}{{ .Values.serviceConfig.type }}
{{- end -}}
{{- end }}
""",

    "charts/app/templates/deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "app.selectorLabels" . | nindent 6 }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        {{- include "app.selectorLabels" . | nindent 8 }}
      {{- with .Values.podAnnotations }}
      annotations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
    spec:
      {{- with .Values.image.pullSecrets }}
      imagePullSecrets:
        {{- range . }}
        - name: {{ . }}
        {{- end }}
      {{- end }}
      terminationGracePeriodSeconds: 30
      {{- with .Values.podSecurityContext }}
      securityContext:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ include "app.fullname" . }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          {{- with .Values.securityContext }}
          securityContext:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          ports:
            - name: http
              containerPort: {{ .Values.containerPort }}
              protocol: TCP
          {{- with .Values.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .Values.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          {{- if or .Values.env .Values.secrets }}
          envFrom:
            {{- if .Values.env }}
            - configMapRef:
                name: {{ include "app.fullname" . }}-config
            {{- end }}
            {{- if .Values.secrets }}
            - secretRef:
                name: {{ include "app.fullname" . }}-secrets
            {{- end }}
          {{- end }}
      {{- with .Values.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
""",

    "charts/app/templates/service.yaml": """\
apiVersion: v1
kind: Service
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  type: {{ include "app.serviceType" . }}
  ports:
    - name: http
      port: {{ .Values.serviceConfig.port }}
      targetPort: http
      protocol: TCP
  selector:
    {{- include "app.selectorLabels" . | nindent 4 }}
""",

    "charts/app/templates/ingress.yaml": """\
{{- if and .Values.ingress.enabled (not .Values.cloudflare.tunnelEnabled) }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
  annotations:
    {{- if .Values.ingress.tls.enabled }}
    cert-manager.io/cluster-issuer: {{ .Values.ingress.tls.clusterIssuer | quote }}
    {{- end }}
    {{- with .Values.ingress.annotations }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
spec:
  ingressClassName: {{ .Values.ingress.className | quote }}
  {{- if .Values.ingress.tls.enabled }}
  tls:
    - hosts:
        - {{ .Values.ingress.host | quote }}
      secretName: {{ include "app.fullname" . }}-tls
  {{- end }}
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "app.fullname" . }}
                port:
                  name: http
{{- end }}
""",

    "charts/app/templates/configmap.yaml": """\
{{- if .Values.env }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "app.fullname" . }}-config
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
data:
  {{- range $k, $v := .Values.env }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
{{- end }}
""",

    "charts/app/templates/secret.yaml": """\
{{- if .Values.secrets }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "app.fullname" . }}-secrets
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
type: Opaque
stringData:
  {{- range $k, $v := .Values.secrets }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
{{- end }}
""",

    "charts/app/templates/hpa.yaml": """\
{{- if .Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "app.fullname" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    {{- if .Values.autoscaling.targetCPUUtilizationPercentage }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
    {{- end }}
    {{- if .Values.autoscaling.targetMemoryUtilizationPercentage }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetMemoryUtilizationPercentage }}
    {{- end }}
{{- end }}
""",

    "charts/app/templates/NOTES.txt": """\
App:         {{ include "app.fullname" . }}
Namespace:   {{ .Release.Namespace }}
Environment: {{ .Values.environment }}
Image:       {{ .Values.image.repository }}:{{ .Values.image.tag }}

{{- if .Values.cloudflare.tunnelEnabled }}
Routing: Cloudflare Tunnel (no Ingress — configure tunnel route externally)
{{- else if .Values.ingress.enabled }}
URL:  https://{{ .Values.ingress.host }}
TLS:  {{ if .Values.ingress.tls.enabled }}cert-manager / {{ .Values.ingress.tls.clusterIssuer }}{{ else }}disabled{{ end }}
{{- else }}
Svc:  {{ include "app.fullname" . }}.{{ .Release.Namespace }}.svc.cluster.local:{{ .Values.serviceConfig.port }}
      (no Ingress — internal only)
{{- end }}
""",
}


def push_shared_chart(
    gitops_repo: str,
    gitops_token: str,
    gitops_branch: str = "main",
    force: bool = False,
) -> dict[str, Any]:
    """Ensure charts/app/ (the shared generic chart) exists in the GitOps repo.

    Skips push if chart already exists and force=False.
    Uses git CLI for atomic commit.
    """
    import os, subprocess, tempfile

    steps: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")
        repo_url = f"https://x-token:{gitops_token}@github.com/{gitops_repo}.git"

        steps.append(f"[clone] git clone --depth=1 -b {gitops_branch} {gitops_repo}")
        clone = subprocess.run(
            ["git", "clone", "--depth=1", "-b", gitops_branch, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60,
        )
        if clone.returncode != 0:
            return {"steps": steps, "error": f"git clone failed: {(clone.stderr or clone.stdout).strip()}"}
        steps.append("[clone] OK")

        chart_yaml_path = os.path.join(repo_dir, "charts", "app", "Chart.yaml")
        if os.path.exists(chart_yaml_path) and not force:
            return {"steps": steps, "pushed": [], "skipped": list(SHARED_CHART_FILES.keys()), "status": "already_exists"}

        for rel_path, content in SHARED_CHART_FILES.items():
            full = os.path.join(repo_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        steps.append(f"[write] {len(SHARED_CHART_FILES)} chart files written")

        git = lambda *args: subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=30)
        git("config", "user.email", "devops-agent@bot")
        git("config", "user.name", "DevOps Agent")
        git("add", os.path.join(repo_dir, "charts", "app"))
        commit = git("commit", "-m", "chore(charts): add shared app Helm chart")
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            steps.append(f"[git] commit: {commit.stdout.strip()}")
        push = git("push")
        if push.returncode != 0:
            return {"steps": steps, "error": f"git push failed: {push.stderr.strip()}"}
        steps.append("[git] push OK")

    return {"steps": steps, "pushed": list(SHARED_CHART_FILES.keys()), "skipped": [], "status": "pushed"}


def validate_helm_chart(
    gitops_repo: str,
    gitops_token: str,
    values_content: str,
    gitops_branch: str = "main",
) -> dict[str, Any]:
    """Clone the GitOps repo, write a temp values file, run helm lint + helm template.

    Returns lint output and rendered manifests so DevOps can review before approving.
    Requires `helm` binary on PATH (present in the devops-agent container).

    Args:
        gitops_repo:    owner/repo slug.
        gitops_token:   GitHub token (read is sufficient).
        values_content: Full YAML content of the project values file to validate.
        gitops_branch:  Branch to clone (default: main).
    """
    import os, subprocess, tempfile

    steps: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")
        repo_url = f"https://x-token:{gitops_token}@github.com/{gitops_repo}.git"

        clone = subprocess.run(
            ["git", "clone", "--depth=1", "-b", gitops_branch, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60,
        )
        if clone.returncode != 0:
            return {"error": f"git clone failed: {(clone.stderr or clone.stdout).strip()}"}
        steps.append("[clone] OK")

        chart_dir = os.path.join(repo_dir, "charts", "app")
        if not os.path.isdir(chart_dir):
            return {"error": "charts/app/ not found in GitOps repo — run push_shared_chart first"}

        # Write values to a temp file inside tmpdir (not inside repo)
        values_file = os.path.join(tmpdir, "values-validate.yaml")
        with open(values_file, "w") as f:
            f.write(values_content)
        steps.append("[write] temp values file")

        def run_helm(*args: str) -> tuple[int, str]:
            r = subprocess.run(["helm", *args], capture_output=True, text=True, timeout=30)
            return r.returncode, (r.stdout + r.stderr).strip()

        # helm lint
        try:
            lint_rc, lint_out = run_helm("lint", chart_dir, "-f", values_file, "--strict")
            steps.append(f"[lint] {'OK' if lint_rc == 0 else 'FAILED'}")
            if lint_rc != 0:
                return {"steps": steps, "lint": lint_out, "error": "helm lint failed"}
        except FileNotFoundError:
            return {"steps": steps, "error": "helm binary not found — cannot validate"}

        # helm template — renders manifests for DevOps review
        template_rc, template_out = run_helm(
            "template", "preview", chart_dir,
            "-f", values_file,
            "--namespace", "preview",
        )
        steps.append(f"[template] {'OK' if template_rc == 0 else 'FAILED'}")

        return {
            "steps": steps,
            "lint": lint_out,
            "rendered_manifests": template_out if template_rc == 0 else None,
            "template_error": template_out if template_rc != 0 else None,
            "valid": lint_rc == 0 and template_rc == 0,
        }


def push_project_values(
    project_slug: str,
    environments: dict[str, dict[str, Any]],
    gitops_repo: str,
    gitops_token: str,
    gitops_branch: str = "main",
    service_name: str = "",
    scaling_profile: str = "small",
) -> dict[str, Any]:
    """Push values files for each environment to the GitOps repo atomically.

    Single-service:  projects/<slug>/values-<env>.yaml
    Multi-service:   projects/<slug>/<service>/values-<env>.yaml

    Args:
        project_slug:    e.g. "my-app"
        environments:    {env_name: {image, port, domain, env_vars, secrets,
                                     health_path, resources, autoscaling, cloudflare}}
        gitops_repo:     owner/repo slug
        gitops_token:    GitHub token with write access
        gitops_branch:   Branch to push to (default: main)
        service_name:    For multi-service: "frontend", "backend", etc. Empty = single.
        scaling_profile: "small" | "medium" | "large" | "xlarge"
    """
    import os, subprocess, tempfile

    steps: list[str] = []
    pushed: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")
        repo_url = f"https://x-token:{gitops_token}@github.com/{gitops_repo}.git"

        clone = subprocess.run(
            ["git", "clone", "--depth=1", "-b", gitops_branch, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60,
        )
        if clone.returncode != 0:
            return {"steps": steps, "error": f"git clone failed: {(clone.stderr or clone.stdout).strip()}"}
        steps.append("[clone] OK")

        # Determine output directory
        if service_name:
            out_dir = os.path.join(repo_dir, "projects", project_slug, service_name)
        else:
            out_dir = os.path.join(repo_dir, "projects", project_slug)
        os.makedirs(out_dir, exist_ok=True)

        for env_name, cfg in environments.items():
            image = cfg.get("image", "")
            port = cfg.get("port", 8000)
            plan_config = {
                "domain":       cfg.get("domain", ""),
                "health_check": {"path": cfg.get("health_path", "/health"), "port": port},
                "env_vars":     cfg.get("env_vars") or {},
                "secrets":      cfg.get("secrets") or {},
                "resources":    cfg.get("resources") or {},
                "autoscaling":  cfg.get("autoscaling") or {},
                "replicas":     cfg.get("replicas"),
            }
            env_config = {
                "ingress_class": cfg.get("ingress_class", "nginx"),
                "domain":        cfg.get("domain", ""),
                "cloudflare":    cfg.get("cloudflare") or {},
                "cert_issuer":   cfg.get("cert_issuer", "letsencrypt-prod"),
            }
            values_yaml = build_values_yaml(
                project_slug, env_name, image, plan_config, env_config,
                service_name=service_name, scaling_profile=scaling_profile,
            )

            if service_name:
                rel = f"projects/{project_slug}/{service_name}/values-{env_name}.yaml"
            else:
                rel = f"projects/{project_slug}/values-{env_name}.yaml"

            with open(os.path.join(repo_dir, rel), "w") as f:
                f.write(values_yaml)
            pushed.append(rel)
            steps.append(f"[write] {rel}")

        git = lambda *args: subprocess.run(  # noqa: E731
            ["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=30
        )
        git("config", "user.email", "devops-agent@bot")
        git("config", "user.name", "DevOps Agent")
        git("add", out_dir)
        svc_label = f"/{service_name}" if service_name else ""
        git("commit", "-m",
            f"feat({project_slug}{svc_label}): values files for {', '.join(environments.keys())}")
        push = git("push")
        if push.returncode != 0:
            return {"steps": steps, "error": f"git push failed: {push.stderr.strip()}"}
        steps.append("[git] push OK")

    return {"steps": steps, "pushed": pushed, "project": project_slug, "service": service_name}


def generate_cicd_workflow(
    project_slug: str,
    gitops_repo: str,
    registry: str,
    environments: list[str],
    services: list[dict[str, Any]] | None = None,
    branch: str = "main",
) -> str:
    """Generate a GitHub Actions workflow YAML.

    Workflow jobs:
      1. detect-changes  — determines which services have changed files
      2. build           — matrix job, builds only changed service images
      3. update-gitops   — updates image.tag in GitOps values files

    PR flow:
      - On PR open/update:  lint + test only (no build, no push)
      - On merge to main:   detect → build changed → update gitops

    Args:
        project_slug: Project slug, e.g. "my-app".
        gitops_repo:  owner/repo of the GitOps repo.
        registry:     Image registry prefix, e.g. "cr.io/org".
        environments: List of environment names, e.g. ["staging", "prod"].
        services:     List of service dicts from discovery:
                      [{"name": "frontend", "dockerfile": "frontend/Dockerfile",
                        "context": "frontend/", "watch_path": "frontend/"}]
                      If None or single-service, generates a simpler single-job workflow.
        branch:       Branch that triggers deployment (default: "main").
    """
    reg_host = registry.split("/")[0]
    is_ghcr = reg_host == "ghcr.io"
    multi = services and len(services) > 1

    # Build the registry login block (differs for GHCR vs custom registry)
    def _login_block(indent: str = "      ") -> str:
        if is_ghcr:
            return (
                f"{indent}- name: Log in to GitHub Container Registry\n"
                f"{indent}  uses: docker/login-action@v3\n"
                f"{indent}  with:\n"
                f"{indent}    registry: ghcr.io\n"
                f"{indent}    username: ${{{{{{ github.actor }}}}}}\n"
                f"{indent}    password: ${{{{{{ secrets.GITHUB_TOKEN }}}}}}"
            )
        return (
            f"{indent}- name: Log in to registry\n"
            f"{indent}  uses: docker/login-action@v3\n"
            f"{indent}  with:\n"
            f"{indent}    registry: {reg_host}\n"
            f"{indent}    username: ${{{{{{ secrets.REGISTRY_USERNAME }}}}}}\n"
            f"{indent}    password: ${{{{{{ secrets.REGISTRY_PASSWORD }}}}}}"
        )

    secrets_note = (
        "#   GITOPS_TOKEN       — GitHub PAT with write access to {gitops_repo}\n"
        "#   (GITHUB_TOKEN is used automatically for GHCR login)"
        if is_ghcr else
        "#   REGISTRY_USERNAME  — container registry login\n"
        "#   REGISTRY_PASSWORD  — container registry password / token\n"
        "#   GITOPS_TOKEN       — GitHub PAT with write access to {gitops_repo}"
    ).format(gitops_repo=gitops_repo)

    if not multi:
        # ── Single-service workflow ────────────────────────────────────────────
        svc = (services[0] if services else None) or {
            "name": project_slug, "dockerfile": "Dockerfile",
            "context": ".", "watch_path": ".",
        }
        svc_name  = svc["name"]
        image_name = f"{registry}/{project_slug}"
        values_paths = "\n          ".join(
            f'sed -i \'s|tag: .*|tag: "\'"$IMAGE_TAG"\'"|g\' projects/{project_slug}/values-{env}.yaml'
            for env in environments
        )
        add_paths = " ".join(f"projects/{project_slug}/values-{env}.yaml" for env in environments)
        commit_msg = f"deploy({project_slug}): $IMAGE_TAG"

        return f"""\
# .github/workflows/deploy.yml
# Auto-generated by devops-agent — commit this to your repository.
#
# Required GitHub Secrets:
{secrets_note}

name: CI/CD

on:
  pull_request:
    branches: ["{branch}"]
  push:
    branches: ["{branch}"]

concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  # ── PR: lint & test only ────────────────────────────────────────────────────
  test:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Add your lint / unit-test steps here

  # ── Merge to main: build → push → update GitOps ────────────────────────────
  build-and-deploy:
    if: github.event_name == 'push' && github.ref == 'refs/heads/{branch}'
    runs-on: ubuntu-latest
    env:
      IMAGE_TAG: ${{{{ github.sha }}}}

    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

{_login_block()}

      - name: Build and push {svc_name}
        uses: docker/build-push-action@v5
        with:
          context: {svc["context"]}
          file: {svc["dockerfile"]}
          push: true
          tags: |
            {image_name}:${{{{ github.sha }}}}
            {image_name}:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Update GitOps values
        env:
          GITOPS_TOKEN: ${{{{ secrets.GITOPS_TOKEN }}}}
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          git clone https://x-access-token:$GITOPS_TOKEN@github.com/{gitops_repo}.git gitops
          cd gitops
          {values_paths}
          git config user.email "ci-bot@github.com"
          git config user.name "GitHub Actions"
          git add {add_paths}
          git commit -m "{commit_msg}"
          git push
"""

    # ── Multi-service workflow ─────────────────────────────────────────────────
    # Build a JSON map used by detect-changes to figure out which service changed.
    svc_list = json.dumps([s["name"] for s in services])  # type: ignore[union-attr]

    # Per-service path-filter entries for detect-changes
    path_checks = ""
    for svc in services:  # type: ignore[union-attr]
        watch = svc.get("watch_path", svc["name"] + "/")
        path_checks += f"""\
          if git diff --name-only HEAD~1 HEAD | grep -q "^{watch}"; then
            changed+=("{svc['name']}")
          fi
"""

    # Per-service build steps (used in the matrix job)
    # GitHub Actions matrix can't easily branch on per-item values, so we
    # encode the full service map as a workflow-level env and look it up.
    svc_map_entries = "\n".join(
        f'          {s["name"]}) DOCKERFILE="{s["dockerfile"]}"; CTX="{s["context"]}" ;;'
        for s in services  # type: ignore[union-attr]
    )

    # GitOps update block — one sed per env per service (called with SERVICE and IMAGE_TAG)
    update_block = ""
    for env in environments:
        for svc in services:  # type: ignore[union-attr]
            svc_n = svc["name"]
            path = f"projects/{project_slug}/{svc_n}/values-{env}.yaml"
            update_block += f"""\
          if [[ "$SERVICE" == "{svc_n}" ]] && [[ -f "{path}" ]]; then
            sed -i 's|tag: .*|tag: "'"$IMAGE_TAG"'"|g' {path}
            git add {path}
          fi
"""

    return f"""\
# .github/workflows/deploy.yml
# Auto-generated by devops-agent — commit this to your repository.
#
# Services: {svc_list}
# GitOps:   https://github.com/{gitops_repo}
#
# Required GitHub Secrets:
{secrets_note}

name: CI/CD

on:
  pull_request:
    branches: ["{branch}"]
  push:
    branches: ["{branch}"]

concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  # ── PR: lint & test only ────────────────────────────────────────────────────
  test:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Add your lint / unit-test steps here

  # ── Merge to main: detect which services changed ────────────────────────────
  detect-changes:
    if: github.event_name == 'push' && github.ref == 'refs/heads/{branch}'
    runs-on: ubuntu-latest
    outputs:
      changed: ${{{{ steps.detect.outputs.changed }}}}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2  # need HEAD~1 for diff

      - name: Detect changed services
        id: detect
        run: |
          changed=()
{path_checks}
          if [ ${{#changed[@]}} -eq 0 ]; then
            echo "No service directories changed — skipping build"
          fi
          echo "changed=$(echo "${{changed[@]}}" | jq -R 'split(" ") | map(select(. != ""))')" >> "$GITHUB_OUTPUT"

  # ── Build only changed services (matrix) ───────────────────────────────────
  build:
    needs: detect-changes
    if: needs.detect-changes.outputs.changed != '[]'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        service: ${{{{ fromJSON(needs.detect-changes.outputs.changed) }}}}
      fail-fast: false
    env:
      IMAGE_TAG: ${{{{ github.sha }}}}

    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

{_login_block()}

      - name: Resolve Dockerfile for ${{{{ matrix.service }}}}
        id: resolve
        run: |
          SERVICE="${{{{ matrix.service }}}}"
          case "$SERVICE" in
{svc_map_entries}
            *) echo "Unknown service $SERVICE"; exit 1 ;;
          esac
          echo "dockerfile=$DOCKERFILE" >> "$GITHUB_OUTPUT"
          echo "context=$CTX" >> "$GITHUB_OUTPUT"

      - name: Build and push ${{{{ matrix.service }}}}
        uses: docker/build-push-action@v5
        with:
          context: ${{{{ steps.resolve.outputs.context }}}}
          file: ${{{{ steps.resolve.outputs.dockerfile }}}}
          push: true
          tags: |
            {registry}/{project_slug}-${{{{ matrix.service }}}}:${{{{ github.sha }}}}
            {registry}/{project_slug}-${{{{ matrix.service }}}}:latest
          cache-from: type=gha,scope=${{{{ matrix.service }}}}
          cache-to: type=gha,scope=${{{{ matrix.service }}}},mode=max

  # ── Update GitOps values files ─────────────────────────────────────────────
  update-gitops:
    needs: build
    runs-on: ubuntu-latest
    env:
      IMAGE_TAG: ${{{{ github.sha }}}}

    steps:
      - name: Checkout GitOps repo
        uses: actions/checkout@v4
        with:
          repository: {gitops_repo}
          token: ${{{{ secrets.GITOPS_TOKEN }}}}
          path: gitops

      - name: Update image tags
        env:
          CHANGED: ${{{{ needs.detect-changes.outputs.changed }}}}
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          cd gitops
          for SERVICE in $(echo "$CHANGED" | jq -r '.[]'); do
{update_block}
          done
          git config user.email "ci-bot@github.com"
          git config user.name "GitHub Actions"
          if git diff --cached --quiet; then
            echo "No values files changed"
          else
            git commit -m "deploy({project_slug}): $IMAGE_TAG"
            git push
          fi
"""


def push_helm_chart(
    project_slug: str,
    files: dict[str, str],
    gitops_repo: str,
    gitops_token: str,
    gitops_branch: str = "main",
    force: bool = False,
) -> dict[str, Any]:
    """Validate and push LLM-generated Helm chart files to the GitOps repo via git CLI.

    Workflow:
      1. git clone --depth=1 (authenticated via HTTPS token)
      2. Write chart files to charts/{project_slug}/
      3. helm lint --strict (abort if fails)
      4. git commit + git push (atomic, single commit)

    Args:
        project_slug: Project name slug (e.g. 'my-app').
        files: Dict of {relative_path: content} for chart files.
               Paths may be relative to charts/{project_slug}/ OR from repo root.
        gitops_repo: GitHub repo slug (owner/repo).
        gitops_token: GitHub token with write access.
        gitops_branch: Branch to push to (default: main).
        force: If True, overwrite existing chart files.

    Returns:
        dict with 'steps', 'pushed', 'skipped', 'lint', and optionally 'error'.
    """
    import os
    import subprocess
    import tempfile

    chart_prefix = f"charts/{project_slug}"
    steps: list[str] = []

    # Normalise paths — accept both relative-to-chart and repo-root paths
    normalised: dict[str, str] = {}
    for path, content in files.items():
        if not path.startswith("charts/"):
            path = f"{chart_prefix}/{path}"
        normalised[path] = content

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")

        # ── Step 1: Clone ─────────────────────────────────────────────────────
        repo_url = f"https://x-token:{gitops_token}@github.com/{gitops_repo}.git"
        steps.append(f"[clone] git clone --depth=1 -b {gitops_branch} {gitops_repo}")
        clone = subprocess.run(
            ["git", "clone", "--depth=1", "-b", gitops_branch, repo_url, repo_dir],
            capture_output=True, text=True, timeout=60,
        )
        if clone.returncode != 0:
            return {
                "steps": steps,
                "error": f"git clone failed: {(clone.stderr or clone.stdout).strip()}",
            }
        steps.append("[clone] OK")

        # ── Step 2: Check existence ───────────────────────────────────────────
        chart_yaml_path = os.path.join(repo_dir, chart_prefix, "Chart.yaml")
        already_exists = os.path.exists(chart_yaml_path)
        if already_exists and not force:
            return {
                "steps": steps,
                "pushed": [],
                "skipped": list(normalised.keys()),
                "lint": "skipped — chart already exists (use force=true to overwrite)",
            }

        # ── Step 3: Write files ───────────────────────────────────────────────
        steps.append(f"[write] Writing {len(normalised)} chart files to {chart_prefix}/")
        for path, content in normalised.items():
            full = os.path.join(repo_dir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        steps.append("[write] OK")

        # ── Step 4: Helm lint ─────────────────────────────────────────────────
        chart_dir = os.path.join(repo_dir, chart_prefix)
        lint_output = "(helm not found — lint skipped)"
        steps.append(f"[lint] helm lint --strict {chart_prefix}")
        try:
            lint_res = subprocess.run(
                ["helm", "lint", chart_dir, "--strict"],
                capture_output=True, text=True, timeout=30,
            )
            lint_output = (lint_res.stdout + lint_res.stderr).strip()
            if lint_res.returncode != 0:
                steps.append(f"[lint] FAILED:\n{lint_output}")
                return {
                    "steps": steps,
                    "pushed": [],
                    "skipped": [],
                    "lint": f"FAILED:\n{lint_output}",
                    "error": "helm lint failed — chart not pushed",
                }
            steps.append(f"[lint] OK: {lint_output.splitlines()[0] if lint_output else 'passed'}")
        except FileNotFoundError:
            steps.append("[lint] helm binary not found — skipped")
        except Exception as e:
            steps.append(f"[lint] error: {e}")

        # ── Step 5: Git commit + push ─────────────────────────────────────────
        action = "update" if already_exists else "add"
        commit_msg = f"chore({project_slug}): {action} Helm chart (generated by devops-agent)"

        git = lambda *args: subprocess.run(  # noqa: E731
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True, timeout=30,
        )
        git("config", "user.email", "devops-agent@bot")
        git("config", "user.name", "DevOps Agent")

        steps.append(f"[git] git add {chart_prefix}/")
        git("add", os.path.join(repo_dir, chart_prefix))

        steps.append(f"[git] git commit -m '{commit_msg}'")
        commit_res = git("commit", "-m", commit_msg)
        if commit_res.returncode != 0 and "nothing to commit" not in commit_res.stdout:
            steps.append(f"[git] commit output: {commit_res.stdout.strip()}")

        steps.append("[git] git push")
        push_res = git("push")
        if push_res.returncode != 0:
            steps.append(f"[git] push stderr: {push_res.stderr.strip()}")
            return {
                "steps": steps,
                "error": f"git push failed: {push_res.stderr.strip()}",
            }
        steps.append("[git] push OK")

    return {
        "steps": steps,
        "pushed": list(normalised.keys()),
        "skipped": [],
        "lint": lint_output,
        "chart_path": chart_prefix,
    }


def build_helm_chart_files(project_slug: str) -> dict[str, str]:
    """Return {gitops_repo_path: content} for a project-specific Helm chart.

    Files are pushed to charts/{project_slug}/ in the GitOps repo.
    Values format matches build_values_yaml() output.
    """
    n = project_slug

    chart_yaml = (
        f"apiVersion: v2\n"
        f"name: {n}\n"
        f"description: Helm chart for {n} (auto-generated by devops-agent)\n"
        f"type: application\n"
        f"version: 0.1.0\n"
        f"appVersion: \"latest\"\n"
    )

    values_yaml = (
        f"# Default values — overridden per-environment via projects/{n}/values-<env>.yaml\n"
        f"app: {n}\n"
        f"environment: staging\n\n"
        f"image:\n"
        f"  repository: your-registry/{n}\n"
        f"  tag: latest\n"
        f"  pullPolicy: IfNotPresent\n\n"
        f"replicas: 1\n\n"
        f"ingress:\n"
        f"  enabled: false\n"
        f"  host: \"\"\n"
        f"  className: traefik\n\n"
        f"service:\n"
        f"  port: 80\n"
        f"  targetPort: 8000\n\n"
        f"healthCheck:\n"
        f"  path: /health\n"
        f"  port: 8000\n\n"
        f"resources:\n"
        f"  requests:\n"
        f"    cpu: 100m\n"
        f"    memory: 128Mi\n"
        f"  limits:\n"
        f"    cpu: 500m\n"
        f"    memory: 512Mi\n\n"
        f"autoscaling:\n"
        f"  enabled: false\n"
        f"  minReplicas: 1\n"
        f"  maxReplicas: 5\n"
        f"  targetCPUUtilizationPercentage: 70\n\n"
        f"env: {{}}\n"
        f"secrets: {{}}\n"
    )

    helpers_tpl = (
        f'{{{{- define "{n}.fullname" -}}}}\n'
        f'{{{{- .Release.Name | trunc 63 | trimSuffix "-" }}}}\n'
        f"{{{{- end }}}}\n\n"
        f'{{{{- define "{n}.labels" -}}}}\n'
        f"helm.sh/chart: {{{{ .Chart.Name }}}}-{{{{ .Chart.Version }}}}\n"
        f'app.kubernetes.io/name: {{{{ include "{n}.fullname" . }}}}\n'
        f"app.kubernetes.io/managed-by: {{{{ .Release.Service }}}}\n"
        f"environment: {{{{ .Values.environment }}}}\n"
        f"{{{{- end }}}}\n"
    )

    deployment_yaml = (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f'  name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  namespace: {{{{ .Release.Namespace }}}}\n"
        f"  labels:\n"
        f'    {{{{- include "{n}.labels" . | nindent 4 }}}}\n'
        f"spec:\n"
        f"  replicas: {{{{ .Values.replicas }}}}\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f'      app.kubernetes.io/name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f'        app.kubernetes.io/name: {{{{ include "{n}.fullname" . }}}}\n'
        f"    spec:\n"
        f"      containers:\n"
        f"        - name: {{{{ .Chart.Name }}}}\n"
        f'          image: "{{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag }}}}"\n'
        f"          imagePullPolicy: {{{{ .Values.image.pullPolicy }}}}\n"
        f"          ports:\n"
        f"            - containerPort: {{{{ .Values.service.targetPort }}}}\n"
        f"              protocol: TCP\n"
        f"          {{{{- if .Values.healthCheck.path }}}}\n"
        f"          livenessProbe:\n"
        f"            httpGet:\n"
        f"              path: {{{{ .Values.healthCheck.path }}}}\n"
        f"              port: {{{{ .Values.healthCheck.port }}}}\n"
        f"            initialDelaySeconds: 15\n"
        f"            periodSeconds: 30\n"
        f"          readinessProbe:\n"
        f"            httpGet:\n"
        f"              path: {{{{ .Values.healthCheck.path }}}}\n"
        f"              port: {{{{ .Values.healthCheck.port }}}}\n"
        f"            initialDelaySeconds: 5\n"
        f"            periodSeconds: 10\n"
        f"          {{{{- end }}}}\n"
        f"          {{{{- if or .Values.env .Values.secrets }}}}\n"
        f"          env:\n"
        f"            {{{{- range $k, $v := .Values.env }}}}\n"
        f"            - name: {{{{ $k }}}}\n"
        f"              value: {{{{ $v | quote }}}}\n"
        f"            {{{{- end }}}}\n"
        f"            {{{{- range $k, $v := .Values.secrets }}}}\n"
        f"            - name: {{{{ $k }}}}\n"
        f"              valueFrom:\n"
        f"                secretKeyRef:\n"
        f'                  name: {{{{ include "{n}.fullname" $ }}}}-secrets\n'
        f"                  key: {{{{ $k }}}}\n"
        f"            {{{{- end }}}}\n"
        f"          {{{{- end }}}}\n"
        f"          resources:\n"
        f"            {{{{- toYaml .Values.resources | nindent 12 }}}}\n"
    )

    service_yaml = (
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n"
        f'  name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  namespace: {{{{ .Release.Namespace }}}}\n"
        f"  labels:\n"
        f'    {{{{- include "{n}.labels" . | nindent 4 }}}}\n'
        f"spec:\n"
        f"  type: ClusterIP\n"
        f"  ports:\n"
        f"    - port: {{{{ .Values.service.port }}}}\n"
        f"      targetPort: {{{{ .Values.service.targetPort }}}}\n"
        f"      protocol: TCP\n"
        f"  selector:\n"
        f'    app.kubernetes.io/name: {{{{ include "{n}.fullname" . }}}}\n'
    )

    ingress_yaml = (
        f"{{{{- if .Values.ingress.enabled }}}}\n"
        f"apiVersion: networking.k8s.io/v1\n"
        f"kind: Ingress\n"
        f"metadata:\n"
        f'  name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  namespace: {{{{ .Release.Namespace }}}}\n"
        f"  labels:\n"
        f'    {{{{- include "{n}.labels" . | nindent 4 }}}}\n'
        f"spec:\n"
        f"  ingressClassName: {{{{ .Values.ingress.className }}}}\n"
        f"  rules:\n"
        f"    - host: {{{{ .Values.ingress.host }}}}\n"
        f"      http:\n"
        f"        paths:\n"
        f"          - path: /\n"
        f"            pathType: Prefix\n"
        f"            backend:\n"
        f"              service:\n"
        f'                name: {{{{ include "{n}.fullname" . }}}}\n'
        f"                port:\n"
        f"                  number: {{{{ .Values.service.port }}}}\n"
        f"{{{{- end }}}}\n"
    )

    secret_yaml = (
        f"{{{{- if .Values.secrets }}}}\n"
        f"apiVersion: v1\n"
        f"kind: Secret\n"
        f"metadata:\n"
        f'  name: {{{{ include "{n}.fullname" . }}}}-secrets\n'
        f"  namespace: {{{{ .Release.Namespace }}}}\n"
        f"  labels:\n"
        f'    {{{{- include "{n}.labels" . | nindent 4 }}}}\n'
        f"type: Opaque\n"
        f"stringData:\n"
        f"  {{{{- range $k, $v := .Values.secrets }}}}\n"
        f"  {{{{ $k }}}}: {{{{ $v | quote }}}}\n"
        f"  {{{{- end }}}}\n"
        f"{{{{- end }}}}\n"
    )

    hpa_yaml = (
        f"{{{{- if .Values.autoscaling.enabled }}}}\n"
        f"apiVersion: autoscaling/v2\n"
        f"kind: HorizontalPodAutoscaler\n"
        f"metadata:\n"
        f'  name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  namespace: {{{{ .Release.Namespace }}}}\n"
        f"spec:\n"
        f"  scaleTargetRef:\n"
        f"    apiVersion: apps/v1\n"
        f"    kind: Deployment\n"
        f'    name: {{{{ include "{n}.fullname" . }}}}\n'
        f"  minReplicas: {{{{ .Values.autoscaling.minReplicas }}}}\n"
        f"  maxReplicas: {{{{ .Values.autoscaling.maxReplicas }}}}\n"
        f"  metrics:\n"
        f"    - type: Resource\n"
        f"      resource:\n"
        f"        name: cpu\n"
        f"        target:\n"
        f"          type: Utilization\n"
        f"          averageUtilization: {{{{ .Values.autoscaling.targetCPUUtilizationPercentage }}}}\n"
        f"{{{{- end }}}}\n"
    )

    base = f"charts/{project_slug}"
    return {
        f"{base}/Chart.yaml": chart_yaml,
        f"{base}/values.yaml": values_yaml,
        f"{base}/templates/_helpers.tpl": helpers_tpl,
        f"{base}/templates/deployment.yaml": deployment_yaml,
        f"{base}/templates/service.yaml": service_yaml,
        f"{base}/templates/ingress.yaml": ingress_yaml,
        f"{base}/templates/secret.yaml": secret_yaml,
        f"{base}/templates/hpa.yaml": hpa_yaml,
    }


# ─── ArgoCD API client ────────────────────────────────────────────────────────

class ArgoCDClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _get(self, path: str, **kwargs) -> dict[str, Any]:
        r = httpx.get(f"{self.base_url}/api/v1{path}", headers=self._headers(), timeout=15, **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None, **kwargs) -> dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/api/v1{path}",
            headers=self._headers(), json=body or {}, timeout=15, **kwargs,
        )
        r.raise_for_status()
        return r.json()

    def app_exists(self, name: str) -> bool:
        try:
            self._get(f"/applications/{name}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    def create_app(self, app_manifest: dict[str, Any]) -> dict[str, Any]:
        """Create an ArgoCD Application from a manifest dict."""
        return self._post("/applications", app_manifest)

    def upsert_app(self, app_manifest: dict[str, Any]) -> dict[str, Any]:
        """Create or update an ArgoCD Application."""
        name = app_manifest["metadata"]["name"]
        if self.app_exists(name):
            r = httpx.put(
                f"{self.base_url}/api/v1/applications/{name}",
                headers=self._headers(), json=app_manifest, timeout=15,
            )
            r.raise_for_status()
            return r.json()
        return self.create_app(app_manifest)

    def sync(self, name: str) -> dict[str, Any]:
        """Trigger a sync for an application."""
        return self._post(f"/applications/{name}/sync")

    def get_status(self, name: str) -> dict[str, Any]:
        """Return a simplified status dict for an application."""
        data = self._get(f"/applications/{name}")
        status = data.get("status", {})
        health = status.get("health", {})
        sync   = status.get("sync", {})
        return {
            "name":        name,
            "health":      health.get("status", "Unknown"),
            "sync_status": sync.get("status", "Unknown"),
            "revision":    sync.get("revision", ""),
            "message":     health.get("message", ""),
            "conditions":  [c.get("message") for c in status.get("conditions", [])],
        }

    def get_pods(self, name: str) -> list[dict[str, Any]]:
        """Return pod names for an application."""
        try:
            tree = self._get(f"/applications/{name}/resource-tree")
            return [
                n for n in tree.get("nodes", [])
                if n.get("kind") == "Pod"
            ]
        except Exception:
            return []

    def get_logs(self, name: str, namespace: str, container: str = "", tail_lines: int = 100) -> str:
        """Return recent logs for a pod in an application."""
        pods = self.get_pods(name)
        if not pods:
            return "(no pods found)"
        pod = pods[0]
        pod_name = pod.get("name", "")
        params: dict[str, Any] = {
            "namespace": namespace,
            "tailLines": tail_lines,
            "follow": False,
        }
        if container:
            params["container"] = container

        try:
            r = httpx.get(
                f"{self.base_url}/api/v1/applications/{name}/pods/{pod_name}/logs",
                headers=self._headers(), params=params, timeout=30,
            )
            r.raise_for_status()
            # Logs come as newline-delimited JSON: {"result":{"content":"...","podName":"..."}}
            lines = []
            for line in r.text.splitlines():
                try:
                    obj = json.loads(line)
                    content = obj.get("result", {}).get("content", "")
                    if content:
                        lines.append(content)
                except Exception:
                    lines.append(line)
            return "\n".join(lines[-tail_lines:])
        except Exception as e:
            return f"(could not fetch logs: {e})"


# ─── High-level deploy via GitOps + ArgoCD ───────────────────────────────────

def gitops_deploy(
    project_slug: str,
    environment: str,
    image_or_ref: str,
    plan_config: dict[str, Any],
    env_config: dict[str, Any] | None,
    gitops_repo: str,
    gitops_token: str,
    gitops_branch: str,
    argocd_client: ArgoCDClient | None,
) -> dict[str, Any]:
    """
    Full GitOps deploy flow:
      1. Generate values YAML and push to gitops repo
      2. Generate ArgoCD Application CR and push to gitops repo
      3. Create/update ArgoCD Application (if ArgoCD client available)
      4. Trigger sync

    Returns a result dict with keys: success, message, values_path, app_name
    """
    app_name  = f"{project_slug}-{environment}"
    namespace = f"{project_slug}-{environment}"
    values_path = f"projects/{project_slug}/values-{environment}.yaml"
    argocd_path = f"argocd/{app_name}.yaml"

    # 1. Push values file
    values_yaml = build_values_yaml(project_slug, environment, image_or_ref, plan_config, env_config)
    push_gitops_file(
        repo=gitops_repo,
        path=values_path,
        content=values_yaml,
        commit_message=f"deploy({app_name}): {image_or_ref}",
        token=gitops_token,
        branch=gitops_branch,
    )

    # 2. Push ArgoCD Application CR
    argocd_yaml = build_argocd_app_yaml(
        app_name=app_name,
        gitops_repo=gitops_repo,
        project_slug=project_slug,
        environment=environment,
        namespace=namespace,
        gitops_branch=gitops_branch,
    )
    push_gitops_file(
        repo=gitops_repo,
        path=argocd_path,
        content=argocd_yaml,
        commit_message=f"argocd({app_name}): register application",
        token=gitops_token,
        branch=gitops_branch,
    )

    chart_path = "charts/app"
    result: dict[str, Any] = {
        "success": True,
        "app_name": app_name,
        "namespace": namespace,
        "chart_path": chart_path,
        "values_path": values_path,
        "argocd_path": argocd_path,
        "gitops_repo": gitops_repo,
        "message": f"Pushed Helm chart + values + ArgoCD Application to {gitops_repo}",
    }

    # 3 & 4. Register repo with ArgoCD (if needed), create/update app, and sync
    if argocd_client:
        # Ensure ArgoCD knows about the gitops repo
        _ensure_argocd_repo(argocd_client, gitops_repo, gitops_token)
        app_manifest = yaml.safe_load(argocd_yaml)
        argocd_client.upsert_app(app_manifest)
        argocd_client.sync(app_name)
        result["message"] += f". ArgoCD app '{app_name}' created and sync triggered."
        result["argocd_status"] = argocd_client.get_status(app_name)

    return result


def _ensure_argocd_repo(client: "ArgoCDClient", gitops_repo: str, gitops_token: str) -> None:
    """Register the GitOps repo with ArgoCD if not already registered."""
    repo_url = f"https://github.com/{gitops_repo}"
    try:
        existing = client._get(f"/repositories/{repo_url}")
        if existing.get("repo"):
            return  # already registered
    except Exception:
        pass
    try:
        client._post("/repositories", {
            "repo": repo_url,
            "type": "git",
            "password": gitops_token,
            "username": "x-token",
        })
    except Exception:
        pass  # non-fatal — ArgoCD may already have access via public repo or SSH
