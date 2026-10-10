from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twg_bin: str = "twg"

    bitbucket_workspace: str = ""
    bitbucket_repo: str = ""
    bitbucket_webhook_secret: str = "change-me"

    teams_webhook_url: str = ""

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://reviews:reviews@localhost:5432/reviews"

    log_level: str = "INFO"

    sandbox_enabled: bool = False
    sandbox_registry_user: str = ""
    sandbox_registry_token: str = ""

    autonomy_config: str = "config/autonomy.yaml"
    repo_profile_config: str = "config/repo_profile.emotivoo-v2.yaml"
    # Plantilla del artefacto con la salida de la herramienta estatica por review.
    # Placeholders: {tool}, {pr_id}, {head_sha}. Debe coincidir con la ruta que
    # publica el CI (emotivoo-v2: var/tooling/<pr_id>/<commit>/phpstan.json).
    tooling_output_template: str = "var/tooling/{pr_id}/{head_sha}/{tool}.json"
    # Directorio raiz bajo el que se resuelve la plantilla (compartido api/worker).
    tooling_output_root: str = "."
    # Secreto para firmar el POST del CI que deposita el artefacto (HMAC-SHA256).
    tooling_webhook_secret: str = "change-me"
    # TTL de la ingesta diferida: cuanto se espera a los artefactos del CI.
    ingest_ttl_ready_s: float = 1200.0
    ingest_ttl_pending_s: float = 24 * 3600.0
    # Herramientas cuya salida espera la review antes de ejecutarse.
    expected_tools: str = "phpstan"


@lru_cache
def get_settings() -> Settings:
    return Settings()
