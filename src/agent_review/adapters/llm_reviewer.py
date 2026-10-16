from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from agent_review.adapters.diff_parser import FileDiff, best_line, file_key, parse_unified_diff
from agent_review.adapters.text_utils import guess_protected_subject, shorten_title, slug
from agent_review.domain.contracts import ContractError, validate_finding
from agent_review.domain.models import Finding, ReviewerResult, Severity
from agent_review.errors import is_transient
from agent_review.ports.budget import BudgetPort, release_tokens, reserve_tokens
from agent_review.ports.llm import LlmError, LlmPort, LlmReply
from agent_review.scope import changed_files

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Eres un revisor de codigo senior. Analizas un diff unificado y devuelves "
    "hallazgos accionables. Respondes SOLO con JSON valido, sin texto alrededor. "
    "Cada hallazgo razona sobre el cambio, no sobre estilo que una herramienta "
    'estatica ya cubre. Si no hay hallazgos, devuelve {"findings": []}.'
)

_SCHEMA_HINT = """\
Devuelve {"findings": [ ... ]}. Cada hallazgo:
{
  "title": "<=100 chars, conciso>",
  "severity": "P0|P1|P2|P3",
  "file": "ruta del fichero tal como aparece en el diff",
  "line": <entero, linea del lado nuevo del diff>,
  "why_it_matters": "impacto y modo de fallo, no 'esta mal'",
  "autofix_class": "gated_auto|manual|advisory",
  "owner": "downstream-resolver|human|release",
  "requires_verification": true|false,
  "confidence": 0|25|50|75|100,
  "evidence": ["cita textual de la linea del diff"],
  "suggested_fix": "opcional, cambio de codigo corto",
  "protected_subject": "opcional: memory-safety|concurrency|data-loss|\
authorization-authentication|injection|public-contract|secrets-exposure|cryptography"
}
Reglas: no inventes ficheros ni lineas que no esten en el diff; `confidence` solo
puede ser 0/25/50/75/100; la primera evidencia cita la linea. Como maximo %d hallazgos.
"""

# Un diff enorme no debe dispararse como prompt entero: se acota por coste.
_MAX_DIFF_CHARS = 60_000
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Estimacion conservadora de tokens: ~4 caracteres por token en texto de codigo.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


