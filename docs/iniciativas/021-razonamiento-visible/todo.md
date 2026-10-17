Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Razonamiento del revisor visible en el panel

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — Modelo y adaptador (checkpoint C1)

- [x] **RZ1** Parámetros por modelo y alias (R10)
  - `src/agent_review/models_config.py`: `ModelSpec.api_model` (por defecto, la clave)
    y `ModelSpec.params` (encima de los `defaults` del proveedor).
    `models_registry.py` construye el puerto con ambos; `probe` usa `api_model`.
  - `config/models.yaml`: entrada `deepseek-v4-flash-revisor` (`reasoning_effort:
    high`, `reasoning_min_max_tokens: 65536`) enlazada a `correctness`; el fixer sigue
    con `deepseek-v4-flash` sin razonamiento.
  - Aceptación: el revisor manda `model: deepseek-v4-flash` con `reasoning_effort:
    high`; el fixer, con `none`; un `params` con clave desconocida no rompe.
  - Verifica: `tests/test_models_config.py`, `tests/test_models_registry.py`.

- [x] **RZ2** *Streaming* siempre en `OpenAiCompatibleLlm` (R1, R8)
  - `src/agent_review/adapters/openai_llm.py`: `stream: true` con
    `stream_options.include_usage`; acumula `content`, `reasoning_content`, `usage` y
    `finish_reason`. `LlmReply.reasoning` (texto completo).
  - Errores: 4xx permanente, 5xx/429/408 transitorio (como hoy); corte a mitad del
    *stream* transitorio; `length` sin contenido, `LlmError`.
  - Aceptación: con SSE grabados (incluidos *chunks* partidos, `[DONE]` y el `usage`
    en el último) el resultado coincide con el de hoy sin *streaming*.
  - *Hecho 2026-10-17:* si el proveedor ignora `stream` y responde JSON, se lee igual
    (los dobles de test existentes siguen valiendo). Comprobado contra
    `nan.builders`: 65 trozos de razonamiento y `usage` correcto.
  - Verifica: `tests/test_openai_llm.py`.

- [x] **RZ3** `on_reasoning` en el puerto (R2)
  - `src/agent_review/ports/llm.py`: `complete(..., on_reasoning=None)`; el adaptador
    lo llama con cada trozo; una excepción del *callback* se registra y se ignora.
  - `tests/eval/harness.py`: `ReplayLlm` y `RecordingLlm` lo aceptan.
  - Verifica: `tests/test_openai_llm.py`.

## Bloque 2 — El revisor (checkpoint C2)

- [x] **RZ4** Lotes de 30 000 caracteres por defecto (R12)
  - `settings.py` (`llm_batch_max_chars`), `.env.example`, `docs/operacion.md` §2.

- [x] **RZ5** Diff numerado para el modelo (R13)
  - `src/agent_review/adapters/llm_reviewer.py`: cada línea añadida o de contexto del
    lote lleva su número del lado nuevo; el *prompt* lo explica. El anclaje sigue
    sobre el diff original.
  - Aceptación: el *prompt* trae los números; un hallazgo con la `line` numerada se
    ancla igual que hoy.
  - Verifica: `tests/test_llm_reviewer.py`.

- [x] **RZ6** El revisor emite su razonamiento con contexto (R3)
  - `LlmReviewer` recibe un `ReasoningSink` opcional y, por lote, emite `start`
    (persona, lote *i/N*, ficheros), los trozos, y `end` (`usage`, `finish_reason`) o
    `error` (motivo).
  - Aceptación: con un LLM falso que emite trozos, el sink recibe la secuencia
    completa por lote; sin sink, nada cambia.
  - Verifica: `tests/test_llm_reviewer.py`.

*Notas de implementación 2026-10-17 (bloque 2):*

- El puerto `ReasoningSink` (`ports/reasoning.py`, con `InMemoryReasoningSink`) nace
  en RZ6, porque el revisor ya emite; RZ7 añade el adaptador Redis y el cableado.
- Eventos por lote: `start` (ficheros), los trozos `reasoning`, y `end` (`usage`,
  `finish_reason`, nº de hallazgos o «salida ilegible») o `error` (sin presupuesto,
  fallo del modelo o fallo transitorio, que además sube para el reintento).
