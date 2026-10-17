from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

CONFIDENCE_ANCHORS = (0, 25, 50, 75, 100)


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AutofixClass(StrEnum):
    GATED_AUTO = "gated_auto"
    MANUAL = "manual"
    ADVISORY = "advisory"


class Owner(StrEnum):
    DOWNSTREAM_RESOLVER = "downstream-resolver"
    HUMAN = "human"
    RELEASE = "release"


class ProtectedSubject(StrEnum):
    MEMORY_SAFETY = "memory-safety"
    CONCURRENCY = "concurrency"
    DATA_LOSS = "data-loss"
    AUTHORIZATION_AUTHENTICATION = "authorization-authentication"
    INJECTION = "injection"
    PUBLIC_CONTRACT = "public-contract"
    SECRETS_EXPOSURE = "secrets-exposure"
    CRYPTOGRAPHY = "cryptography"


class Verification(StrEnum):
    """Estado del hallazgo en la cascada de verificacion (007 D2).

    Nace `unverified`; solo la cascada lo promueve o lo refuta. Un `refuted` se
    conserva en el estado (D3): alimenta la metrica de falsos positivos.
    """

    UNVERIFIED = "unverified"
    VERIFIED_STATIC = "verified_static"
    VERIFIED_GROUNDED = "verified_grounded"
    VERIFIED_EXECUTED = "verified_executed"
    REFUTED = "refuted"


class ContextScope(StrEnum):
    """Lo que sostiene la afirmacion: solo el diff o algo que el revisor no ve (007 D5)."""

    IN_DIFF = "in_diff"
    OUTSIDE_DIFF = "outside_diff"


# Severidades que, sin verificar, deben trazar su escenario de fallo (007 I1).
SCENARIO_SEVERITIES = frozenset({Severity.P0, Severity.P1, Severity.P2})


class ToolingSourceStatus(StrEnum):
    """Estado de la fuente de tooling de una persona estatica.

    Distingue "analisis presente" de "no se pudo leer la salida": sin esto, un
    artefacto ausente y un analisis limpio producen los mismos 0 hallazgos.
    """

    PRESENT = "present"
    MISSING = "missing"
    UNREADABLE = "unreadable"

    @property
    def available(self) -> bool:
        return self is ToolingSourceStatus.PRESENT


class FindingAction(StrEnum):
    REPORT = "report"
    ESCALATE = "escalate"
    FIX_WITH_APPROVAL = "fix_with_approval"
    FIX_AUTO = "fix_auto"

    @property
    def pending(self) -> bool:
        """¿La accion pide a una persona? Freno (escalate) o aprobacion (fix)."""
        return self in (FindingAction.ESCALATE, FindingAction.FIX_WITH_APPROVAL)


# Acciones que dejan la review pendiente de una persona. Se mantiene como
# frozenset de strings por compatibilidad.
PENDING_ACTIONS = frozenset({FindingAction.ESCALATE.value, FindingAction.FIX_WITH_APPROVAL.value})


class Finding(BaseModel):
    """Espejo del findings-schema de ce-code-review."""

    id: str
    title: str = Field(max_length=100)
    severity: Severity
    file: str
    line: int = Field(ge=1)
    why_it_matters: str
    autofix_class: AutofixClass
    owner: Owner
    confidence: int
    evidence: list[str] = Field(min_length=1)
    pre_existing: bool = False
    suggested_fix: str | None = None
    protected_subject: ProtectedSubject | None = None
    # Contrato de verificacion (007). Son hechos, no decisiones: ninguno cambia la
    # severidad; la publicada la calcula solo el gate de 011 (D11).
    failure_scenario: str | None = None
    verification: Verification = Verification.UNVERIFIED
    verification_evidence: list[str] = Field(default_factory=list)
    context_scope: ContextScope | None = None

    @field_validator("confidence")
    @classmethod
    def _anchored(cls, value: int) -> int:
        if value not in CONFIDENCE_ANCHORS:
            raise ValueError(f"confidence debe ser uno de {CONFIDENCE_ANCHORS}")
        return value

    @model_validator(mode="after")
    def _verification_contract(self) -> Finding:
        # I1: una hipotesis P0-P2 traza su camino de fallo; opinar no basta.
        if (
            self.severity in SCENARIO_SEVERITIES
            and self.verification is Verification.UNVERIFIED
            and not (self.failure_scenario or "").strip()
        ):
            raise ValueError("un P0-P2 sin verificar necesita failure_scenario (007 I1)")
        # I2: un veredicto sin cita es otra opinion.
        if self.verification is not Verification.UNVERIFIED and not self.verification_evidence:
            raise ValueError(
                "un hallazgo verificado o refutado necesita verification_evidence (007 I2)"
            )
        return self


