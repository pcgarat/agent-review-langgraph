from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Proveedores de VCS soportados. Un valor distinto es un error de configuracion:
# falla al construir `Settings` (arranque), no en la primera review.
_VCS_PROVIDERS = {"twg", "github"}

# Procesos que tienen contrato de secretos propio (I3). Un `required_in` con otro
# nombre es un error al construir el descriptor: un secreto que nadie exige nunca.
PROCESSES = frozenset({"api", "worker", "fix-worker", "web", "teams-bot"})

# Condiciones de activacion del contrato (020 §5.3). Lo unico que la declaracion de
# un campo no puede capturar: el nombre se declara en el `Secret` y aqui vive como se
# evalua. `sandbox` y `panel` miran el proceso porque su condicion cambia segun quien
# pregunte: el contrato del `fix-worker` es hablar con el runner (siempre), y el `web`
# **es** el panel (siempre). Escrito aqui, no escondido en una lista. Todas se usan:
# un nombre sin campo que lo declare es andamiaje muerto.
_FEATURES: Mapping[str, Callable[[Settings, str], bool]] = {
    "sandbox": lambda s, process: s.sandbox_enabled or process == "fix-worker",
    "panel": lambda s, process: s.panel_enabled or process == "web",
    "teams": lambda s, _process: s.teams_enabled,
    "vcs_twg": lambda s, _process: s.vcs_provider != "github",
    "vcs_github": lambda s, _process: s.vcs_provider == "github",
    "llm_legacy": lambda s, _process: s.llm_reviewer_enabled and not s.models_config.strip(),
    "llm_registry": lambda s, _process: s.llm_reviewer_enabled and bool(s.models_config.strip()),
}

# Contrato extra por proceso que no cabe en el descriptor: el registro declarativo de
# modelos nombra sus secretos en el YAML (`${VAR}`), no en un campo de `Settings` (R6).
# Se declara aqui una vez, junto a la condicion, en vez de repetir el par
# proceso/condicion dentro del recorrido generico.
_EXTRA_BY_PROCESS: Mapping[str, tuple[str, ...]] = {
    "api": ("llm_registry",),
    "worker": ("llm_registry",),
}