- Los 3 tests del *gold set* que dependen del *prompt* quedan en `xfail(strict=True)`
  hasta RZ12: al regrabar, pasarán y el CI obligará a quitar la marca.

## Bloque 3 — Sink y panel (checkpoint C3)

- [x] **RZ7** `ReasoningSink` con adaptador Redis (R4)
  - `src/agent_review/ports/reasoning.py` y `adapters/reasoning_redis.py`: `XADD` a
    `reviews:reasoning:<review_key>` agrupando trozos (~250 ms o ~4 KB), `MAXLEN ~`
    por `REASONING_MAX_BYTES` y `EXPIRE` por `REASONING_TTL_S`. Doble en memoria.
  - Configuración: `REASONING_STREAM_ENABLED` (`false`), `REASONING_TTL_S`
    (864000), `REASONING_MAX_BYTES` (10485760) en `settings.py`, `.env.example` y
    `operacion.md` §2. Cableado en el worker.
  - Aceptación: un fallo de Redis no cambia la review; el *stream* respeta el TTL.
  - Verifica: `tests/test_reasoning.py` (sobre `fakeredis`).

- [x] **RZ8** SSE en el panel (R5)
  - `src/agent_review/web/main.py`: `GET /api/reviews/{key}/reasoning` (sesión
    `lector`): `XRANGE` + `XREAD BLOCK` hasta el `end` de todas las llamadas abiertas o
    un tope de inactividad; `404` si no hay *stream*.
  - Verifica: `tests/test_web.py`.

- [x] **RZ9** Pestaña «Razonamiento» en el detalle (§5.3)
  - `src/agent_review/web/ui.py`: secciones por persona y lote, en vivo con
    `EventSource`, plegables, el lote en curso marcado; texto escapado.
  - Verifica: `tests/test_web_ui.py`.

*Notas de implementación 2026-10-17 (bloque 3):*

- El lector (`RedisReasoningReader`) vive junto al sink para compartir el formato; el
  panel lo usa tras el protocolo `ReasoningReader`, y los tests con el doble en
  memoria.
- En el SSE, el fallo de un lote viaja como evento `fallo`: `error` es el evento
  nativo de `EventSource` para los fallos de conexión.
- El SSE se cierra cuando todas las personas cierran todos sus lotes (o se quedan sin
  presupuesto, que corta los siguientes sin emitir) o tras 5 minutos sin novedades.
- Sin probar aún en un navegador real: se verá en la corrida de RZ12.

## Bloque 4 — Histórico (checkpoint C4)

- [x] **RZ10** `reviews:history` (R9)
  - El worker escribe el resumen al cerrar cada review (`_log_outcome`): `review_key`,
    repo, PR, `head_sha`, completa/parcial/incompleta, hallazgos, tokens y si hay
    razonamiento. Recorte por fecha en cada escritura y en el barrido de
    `prune_completed_threads`.
  - Verifica: `tests/test_worker.py`.

- [x] **RZ11** Página «Histórico» del panel (§5.5)
  - `GET /api/history` y la vista: más reciente primero, filtro por repo y PR, enlace
    al detalle de cada review.
  - Verifica: `tests/test_web.py`, `tests/test_web_ui.py`.

*Notas de implementación 2026-10-17 (bloque 4):*

- El resumen (`domain/history.py`) sale del `ReviewOutcome`; el almacén
  (`history_store.py`) usa `review_key` como miembro, así que una reentrega actualiza
  la entrada en lugar de duplicarla. Lo escribe `_consume` cuando la review no queda
  pausada (incluidas las que terminan al reanudar).
- «Razonamiento: sí» significa que el sink estaba activo y la review gastó tokens; el
  detalle dice si el *stream* sigue vivo (10 días).

## Bloque 4b — Robustez de los lotes (enmienda 2026-10-17, §3.4b)

