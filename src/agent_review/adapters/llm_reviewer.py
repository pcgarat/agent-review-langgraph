from __future__ import annotations

import functools
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import ValidationError

from agent_review.adapters.context_cache import CacheKey, CacheStats, CachingReader
from agent_review.adapters.diff_parser import (
    FileDiff,
    file_key,
    number_diff,
    parse_unified_diff,
)
from agent_review.adapters.text_utils import guess_protected_subject, shorten_title, slug
from agent_review.adapters.tool_loop import (
    LoopResult,
    ReviewToolsOptions,
    citation_line,
    citation_path,
    run_tool_loop,
)
from agent_review.domain.contracts import ContractError, validate_finding
from agent_review.domain.models import (
    ContextScope,
    Finding,
    ReviewContext,
    ReviewCoverage,
    ReviewerResult,
    Severity,
    SkipReason,
    ToolUsage,
    Verification,
)
from agent_review.errors import is_transient
from agent_review.ports.budget import BudgetPort, release_tokens, reserve_tokens
from agent_review.ports.context import (
    ContextLimits,
    ContextReaderPort,
    ReadingRegistry,
    ReadResult,
)
from agent_review.ports.llm import FingerprintMismatch, LlmError, LlmPort, LlmReply
from agent_review.ports.reasoning import (
    ReasoningEvent,
    ReasoningKind,
    ReasoningSink,
    emit_safely,
)
from agent_review.scope import split_diff
from agent_review.verification.l0 import L0Disposition, evaluate_l0

logger = logging.getLogger(__name__)

# Fabrica por lote (014 §5.3): (repo, head_sha, base_sha, diff) → lector. El `diff`
# alimenta el modo degradado VCS (016); los tests con FixtureContextReader lo ignoran.
ReviewerReaderFactory = Callable[[str, str, str, str], ContextReaderPort]

_SYSTEM = (
    "Eres un revisor de codigo senior. Analizas un diff unificado y devuelves "
    "hallazgos accionables. Respondes SOLO con JSON valido, sin texto alrededor. "
    "Cada hallazgo razona sobre el cambio, no sobre estilo que una herramienta "
    "estatica ya cubre. Si tu afirmacion depende de codigo que no ves (fuera del "
    "diff y del CONTEXT), declara `context_scope: outside_diff` y no inventes su "
    'semantica. Si no hay hallazgos, devuelve {"findings": []}.'
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
# de 60 000 caracteres agota la salida del proveedor antes de responder (021 R12) y
# uno de 30 000 puede pasar del tope de duracion del stream (~3 min, R15).
DEFAULT_BATCH_MAX_CHARS = 20_000
# Tope del bloque L1 por lote (`LLM_CONTEXT_MAX_CHARS`, 008 CX8 / D12).
DEFAULT_CONTEXT_MAX_CHARS = 40_000
# Reintentos por lote ante un fallo transitorio (`LLM_BATCH_RETRIES`, 021 R14) y la
# espera base entre ellos (se dobla en cada intento).
DEFAULT_BATCH_RETRIES = 2
_RETRY_BASE_S = 5.0
_SEVERITY_RANK = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2, Severity.P3: 3}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Estimacion conservadora de tokens: ~4 caracteres por token en texto de codigo.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def pack_l1_context(
    *,
    paths: Sequence[str],
    context: ReviewContext | None,
    max_chars: int,
) -> tuple[str, list[str]]:
    """Empaqueta ficheros del lote + docs bajo el tope L1 (008 CX8).

    Prioridad: ficheros del lote, luego docs del perfil. Lo que no cabe se omite
    (lista de rutas omitidas). El diff va aparte y nunca se recorta aqui.
    """
    if context is None or max_chars <= 0:
        return "", []
    parts: list[str] = []
    used = 0
    omitted: list[str] = []

    def try_add(label: str, path: str, body: str) -> None:
        nonlocal used
        chunk = f"\n{label} {path}:\n{body}"
        if used + len(chunk) > max_chars:
            omitted.append(path)
            return
        parts.append(chunk)
        used += len(chunk)

    for path in paths:
        body = context.contents.get(path)
        if body is None:
            continue
        try_add("FILE", path, body)
    for path, body in context.docs.items():
        try_add("DOC", path, body)
    return "".join(parts), omitted


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
    """Lotes en el orden recibido (ya priorizado por `scope`), sin cortar un hunk.

    Un fichero que cabe va entero (012 C2). Uno que no cabe se trocea por hunks
    completos, cada trozo con las cabeceras del fichero (021 R16); solo un hunk que
    por si solo no cabe va a `too_large` sin gastar llamada. Llenar en orden mantiene
    la prioridad: si el presupuesto se agota, lo que queda fuera es lo ultimo.
    """
    batches: list[list[tuple[str, str]]] = []
    too_large: list[str] = []
    current: list[tuple[str, str]] = []
    size = 0
    for path, block in blocks:
        pieces = [block] if len(block) <= max_chars else _split_block(block, max_chars)
        if pieces is None:
            too_large.append(path)
            continue
        for piece in pieces:
            if current and size + len(piece) > max_chars:
                batches.append(current)
                current, size = [], 0
            current.append((path, piece))
            size += len(piece)
    if current:
        batches.append(current)
    return batches, too_large


