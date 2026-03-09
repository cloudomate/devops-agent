"""Create the right deployer for an environment config."""
from __future__ import annotations

from typing import Any

from .base import BaseDeployer
from .docker_compose import DockerComposeDeployer
from .kubernetes import KubernetesDeployer
from .ssh import SSHDeployer


def make_deployer(env_config: dict[str, Any]) -> BaseDeployer:
    type_ = env_config.get("type", "").lower()
    if type_ == "kubernetes":
        return KubernetesDeployer(env_config)
    elif type_ == "ssh":
        return SSHDeployer(env_config)
    elif type_ in ("docker_compose", "docker-compose"):
        return DockerComposeDeployer(env_config)
    else:
        raise ValueError(f"Unknown environment type: {type_!r}. Supported: kubernetes, ssh, docker_compose")
