"""SSH deployer — runs a remote deploy script via paramiko."""
from __future__ import annotations

import os
from typing import Any

import paramiko

from .base import BaseDeployer, DeployResult


class SSHDeployer(BaseDeployer):
    """Connects over SSH and runs a deploy script on the remote server."""

    def __init__(self, env_config: dict[str, Any]):
        super().__init__(env_config)
        self.host = self.config["host"]
        self.user = self.config.get("user", "deploy")
        self.key_file = os.path.expanduser(self.config.get("key_file", "~/.ssh/id_rsa"))
        self.deploy_script = self.config["deploy_script"]
        self.port = int(self.config.get("port", 22))

    def _client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_file,
            timeout=30,
        )
        return client

    def _run(self, command: str) -> tuple[bool, str]:
        with self._client() as client:
            _, stdout, stderr = client.exec_command(command, timeout=300)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode() + stderr.read().decode()
            return exit_code == 0, out

    def deploy(self, image_or_ref: str) -> DeployResult:
        success, output = self._run(f"{self.deploy_script} {image_or_ref}")
        msg = f"Deployed {image_or_ref} via SSH" if success else "SSH deploy failed"
        return DeployResult(success, msg, output)

    def rollback(self, previous_image: str) -> DeployResult:
        return self.deploy(previous_image)

    def get_logs(self, lines: int = 100) -> str:
        log_cmd = self.config.get("log_command", "journalctl -u app --no-pager -n {lines}")
        _, output = self._run(log_cmd.format(lines=lines))
        return output

    def get_status(self) -> dict[str, Any]:
        status_cmd = self.config.get("status_command", "systemctl is-active app && echo running")
        success, output = self._run(status_cmd)
        return {"running": success, "output": output.strip()}