class LlmReviewer:
    """Persona LLM: propone hallazgos y los valida contra el contrato.

    El modelo es un puerto inyectable (`LlmPort`); esta persona no conoce ningun
    proveedor. La salida se anca al diff igual que la persona estatica, de modo
    que una linea inventada por el modelo se descarta y nunca se publica. Un
    fallo permanente del modelo o un parseo invalido produce 0 hallazgos. Un
    fallo transitorio se propaga para que el nodo lo reintente; agotados los
    intentos, el nodo degrada a 0 hallazgos: jamas una review caida.
    """

    def __init__(
        self,
        *,
        llm: LlmPort,
        name: str = "correctness",
        max_findings: int = 10,
        max_tokens: int = 4096,
        budget: BudgetPort | None = None,
        budget_limit: int | None = None,
    ) -> None:
        self.name = name
        self._llm = llm
        self._max_findings = max_findings
        self._max_tokens = max_tokens
        # Presupuesto **compartido** por review (017 B1/B6): el puerto se consulta por
        # `review_key` y el contador lo ven a la vez todas las personas. `budget_limit`
        # es el tope blando por persona (B10) que calcula el wiring; sin el, el puerto
        # es el unico tope (comportamiento compartido).
        self._budget = budget
        self._budget_limit = budget_limit
        # Tokens estimados de la ultima llamada: se expone para poder medir el
        # gasto real por review (C3) sin acoplar el puerto al proveedor. `last_usage`
        # lleva el gasto **real** que reporta el proveedor, que es la fuente de
        # verdad cuando esta disponible.
        self.last_estimated_tokens = 0
        self.last_usage: LlmReply | None = None
        # Se corto por presupuesto: los ficheros que falten no se revisaron (017 B4).
        # El **hecho** lo consume 012 para la cobertura; aqui solo se declara.
        self.budget_exhausted = False
        self.tokens_used = 0
        # Ficheros que entraron de verdad en el prompt de la ultima llamada. Sale
        # del diff ya recortado a `_MAX_DIFF_CHARS` (lo que el modelo vio, no lo
        # que el PR traia): es la cota fiel para medir cobertura (E11 de 006) sin
        # depender de una constante externa. Solo lectura.
        self.last_seen_files: tuple[str, ...] = ()

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
    ) -> ReviewerResult:
        del repo, pr_id, head_sha
        # El worker reutiliza la instancia entre reviews: los hechos del presupuesto
        # son **por review**, no acumulados en el objeto (017 B4).
        self.budget_exhausted = False
        self.tokens_used = 0
        if not diff.strip():
            return ReviewerResult()
        # `tooling_status` se deja en None: esta persona no depende de un artefacto
        # del CI, asi que su ausencia de hallazgos si es un analisis limpio.
        reply = self._call(diff, review_key)
        if reply is None:
            return ReviewerResult(
                budget_exhausted=self.budget_exhausted, tokens_used=self.tokens_used
            )
        entries = self._entries(reply.text)
        if entries is None:
            return ReviewerResult(
                budget_exhausted=self.budget_exhausted, tokens_used=self.tokens_used
            )
        files = parse_unified_diff(diff)
        findings: list[Finding] = []
        for entry in entries:
            if len(findings) >= self._max_findings:
                logger.info("correctness: tope de %d hallazgos alcanzado", self._max_findings)
                break
            finding = _to_finding(entry, files)
            if finding is not None:
                findings.append(finding)
        return ReviewerResult(
            findings=findings,
            budget_exhausted=self.budget_exhausted,
            tokens_used=self.tokens_used,
        )

    def _call(self, diff: str, review_key: str = "") -> LlmReply | None:
        if len(diff) > _MAX_DIFF_CHARS:
            logger.warning(
                "correctness: diff truncado de %d a %d chars", len(diff), _MAX_DIFF_CHARS
            )
        window = diff[:_MAX_DIFF_CHARS]
        # Los ficheros que el modelo ve de verdad: se derivan del diff ya recortado,
        # de modo que la cobertura mide el recorte real y no una constante aparte.
        self.last_seen_files = tuple(changed_files(window))
        prompt = f"{_SCHEMA_HINT % self._max_findings}\n\nDIFF:\n{window}"
        estimated = _estimate_tokens(_SYSTEM) + _estimate_tokens(prompt) + self._max_tokens
        self.last_estimated_tokens = estimated
        # Tope blando por persona (B10): acota lo que una persona con prompt grande
        # puede comerse del presupuesto compartido.
        if self._budget_limit is not None and estimated > self._budget_limit:
            logger.warning(
                "correctness: coste estimado %d tokens supera el tope de la persona %d; se omite",
                estimated,
                self._budget_limit,
            )
            self.budget_exhausted = True
            return None
        # Reserva en el contador compartido (B3/B7): un `False` corta igual que el
        # tope blando. Es lo que ven a la vez todas las ramas del fan-out.
        if not reserve_tokens(self._budget, review_key, estimated):
            logger.warning(
                "correctness: sin presupuesto compartido para esta review (%d tokens); "
                "se omite la persona",
                estimated,
            )
            self.budget_exhausted = True
            return None
        try:
            reply = self._llm.complete(
                system=_SYSTEM,
                prompt=prompt,
                max_tokens=self._max_tokens,
                json_mode="json_object",
            )
        except Exception as exc:
            # La reserva no llego a gastarse: se devuelve antes de subir o degradar,
            # para que el reintento del nodo no reserve dos veces (B8).
            release_tokens(self._budget, review_key, estimated)
            # Un fallo transitorio (red, timeout, 5xx, 429) sube para que la
            # `RetryPolicy` del nodo `persona` lo reintente; si se agotan los
            # intentos, el nodo degrada a 0 hallazgos. Tragarlo aqui dejaba al
            # nodo sin nada que reintentar.
            if is_transient(exc):
                raise
            if isinstance(exc, LlmError):
                logger.warning("correctness: el modelo fallo (%s); 0 hallazgos", exc)
            else:
                logger.exception("correctness: error inesperado del modelo; 0 hallazgos")
            return None
        # Ajuste con el gasto real: lo reservado de mas vuelve al contador comun.
        if self._budget is not None and review_key:
            self._budget.commit(review_key, estimated, reply.total_tokens)
        self.tokens_used += reply.total_tokens
        self.last_usage = reply
        return reply

    def _entries(self, raw: str) -> list[dict[str, Any]] | None:
        data = _parse_json(raw)
        if data is None:
            logger.warning("correctness: salida no parseable; 0 hallazgos")
            return None
        entries = data.get("findings") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            logger.warning("correctness: la salida no trae una lista 'findings'; 0 hallazgos")
            return None
        return [entry for entry in entries if isinstance(entry, dict)]


