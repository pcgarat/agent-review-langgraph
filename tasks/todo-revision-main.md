Última modificación: 2026-10-11 | Creado: 2026-10-11

# Todo: fallos de la revisión de `main`

Fallos B1–B10 de `docs/revision-main-20261011.md` §1. Cada arreglo lleva su test
de regresión. Orden: el de la prioridad recomendada del documento.

- [x] **B1** (Alta) DLQ de un job con decisión tumba el worker
  - Arreglo: `model_dump(mode="json")` en `_dead_letter_payload`; `_consume` no deja
    escapar una excepción del propio reencolado/DLQ.
  - Ficheros: `src/agent_review/queue.py`, `src/agent_review/worker/main.py`.
- [x] **B2** (Alta) Un reintento re-ejecuta el grafo desde `START`
  - Arreglo: hilo a medias (`next` no vacío, sin `interrupts`) →
    `graph.invoke(None, config)`, que continúa desde el último checkpoint.
  - Ficheros: `src/agent_review/worker/main.py`.
- [x] **B9** (Baja, va con B2) Un fallo de Teams cuenta como fallo de la review
  - Arreglo: la notificación pasa a un nodo `notify` propio tras `publish`. Un fallo
    de Teams ya no repite `publish` y, con B2, el reintento continúa en `notify`.
  - Ficheros: `src/agent_review/graph/build.py`.
- [x] **B3** (Alta) Pausa vencida cuyo resume falla esquiva la DLQ para siempre
  - Arreglo: el barrido marca la entrada del inbox (`expired_enqueued_at`) y no la
    vuelve a encolar; la rama «ya completada» también limpia el inbox.
  - Ficheros: `src/agent_review/domain/paused.py`, `src/agent_review/paused_store.py`,
    `src/agent_review/worker/main.py`.
- [x] **B7** (Media) La idempotencia se reclama antes de hacer el trabajo
  - Arreglo: `IdempotencyStore.release`; la API libera la clave si falla el paso
    que protege.
  - Ficheros: `src/agent_review/idempotency.py`, `src/agent_review/api/main.py`.
- [x] **B4** (Media) Un artefacto tardío en la misma ejecución nunca publica la completa
  - Decisión: se mantiene lo que promete el README (la completa se publica). La
    pasada degradada usa su propio hilo (`thread_id` con sufijo `:partial`).
  - Ficheros: `src/agent_review/domain/models.py`, `src/agent_review/worker/main.py`.
- [x] **B5** (Media) El merge por `(file, title)` se come hallazgos
  - Arreglo: la línea entra en la clave de merge; el token inline usa
    `_finding_id` (el mismo que la comprobación de `publish`).
  - Nota: para no romper la fusión entre personas (estática + LLM con anclas a 3
    líneas, `test_e2e_estatica_y_llm_conviven_y_el_merge_deduplica`), el estado
    guarda la procedencia (`finding_sources`) y el merge fusiona anclas a ≤5
    líneas solo si vienen de personas distintas. `Finding` no cambia (contrato).
  - Ficheros: `src/agent_review/graph/build.py`.
- [x] **B6** (Media) El webhook acepta cualquier `repo`
  - Arreglo: la API rechaza (400) un `repo` distinto de `{workspace}/{repo}`
    configurado. Sin `BITBUCKET_WORKSPACE`/`BITBUCKET_REPO` no se filtra.
  - Ficheros: `src/agent_review/api/main.py`.
- [x] **B8** (Media) `twg` se lanza sin `timeout`
  - Arreglo: `timeout` en `_run` (ya estaba a medias en el árbol) y en `check_twg`;
    `TimeoutExpired` → `TwgError`.
  - Ficheros: `src/agent_review/adapters/twg_vcs.py`, `src/agent_review/worker/main.py`
    (`build_vcs`).
  - Pendiente (no es el fallo en sí): que el heartbeat refleje el progreso del
    bucle. Con el timeout, un `twg` colgado ya no bloquea indefinidamente.
- [x] **B10** (Baja) El aviso de pausa nunca lleva enlace al PR
  - Arreglo: el worker pasa el `VcsPort` a `process_request`/`_on_paused`.
    Implementado por una sesión en paralelo (`build_vcs`); aquí solo el test.
  - Ficheros: `src/agent_review/worker/main.py`.

## Cierre

- [x] `make ci` en verde (lint, formato, mypy strict, 253 tests).
- [x] README alineado con B4.

Regresiones en `tests/test_revision_main.py` (B1–B5, B8–B10) y `tests/test_api.py`
(B6, B7). Todas fallan contra el código anterior al arreglo.

## Riesgos residuales

- **B4:** si la pasada degradada pausa y después la completa también pausa, la
  completa sobrescribe la entrada del inbox (misma `review_key`). El hilo
  `:partial` queda pausado sin entrada; no publica nada, pero no se poda nunca.
- **B4 × `reconcile_paused`:** busca en el inbox por `thread_id`, que en una
  pasada degradada lleva `:partial`; debería buscar por `review_key`. Hoy solo
  cuesta un `get_state` de más por barrido, sin efecto visible.