class SkipReason(StrEnum):
    """Por que un fichero del PR no se reviso (012 §5.2)."""

    IGNORED = "ignored"  # SCOPE_IGNORE / patrones por defecto
    TOO_LARGE = "too_large"  # no cabe solo en un lote
    BUDGET = "budget"  # presupuesto agotado antes de su lote
    FAILED = "failed"  # su lote fallo de forma permanente


class FileClass(StrEnum):
    """Clase de un fichero para priorizar y para decidir si la review es parcial."""

    CODE = "code"
    CONFIG = "config"
    TEST = "test"
    DOCS = "docs"


# Clases cuya ausencia hace parcial una review (012 C7): docs o tests sin revisar
# se listan, pero no la marcan como parcial.
PARTIAL_CLASSES = frozenset({FileClass.CODE, FileClass.CONFIG})


class ReviewCoverage(BaseModel):
    """Que ficheros del PR se revisaron, cuales no y por que (012 C1).

    Solo cuenta lo **declarado**: un fichero que nadie revisa ni declara saltado
    (p. ej. una review sin persona LLM, que es opcional) no hace parcial la review.
    Quien revisa por lotes declara todo lo que no llego a mirar.
    """

    files_total: int = 0
    reviewed: list[str] = Field(default_factory=list)
    skipped: dict[str, SkipReason] = Field(default_factory=dict)
    file_class: dict[str, FileClass] = Field(default_factory=dict)

    @property
    def partial(self) -> bool:
        # Lo ignorado es ruido por decision (lockfiles, generados): no cuenta.
        return any(
            self.class_of(path) in PARTIAL_CLASSES and reason is not SkipReason.IGNORED
            for path, reason in self.skipped.items()
        )

    def class_of(self, path: str) -> FileClass:
        return self.file_class.get(path, FileClass.CODE)

    def combine(self, other: ReviewCoverage) -> ReviewCoverage:
        """Une dos coberturas: revisado si **alguien** lo reviso (012 §5.2).

        Un fichero saltado por varios conserva el primer motivo declarado; uno que
        alguien reviso deja de contar como saltado.
        """
        reviewed = list(dict.fromkeys([*self.reviewed, *other.reviewed]))
        done = set(reviewed)
        skipped = {
            path: reason
            for path, reason in {**other.skipped, **self.skipped}.items()
            if path not in done
        }
        return ReviewCoverage(
            files_total=max(self.files_total, other.files_total),
            reviewed=reviewed,
            skipped=skipped,
            file_class={**other.file_class, **self.file_class},
        )


class ReviewerResult(BaseModel):
    """Hallazgos de una persona mas el estado de su fuente de datos.

    `tooling_status` solo lo pueblan las personas estaticas; una persona sin
    fuente externa (LLM, nula) devuelve `None` y no aporta avisos ni degrada la
    review. Cuando una persona estatica no puede leer su salida, el estado
    viaja hasta el publish para que "sin hallazgos" no se lea como "sin errores".

    `budget_exhausted` y `tokens_used` son el **hecho** del presupuesto (017 B4).
    `coverage` es lo que la persona leyo de verdad (012): solo la pueblan las
    personas que revisan el diff por lotes; `None` = no declara cobertura.
    """

    findings: list[Finding] = Field(default_factory=list)
    tooling_status: ToolingSourceStatus | None = None
    budget_exhausted: bool = False
    tokens_used: int = 0
    coverage: ReviewCoverage | None = None


class Decision(BaseModel):
    finding_id: str
    action: FindingAction
    reason: str
    # Regla de la politica que disparo la decision ("rule:3" o "guard:0").
    # Permite auditar el por que sin releer la politica del momento.
    matched_rule: str
    # Version de la politica aplicada, para trazabilidad.
    policy_version: str


# ---------------------------------------------------------------------------
# HITL (Fase 2): el contrato de la pausa y de la decision humana.
# ---------------------------------------------------------------------------


class DecisionOutcome(StrEnum):
    """Que decide una persona sobre un hallazgo pendiente."""

    APPROVED = "approved"  # autoriza el arreglo (lo ejecuta la Fase 3)
    REJECTED = "rejected"  # descarta el hallazgo (falso positivo o no aplica)
    NOTED = "noted"  # se atiende fuera del agente (deuda, ticket)
    EXPIRED = "expired"  # nadie decidio dentro del TTL


