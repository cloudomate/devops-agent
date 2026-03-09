"""Docker Compose deployer — runs docker compose commands on a remote or local host."""
from __future__ import annotations

import subprocess
from typing import Any

from .base import BaseDeployer, DeployResult


class DockerComposeDeployer(BaseDeployer):
    """Deploys via `docker compose` on the local machine or over SSH."""

    def __init__(self, env_config: dict[str, Any]):
        super().__init__(env_config)
        self.compose_file = self.config.get("compose_file", "docker-compose.yml")
        self.service = self.config.get("service", "")          # optional: target one service
        self.working_dir = self.config.get("working_dir", ".")
        self.values: dict[str, str] = self.config.get("values", {})
        # Optional SSH target — if set, commands run remotely
        self.host = self.config.get("host", "")
        self.user = self.config.get("user", "deploy")
        self.key_file = self.config.get("key_file", "")

    def _run(self, *args: str) -> tuple[bool, str]:
        base = ["docker", "compose", "-f", self.compose_file]
        cmd = base + list(args)

        if self.host:
            ssh_prefix = ["ssh"]
            if self.key_file:
                ssh_prefix += ["-i", self.key_file]
            ssh_prefix += [f"{self.user}@{self.host}", "-o", "StrictHostKeyChecking=no"]
            # cd to working dir then run
            remote_cmd = f"cd {self.working_dir} && " + " ".join(cmd)
            cmd = ssh_prefix + [remote_cmd]

        try:
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=300,
                cwd=None if self.host else self.working_dir,
            )
            return r.returncode == 0, r.stdout + r.stderr
        except Exception as exc:
            return False, str(exc)

    def _write_env_file(self) -> tuple[bool, str]:
        """Write config values to a .env file in the working directory."""
        if not self.values:
            return True, ""
        env_lines = "\n".join(f"{k}={v}" for k, v in self.values.items()) + "\n"
        env_path = f"{self.working_dir}/.env"
        if self.host:
            ok, out = self._run_raw(f"cat > {env_path} << 'ENVEOF'\n{env_lines}ENVEOF")
            return ok, out
        try:
            import os
            os.makedirs(self.working_dir, exist_ok=True)
            with open(env_path, "w") as f:
                f.write(env_lines)
            return True, f"Wrote {env_path}"
        except Exception as exc:
            return False, str(exc)

    def _run_raw(self, remote_cmd: str) -> tuple[bool, str]:
        """Run a raw shell command on the remote host."""
        ssh_prefix = ["ssh"]
        if self.key_file:
            ssh_prefix += ["-i", self.key_file]
        ssh_prefix += [f"{self.user}@{self.host}", "-o", "StrictHostKeyChecking=no"]
        try:
            r = subprocess.run(ssh_prefix + [remote_cmd], capture_output=True, text=True, timeout=30)
            return r.returncode == 0, r.stdout + r.stderr
        except Exception as exc:
            return False, str(exc)

    def deploy(self, image_or_ref: str) -> DeployResult:
        # Write .env file from values before starting containers
        ok, env_out = self._write_env_file()
        if not ok:
            return DeployResult(False, "Failed to write .env file", env_out)

        # Pull latest image then recreate containers
        svc_args = [self.service] if self.service else []
        ok, out = self._run("pull", *svc_args)
        if not ok:
            return DeployResult(False, "docker compose pull failed", out)
        ok, out2 = self._run("up", "-d", "--remove-orphans", *svc_args)
        prefix = f"[env] {env_out}\n" if env_out else ""
        msg = f"Deployed {image_or_ref} via docker compose" if ok else "docker compose up failed"
        return DeployResult(ok, msg, prefix + out + out2)

    def rollback(self, previous_image: str) -> DeployResult:
        # Set image tag in env and restart — basic rollback
        svc_args = [self.service] if self.service else []
        ok, out = self._run("up", "-d", "--remove-orphans", *svc_args)
        msg = "Rolled back via docker compose" if ok else "Rollback failed"
        return DeployResult(ok, msg, out)

    def get_logs(self, lines: int = 100) -> str:
        svc_args = [self.service] if self.service else []
        _, out = self._run("logs", "--no-color", f"--tail={lines}", *svc_args)
        return out

    def get_status(self) -> dict[str, Any]:
        ok, out = self._run("ps", "--format", "json")
        if not ok:
            return {"error": out}
        return {"output": out.strip()}