@dataclass(frozen=True)
class Secret:
    """Declaracion de un campo del contrato de secretos (020 D1, ADR-0022).

    Vive en el `metadata` del `Field`, asi que el inventario del modelo
    (`model_fields`) y la declaracion de seguridad son el mismo sitio y no pueden
    desincronizarse. La **unica** via de lectura es `secrets_for`; leer
    `model_fields[...].metadata` a mano se salta esta declaracion (R5).
    """

    # Procesos que pueden preguntar por este campo. Vacio = todos. No es una lista
    # de «quien lo exige»: quien decide es `feature`; esto solo acota donde aplica.
    required_in: frozenset[str] = frozenset()
    # Condicion de activacion, por nombre de la tabla `_FEATURES`. `None` = siempre.
    # Es el segundo eje del contrato: la condicion puede depender del proceso
    # (p. ej. `sandbox`, que en el `fix-worker` es siempre).
    feature: str | None = None
    # Que se espera del valor. `secret`: vacio o placeholder cuenta como «sin
    # configurar». `plain`: solo el vacio cuenta (una URL, el nombre de una cola).
    # Un unico eje: `plain` cubre los dos casos que antes se declaraban ademas con
    # `required_when_empty`.
    expect: Literal["secret", "plain"] = "secret"
    # Campo que se declara **solo** para que el inventario (I1) sea completo, con el
    # motivo por el que no entra en el contrato de arranque. `""` = si entra.
    inventory_only: str = ""

    def __post_init__(self) -> None:
        unknown = self.required_in - PROCESSES
        if unknown:
            raise ValueError(f"procesos desconocidos en `required_in`: {sorted(unknown)}")
        if self.expect not in {"secret", "plain"}:
            raise ValueError(f"`expect` desconocido: {self.expect!r}")
        if self.feature is not None and self.feature not in _FEATURES:
            raise ValueError(f"`feature` desconocida: {self.feature!r}")
        if self.inventory_only and (self.required_in or self.feature is not None):
            raise ValueError("un campo `inventory_only` no declara proceso ni condicion")


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
    twg_bbc_token: Annotated[
        str, Secret(required_in=frozenset({"api", "worker"}), feature="vcs_twg")
    ] = ""
    # Site y usuario del actor de servicio; los consume twg directamente.
    twg_site: str = ""
    twg_user: str = ""
    # API token de Atlassian (Jira/Confluence). Solo necesario si se usa
    # `twg jira` (escalado a workitem); twg lo lee de `TWG_TOKEN`. Fase 4: entra en
    # el inventario de secretos, no en el contrato de arranque de hoy.
    twg_token: Annotated[
        str, Secret(inventory_only="TWG_TOKEN es de Fase 4 (`twg jira`), no de hoy")
    ] = ""

    # GitHub (VCS_PROVIDER=github). `github_owner`/`github_repo` son la identidad
    # del repo revisado (equivalente a BITBUCKET_WORKSPACE/BITBUCKET_REPO). El
    # token es un PAT fine-grained con `pull_requests: write` + `contents: read`.
    github_owner: str = ""
    github_repo: str = ""
    github_token: Annotated[
        str, Secret(required_in=frozenset({"api", "worker"}), feature="vcs_github")
    ] = ""
    # Base de la API. Se cambia para GitHub Enterprise Server.
    github_api_url: str = "https://api.github.com"

    # El canal de Teams es parte del despliegue. Con `true`, el worker encola los
    # avisos en la outbox (`TEAMS_NOTICES_KEY`) y el bot los publica (C10).
    teams_enabled: bool = False
    # Outbox de avisos al canal (C10). El worker encola aqui; el bot la consume.
    # No es un secreto: se exige por estar VACIA, y `reviews:notices` (su valor por
    # defecto) es legitimo. El placeholder no debe contar aqui.
    teams_notices_key: Annotated[
        str,
        Secret(required_in=frozenset({"api", "worker"}), feature="teams", expect="plain"),
    ] = "reviews:notices"
    # Espera de la lectura bloqueante de la outbox por parte del bot. Vencida, el
    # bucle vuelve a intentarlo (no distingue "sin avisos" de un Redis caido).
    teams_bot_poll_s: float = 5.0
    # Identidad y secreto del bot (T6). El SDK elige el metodo de auth: con
    # `client_secret` usa *client secret*; sin el, identidad gestionada. Los lee
    # SOLO el proceso `teams-bot`.
    teams_bot_id: Annotated[str, Secret(required_in=frozenset({"teams-bot"}), feature="teams")] = ""
    teams_bot_tenant_id: Annotated[
        str, Secret(required_in=frozenset({"teams-bot"}), feature="teams")
    ] = ""
    teams_bot_token: Annotated[
        str, Secret(required_in=frozenset({"teams-bot"}), feature="teams")
    ] = ""
    # Puerto del *messaging endpoint* (`POST /api/messages`) dentro del contenedor.
    teams_bot_port: int = 3978
    # Bearer del **ingress interno** del bot (T7): protege `/healthz` y la
    # observabilidad. El egress no lo usa (va por la outbox). El `change-me` es un
    # valor por defecto del campo (no duplica la lista `PLACEHOLDER_VALUES`): su
    # forma documenta que es un secreto y `.env.example` trae un valor.
    teams_bot_shared_secret: Annotated[
        str, Secret(required_in=frozenset({"teams-bot"}), feature="teams")
    ] = "change-me"

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
    # Razonamiento de los revisores en vivo (021): con `reasoning_stream_enabled` el
    # worker escribe en `reviews:reasoning:<review_key>` lo que el modelo razona en
    # cada lote, y el panel lo muestra. Se conserva `reasoning_ttl_s` (10 dias) con
    # un tope aproximado de `reasoning_max_bytes` (10 MB) por review.
    reasoning_stream_enabled: bool = False
    reasoning_ttl_s: float = 10 * 24 * 3600.0
    reasoning_max_bytes: int = 10 * 1024 * 1024
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
    # no ejecutar comandos. Un secreto real por despliegue. Lo exige el `web`
    # siempre (`panel_always`) y el `api`/`worker` con el panel desplegado.
    panel_session_secret: Annotated[
        str, Secret(required_in=frozenset({"api", "worker", "web"}), feature="panel")
    ] = ""
    # Tope de la consola en vivo por verificacion (lineas del buffer del runner).
    sandbox_console_buffer_lines: int = 2000

    sandbox_enabled: bool = False
    sandbox_registry_user: str = ""
    # Credencial del registro de la imagen del CI. La usa el runner para el `pull`;
    # entra en el inventario, no en el contrato de arranque de hoy.
    sandbox_registry_token: Annotated[
        str, Secret(inventory_only="lo usa el runner para el `pull`, no lo exige el arranque")
    ] = ""
    # Token de Bitbucket de SOLO LECTURA que usa el runner para clonar. Distinto
    # del de `twg` del worker: aunque un PR malicioso lo viera, no da push (Q3).
    sandbox_git_token: Annotated[
        str, Secret(required_in=frozenset({"api", "worker"}), feature="sandbox")
    ] = ""
    # Binario de Docker del runner (rootless en el host del runner).
    sandbox_docker_bin: str = "docker"
    # URL interna del `sandbox-runner` (Fase 3). Con `SANDBOX_ENABLED=true` y esta
    # URL, el nodo `fix` puede verificar parches; sin ellas, queda `NOT_VERIFIABLE`.
    # No es un secreto: es una URL que el fix-worker necesita SIEMPRE para arrancar.
    # Solo su contrato la exige (el worker de reviews la usa, pero no la exige).
    sandbox_runner_url: Annotated[
        str, Secret(required_in=frozenset({"fix-worker"}), expect="plain")
    ] = ""
    sandbox_runner_token: Annotated[
        str, Secret(required_in=frozenset({"api", "worker", "fix-worker"}), feature="sandbox")
    ] = ""
    # Bearer de **solo lectura** de la consola en vivo del runner (019 OP24): lo
    # recibe el panel para reenviar el SSE. Solo abre `GET /verify/{run_key}/console`;
    # no sirve para `POST /verify` ni para los workspaces, asi que el panel no puede
    # lanzar verificaciones. Vacio: la consola solo acepta `SANDBOX_RUNNER_TOKEN`.
    # Es una guarda de **peticion**: entra en el inventario, no en el arranque.
    sandbox_console_token: Annotated[
        str, Secret(inventory_only="guarda de peticion de la consola del runner (019 OP24)")
    ] = ""
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
    # El `change-me` es el valor por defecto del campo, no una copia de la lista.
    tooling_webhook_secret: Annotated[str, Secret(required_in=frozenset({"api", "worker"}))] = (
        "change-me"
    )
    # Bearer del canal de decision (CLI/API). En `development` se relaja como el
    # secreto de tooling: con el valor de ejemplo, el endpoint no exige token.
    reviews_decision_token: Annotated[str, Secret(required_in=frozenset({"api", "worker"}))] = (
        "change-me"
    )
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
    llm_api_key: Annotated[
        str, Secret(required_in=frozenset({"api", "worker"}), feature="llm_legacy")
    ] = ""
    llm_max_findings: int = 10
    llm_budget_tokens_per_pr: int = 2000000
    # Tamano maximo de un lote del `LlmReviewer`, en caracteres de diff (012 C2): se
    # revisan ficheros enteros por lotes; uno que solo no cabe se declara `too_large`.
    # 20 000 con razonamiento (021 R12/R15): con 60 000 el proveedor corta antes de
    # que el modelo responda, y con 30 000 una llamada puede pasar del tope de
    # duracion del stream (~3 min). Un fichero mayor se trocea por hunks (R16).
    llm_batch_max_chars: int = 20000
    # Reintentos de un lote ante un fallo transitorio (021 R14): se repite ese lote,
    # no la persona entera; agotados, el lote sale `failed` y se sigue.
    llm_batch_retries: int = 2
    # Tope de caracteres del bloque L1 (fichero completo + docs del perfil) por
    # lote (008 CX8 / D12). El diff del lote no se recorta; lo que no cabe se omite.
    llm_context_max_chars: int = 40000

    # Verificador adversarial L2 (009 V8/V11): topes por review y por bucle.
    # Sin binding `verifier` el nodo es paso directo y estos ajustes no se usan.
    verifier_max_findings: int = 5
    verifier_max_tool_calls: int = 8
    verifier_max_context_bytes: int = 60_000
    # Umbral de candidatos mientras 011 no declare `severity_gate` (Q10). Lista
    # separada por comas; el nodo prefiere la clave de la política si existe.
    verifier_require_verified_for: str = "P0,P1"

    @property
    def fix_recipes_list(self) -> list[str]:
        return [item.strip() for item in self.fix_recipes.split(",") if item.strip()]

    @property
    def verifier_require_verified_for_list(self) -> list[str]:
        return [
            item.strip() for item in self.verifier_require_verified_for.split(",") if item.strip()
        ]

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
# webhook se omite y cualquiera podria inyectar artefactos. Es la unica definicion de
# la lista: `api`, `web` y el `sandbox_runner` la importan de aqui (020 D5).
PLACEHOLDER_VALUES = frozenset({"change-me", "changeme"})


