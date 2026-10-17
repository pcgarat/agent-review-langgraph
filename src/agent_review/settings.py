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

    # El canal de Teams es parte del despliegue. Con `true`, el worker encola los
    # avisos en la outbox (`TEAMS_NOTICES_KEY`) y el bot los publica (C10).
    teams_enabled: bool = False
    # Outbox de avisos al canal (C10). El worker encola aqui; el bot la consume.
    teams_notices_key: str = "reviews:notices"
    # Espera de la lectura bloqueante de la outbox por parte del bot. Vencida, el
    # bucle vuelve a intentarlo (no distingue "sin avisos" de un Redis caido).
    teams_bot_poll_s: float = 5.0
    # Identidad y secreto del bot (T6). El SDK elige el metodo de auth: con
    # `client_secret` usa *client secret*; sin el, identidad gestionada. Los lee
    # SOLO el proceso `teams-bot`.
    teams_bot_id: str = ""
    teams_bot_tenant_id: str = ""
    teams_bot_token: str = ""
    # Puerto del *messaging endpoint* (`POST /api/messages`) dentro del contenedor.
    teams_bot_port: int = 3978
    # Bearer del **ingress interno** del bot (T7): protege `/healthz` y la
    # observabilidad. El egress no lo usa (va por la outbox).
    teams_bot_shared_secret: str = "change-me"

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://reviews:reviews@localhost:5432/reviews"

    log_level: str = "INFO"

    # Progreso del worker (iniciativa 019, W10): cierra D16 («el latido mide vida,
    # no avance»). Con `progress_enabled`, cada nodo deja en Redis el nodo en curso
    # y su `started_at`, que es lo que el panel lee. Sin el, el worker se comporta
    # igual que antes: `NullProgress`, sin tocar Redis por progreso (W2).
    progress_enabled: bool = False
    # TTL de la marca de progreso: por encima del peor caso de un nodo con
    # reintentos. Una marca caducada no se confunde con un trabajo vivo.
    progress_ttl_s: float = 3600.0
    # TTL de la marca de cancelacion `reviews:cancel:<review_key>` (019 §5.4). La
    # marca sobrevive al aborto como *tombstone*: cualquier job rezagado de la misma
    # review (la reanudacion del fix-worker, una decision encolada) se aborta al
    # desencolar en vez de re-ejecutar la review desde cero. Debe superar el plazo
    # mas largo en que puede llegar un rezagado: `DECISION_TTL_S` (7 dias).
    cancel_ttl_s: float = 7 * 24 * 3600.0
    # Clave del hash de progreso (019 §5.2). Un campo por nodo en curso.
    progress_key: str = "reviews:active"

    # Panel de operacion (iniciativa 019, W1/W7). El panel es un servicio `web`
    # aparte, opt-in por perfil de compose (`profiles: ["panel"]`): sin el perfil,
    # `docker compose up` arranca lo de siempre (R7). `panel_enabled` lo declara el
    # despliegue y es lo que exige su secreto de sesion en produccion.
    panel_enabled: bool = False
    # Puerto del panel dentro del contenedor; lo publica el perfil `panel`.
    panel_port: int = 8090
    # Sesion del panel: NO es el bearer de decision. Identifica a la persona y su
    # rol en la forma `<usuario>:<lector|operador>`; un `lector` puede mirar pero
    # no ejecutar comandos. Un secreto real por despliegue.
    panel_session_secret: str = ""
    # Tope de la consola en vivo por verificacion (lineas del buffer del runner).
    sandbox_console_buffer_lines: int = 2000

    sandbox_enabled: bool = False
    sandbox_registry_user: str = ""
    sandbox_registry_token: str = ""
    # Token de Bitbucket de SOLO LECTURA que usa el runner para clonar. Distinto
    # del de `twg` del worker: aunque un PR malicioso lo viera, no da push (Q3).
    sandbox_git_token: str = ""
    # Binario de Docker del runner (rootless en el host del runner).
    sandbox_docker_bin: str = "docker"
    # URL interna del `sandbox-runner` (Fase 3). Con `SANDBOX_ENABLED=true` y esta
    # URL, el nodo `fix` puede verificar parches; sin ellas, queda `NOT_VERIFIABLE`.
    sandbox_runner_url: str = ""
    sandbox_runner_token: str = ""
    # Bearer de **solo lectura** de la consola en vivo del runner (019 OP24): lo
    # recibe el panel para reenviar el SSE. Solo abre `GET /verify/{run_key}/console`;
    # no sirve para `POST /verify` ni para los workspaces, asi que el panel no puede
    # lanzar verificaciones. Vacio: la consola solo acepta `SANDBOX_RUNNER_TOKEN`.
    sandbox_console_token: str = ""
    # Techo de una verificacion en el sandbox, si el perfil no lo declara. Vive en
    # el perfil (`sandbox.timeout_s`); esto es el fallback sin perfil.
    sandbox_timeout_s: float = 1200.0
    # Workspace de lectura (iniciativa 016): clon *bare* que el runner sirve para
    # que un modelo lea ficheros y haga `grep` sin ejecutar codigo del PR. TTL de un
    # workspace abierto y tope de abiertos a la vez (reaper perezoso).
    sandbox_workspace_ttl_s: float = 1800.0
    sandbox_max_workspaces: int = 4
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
    # Fase 3-E7: TTL del handoff del fix. Una wave que el worker de fixes no
    # resuelve en este plazo se reanuda con `outcome="error"` (el fix queda
    # `NOT_VERIFIABLE`, publicado); nunca se deja colgada esperando al runner.
    fix_verify_ttl_s: float = 2 * 3600.0
    # Reintentos del job de fix ante un fallo transitorio del runner antes de
    # mandarlo a su DLQ. Ortogonal al reintento de nodo del runner.
    max_fix_attempts_job: int = 3
    # Cadencia del poll y el barrido del worker de fixes.
    fix_poll_s: float = 5.0
    fix_sweep_s: float = 30.0
    # Retencion de checkpoints de hilos COMPLETADOS (Fase 2). Un hilo pausado
    # nunca se poda, aunque supere la retencion.
    checkpoint_retention_days: float = 30.0
    # Fase 3-E7: retencion de hilos pausados en `fix_wait` (F21). Un hilo en
    # `fix_wait` no espera a nadie (su wave se resolvio o vencio y la review quedo
    # publicada), asi que se poda pasado este plazo. Debe ser holgadamente mayor
    # que `FIX_VERIFY_TTL_S` para no podar una wave aun viva. `0` desactiva.
    fix_wait_max_age_s: float = 3 * 24 * 3600.0
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

    # Persona LLM `correctness` y fixer. Desactivados por defecto: la Fase 1 arranca
    # solo con la persona estatica. Con `MODELS_CONFIG` presente manda el registro
    # declarativo (roles -> modelo); sin el, se sintetiza desde `LLM_*` para no
    # romper un despliegue existente (D7 de la iniciativa 005).
    llm_reviewer_enabled: bool = False
    # Registro declarativo de proveedores y modelos (p. ej. `config/models.yaml`).
    # Un fichero declarado que no existe es un error de arranque (fail-fast). Vacio
    # por defecto: un despliegue que solo tiene `LLM_*` sigue usando la via de
    # compatibilidad sin tocar su `.env` (D7 de la iniciativa 005); el registro se
    # activa declarando `MODELS_CONFIG`, como hace `.env.example`.
    models_config: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_max_findings: int = 10
    llm_budget_tokens_per_pr: int = 2000000
    # Tamano maximo de un lote del `LlmReviewer`, en caracteres de diff (012 C2): se
    # revisan ficheros enteros por lotes; uno que solo no cabe se declara `too_large`.
    # 30 000 con razonamiento (021 R12): con 60 000 el proveedor corta antes de que
    # el modelo responda.
    llm_batch_max_chars: int = 30000

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


