from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Proveedores de VCS soportados. Un valor distinto es un error de configuracion:
# falla al construir `Settings` (arranque), no en la primera review.
_VCS_PROVIDERS = {"twg", "github"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twg_bin: str = "twg"

    # Proveedor de VCS del despliegue: `twg` (Bitbucket via el CLI de Atlassian)
    # o `github` (API REST). El grafo no lo conoce; solo el `build_vcs` del worker
    # y la verificacion de repo de la API leen esta clave.
    vcs_provider: str = "twg"

    bitbucket_workspace: str = ""
    bitbucket_repo: str = ""
    # Credencial de servicio de twg para `twg bb` (Bitbucket). Es un API token
    # scoped de un actor de servicio, no el OAuth de una persona: no caduca cada
    # hora y se revoca sin tocar a nadie. twg lo lee de `TWG_BBC_TOKEN`
    # (verificado: no necesita auth.conf si esta la variable).
    twg_bbc_token: str = ""
    # Site y usuario del actor de servicio; los consume twg directamente.
    twg_site: str = ""
    twg_user: str = ""
    # API token de Atlassian (Jira/Confluence). Solo necesario si se usa
    # `twg jira` (escalado a workitem); twg lo lee de `TWG_TOKEN`.
    twg_token: str = ""

    # GitHub (VCS_PROVIDER=github). `github_owner`/`github_repo` son la identidad
    # del repo revisado (equivalente a BITBUCKET_WORKSPACE/BITBUCKET_REPO). El
    # token es un PAT fine-grained con `pull_requests: write` + `contents: read`.
    github_owner: str = ""
    github_repo: str = ""
    github_token: str = ""
    # Base de la API. Se cambia para GitHub Enterprise Server.
    github_api_url: str = "https://api.github.com"

    teams_webhook_url: str = ""
    # Declara que el canal de Teams es parte del despliegue. Con `true`, en
    # produccion el arranque falla si falta la URL (evita perder el teaser en
    # silencio). La URL en si es una capacidad: quien la tiene, publica.
    teams_enabled: bool = False

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://reviews:reviews@localhost:5432/reviews"

    log_level: str = "INFO"

    sandbox_enabled: bool = False
    sandbox_registry_user: str = ""
    sandbox_registry_token: str = ""
    # URL interna del `sandbox-runner` (Fase 3). Con `SANDBOX_ENABLED=true` y esta
    # URL, el nodo `fix` puede verificar parches; sin ellas, queda `NOT_VERIFIABLE`.
    sandbox_runner_url: str = ""
    sandbox_runner_token: str = ""
    # Formato de entrega del parche verificado: `diff` (fallback seguro) o
    # `suggestion` (si Bitbucket lo renderiza). E6.
    fix_delivery: str = "diff"
    # Nombres de recipe del perfil que ejecuta la verificacion, en orden.
    fix_recipes: str = "typecheck,arch,lint,targeted,tests"
    # Tope de lineas de un parche (guard espejo de autonomy.yaml).
    max_fix_lines: int = 30

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
    # Bearer del canal de decision (CLI/API). En `development` se relaja como el
    # secreto de tooling: con el valor de ejemplo, el endpoint no exige token.
    reviews_decision_token: str = "change-me"
    # Entorno de despliegue. En `production` se exige que los secretos no sean el
    # valor de ejemplo (fallo ruidoso al arrancar, no un agujero silencioso).
    environment: str = "development"
    # TTL de la ingesta diferida: cuanto se espera a los artefactos del CI.
    ingest_ttl_ready_s: float = 1200.0
    ingest_ttl_pending_s: float = 24 * 3600.0
    # TTL de una decision pendiente (Fase 2): pasado, el barrido reanuda la review
    # como `expired` (se publica "sin decision"), nunca se deja colgada.
    decision_ttl_s: float = 7 * 24 * 3600.0
    # Retencion de checkpoints de hilos COMPLETADOS (Fase 2). Un hilo pausado
    # nunca se poda, aunque supere la retencion.
    checkpoint_retention_days: float = 30.0
    # Herramientas cuya salida espera la review antes de ejecutarse.
    expected_tools: str = "phpstan"
    # Patrones extra (coma-separados) de ficheros que el scope ignora, ademas de
    # los por defecto (lockfiles, generados, vendor...).
    scope_ignore: str = ""
    # Reintentos de una review ante un fallo transitorio (twg, Teams). Agotados,
    # el job se descarta con el `review_key` en el log para reencolarlo a mano.
    max_review_attempts: int = 3
    # Reintentos por nodo dentro de una misma invocacion (fallo transitorio de twg
    # o de Teams). Es ortogonal al reintento de job: aquel reencola la review
    # entera (unidad de idempotencia: la marca del PR); este reintenta la llamada
    # concreta sin sacar el job de la cola. `node_retry_max_attempts=1` lo
    # desactiva. El LLM no pasa por aqui: `LlmReviewer` captura su fallo y degrada
    # la persona a 0 hallazgos, para que una caida del proveedor no tumbe la review.
    node_retry_max_attempts: int = 3
    node_retry_initial_interval_s: float = 0.5
    node_retry_backoff_factor: float = 2.0
    node_retry_max_interval_s: float = 8.0
    # Techo de una llamada a `twg`. Sin el, un twg colgado bloquea el nodo para
    # siempre (el timeout de nodo de LangGraph no aplica a nodos sincronos).
    twg_timeout_s: float = 60.0
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

    @property
    def fix_recipes_list(self) -> list[str]:
        return [item.strip() for item in self.fix_recipes.split(",") if item.strip()]

    @field_validator("vcs_provider", mode="before")
    @classmethod
    def _normalize_vcs_provider(cls, value: object) -> object:
        """Normaliza y valida el proveedor. Un valor desconocido es un error de
        configuracion que debe fallar en el arranque, no en la primera review."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in _VCS_PROVIDERS:
                raise ValueError(
                    f"VCS_PROVIDER debe ser uno de {sorted(_VCS_PROVIDERS)}, no {value!r}"
                )
            return normalized
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Valores de ejemplo que no deben llegar a produccion: con ellos la firma del
# webhook se omite y cualquiera podria inyectar artefactos.
_INSECURE_DEFAULTS = {"change-me", "changeme"}


def _is_unset(value: str) -> bool:
    return value.strip() in _INSECURE_DEFAULTS


def insecure_secrets(settings: Settings) -> list[str]:
    """Nombres de secretos que siguen en su valor de ejemplo o sin rellenar (D4).

    Cubre el contrato completo, no solo la firma del webhook: un despliegue en
    produccion sin credencial de Bitbucket arranca pero falla en la primera
    review, que es exactamente el fallo silencioso que se quiere evitar.
    """
    insecure: list[str] = []
    if _is_unset(settings.tooling_webhook_secret):
        insecure.append("TOOLING_WEBHOOK_SECRET")
    # El token de decision autoriza a un humano a mandar sobre el clamp; en Fase 3
    # sera la llave del repo (spec §2.1). No puede quedarse en el valor de ejemplo.
    if _is_unset(settings.reviews_decision_token):
        insecure.append("REVIEWS_DECISION_TOKEN")
    # La credencial del proveedor de VCS es obligatoria: sin ella no hay diff ni
    # comentarios. Se exige la del proveedor activo, no siempre la de Bitbucket.
    if settings.vcs_provider == "github":
        if not settings.github_token.strip():
            insecure.append("GITHUB_TOKEN")
    elif not settings.twg_bbc_token.strip():
        insecure.append("TWG_BBC_TOKEN")
    if settings.teams_enabled and not settings.teams_webhook_url.strip():
        insecure.append("TEAMS_WEBHOOK_URL")
    if settings.llm_reviewer_enabled and not settings.llm_api_key.strip():
        insecure.append("LLM_API_KEY")
    return insecure


def is_production(settings: Settings) -> bool:
    return settings.environment.strip().lower() in {"production", "prod"}


def require_production_secrets(settings: Settings) -> None:
    """Falla el arranque en produccion si un secreto sigue en el valor de ejemplo.

    En local/test se permite (el secreto `change-me` relaja la firma a proposito);
    en produccion, arrancar con el valor por defecto seria un agujero silencioso.
    """
    if not is_production(settings):
        return
    insecure = insecure_secrets(settings)
    if insecure:
        raise RuntimeError(
            "Secretos sin configurar en produccion: "
            + ", ".join(insecure)
            + ". Fija un valor real antes de arrancar."
        )