class FindingDecision(BaseModel):
    finding_id: str
    outcome: DecisionOutcome
    note: str = ""


class HitlDecision(BaseModel):
    """Respuesta humana a una review pausada. Viaja *en* el job de reanudacion."""

    review_key: str
    actor: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Estable por decision: un reintento del resume reutiliza el mismo id.
    decision_id: str = Field(default_factory=lambda: uuid4().hex)
    items: list[FindingDecision] = Field(default_factory=list)

    def outcome_for(self, finding_id: str) -> DecisionOutcome | None:
        for item in self.items:
            if item.finding_id == finding_id:
                return item.outcome
        return None


class PendingDecision(BaseModel):
    """Lo que el nodo `hitl` pide a una persona: contexto + accion propuesta.

    Lleva lo justo para decidir sin abrir el PR: el hallazgo (`title`,
    `severity`), donde esta (`file:line`) y por que importa (`why_it_matters`).
    El canal pinta la tarjeta con esto; no lee el inbox ni el diffs
    (C12). Con default `""` para leer sin romper las pausas ya en Redis.
    """

    finding_id: str
    proposed_action: FindingAction  # escalate | fix_with_approval
    reason: str
    matched_rule: str
    policy_version: str
    title: str
    file: str
    line: int = Field(ge=1)
    severity: Severity
    why_it_matters: str = ""
    # Enlace directo al codigo del hallazgo en el `head_sha`. Lo resuelve el
    # worker (unico que conoce el VCS) al pausar; el canal solo lo pinta.
    code_url: str | None = None


class ApprovalRequest(BaseModel):
    """Carga del `interrupt`: lo que se muestra en el aviso y en el CLI."""

    # Discriminante de la pausa (Fase 3-E7): una pausa humana frente a la pausa
    # del fix (`FixWaitRequest`). Simetrico con `FixWaitRequest.kind`.
    kind: Literal["human"] = "human"
    review_key: str
    repo: str
    pr_id: int
    head_sha: str
    pending: list[PendingDecision] = Field(default_factory=list)
    command_hint: str = ""


class ChatNotice(BaseModel):
    """Aviso del worker al canal de chat; viaja por la outbox (C10).

    Es el mensaje que el worker **encola** (`reviews:notices`): durable y sin
    conocer el transporte. `published` es el teaser de una review ya publicada en
    el PR; `pause` es la tarjeta de decision y lleva la `ApprovalRequest`
    **estructurada** (C12), no su render en texto. `review_key`, `repo` y `pr_id`
    viajan siempre: son la identidad con la que el canal resuelve el hilo (C6).
    """

    kind: Literal["published", "pause"]
    review_key: str
    repo: str
    pr_id: int
    link: str | None = None
    # `kind=published`: el teaser, ya renderizado por el grafo.
    title: str = ""
    body: str = ""
    # `kind=pause`: la carga del `interrupt`; el canal pinta la tarjeta con ella.
    approval: ApprovalRequest | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> ChatNotice:
        """Un aviso incompleto no llega a la outbox: falla al construirlo."""
        if self.kind == "pause":
            if self.approval is None:
                raise ValueError("un aviso de pausa necesita la ApprovalRequest")
        elif not self.title.strip():
            raise ValueError("un aviso de review necesita titulo")
        return self


class VerifyJob(BaseModel):
    """Una verificacion pendiente en el sandbox (un hallazgo, un intento).

    Es la unidad que consume el worker de fixes: lleva todo lo necesario para un
    `VerifyRequest` (E7) sin que ese proceso conozca el grafo ni el VCS.
    """

    run_key: str  # review_key:finding_id:attempt — idempotencia
    finding_id: str
    attempt: int = Field(ge=1)
    repo: str
    head_sha: str
    patch: str
    recipes: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)


class FixWaitRequest(BaseModel):
    """Carga del `interrupt` del nodo `fix_wait`: la wave de verificaciones.

    Espejo de `ApprovalRequest`. El worker de reviews la registra en el inbox y
    la encola en la cola de fixes; el worker de fixes la resuelve entera.
    """

    kind: Literal["fix"] = "fix"
    review_key: str
    repo: str
    pr_id: int
    head_sha: str
    jobs: list[VerifyJob] = Field(default_factory=list)


class FixResolution(BaseModel):
    """Resultado de una verificacion, producido por el worker de fixes."""

    run_key: str
    # `error` es un fallo del runner/peticion (no un check rojo): el fix queda
    # `NOT_VERIFIABLE`, nunca `FAILED` (que implicaria que la verificacion corrio).
    outcome: Literal["passed", "failed", "error"]
    checks: list[CommandResult] = Field(default_factory=list)
    image_digest: str | None = None
    error: str = ""


