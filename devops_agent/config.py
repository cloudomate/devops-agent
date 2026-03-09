"""App settings loaded from environment variables, with optional DB overrides."""
from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llm_base_url: str = "https://api.anthropic.com/v1"
    llm_api_key: str = ""
    llm_model: str = "claude-opus-4-6"

    github_token: str = ""
    github_webhook_secret: str = ""

    secret_key: str = "dev-secret-key"
    port: int = 8000

    # Entra ID OIDC — leave empty to run in no-auth dev mode
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_redirect_uri: str = "http://localhost:8000/auth/callback"

    # Optional: Entra security group object ID → admin role
    entra_admin_group_id: str = ""

    # ArgoCD integration (optional — leave empty to skip ArgoCD)
    argocd_url: str = ""
    argocd_token: str = ""

    # GitOps repository (required for ArgoCD integration)
    gitops_repo: str = ""
    gitops_token: str = ""
    gitops_branch: str = "main"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


_settings: Settings | None = None
# DB overrides applied via apply_db_overrides() at startup and on admin save
_db_overrides: dict = {}


def apply_db_overrides(overrides: dict) -> None:
    """Merge DB-stored settings on top of env-var defaults. Resets the singleton."""
    global _db_overrides, _settings
    _db_overrides = {k.lower(): v for k, v in overrides.items() if v}
    _settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        base = Settings()  # type: ignore[call-arg]
        if _db_overrides:
            _settings = base.model_copy(update=_db_overrides)
        else:
            _settings = base
    return _settings