- [x] **RZ15** Reintento por lote en el `LlmReviewer` (R14)
  - Un fallo transitorio de una llamada se reintenta en el mismo lote hasta
    `LLM_BATCH_RETRIES` (2) con espera creciente; agotado, el lote sale `failed` y se
    sigue. El panel ve cada intento (`error` con «reintento i/N»).
  - `settings.py`, `.env.example`, `operacion.md` §2 (`LLM_BATCH_RETRIES`).
  - Aceptación: un corte en el lote 4 no repite los lotes 1–3; tres cortes seguidos
    dejan ese lote `failed` y el resto revisado.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_retry.py`.

- [x] **RZ16** Lotes de 20 000 caracteres por defecto (R15)
  - `settings.py`, `.env.example`, `llm_reviewer.DEFAULT_BATCH_MAX_CHARS`,
    `operacion.md` §2.

- [x] **RZ17** Trocear por hunks los ficheros que no caben (R16)
  - `_batches` parte un bloque mayor que el lote en trozos de hunks completos, cada
    uno con las cabeceras del fichero; un hunk que solo no cabe sigue `too_large`.
  - Cobertura: revisado si lo fueron todos sus trozos; si no, el motivo del primero
    que faltó.
  - Aceptación: un fichero de 3 lotes de tamaño se revisa en 3 trozos y sale
    revisado; el anclaje de un hallazgo cae en su trozo.
  - Verifica: `tests/test_llm_reviewer.py`.

*Notas de implementación 2026-10-17 (bloque 4b):*

- El reintento del lote vuelve a emitir `start` (el panel reinicia el texto del lote)
  y su aviso lleva `meta.retry`, para que el SSE no dé el lote por cerrado.
- Un fallo transitorio agotado ya no sube al nodo `persona`: el lote sale `failed`.
  El reintento del nodo queda para lo que falle fuera de las llamadas al modelo.
- En los tests, un *fixture* global pone a 0 la espera entre reintentos.
- La corrida real previa (RZ12, 25,6 min, 284 066 tokens) dio el primer TP (A) y
  candidatos a integracion-1; `worker/main.py` salió `too_large` con lotes de 30k, de
  ahí RZ17.

## Bloque 5 — Medida y cierre (checkpoint C5)

- [x] **RZ12** *(Red)* Review real del PR #3 con razonamiento y *gold set*
  - Corrida real: sin 408 ni lotes vacíos por `length` (criterio 4). Anotar coste,
    latencia y tokens de razonamiento por lote.
  - `make eval-record`; etiquetar los hallazgos nuevos contra `008da57` (revisión
    humana de las etiquetas) y congelar el baseline (criterio 6).
  - Quitar el `xfail(strict=True)` de los 3 tests de `tests/eval/test_eval_pr_3.py`.
  - *Resultado 2026-10-17.* Dos corridas reales del PR #3 en el stack (Redis,
    Postgres y panel; VCS falso, sin publicar ni pasar por la cola):
    - **Primera** (lotes de 30k): un corte del *stream* a los ~3 min hizo que la
      `RetryPolicy` repitiera la persona entera (25,6 min, 284 066 tokens) y
      `worker/main.py` salió `too_large`. De ahí el bloque 4b (RZ15–RZ17).
    - **Segunda** (lotes de 20k, reintento por lote, troceo por hunks): 19 lotes sin
      cortes, review completa (51/51), 21 min, 336 246 tokens (189 031 de
      razonamiento, máx. 20 832 por lote). La pestaña del panel y el histórico,
      probados en un navegador.
    - **Gold set** (esta grabación): cobertura 1.0, **precisión 0.375 y recall 0.375**
      (3 TP: A en la dirección correcta, integracion-1 y no-detectado-1; 5 FP, P10–P14;
      2 aceptables, Q3–Q4). Sin razonamiento eran 0 TP con la misma cobertura. Se
      amplió el `match` de A, integracion-1 y no-detectado-1 (nota fechada en
      `labels.yaml`).

- [ ] **RZ13** ADR y documentos vivos
  - ADR nuevo: razonamiento por *streaming* con un único punto de captura y un
    *stream* de Redis por review (alternativas: guardarlo en el estado del grafo, en
    Postgres o en Langfuse).
  - `docs/arquitectura.md` §5 (claves `reviews:reasoning:*`, `reviews:history`) y §3
    (lo que emite la persona); `operacion.md` §2 y §7; `desarrollo.md` §2 y §3 (una
    fuente de razonamiento nueva); `CONTEXT.md` («razonamiento», «histórico»).

- [ ] **RZ14** Cierre
  - Spec en `implementada`; índice; `make ci` y `make docs-check` en verde.
