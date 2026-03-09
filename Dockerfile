# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# git — for local repo cloning during discovery and GitOps chart push
# kubectl — for Kubernetes deployments
# helm — for chart linting before GitOps push
ARG KUBECTL_VERSION=v1.31.0
ARG HELM_VERSION=v3.16.3
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
         -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
         | tar -xz --strip-components=1 -C /usr/local/bin "linux-${ARCH}/helm" && \
    apt-get remove -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY devops_agent/ devops_agent/
COPY main.py .

# Data directory for SQLite DB and kubeconfig
RUN mkdir -p /data /app/.kube

# Non-root user
RUN useradd -r -u 1001 -g root agent && \
    chown -R agent:root /app /data && \
    chmod -R g=u /app /data

USER 1001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KUBECONFIG=/app/.kube/config

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')"

CMD ["python", "-m", "uvicorn", "devops_agent.web.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