def _split_block(block: str, max_chars: int) -> list[str] | None:
    """Trozos de hunks completos de un fichero, o `None` si un hunk no cabe solo."""
    lines = block.splitlines(keepends=True)
    first_hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), None)
    if first_hunk is None:
        return None
    header = "".join(lines[:first_hunk])
    hunks: list[str] = []
    for line in lines[first_hunk:]:
        if line.startswith("@@") or not hunks:
            hunks.append(line)
        else:
            hunks[-1] += line
    pieces: list[str] = []
    current = ""
    for hunk in hunks:
        if len(header) + len(hunk) > max_chars:
            return None
        if current and len(header) + len(current) + len(hunk) > max_chars:
            pieces.append(header + current)
            current = ""
        current += hunk
    if current:
        pieces.append(header + current)
    return pieces


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
        context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
        reasoning_sink: ReasoningSink | None = None,
        batch_retries: int = DEFAULT_BATCH_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        reader_factory: ReviewerReaderFactory | None = None,
        tool_limits: ContextLimits | None = None,
        tool_options: ReviewToolsOptions | None = None,
    ) -> None:
        self.name = name
        self._llm = llm
        self._max_findings = max_findings
        self._max_tokens = max_tokens
        self._batch_max_chars = batch_max_chars
        self._context_max_chars = max(0, context_max_chars)
        # Destino del razonamiento (021 R3): sin el, no se escucha al modelo y la
        # review es la de siempre.
        self._reasoning_sink = reasoning_sink
        self._last_error = ""
        # Reintento por lote (021 R14): un corte del proveedor repite *ese* lote, no
        # la persona entera; agotado, el lote sale `failed` y se sigue.
        self._batch_retries = max(0, batch_retries)
        self._sleep = sleep
        # Presupuesto **compartido** por review (017 B1/B6): el puerto se consulta por
        # `review_key` y el contador lo ven a la vez todas las personas. `budget_limit`
        # es el tope blando por persona (B10) que calcula el wiring; sin el, el puerto
        # es el unico tope (comportamiento compartido).
        self._budget = budget
        self._budget_limit = budget_limit
        # Opt-in del bucle de herramientas (014 H1/H4): sin fabrica, una llamada por
        # lote como en 008. Con fabrica, RH4 cablea `run_tool_loop` por lote.
        self._reader_factory = reader_factory
        self._tool_limits = tool_limits
        # Comportamiento del bucle (023 F1): topes y forma de servir van separados.
        # Con `None` rigen los defaults, que son los de hoy.
        self._tool_options = tool_options or ReviewToolsOptions()
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
        self._context: ReviewContext | None = None
        # Ancla del lote al repo/commit/diff (014): el bucle abre el lector con ellos.
        self._repo = ""
        self._head_sha = ""
        self._base_sha = ""
        self._diff = ""
        # Ultimo bucle del lote (014): `served` y metricas para el ReviewerResult.
        self._last_loop: LoopResult | None = None
        # Registro de lecturas de **la review** (023 S1): sobrevive a los lotes y
        # se vacia al empezar cada una, porque el worker reutiliza la instancia.
        self._readings = ReadingRegistry()
        # Cache de E/S por review (023 C1): solo `read_file`, y solo si el flag
        # esta encendido. Las estadisticas existen para poder medir el ahorro.
        self._io_cache: dict[CacheKey, ReadResult | None] | None = None
        self._io_stats = CacheStats()
        # Peticiones servidas por herramienta y modo en esta review (023 M1):
        # mide si el modelo usa la palanca (`outline`, `regex`), no si existe.
        self._operations: dict[str, int] = {}
        # Rutas de L1 omitidas por tope en esta review (cobertura / diagnostico).
        self.context_omitted: list[str] = []

    # Revisa por lotes y declara lo que no lee (012): si la persona cae entera, el
    # nodo marca sus ficheros como `failed` en vez de callar.
    declares_coverage = True

    @property
    def io_cache_hits(self) -> int:
        """Aciertos de la cache de E/S de esta review (023 C1)."""
        return self._io_stats.hits

    @property
    def io_cache_misses(self) -> int:
        """Lecturas que fueron al lector: E/S real de esta review (023 C1)."""
        return self._io_stats.misses

    @property
    def operation_counts(self) -> dict[str, int]:
        """Peticiones servidas por herramienta y modo en esta review (023 M1)."""
        return dict(self._operations)

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
        context: ReviewContext | None = None,
    ) -> ReviewerResult:
        del pr_id
        self._context = context
        # El bucle de herramientas (014) necesita repo/head/base/diff para abrir el
        # lector; sin fabrica se guardan igual y `_call` sigue siendo una sola
        # `complete` por lote (comportamiento de 008).
        self._repo = repo
        self._head_sha = head_sha
        self._base_sha = context.base_sha if context is not None else ""
        self._diff = diff
        # El worker reutiliza la instancia entre reviews: los hechos del presupuesto
        # son **por review**, no acumulados en el objeto (017 B4).
        self.budget_exhausted = False
        self.tokens_used = 0
        self.context_omitted = []
        self._last_loop = None
        # Una review no valida citas de la anterior (S1): el registro es por review.
        self._readings.clear()
        # La cache de E/S tambien es por review (C1): el contador se reinicia.
        self._io_cache = {} if self._tool_options.io_cache else None
        self._io_stats = CacheStats()
        self._operations = {}
        if not diff.strip():
            return ReviewerResult()
        # `tooling_status` se deja en None: esta persona no depende de un artefacto
        # del CI, asi que su ausencia de hallazgos si es un analisis limpio. Lo que no
        # lee, en cambio, lo declara en la cobertura (012).
        batches, too_large = _batches(split_diff(diff), self._batch_max_chars)
        order = list(dict.fromkeys([*(p for b in batches for p, _ in b), *too_large]))
        # Un fichero troceado cuenta como revisado si lo fueron todos sus trozos; si
        # no, sale con el motivo del primero que falto (021 R16).
        missing: dict[str, SkipReason] = dict.fromkeys(too_large, SkipReason.TOO_LARGE)
        findings: list[Finding] = []
        tool_usage = ToolUsage() if self._reader_factory is not None else None
        total = len(batches)
        for index, batch in enumerate(batches):
            paths = list(dict.fromkeys(path for path, _ in batch))
            if self.budget_exhausted:
                for path in paths:
                    missing.setdefault(path, SkipReason.BUDGET)
                continue
            text = "".join(block for _, block in batch)
            event = functools.partial(self._event, review_key, index + 1, total)
            reply = self._call_with_retries(text, review_key, paths, event)
            if reply is None:
                reason = SkipReason.BUDGET if self.budget_exhausted else SkipReason.FAILED
                note = "sin presupuesto" if reason is SkipReason.BUDGET else self._last_error
                event("error", text=note)
                for path in paths:
                    missing.setdefault(path, reason)
                continue
            entries = self._entries(reply.text)
            event(
                "end", meta=_usage_meta(reply, findings=None if entries is None else len(entries))
            )
            if entries is None:
                for path in paths:
                    missing.setdefault(path, SkipReason.FAILED)
                continue
            files = parse_unified_diff(text)
            served = self._last_loop.readings if self._last_loop is not None else None
            findings.extend(
                f
                for f in (_to_finding(e, files, readings=served) for e in entries)
                if f is not None
            )
            if tool_usage is not None and self._last_loop is not None:
                tool_usage = _accumulate_usage(tool_usage, self._last_loop)
            logger.info(
                "correctness: lote %d/%d revisado (%d ficheros)",
                index + 1,
                len(batches),
                len(paths),
            )
        reviewed = [path for path in order if path not in missing]
        skipped = {path: missing[path] for path in order if path in missing}
        return ReviewerResult(
            findings=self._cap(findings),
            budget_exhausted=self.budget_exhausted,
            tokens_used=self.tokens_used,
            coverage=ReviewCoverage(
                reviewed=reviewed,
                skipped=skipped,
                context_omitted=list(self.context_omitted),
            ),
            tool_usage=tool_usage,
        )

    def _call_with_retries(
        self,
        text: str,
        review_key: str,
        paths: list[str],
        event: Callable[..., None],
    ) -> LlmReply | None:
        """La llamada de un lote con su reintento propio (021 R14).

        Un fallo transitorio (corte del stream, 5xx, 429) repite el lote con espera
        creciente; agotados los reintentos, devuelve `None` como un fallo permanente
        y el lote sale `failed`. Cada intento reabre el lote en el panel.
        """
        for attempt in range(self._batch_retries + 1):
            event("start", meta={"files": paths})
            try:
                return self._call(
                    text,
                    review_key,
                    paths=paths,
                    on_reasoning=self._listener(event),
                )
            except FingerprintMismatch:
                # Una grabacion caducada no es un corte: reintentarla solo repite el
                # aborto y acaba en «0 hallazgos» (D23).
                raise
            except Exception as exc:
                if not is_transient(exc):
                    # Un error permanente no cambia por repetirlo (E0c), pero tampoco
                    # tumba la review: el lote sale `failed` y la review queda parcial
                    # (021 R14, 012 C7). Relanzarlo sube hasta el nodo `persona`, que
                    # tambien lo relanza (`graph/build.py:787`), y se pierden los
                    # hallazgos de los demas lotes.
                    self._last_error = self._last_error or f"fallo permanente del lote: {exc}"
                    logger.warning("correctness: %s; el lote sale failed", self._last_error)
                    return None
                if attempt < self._batch_retries:
                    retry = f"{attempt + 1}/{self._batch_retries}"
                    event(
                        "error",
                        text=f"corte del modelo, reintento {retry}: {exc}",
                        meta={"retry": attempt + 1},
                    )
                    logger.warning(
                        "correctness: lote con fallo transitorio (%s); reintento %d/%d",
                        exc,
                        attempt + 1,
                        self._batch_retries,
                    )
                    self._sleep(_RETRY_BASE_S * 2**attempt)
                    continue
                self._last_error = f"fallo transitorio tras {attempt + 1} intentos: {exc}"
                logger.warning("correctness: %s; el lote sale failed", self._last_error)
        return None

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
        paths: Sequence[str] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply | None:
        """Una llamada por lote. `None` y `budget_exhausted` = sin presupuesto; `None`
        sin el = fallo permanente del modelo (el motivo queda en `_last_error`).

        Con `reader_factory` (014) delega en el bucle de herramientas: el presupuesto
        lo reserva el bucle turno a turno (sin doble reserva).
        """
        self._last_error = ""
        self._last_loop = None
        # El modelo ve el diff numerado (021 R13); el anclaje usa el original.
        # L1 (008 CX8): ficheros + docs bajo tope propio; el diff no se recorta.
        # Con herramientas, `PRECACHE_CONTEXT=off` deja el CONTEXT fuera del prompt:
        # el modelo lee lo que necesita (cuesta turnos) y el prompt base baja (C1).
        numbered = number_diff(diff)
        precache = self._reader_factory is None or self._tool_options.precache_context
        l1, omitted = (
            pack_l1_context(
                paths=list(paths or ()),
                context=self._context,
                max_chars=self._context_max_chars,
            )
            if precache
            else ("", [])
        )
        if omitted:
            for path in omitted:
                if path not in self.context_omitted:
                    self.context_omitted.append(path)
            logger.info(
                "correctness: contexto L1 omitido por tope (%d chars): %s",
                self._context_max_chars,
                ", ".join(omitted),
            )
        prompt = f"{_SCHEMA_HINT % self._max_findings}\n\nDIFF:\n{numbered}"
        if l1:
            prompt = f"{prompt}\n\nCONTEXT:{l1}"
        if self._reader_factory is not None:
            return self._call_with_tools(prompt, review_key, on_reasoning=on_reasoning)
        return self._call_once(prompt, review_key, on_reasoning=on_reasoning)

    def _call_with_tools(
        self,
        prompt: str,
        review_key: str,
        *,
        on_reasoning: Callable[[str], None] | None,
    ) -> LlmReply | None:
        """Bucle *ReAct* por lote (014 RH4): el presupuesto lo consume el bucle."""
        factory = self._reader_factory
        if factory is None:
            raise RuntimeError("LlmReviewer._call_with_tools sin reader_factory")
        limits = self._tool_limits or ContextLimits()
        # Estimacion de un turno: el tope blando (B10) corta antes de abrir el bucle.
        estimated = _estimate_tokens(_SYSTEM) + _estimate_tokens(prompt) + self._max_tokens
        self.last_estimated_tokens = estimated
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
        # El presupuesto compartido lo reserva el bucle turno a turno (sin doble
        # reserva). Un `False` de `reserve` fuerza el turno final (016/017 B7).
        reader = factory(self._repo, self._head_sha, self._base_sha, self._diff)
        if self._io_cache is not None:
            reader = CachingReader(reader, cache=self._io_cache, stats=self._io_stats)
        try:
            loop = run_tool_loop(
                self._llm,
                system=_SYSTEM,
                prompt=prompt,
                reader=reader,
                limits=limits,
                options=self._tool_options,
                is_final=_is_findings,
                budget=self._budget,
                review_key=review_key,
                max_tokens=self._max_tokens,
                on_reasoning=on_reasoning,
                readings=self._readings,
            )
        except FingerprintMismatch:
            # El replay no casa: es un fallo de medida, no una review limpia (D23).
            raise
        except Exception as exc:
            if is_transient(exc):
                raise
            self._last_error = f"el modelo fallo: {exc}"
            if isinstance(exc, LlmError):
                logger.warning("correctness: el modelo fallo (%s); lote no revisado", exc)
            else:
                logger.exception("correctness: error inesperado del modelo; lote no revisado")
            return None
        self._last_loop = loop
        self.tokens_used += loop.usage
        for operation, count in loop.operations.items():
            self._operations[operation] = self._operations.get(operation, 0) + count
        # Preferir el objeto final parseado: el texto crudo puede traer prosa alrededor.
        text = (
            json.dumps(loop.data, ensure_ascii=False)
            if loop.data is not None
            else (loop.text or "")
        )
        # El `LlmReply` del lote lleva los contadores **separados** (024 E9): antes
        # ponia `prompt_tokens=0` y el total como *completion*, que es un gasto
        # mal atribuido aunque nadie lo leyera todavia.
        reply = LlmReply(
            text=text,
            prompt_tokens=loop.prompt_tokens,
            completion_tokens=loop.completion_tokens,
            reasoning_tokens=loop.reasoning_tokens,
            cached_tokens=loop.cached_tokens,
        )
        self.last_usage = reply
        return reply

    def _call_once(
        self,
        prompt: str,
        review_key: str,
        *,
        on_reasoning: Callable[[str], None] | None,
    ) -> LlmReply | None:
        """Una `complete` por lote (camino 008, sin herramientas)."""
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
        except FingerprintMismatch:
            # El replay no casa: es un fallo de medida, no una review limpia (D23).
            # La reserva no llego a gastarse.
            release_tokens(self._budget, review_key, estimated)
            raise
        except Exception as exc:
            # La reserva no llego a gastarse: se devuelve antes de subir o degradar,
            # para que el reintento del nodo no reserve dos veces (B8).
            release_tokens(self._budget, review_key, estimated)
            # Un fallo transitorio (red, timeout, 5xx, 429) sube para que la
            # `RetryPolicy` del nodo `persona` lo reintente; si se agotan los
            # intentos, el nodo cierra con `FAILED`. Tragarlo aqui dejaba al
            # nodo sin nada que reintentar.
            if is_transient(exc):
                raise
            self._last_error = f"el modelo fallo: {exc}"
            if isinstance(exc, LlmError):
                logger.warning("correctness: el modelo fallo (%s); lote no revisado", exc)
            else:
                logger.exception("correctness: error inesperado del modelo; lote no revisado")
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