class FixResolutions(BaseModel):
    """Valor del `Command(resume=...)` de `fix_wait`: una wave resuelta de una vez."""

    items: list[FixResolution] = Field(default_factory=list)


class FixJob(BaseModel):
    """Unidad de la cola de fixes: una wave a verificar y su reanudacion.

    Viaja entera por `FixQueue` para que el worker de fixes pueda, sin grafo ni
    checkpointer, verificar los `jobs` y encolar de vuelta el `request` ya con su
    `fix_resolution`. `request` conserva la identidad del hilo (`review_key`), que
    es la clave del resume. `attempts` cuenta las reentregas del job de fix ante
    un fallo transitorio del runner, igual que `ReviewRequest.attempts`.
    """

    review_key: str
    request: ReviewRequest
    wave: FixWaitRequest
    attempts: int = Field(default=0, ge=0)


class ReviewRequest(BaseModel):
    repo: str
    pr_id: int
    head_sha: str
    personas: list[str] = Field(default_factory=list)
    # Identifica la ejecucion del CI que produjo los artefactos. Un pipeline
    # nuevo sobre el mismo commit es una review nueva; sin el, se cae a la clave
    # de commit.
    pipeline_id: str = ""

    # Procesamientos ya intentados por el worker. Un fallo transitorio reencola
    # el mismo request con `attempts + 1`; agotados los intentos, se descarta con
    # el `review_key` en el log para reinyectarlo a mano.
    attempts: int = Field(default=0, ge=0)

    # HITL (Fase 2): si viene una decision, este job es una *reanudacion* de un
    # hilo pausado, no una review nueva. La identidad (review_key/thread_id) no
    # cambia: la reanudacion reutiliza el hilo de la review.
    decision: HitlDecision | None = None

    # Fase 3-E7: reanudacion de una pausa de fix. La wave la resolvio el worker de
    # fixes y la aplica el worker de reviews, por la misma cola que la decision.
    # `None` en una review normal: el campo no cambia su forma.
    fix_resolution: FixResolutions | None = None

    # Panel (019 W6): comando de cancelacion encolado. La marca de Redis
    # (`reviews:cancel:<review_key>`) la ve la frontera de nodo de una review que
    # corre o espera en la cola, pero una review **pausada** no tiene ningun nodo
    # que la mire: este campo es la senal que el worker aplica al desencolar, sin
    # reanudar el grafo (no se publica nada).
    cancelled: bool = False

    # Pasada degradada: la encola el barrido de ingesta cuando vence la espera sin
    # todos los artefactos. Tiene su propio hilo (`thread_id`) para que, si el
    # artefacto llega tarde, la pasada completa de la misma ejecucion no encuentre
    # el hilo ya terminado y se quede en no-op.
    degraded: bool = False

    @property
    def idempotency_key(self) -> str:
        return f"{self.repo}:{self.pr_id}:{self.head_sha}"

    @property
    def review_key(self) -> str:
        """Clave de la *ejecucion* de la review, estable por pipeline."""
        if self.pipeline_id:
            return f"{self.idempotency_key}:{self.pipeline_id}"
        return self.idempotency_key

    @property
    def thread_id(self) -> str:
        """Hilo del checkpointer: una review re-ejecutada no duplica comentarios.

        Es la clave de ejecucion (`review_key`), no el commit: un `pipeline_id`
        nuevo sobre el mismo `sha` es una review nueva (intencional), y por eso
        va en el hilo. Repetir la *misma* ejecucion reutiliza el hilo.

        La pasada degradada lleva el sufijo `:partial`: comparte `review_key` (y
        token de dedupe) con la completa, pero no su hilo.
        """
        if self.degraded:
            return f"{self.review_key}:partial"
        return self.review_key

    @property
    def dedupe_token(self) -> str:
        """Marca de esta *ejecucion* de review, embebida en lo que publica.

        Se ancla a la clave de ejecucion (`review_key`), no solo al commit: un
        reintento del mismo `pipeline_id` reutiliza el token y el publish
        reconoce que ya publico, mientras que un `pipeline_id` nuevo sobre el
        mismo `sha` es una review nueva (intencional) y publica la suya. Asi el
        dedupe coincide con `thread_id` y deja de regir "gana la primera marca
        por commit". El `sha` va dentro, de modo que commits distintos nunca
        colisionan.
        """
        digest = hashlib.sha256(self.review_key.encode()).hexdigest()[:12]
        return f"reviews:{digest}"

    def ingest_key(self, tool: str) -> str:
        """Clave del *evento de CI* que deposita la salida de `tool`."""
        return f"{self.idempotency_key}:{tool}"


