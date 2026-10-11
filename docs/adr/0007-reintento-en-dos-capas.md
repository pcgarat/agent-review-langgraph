# ADR-0007 — Reintento en dos capas con clasificación propia de errores

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

Al principio solo se reintentaba el job entero, reencolándolo. Para un fallo corto y
local (un 500 de `twg`, un 429 del LLM, Teams caído un segundo) eso tiraba el grafo y
repetía las personas. Peor aún: un reintento con input nuevo arrancaba desde `START`,
acumulaba `findings`, pagaba el LLM otra vez y, tras una decisión humana, podía
publicar hallazgos recalculados que nadie había decidido.

`default_retry_on` de LangGraph **no** reintenta `RuntimeError` (de la que heredan los
errores de los adaptadores), ni `TimeoutError`, ni un 429.

## Decisión

**Capa de nodo.** `RetryPolicy` (3 intentos, backoff 0,5 s ×2 hasta 8 s) con
`retry_on=errors.is_transient`, **solo** en los nodos con efectos externos: `scope`,
`persona`, `fix`, `publish` y `notify`.

- `merge` y `policy` no la llevan: son deterministas y un fallo ahí es un bug.
- `hitl` no la lleva: reintentar repetiría el `interrupt`.
- `persona` degrada en el último intento: devuelve 0 hallazgos y la review sigue.

**Clasificación por tipo.** `TransientError` / `PermanentError` en
[`errors.py`](../../src/agent_review/errors.py).

| Se reintenta | No se reintenta |
|---|---|
| `TransientError` (`TwgError`, `LlmTransientError`, `GithubUnavailable`) | `PermanentError` (`LlmError`, `RunnerError`, …) |
| `httpx.RequestError`; `HTTPStatusError` 5xx o 429 | Otros 4xx |
| `subprocess.TimeoutExpired`, `TimeoutError`, `ConnectionError` | Todo lo demás |

**Capa de job.** El worker reencola con `attempts+1`. Al agotar `MAX_REVIEW_ATTEMPTS`
(3), el job va a la DLQ (`reviews:jobs:dead`). El manejo del error nunca tumba el
worker.

**El checkpoint es la unidad de reintento.** Según el estado del hilo:

| Estado del hilo | Acción |
|---|---|
| Sin estado | `invoke(input)` |
| A medias (`next` sin `interrupts`) | `invoke(None)`: continúa desde el nodo que falló |
| Pausado | Con decisión, `Command(resume=…)`; sin decisión, no hace nada |
| Completado | No hace nada |

**Techo de tiempo en el adaptador.** `twg` corre con `timeout=TWG_TIMEOUT_S`, y un
cuelgue se convierte en `TwgError` transitorio.

## Consecuencias

- Los nodos con efectos tienen que ser idempotentes. `publish` lo es por las marcas.
  LangGraph descarta las escrituras del intento fallido.
- Un adaptador nuevo solo necesita lanzar subclases de `TransientError` o
  `PermanentError`.
- Las reviews **incompletas no se reencolan**: la ingesta ya esperó y repetir solo
  duplicaría.
- La reanudación `expired` se encola como mucho 3 veces por pausa, para que una que
  falla siempre no esquive la DLQ.

## Alternativas descartadas

- **`default_retry_on`.** Ver el contexto.
- **`add_node(timeout=…)`.** Solo funciona con nodos async.
- **`error_handler` por nodo.** No hay ningún caso real de compensación.
- **Reintentar a mano en cada adaptador.** Reparte la política en N sitios.