def _is_findings(data: dict[str, Any]) -> bool:
    """¿Es el objeto final del revisor (`findings`) y no una peticion de herramientas?"""
    if "tools" in data and "findings" not in data:
        return False
    return isinstance(data.get("findings"), list)


def _accumulate_usage(acc: ToolUsage, loop: LoopResult) -> ToolUsage:
    """Suma metricas del bucle entre lotes (014 H8).

    `files_read` cuenta solo cuerpos (`full`/`range`): un `outline` o un `grep` no
    es haber leido el fichero (023 S1).
    """
    return ToolUsage(
        turns=acc.turns + loop.turns,
        tool_calls=acc.tool_calls + loop.tool_calls,
        bytes_served=acc.bytes_served + loop.bytes_served,
        files_read=list(dict.fromkeys([*acc.files_read, *loop.readings.files_read()])),
    )


def _filter_served_evidence(evidence: list[str], readings: ReadingRegistry) -> list[str]:
    """Quita citas `path:line` que el bucle no sirvio (014 H7, 023 S1).

    Una lectura de cuerpo de la misma `ref` que cubre la linea; `outline`, `grep`
    y las lecturas de `base` no prueban un hallazgo sobre `head`.
    """
    kept: list[str] = []
    for item in evidence:
        path = citation_path(item)
        if path and not readings.serves(path, line=citation_line(item)):
            continue
        kept.append(item)
    return kept


