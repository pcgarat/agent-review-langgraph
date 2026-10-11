from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

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


PENDING_ACTIONS = frozenset({"escalate", "fix_with_approval"})


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
        """
        return self.review_key

    @property
    def dedupe_token(self) -> str:
        """Marca estable de esta review, embebida en lo que publica.

        Es la clave de commit (`idempotency_key`), no la de ejecucion: si un
        `pipeline_id` nuevo re-corre el mismo `sha`, el token coincide y el
        publish reconoce que ya publico sobre ese commit.
        """
        digest = hashlib.sha256(self.idempotency_key.encode()).hexdigest()[:12]
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

    @property
    def complete(self) -> bool:
        return not self.unavailable_sources

    @property
    def pending_approvals(self) -> list[str]:
        """Ids que esperan a una persona. Derivado, nunca almacenado."""
        return [d.finding_id for d in self.decisions if d.action.value in PENDING_ACTIONS]


ActionLiteral = Literal["report", "escalate", "fix_with_approval", "fix_auto"]
