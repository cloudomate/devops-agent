"""Kubernetes deployer using kubectl (subprocess) or Python k8s client."""
from __future__ import annotations

import subprocess
from typing import Any

from .base import BaseDeployer, DeployResult


class KubernetesDeployer(BaseDeployer):
    """Deploys by patching a Kubernetes Deployment's container image."""

    def __init__(self, env_config: dict[str, Any]):
        super().__init__(env_config)
        self.namespace = self.config.get("namespace", "default")
        self.context = self.config.get("context")
        self.deployment = self.config["deployment"]
        self.container = self.config.get("container", self.deployment)
        self.values: dict[str, str] = self.config.get("values", {})

    def _kubectl(self, *args: str) -> subprocess.CompletedProcess:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["--namespace", self.namespace, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def _apply_secret(self) -> tuple[bool, str]:
        """Create or update a k8s Secret from config values before deploy."""
        if not self.values:
            return True, ""
        secret_name = f"{self.deployment}-env"
        # Build --from-literal args
        literals = [f"--from-literal={k}={v}" for k, v in self.values.items()]
        # kubectl create secret generic ... --dry-run=client -o yaml | kubectl apply -f -
        create = self._kubectl(
            "create", "secret", "generic", secret_name,
            *literals,
            "--dry-run=client", "-o", "yaml",
        )
        if create.returncode != 0:
            return False, f"Secret dry-run failed: {create.stderr}"
        apply = subprocess.run(
            ["kubectl"] + (["--context", self.context] if self.context else []) + [
                "--namespace", self.namespace, "apply", "-f", "-"
            ],
            input=create.stdout, capture_output=True, text=True, timeout=30,
        )
        ok = apply.returncode == 0
        return ok, (apply.stdout + apply.stderr).strip()

    def deploy(self, image_or_ref: str) -> DeployResult:
        # Apply values as a Kubernetes secret before deploying
        ok, secret_out = self._apply_secret()
        if not ok:
            return DeployResult(False, "Failed to apply secret", secret_out)

        result = self._kubectl(
            "set", "image",
            f"deployment/{self.deployment}",
            f"{self.container}={image_or_ref}",
        )
        if result.returncode != 0:
            return DeployResult(False, "kubectl set image failed", result.stderr)

        # Wait for rollout
        rollout = self._kubectl(
            "rollout", "status",
            f"deployment/{self.deployment}",
            "--timeout=5m",
        )
        success = rollout.returncode == 0
        output = (f"[secret] {secret_out}\n" if secret_out else "") + rollout.stdout + rollout.stderr
        msg = f"Deployed {image_or_ref} to {self.deployment}" if success else "Rollout failed"
        return DeployResult(success, msg, output)

    def rollback(self, previous_image: str) -> DeployResult:
        return self.deploy(previous_image)

    def get_logs(self, lines: int = 100) -> str:
        result = self._kubectl(
            "logs", f"deployment/{self.deployment}",
            f"--tail={lines}", "--all-containers=true",
        )
        return result.stdout or result.stderr

    def get_status(self) -> dict[str, Any]:
        result = self._kubectl(
            "get", "deployment", self.deployment, "-o", "json",
        )
        if result.returncode != 0:
            return {"error": result.stderr}
        import json
        data = json.loads(result.stdout)
        status = data.get("status", {})
        return {
            "ready_replicas": status.get("readyReplicas", 0),
            "desired_replicas": status.get("replicas", 0),
            "available_replicas": status.get("availableReplicas", 0),
        }