def _is_placeholder(value: str) -> bool:
    """¿Sigue en uno de los valores de ejemplo de `.env.example`?"""
    return value.strip() in _INSECURE_DEFAULTS


def is_missing(value: str) -> bool:
    """¿Vacío o en el placeholder de `.env.example` (`change-me`)? Igual de ausente.

    `.env.example` rellena cada secreto e identidad con `change-me`: copiarlo tal
    cual no puede contar como «configurado», ni en el contrato de producción ni en
    los puntos donde el vacío desactiva algo (p. ej. el login del registro). Vacío
    es el caso más probable (una variable sin asignar, `VAR=`), así que también
    cuenta. Usar siempre este predicado, nunca `_is_placeholder` a secas: si no,
    el mismo secreto se juzga distinto según el proceso.
    """
    return not value.strip() or _is_placeholder(value)


def insecure_secrets(settings: Settings) -> list[str]:
    """Nombres de secretos que siguen en su valor de ejemplo o sin rellenar (D4).

    Cubre el contrato completo, no solo la firma del webhook: un despliegue en
    produccion sin credencial de Bitbucket arranca pero falla en la primera
    review, que es exactamente el fallo silencioso que se quiere evitar.
    """
    insecure: list[str] = []
    if is_missing(settings.tooling_webhook_secret):
        insecure.append("TOOLING_WEBHOOK_SECRET")
    # El token de decision autoriza a un humano a mandar sobre el clamp; en Fase 3
    # sera la llave del repo (spec §2.1). No puede quedarse en el valor de ejemplo.
    if is_missing(settings.reviews_decision_token):
        insecure.append("REVIEWS_DECISION_TOKEN")
    # La credencial del proveedor de VCS es obligatoria: sin ella no hay diff ni
    # comentarios. Se exige la del proveedor activo, no siempre la de Bitbucket.
    if settings.vcs_provider == "github":
        if is_missing(settings.github_token):
            insecure.append("GITHUB_TOKEN")
    elif is_missing(settings.twg_bbc_token):
        insecure.append("TWG_BBC_TOKEN")
    if settings.teams_enabled and not settings.teams_notices_key.strip():
        insecure.append("TEAMS_NOTICES_KEY")
    if settings.llm_reviewer_enabled:
        # Con registro declarativo, el secreto es el que referencia el YAML. Sin el,
        # la via de compatibilidad exige `LLM_API_KEY` como antes.
        if settings.models_config.strip():
            from agent_review.models_config import (
                env_for,
                load_models_config,
                unresolved_secrets,
            )

            try:
                config = load_models_config(settings.models_config)
            except Exception:
                # Un registro ilegible ya falla al construir el worker con un error
                # mas util; aqui no se duplica.
                config = None
            if config is not None:
                insecure.extend(unresolved_secrets(config, env_for(settings)))
        elif is_missing(settings.llm_api_key):
            insecure.append("LLM_API_KEY")
    # Con el sandbox activo hay dos superficies nuevas: el token compartido
    # worker<->runner y el token de clonado de solo lectura del runner. Sin ellos
    # el worker no puede pedir la verificacion o el runner no puede clonar.
    if settings.sandbox_enabled:
        if is_missing(settings.sandbox_runner_token):
            insecure.append("SANDBOX_RUNNER_TOKEN")
        if is_missing(settings.sandbox_git_token):
            insecure.append("SANDBOX_GIT_TOKEN")
    # El secreto de sesion del panel (019 W7) solo se exige si el panel esta
    # desplegado: un despliegue sin `web` no debe fallar por un secreto que no usa.
    if settings.panel_enabled:
        insecure.extend(panel_insecure_secrets(settings))
    return insecure


