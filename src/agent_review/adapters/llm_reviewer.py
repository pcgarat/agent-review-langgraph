from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from agent_review.adapters.diff_parser import (
    FileDiff,
    best_line,
    file_key,
    number_diff,
    parse_unified_diff,
)
from agent_review.adapters.text_utils import guess_protected_subject, shorten_title, slug
from agent_review.domain.contracts import ContractError, validate_finding
from agent_review.domain.models import (
    ContextScope,
    Finding,
    ReviewCoverage,
    ReviewerResult,
    Severity,
    SkipReason,
    Verification,
)
from agent_review.errors import is_transient
from agent_review.ports.budget import BudgetPort, release_tokens, reserve_tokens
from agent_review.ports.llm import LlmError, LlmPort, LlmReply
from agent_review.ports.reasoning import (
    ReasoningEvent,
    ReasoningKind,
    ReasoningSink,
    emit_safely,
)
from agent_review.scope import split_diff

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
  "line": <entero: el numero que lleva delante la linea en el diff>,
  "why_it_matters": "impacto y modo de fallo, no 'esta mal'",
  "autofix_class": "gated_auto|manual|advisory",
  "owner": "downstream-resolver|human|release",
  "confidence": 0|25|50|75|100,
  "evidence": ["cita textual de la linea, sin el numero ni el +"],
  "failure_scenario": "obligatorio en P0/P1/P2: input o estado -> ruta file:line -> \
resultado incorrecto",
  "context_scope": "in_diff|outside_diff",
  "suggested_fix": "opcional, cambio de codigo corto",
  "protected_subject": "opcional: memory-safety|concurrency|data-loss|\
authorization-authentication|injection|public-contract|secrets-exposure|cryptography"
}
Cada linea anadida o de contexto del diff lleva delante su numero en el fichero
nuevo; usa ese numero en `line`, no lo cuentes. Las borradas no llevan numero.
Reglas: no inventes ficheros ni lineas que no esten en el diff; `confidence` solo
puede ser 0/25/50/75/100; la primera evidencia cita la linea. Un P0, P1 o P2 sin
`failure_scenario` concreto se descarta: traza el camino del fallo, no opines.
`context_scope` es `outside_diff` si la afirmacion depende de codigo que no esta en
el diff (una firma, una configuracion, otro fichero); `in_diff` si basta el diff.
Como maximo %d hallazgos.
"""

# Tamano de lote por defecto (`LLM_BATCH_MAX_CHARS`). Con razonamiento alto, un lote
# de 60 000 caracteres agota la salida del proveedor antes de responder (021 R12).
DEFAULT_BATCH_MAX_CHARS = 30_000
_SEVERITY_RANK = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2, Severity.P3: 3}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Estimacion conservadora de tokens: ~4 caracteres por token en texto de codigo.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _usage_meta(reply: LlmReply, *, findings: int | None) -> dict[str, Any]:
    """Lo que el panel muestra al cerrar un lote (021 §5.2)."""
    return {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "reasoning_tokens": reply.reasoning_tokens,
        "finish_reason": reply.finish_reason,
        "findings": findings if findings is not None else "salida ilegible",
    }


def _batches(
    blocks: list[tuple[str, str]], max_chars: int
) -> tuple[list[list[tuple[str, str]]], list[str]]:
    """Lotes de ficheros **enteros** en el orden recibido (ya priorizado por `scope`).

    Nunca se corta un fichero (012 C2): el que solo ya no cabe va a `too_large` sin
    gastar llamada (C3). Llenar en orden mantiene la prioridad: si el presupuesto se
    agota, lo que se queda fuera es lo ultimo (tests y docs).
    """
    batches: list[list[tuple[str, str]]] = []
    too_large: list[str] = []
    current: list[tuple[str, str]] = []
    size = 0
    for path, block in blocks:
        if len(block) > max_chars:
            too_large.append(path)
            continue
        if current and size + len(block) > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append((path, block))
        size += len(block)
    if current:
        batches.append(current)
    return batches, too_large


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
        batch_max_chars: int = DEFAULT_BATCH_MAX_CHARS,
        reasoning_sink: ReasoningSink | None = None,
    ) -> None:
        self.name = name
        self._llm = llm
        self._max_findings = max_findings
        self._max_tokens = max_tokens
        self._batch_max_chars = batch_max_chars
        # Destino del razonamiento (021 R3): sin el, no se escucha al modelo y la
        # review es la de siempre.
        self._reasoning_sink = reasoning_sink
        self._last_error = ""
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

    # Revisa por lotes y declara lo que no lee (012): si la persona cae entera, el
    # nodo marca sus ficheros como `failed` en vez de callar.
    declares_coverage = True

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
        # del CI, asi que su ausencia de hallazgos si es un analisis limpio. Lo que no
        # lee, en cambio, lo declara en la cobertura (012).
        batches, too_large = _batches(split_diff(diff), self._batch_max_chars)
        reviewed: list[str] = []
        skipped: dict[str, SkipReason] = {path: SkipReason.TOO_LARGE for path in too_large}
        findings: list[Finding] = []
        total = len(batches)
        for index, batch in enumerate(batches):
            paths = [path for path, _ in batch]
            if self.budget_exhausted:
                skipped.update(dict.fromkeys(paths, SkipReason.BUDGET))
                continue
            text = "".join(block for _, block in batch)
            event = functools.partial(self._event, review_key, index + 1, total)
            event("start", meta={"files": paths})
            try:
                reply = self._call(text, review_key, on_reasoning=self._listener(event))
            except Exception as exc:
                # Transitorio: sube para que lo reintente el nodo; el panel lo ve.
                event("error", text=f"fallo transitorio del modelo: {exc}")
                raise
            if reply is None:
                reason = SkipReason.BUDGET if self.budget_exhausted else SkipReason.FAILED
                note = "sin presupuesto" if reason is SkipReason.BUDGET else self._last_error
                event("error", text=note)
                skipped.update(dict.fromkeys(paths, reason))
                continue
            entries = self._entries(reply.text)
            event(
                "end", meta=_usage_meta(reply, findings=None if entries is None else len(entries))
            )
            if entries is None:
                skipped.update(dict.fromkeys(paths, SkipReason.FAILED))
                continue
            reviewed.extend(paths)
            files = parse_unified_diff(text)
            findings.extend(f for f in (_to_finding(e, files) for e in entries) if f is not None)
            logger.info(
                "correctness: lote %d/%d revisado (%d ficheros)",
                index + 1,
                len(batches),
                len(paths),
            )
        return ReviewerResult(
            findings=self._cap(findings),
            budget_exhausted=self.budget_exhausted,
            tokens_used=self.tokens_used,
            coverage=ReviewCoverage(reviewed=reviewed, skipped=skipped),
        )

    def _event(
        self,
        review_key: str,
        batch: int,
        total: int,
        kind: ReasoningKind,
        *,
        text: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        emit_safely(
            self._reasoning_sink,
            review_key,
            ReasoningEvent(
                kind=kind, role=self.name, batch=batch, total=total, text=text, meta=meta or {}
            ),
        )

    def _listener(self, event: Callable[..., None]) -> Callable[[str], None] | None:
        """El oyente del modelo, solo si alguien escucha (021 R2)."""
        if self._reasoning_sink is None:
            return None
        return lambda piece: event("reasoning", text=piece)

    def _cap(self, findings: list[Finding]) -> list[Finding]:
        """`max_findings` sobre el total de la persona, primero lo mas grave (012 §5.3)."""
        if len(findings) <= self._max_findings:
            return findings
        logger.info("correctness: tope de %d hallazgos alcanzado", self._max_findings)
        ranked = sorted(findings, key=lambda f: _SEVERITY_RANK[f.severity])
        return ranked[: self._max_findings]

    def _call(
        self,
        diff: str,
        review_key: str = "",
        *,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply | None:
        """Una llamada por lote. `None` y `budget_exhausted` = sin presupuesto; `None`
        sin el = fallo permanente del modelo (el motivo queda en `_last_error`)."""
        self._last_error = ""
        # El modelo ve el diff numerado (021 R13); el anclaje usa el original.
        prompt = f"{_SCHEMA_HINT % self._max_findings}\n\nDIFF:\n{number_diff(diff)}"
        estimated = _estimate_tokens(_SYSTEM) + _estimate_tokens(prompt) + self._max_tokens
        self.last_estimated_tokens = estimated
        # Tope blando por persona (B10), sobre lo **acumulado** entre lotes: acota lo
        # que una persona con muchos lotes puede comerse del presupuesto compartido.
        if self._budget_limit is not None and self.tokens_used + estimated > self._budget_limit:
            logger.warning(
                "correctness: %d tokens gastados + %d estimados superan el tope de la "
                "persona %d; se omite el resto",
                self.tokens_used,
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
                "se omite el resto",
                estimated,
            )
            self.budget_exhausted = True
            return None
        try:
            # `on_reasoning` solo viaja si alguien escucha: un puerto que no razona
            # (o un doble de test) no tiene por que aceptarlo.
            if on_reasoning is None:
                reply = self._llm.complete(
                    system=_SYSTEM,
                    prompt=prompt,
                    max_tokens=self._max_tokens,
                    json_mode="json_object",
                )
            else:
                reply = self._llm.complete(
                    system=_SYSTEM,
                    prompt=prompt,
                    max_tokens=self._max_tokens,
                    json_mode="json_object",
                    on_reasoning=on_reasoning,
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
            self._last_error = f"el modelo fallo: {exc}"
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
        "confidence": _confidence(entry.get("confidence")),
        "evidence": evidence,
        "pre_existing": bool(entry.get("pre_existing", False)),
        "suggested_fix": _as_text(entry.get("suggested_fix")),
        "protected_subject": subject,
        "failure_scenario": _as_text(entry.get("failure_scenario")),
        # El modelo propone hipotesis: no puede autopromoverse (007 §5.4). Lo que
        # devuelva en `verification`/`verification_evidence` se ignora.
        "verification": Verification.UNVERIFIED.value,
        "context_scope": _context_scope(entry.get("context_scope")),
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


def _context_scope(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {scope.value for scope in ContextScope} else None


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
