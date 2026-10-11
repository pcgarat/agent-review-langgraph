from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

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
    requires_verification: bool
    confidence: int
    evidence: list[str] = Field(min_length=1)
    pre_existing: bool = False
    suggested_fix: str | None = None
    protected_subject: ProtectedSubject | None = None

    @field_validator("confidence")
    @classmethod
    def _anchored(cls, value: int) -> int:
        if value not in CONFIDENCE_ANCHORS:
            raise ValueError(f"confidence debe ser uno de {CONFIDENCE_ANCHORS}")
        return value


class ReviewerResult(BaseModel):
    """Hallazgos de una persona mas el estado de su fuente de datos.

    `tooling_status` solo lo pueblan las personas estaticas; una persona sin
    fuente externa (LLM, nula) devuelve `None` y no aporta avisos ni degrada la
    review. Cuando una persona estatica no puede leer su salida, el estado
    viaja hasta el publish para que "sin hallazgos" no se lea como "sin errores".
    """

    findings: list[Finding] = Field(default_factory=list)
    tooling_status: ToolingSourceStatus | None = None


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
    """Lo que el nodo `hitl` pide a una persona: contexto + accion propuesta."""

    finding_id: str
    proposed_action: FindingAction  # escalate | fix_with_approval
    reason: str
    matched_rule: str
    policy_version: str
    title: str
    file: str
    line: int = Field(ge=1)
    severity: Severity


class ApprovalRequest(BaseModel):
    """Carga del `interrupt`: lo que se muestra en el aviso y en el CLI."""

    review_key: str
    repo: str
    pr_id: int
    head_sha: str
    pending: list[PendingDecision] = Field(default_factory=list)
    command_hint: str = ""


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

    @property
    def complete(self) -> bool:
        return not self.unavailable_sources

    @property
    def pending_approvals(self) -> list[str]:
        """Ids que esperan a una persona. Derivado, nunca almacenado."""
        return [d.finding_id for d in self.decisions if d.action.value in PENDING_ACTIONS]


ActionLiteral = Literal["report", "escalate", "fix_with_approval", "fix_auto"]
