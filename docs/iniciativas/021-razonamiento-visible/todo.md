Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Razonamiento del revisor visible en el panel

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — Modelo y adaptador (checkpoint C1)

- [ ] **RZ1** Parámetros por modelo y alias (R10)
  - `src/agent_review/models_config.py`: `ModelSpec.api_model` (por defecto, la clave)
    y `ModelSpec.params` (encima de los `defaults` del proveedor).
    `models_registry.py` construye el puerto con ambos; `probe` usa `api_model`.
  - `config/models.yaml`: entrada `deepseek-v4-flash-revisor` (`reasoning_effort:
    high`, `reasoning_min_max_tokens: 65536`) enlazada a `correctness`; el fixer sigue
    con `deepseek-v4-flash` sin razonamiento.
  - Aceptación: el revisor manda `model: deepseek-v4-flash` con `reasoning_effort:
    high`; el fixer, con `none`; un `params` con clave desconocida no rompe.
  - Verifica: `tests/test_models_config.py`, `tests/test_models_registry.py`.

- [ ] **RZ2** *Streaming* siempre en `OpenAiCompatibleLlm` (R1, R8)
  - `src/agent_review/adapters/openai_llm.py`: `stream: true` con
    `stream_options.include_usage`; acumula `content`, `reasoning_content`, `usage` y
    `finish_reason`. `LlmReply.reasoning` (texto completo).
  - Errores: 4xx permanente, 5xx/429/408 transitorio (como hoy); corte a mitad del
    *stream* transitorio; `length` sin contenido, `LlmError`.
  - Aceptación: con SSE grabados (incluidos *chunks* partidos, `[DONE]` y el `usage`
    en el último) el resultado coincide con el de hoy sin *streaming*.
  - Verifica: `tests/test_openai_llm.py`.

- [ ] **RZ3** `on_reasoning` en el puerto (R2)
  - `src/agent_review/ports/llm.py`: `complete(..., on_reasoning=None)`; el adaptador
    lo llama con cada trozo; una excepción del *callback* se registra y se ignora.
  - `tests/eval/harness.py`: `ReplayLlm` y `RecordingLlm` lo aceptan.
  - Verifica: `tests/test_openai_llm.py`.

## Bloque 2 — El revisor (checkpoint C2)

- [ ] **RZ4** Lotes de 30 000 caracteres por defecto (R12)
  - `settings.py` (`llm_batch_max_chars`), `.env.example`, `docs/operacion.md` §2.

- [ ] **RZ5** Diff numerado para el modelo (R13)
  - `src/agent_review/adapters/llm_reviewer.py`: cada línea añadida o de contexto del
    lote lleva su número del lado nuevo; el *prompt* lo explica. El anclaje sigue
    sobre el diff original.
  - Aceptación: el *prompt* trae los números; un hallazgo con la `line` numerada se
    ancla igual que hoy.
  - Verifica: `tests/test_llm_reviewer.py`.

- [ ] **RZ6** El revisor emite su razonamiento con contexto (R3)
  - `LlmReviewer` recibe un `ReasoningSink` opcional y, por lote, emite `start`
    (persona, lote *i/N*, ficheros), los trozos, y `end` (`usage`, `finish_reason`) o
    `error` (motivo).
  - Aceptación: con un LLM falso que emite trozos, el sink recibe la secuencia
    completa por lote; sin sink, nada cambia.
  - Verifica: `tests/test_llm_reviewer.py`.

## Bloque 3 — Sink y panel (checkpoint C3)

- [ ] **RZ7** `ReasoningSink` con adaptador Redis (R4)
  - `src/agent_review/ports/reasoning.py` y `adapters/reasoning_redis.py`: `XADD` a
    `reviews:reasoning:<review_key>` agrupando trozos (~250 ms o ~4 KB), `MAXLEN ~`
    por `REASONING_MAX_BYTES` y `EXPIRE` por `REASONING_TTL_S`. Doble en memoria.
  - Configuración: `REASONING_STREAM_ENABLED` (`false`), `REASONING_TTL_S`
    (864000), `REASONING_MAX_BYTES` (10485760) en `settings.py`, `.env.example` y
    `operacion.md` §2. Cableado en el worker.
  - Aceptación: un fallo de Redis no cambia la review; el *stream* respeta el TTL.
  - Verifica: `tests/test_reasoning.py` (sobre `fakeredis`).

- [ ] **RZ8** SSE en el panel (R5)
  - `src/agent_review/web/main.py`: `GET /api/reviews/{key}/reasoning` (sesión
    `lector`): `XRANGE` + `XREAD BLOCK` hasta el `end` de todas las llamadas abiertas o
    un tope de inactividad; `404` si no hay *stream*.
  - Verifica: `tests/test_web.py`.

- [ ] **RZ9** Pestaña «Razonamiento» en el detalle (§5.3)
  - `src/agent_review/web/ui.py`: secciones por persona y lote, en vivo con
    `EventSource`, plegables, el lote en curso marcado; texto escapado.
  - Verifica: `tests/test_web_ui.py`.

## Bloque 4 — Histórico (checkpoint C4)

- [ ] **RZ10** `reviews:history` (R9)
  - El worker escribe el resumen al cerrar cada review (`_log_outcome`): `review_key`,
    repo, PR, `head_sha`, completa/parcial/incompleta, hallazgos, tokens y si hay
    razonamiento. Recorte por fecha en cada escritura y en el barrido de
    `prune_completed_threads`.
  - Verifica: `tests/test_worker.py`.

- [ ] **RZ11** Página «Histórico» del panel (§5.5)
  - `GET /api/history` y la vista: más reciente primero, filtro por repo y PR, enlace
    al detalle de cada review.
  - Verifica: `tests/test_web.py`, `tests/test_web_ui.py`.

## Bloque 5 — Medida y cierre (checkpoint C5)

- [ ] **RZ12** *(Red)* Review real del PR #3 con razonamiento y *gold set*
  - Corrida real: sin 408 ni lotes vacíos por `length` (criterio 4). Anotar coste,
    latencia y tokens de razonamiento por lote.
  - `make eval-record`; etiquetar los hallazgos nuevos contra `008da57` (revisión
    humana de las etiquetas) y congelar el baseline (criterio 6).

- [ ] **RZ13** ADR y documentos vivos
  - ADR nuevo: razonamiento por *streaming* con un único punto de captura y un
    *stream* de Redis por review (alternativas: guardarlo en el estado del grafo, en
    Postgres o en Langfuse).
  - `docs/arquitectura.md` §5 (claves `reviews:reasoning:*`, `reviews:history`) y §3
    (lo que emite la persona); `operacion.md` §2 y §7; `desarrollo.md` §2 y §3 (una
    fuente de razonamiento nueva); `CONTEXT.md` («razonamiento», «histórico»).

- [ ] **RZ14** Cierre
  - Spec en `implementada`; índice; `make ci` y `make docs-check` en verde.