class ReviewOutcome(BaseModel):
    request: ReviewRequest
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    # Fuentes estaticas declaradas que no se pudieron leer. Derivado del estado
    # del grafo, nunca almacenado por las personas: si no esta vacio, la review
    # NO esta completa y "sin hallazgos" no puede leerse como "sin errores".
    unavailable_sources: dict[str, ToolingSourceStatus] = Field(default_factory=dict)
    # HITL (Fase 2): decision humana ya aplicada, si la hubo.
    human_decisions: HitlDecision | None = None
    # El grafo quedo pausado esperando a una persona (no publico los hallazgos).
    paused: bool = False
    # Fase 3: resultado del fix por hallazgo, indexado por `finding_id`.
    fixes: dict[str, FixResult] = Field(default_factory=dict)
    # 017: personas que se cortaron por presupuesto y tokens confirmados por persona.
    # El **hecho** viaja aqui; el render en la cobertura es de 012.
    budget_exhausted: list[str] = Field(default_factory=list)
    tokens_spent: dict[str, int] = Field(default_factory=dict)
    # Saldo del contador compartido al cerrar la review, si el grafo conocia el
    # tope (`None` en tests sin presupuesto). Informativo y derivado del estado:
    # nunca se consulta el puerto durante el `replay` (017 B4).
    tokens_remaining: int | None = None
    # 012: que se reviso y que no. `None` en hilos de antes de 012.
    coverage: ReviewCoverage | None = None

    @property
    def tokens_total(self) -> int:
        """Gasto confirmado de toda la review, sumando a todas las personas."""
        return sum(self.tokens_spent.values())

    @property
    def complete(self) -> bool:
        # Ni una fuente estatica sin cubrir ni codigo o configuracion sin leer (012).
        partial = self.coverage is not None and self.coverage.partial
        return not self.unavailable_sources and not partial

    @property
    def pending_approvals(self) -> list[str]:
        """Ids que esperan a una persona. Derivado, nunca almacenado."""
        return [d.finding_id for d in self.decisions if d.action.value in PENDING_ACTIONS]


ActionLiteral = Literal["report", "escalate", "fix_with_approval", "fix_auto"]


# ---------------------------------------------------------------------------
# Ejecucion de recipes (sandbox). El resultado de un comando es un valor de
# dominio: lo produce el sandbox, lo consume el fix y lo expone el outcome.
# ---------------------------------------------------------------------------


class CommandResult(BaseModel):
    recipe: str
    argv: list[str]
    exit_code: int
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Fase 3: fix verificado. El agente propone un parche, lo verifica en el
# sandbox y, solo si pasa, lo entrega como sugerencia. Nunca escribe el repo.
# ---------------------------------------------------------------------------


class FixStatus(StrEnum):
    VERIFIED = "verified"  # pasa todo; se publica como sugerencia
    FAILED = "failed"  # agota intentos; escala con log
    INVALID_PATCH = "invalid_patch"  # no pasa la validacion local
    NOT_VERIFIABLE = "not_verifiable"  # imagen cambiada, sin digest, runner caido
    SKIPPED = "skipped"  # tope max_fixes_per_pr o presupuesto
    # Bloqueo de seguridad: ruta nunca-auto o tema protegido. Ni con aprobacion
    # humana se arregla: la proteccion manda sobre la decision (D3 invertida).
    BLOCKED = "blocked"
    # E7: parche valido, verificacion **despachada** al worker de fixes y aun sin
    # resolver. Es el unico estado que `fix_dispatch` no puede cerrar por si mismo:
    # la wave se resuelve en otro proceso y el grafo reanuda en `fix_wait`.
    PENDING = "pending_verification"


class ProposedPatch(BaseModel):
    """Parche propuesto por el fixer: reemplaza un rango de un solo fichero.

    El `diff` es unificado (un hunk, un fichero), pero lo que se publica es el
    rango `start_line..end_line` con `replacement`: una sugerencia inline
    reemplaza lineas del lado nuevo del diff, no aplica un parche.
    """

    finding_id: str
    file: str
    diff: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    replacement: str


class FixResult(BaseModel):
    finding_id: str
    status: FixStatus
    attempts: int = Field(default=0, ge=0)
    patch: ProposedPatch | None = None
    # Tails de los checks ejecutados, nunca el log completo.
    checks: list[CommandResult] = Field(default_factory=list)
    image_digest: str | None = None
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.status is FixStatus.VERIFIED