def _is_placeholder(value: str) -> bool:
    """¿Sigue en uno de los valores de ejemplo de `.env.example`?"""
    return value.strip() in PLACEHOLDER_VALUES


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


def _secret_of(field_info: object) -> Secret | None:
    """El `Secret` declarado en el `metadata` del campo, si lo hay."""
    metadata = getattr(field_info, "metadata", ())
    return next((item for item in metadata if isinstance(item, Secret)), None)


def _is_unset(value: str, secret: Secret) -> bool:
    """¿El campo cuenta como «sin configurar» segun lo que se espera de el?

    `secret`: vacio o placeholder. `plain`: solo el vacio (una URL, el nombre de una
    cola de Redis).
    """
    if secret.expect == "secret":
        return is_missing(value)
    return not value.strip()


def secrets_for(settings: Settings, process: str) -> list[str]:
    """Nombres de los secretos sin configurar que este proceso exige al arrancar.

    Un unico recorrido de `model_fields`, no una lista a mano (020 D4, ADR-0022):
    anadir un campo secreto y olvidar su comprobacion deja de ser posible porque el
    contrato se deriva de la declaracion. El orden es el de `model_fields`, estable.

    `process` se valida contra el conjunto cerrado `PROCESSES` (I3): un nombre
    desconocido es un error, no un contrato vacio. Sin esta comprobacion, un punto de
    arranque nuevo (o un `"fix_worker"` en vez de `"fix-worker"`) pasaria produccion
    con cero comprobaciones y los tests en verde.

    La unica rama que no sale del descriptor es el registro declarativo de modelos:
    los secretos que exige los nombra el YAML (`unresolved_secrets`), no un campo de
    `Settings` (R6); el proceso con ese contrato extra lo declara `_EXTRA_BY_PROCESS`.
    """
    if process not in PROCESSES:
        raise ValueError(f"proceso desconocido: {process!r}; esperado uno de {sorted(PROCESSES)}")
    insecure: list[str] = []
    for name, field_info in Settings.model_fields.items():
        secret = _secret_of(field_info)
        if secret is None or secret.inventory_only:
            continue
        if secret.required_in and process not in secret.required_in:
            continue
        if secret.feature is not None and not _FEATURES[secret.feature](settings, process):
            continue
        if _is_unset(getattr(settings, name), secret):
            insecure.append(name.upper())
    for feature in _EXTRA_BY_PROCESS.get(process, ()):
        if _FEATURES[feature](settings, process):
            insecure.extend(_unresolved_registry_secrets(settings))
    return insecure


