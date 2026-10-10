from __future__ import annotations

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

    @property
    def idempotency_key(self) -> str:
        return f"{self.repo}:{self.pr_id}:{self.head_sha}"


class ReviewOutcome(BaseModel):
    request: ReviewRequest
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)

    @property
    def pending_approvals(self) -> list[str]:
        """Ids que esperan a una persona. Derivado, nunca almacenado."""
        return [d.finding_id for d in self.decisions if d.action.value in PENDING_ACTIONS]


ActionLiteral = Literal["report", "escalate", "fix_with_approval", "fix_auto"]