def panel_insecure_secrets(settings: Settings) -> list[str]:
    """Secreto de sesion del panel (019 W7) sin configurar o en su valor de ejemplo.

    Contrato propio del proceso `web`, mucho menor que el del worker: el panel no
    recibe credencial de VCS ni clave del LLM (solo lee estado y encola comandos),
    asi que exigirle el contrato completo lo tumbaria en produccion. Una sesion de
    ejemplo (`change-me`) o vacia dejaria el panel sin autenticacion: es
    exactamente el agujero que el fail-closed debe evitar.
    """
    if is_missing(settings.panel_session_secret):
        return ["PANEL_SESSION_SECRET"]
    return []


def fix_worker_insecure_secrets(settings: Settings) -> list[str]:
    """Secretos que necesita el worker de fixes y siguen sin configurar (E7).

    Su contrato es mucho menor que el del worker de reviews: solo habla con Redis y
    con el runner. No recibe credencial de VCS, token de decision ni clave del LLM
    (vive aislado), asi que exigirle el contrato completo lo tumbaria en produccion.
    """
    insecure: list[str] = []
    if not settings.sandbox_runner_url.strip():
        insecure.append("SANDBOX_RUNNER_URL")
    if is_missing(settings.sandbox_runner_token):
        insecure.append("SANDBOX_RUNNER_TOKEN")
    return insecure