def _parse_json(raw: str) -> Any:
    text = _FENCE.sub("", raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Ultimo intento: el modelo a veces rodea el JSON de prosa.
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _to_finding(entry: dict[str, Any], files: dict[str, FileDiff]) -> Finding | None:
    title = str(entry.get("title", "")).strip()
    why = str(entry.get("why_it_matters", "")).strip()
    path = str(entry.get("file", "")).strip()
    if not title or not why or not path:
        return None
    key = file_key(files, path)
    if key is None:
        return None  # el modelo invento la ruta: fuera del diff, no se publica
    added = files[key].added
    hint = _as_int(entry.get("line"))
    evidence_in = [
        str(item).strip() for item in _as_list(entry.get("evidence")) if str(item).strip()
    ]
    anchor = _anchor_text(evidence_in, title)
    line = best_line(added, hint, anchor)
    if line is None:
        return None
    evidence = evidence_in or [f"{key}:{line} {added.get(line, '').strip()}".strip()]
    subject = entry.get("protected_subject") or guess_protected_subject(f"{title} {why}")
    payload = {
        "id": str(entry.get("id") or f"correctness-{slug(key)}-{line}-{slug(title)[:24]}"),
        "title": shorten_title(title),
        "severity": _severity(entry.get("severity")),
        "file": key,
        "line": line,
        "why_it_matters": why,
        "autofix_class": str(entry.get("autofix_class") or "advisory"),
        "owner": str(entry.get("owner") or "downstream-resolver"),
        "requires_verification": bool(entry.get("requires_verification", False)),
        "confidence": _confidence(entry.get("confidence")),
        "evidence": evidence,
        "pre_existing": bool(entry.get("pre_existing", False)),
        "suggested_fix": _as_text(entry.get("suggested_fix")),
        "protected_subject": subject,
    }
    try:
        return validate_finding(payload)
    except (ContractError, ValidationError) as exc:
        logger.info("correctness: hallazgo descartado por el contrato (%s)", exc)
        return None


def _anchor_text(evidence: list[str], title: str) -> str | None:
    """Texto para `best_line`: la parte citada, sin el prefijo `file:line`."""
    for item in evidence:
        head, _, rest = item.partition(" ")
        if rest and ":" in head:
            return rest.strip()[:40]
        if rest:
            return rest.strip()[:40]
    return title[:40] or None


def _severity(value: object) -> str:
    text = str(value or "P3").upper()
    return text if text in {sev.value for sev in Severity} else "P3"


def _confidence(value: object) -> int:
    number = _as_int(value)
    return number if number in (0, 25, 50, 75, 100) else 50


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