def _unresolved_registry_secrets(settings: Settings) -> list[str]:
    """Secretos sin resolver que el registro declarativo de modelos referencia.

    Con registro, el secreto lo nombra el YAML (`${VAR}`), no un campo: por eso no
    cabe en la declaracion y se resuelve aqui. Sin registro, no hay nada que mirar
    (la via de compatibilidad la cubre `llm_api_key`).
    """
    if not settings.models_config.strip():
        return []
    from agent_review.models_config import env_for, load_models_config, unresolved_secrets

    try:
        config = load_models_config(settings.models_config)
    except Exception:
        # Un registro ilegible ya falla al construir el worker con un error mas
        # util; aqui no se duplica.
        return []
    return unresolved_secrets(config, env_for(settings))


def insecure_secrets(settings: Settings) -> list[str]:
    """Nombres de secretos que siguen en su valor de ejemplo o sin rellenar (D4).

    Cubre el contrato completo del proceso `api`/`worker`, no solo la firma del
    webhook: un despliegue en produccion sin credencial de Bitbucket arranca pero
    falla en la primera review, que es exactamente el fallo silencioso que se quiere
    evitar. El contrato se deriva de la declaracion de cada campo (020, ADR-0022).
    """
    return secrets_for(settings, "worker")


def panel_insecure_secrets(settings: Settings) -> list[str]:
    """Secreto de sesion del panel (019 W7) sin configurar o en su valor de ejemplo.

    Contrato propio del proceso `web`, mucho menor que el del worker: el panel no
    recibe credencial de VCS ni clave del LLM (solo lee estado y encola comandos),
    asi que exigirle el contrato completo lo tumbaria en produccion. Una sesion de
    ejemplo (`change-me`) o vacia dejaria el panel sin autenticacion: es
    exactamente el agujero que el fail-closed debe evitar.
    """
    return secrets_for(settings, "web")


def fix_worker_insecure_secrets(settings: Settings) -> list[str]:
    """Secretos que necesita el worker de fixes y siguen sin configurar (E7).

    Su contrato es mucho menor que el del worker de reviews: solo habla con Redis y
    con el runner. No recibe credencial de VCS, token de decision ni clave del LLM
    (vive aislado), asi que exigirle el contrato completo lo tumbaria en produccion.
    """
    return secrets_for(settings, "fix-worker")


def teams_bot_insecure_secrets(settings: Settings) -> list[str]:
    """Secretos que necesita el servicio `teams-bot` y siguen sin configurar (T6).

    Contrato propio: el bot recibe el trafico del Bot Framework (valida su JWT con
    la identidad del bot) y publica proactivamente, pero **no** tiene credencial de
    VCS ni clave del LLM. Solo con `TEAMS_ENABLED=true`; sin el canal, el servicio
    no debe arrancar, asi que su ausencia no es un fallo.
    """
    return secrets_for(settings, "teams-bot")


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