def teams_bot_insecure_secrets(settings: Settings) -> list[str]:
    """Secretos que necesita el servicio `teams-bot` y siguen sin configurar (T6).

    Contrato propio: el bot recibe el trafico del Bot Framework (valida su JWT con
    la identidad del bot) y publica proactivamente, pero **no** tiene credencial de
    VCS ni clave del LLM. Solo con `TEAMS_ENABLED=true`; sin el canal, el servicio
    no debe arrancar, asi que su ausencia no es un fallo.
    """
    insecure: list[str] = []
    if not settings.teams_enabled:
        return insecure
    if is_missing(settings.teams_bot_id):
        insecure.append("TEAMS_BOT_ID")
    if is_missing(settings.teams_bot_tenant_id):
        insecure.append("TEAMS_BOT_TENANT_ID")
    # El token es el secreto del bot (client secret). Sin el, el SDK no valida el
    # JWT del Bot Framework: en produccion no puede quedarse vacio ni de ejemplo.
    if is_missing(settings.teams_bot_token):
        insecure.append("TEAMS_BOT_TOKEN")
    if is_missing(settings.teams_bot_shared_secret):
        insecure.append("TEAMS_BOT_SHARED_SECRET")
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
    _raise_if_insecure(insecure_secrets(settings))


def require_fix_worker_secrets(settings: Settings) -> None:
    """Como `require_production_secrets`, con el contrato del worker de fixes."""
    if not is_production(settings):
        return
    _raise_if_insecure(fix_worker_insecure_secrets(settings))


def require_panel_secrets(settings: Settings) -> None:
    """Como `require_production_secrets`, con el contrato del panel (019 W7).

    El proceso `web` **es** el panel, asi que exige su sesion en produccion aunque
    `PANEL_ENABLED` no venga declarado en el entorno: la ausencia del flag no puede
    convertirse en un panel sin autenticacion. No exige el resto del contrato (VCS,
    LLM), que el panel no recibe.
    """
    if not is_production(settings):
        return
    _raise_if_insecure(panel_insecure_secrets(settings))


def require_teams_bot_secrets(settings: Settings) -> None:
    """Como `require_production_secrets`, con el contrato del bot de Teams (T6)."""
    if not is_production(settings):
        return
    _raise_if_insecure(teams_bot_insecure_secrets(settings))


def _raise_if_insecure(insecure: list[str]) -> None:
    if insecure:
        raise RuntimeError(
            "Secretos sin configurar en produccion: "
            + ", ".join(insecure)
            + ". Fija un valor real antes de arrancar."
        )