def _to_finding(
    entry: dict[str, Any],
    files: dict[str, FileDiff],
    *,
    readings: ReadingRegistry | None = None,
) -> Finding | None:
    title = str(entry.get("title", "")).strip()
    why = str(entry.get("why_it_matters", "")).strip()
    path = str(entry.get("file", "")).strip()
    if not title or not why or not path:
        return None
    key = file_key(files, path)
    if key is None:
        return None  # el modelo invento la ruta: fuera del diff, no se publica
    file_diff = files[key]
    hint = _as_int(entry.get("line"))
    evidence_in = [
        str(item).strip() for item in _as_list(entry.get("evidence")) if str(item).strip()
    ]
    if readings is not None:
        evidence_in = _filter_served_evidence(evidence_in, readings)
    cite = _cite_text(evidence_in)
    # 008 CX5 / D2 / D13: L0 manda; el `pre_existing` del modelo se ignora.
    l0 = evaluate_l0(file_diff, hint=hint, cite=cite)
    if l0.disposition is L0Disposition.DROP or l0.line is None:
        logger.info("correctness: hallazgo descartado por L0 (%s)", l0.reason)
        return None
    line = l0.line
    line_text = (
        file_diff.added.get(line)
        or file_diff.context.get(line)
        or file_diff.removed.get(line)
        or ""
    )
    evidence = evidence_in or [f"{key}:{line} {line_text.strip()}".strip()]
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
        "pre_existing": l0.pre_existing,
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


def _cite_text(evidence: list[str]) -> str | None:
    """Texto de evidencia para L0, sin el prefijo `file:line`."""
    for item in evidence:
        head, _, rest = item.partition(" ")
        if rest and ":" in head:
            return rest.strip()
        if rest:
            return rest.strip()
        if item.strip():
            return item.strip()
    return None


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
