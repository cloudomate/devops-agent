"""Base deployer interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class DeployResult:
    success: bool
    message: str
    output: str = ""


class BaseDeployer(ABC):
    def __init__(self, env_config: dict[str, Any]):
        self.config = env_config["config"]
        self.health_check_url = env_config.get("health_check_url")

    @abstractmethod
    def deploy(self, image_or_ref: str) -> DeployResult:
        ...

    @abstractmethod
    def rollback(self, previous_image: str) -> DeployResult:
        ...

    @abstractmethod
    def get_logs(self, lines: int = 100) -> str:
        ...

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        ...
