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
    # Override del techo de autonomia (D1): solo puede BAJAR el `max_autonomy` del
    # YAML, nunca subirlo. Vacio = manda el YAML versionado.
    max_autonomy_override: str = ""
    # Plantilla del artefacto con la salida de la herramienta estatica por review.
    # Placeholders: {tool}, {pr_id}, {head_sha}. Debe coincidir con la ruta que
    # publica el CI (emotivoo-v2: var/tooling/<pr_id>/<commit>/phpstan.json).
    tooling_output_template: str = "var/tooling/{pr_id}/{head_sha}/{tool}.json"
    # Directorio raiz bajo el que se resuelve la plantilla (compartido api/worker).
    tooling_output_root: str = "."
    # Secreto para firmar el POST del CI que deposita el artefacto (HMAC-SHA256).
    tooling_webhook_secret: str = "change-me"
    # Entorno de despliegue. En `production` se exige que los secretos no sean el
    # valor de ejemplo (fallo ruidoso al arrancar, no un agujero silencioso).
    environment: str = "development"
    # TTL de la ingesta diferida: cuanto se espera a los artefactos del CI.
    ingest_ttl_ready_s: float = 1200.0
    ingest_ttl_pending_s: float = 24 * 3600.0
    # Herramientas cuya salida espera la review antes de ejecutarse.
    expected_tools: str = "phpstan"
    # Patrones extra (coma-separados) de ficheros que el scope ignora, ademas de
    # los por defecto (lockfiles, generados, vendor...).
    scope_ignore: str = ""
    # Reintentos de una review ante un fallo transitorio (twg, Teams). Agotados,
    # el job se descarta con el `review_key` en el log para reencolarlo a mano.
    max_review_attempts: int = 3
    # Limite del cuerpo del POST de tooling, para rechazar payloads enormes (413).
    max_tooling_payload_bytes: int = 5 * 1024 * 1024

    # Persona LLM `correctness`. Desactivada por defecto: la Fase 1 arranca solo
    # con la persona estatica. Al activarla, si no hay proveedor configurado, la
    # persona no se construye y se registra un aviso.
    llm_reviewer_enabled: bool = False
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_max_findings: int = 10
    llm_budget_tokens_per_pr: int = 500000


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Valores de ejemplo que no deben llegar a produccion: con ellos la firma del
# webhook se omite y cualquiera podria inyectar artefactos.
_INSECURE_DEFAULTS = {"change-me", "", "changeme"}


def insecure_secrets(settings: Settings) -> list[str]:
    """Nombres de secretos que siguen en su valor de ejemplo (D4)."""
    insecure: list[str] = []
    if settings.tooling_webhook_secret.strip() in _INSECURE_DEFAULTS:
        insecure.append("TOOLING_WEBHOOK_SECRET")
    if settings.bitbucket_webhook_secret.strip() in _INSECURE_DEFAULTS:
        insecure.append("BITBUCKET_WEBHOOK_SECRET")
    return insecure


def require_production_secrets(settings: Settings) -> None:
    """Falla el arranque en produccion si un secreto sigue en el valor de ejemplo.

    En local/test se permite (el secreto `change-me` relaja la firma a proposito);
    en produccion, arrancar con el valor por defecto seria un agujero silencioso.
    """
    if settings.environment.strip().lower() not in {"production", "prod"}:
        return
    insecure = insecure_secrets(settings)
    if insecure:
        raise RuntimeError(
            "Secretos sin configurar en produccion: "
            + ", ".join(insecure)
            + ". Fija un valor real antes de arrancar."
        )
