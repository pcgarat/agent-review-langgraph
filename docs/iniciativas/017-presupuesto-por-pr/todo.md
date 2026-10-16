Última modificación: 2026-10-16 | Creado: 2026-10-16

# Todo: Presupuesto de tokens por review

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-16. Cierra [D17](../../arquitectura.md)
§7 y [D21](../../arquitectura.md) §7.

## Bloque 1 — El puerto y su semántica (cierra D21)

- [x] **P1** Semántica fiel de `remaining` en el adaptador en memoria
  - `src/agent_review/ports/budget.py` documenta `remaining` como
    `total − confirmado − reservado sin confirmar` (B9). El doble de tests (hoy
    `MemoryBudget` en `tests/test_tool_loop.py`) pasa a
    `src/agent_review/adapters/budget_memory.py` (`InMemoryBudget`), con esa
    definición, para que el bucle lo use también fuera de los tests.
  - Aceptación: dos reservas seguidas sin `commit` reducen `remaining`; un `commit`
    con `actual` menor que lo reservado devuelve la diferencia; un `commit` con
    `actual` **mayor** que lo reservado cobra el real (el exceso descuenta, no se
    recorta; `remaining` puede quedar negativo).
  - Verifica: `tests/test_budget.py`.
  - Ficheros: `src/agent_review/ports/budget.py`,
    `src/agent_review/adapters/budget_memory.py`.

- [x] **P2** `reserve(...) is False` corta la llamada (B7, cierra D21)
  - `src/agent_review/adapters/tool_loop.py`: `_reserve` devuelve el `bool` y el
    bucle lo trata como presupuesto agotado (un `False` fuerza el turno final igual
    que un `remaining` corto). `_budget_exhausted` se mantiene como cota previa.
  - Si `reserve` devuelve `False`, **no se llama a `commit`** (B12): no hubo reserva
    que ajustar. El `commit` del bucle queda condicionado a `reserve is True`.
  - Aceptación: un doble de presupuesto que devuelva `False` con `remaining` alto hace
    que el bucle no gaste un turno con herramientas y que no registre ningún `commit`.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `src/agent_review/adapters/tool_loop.py`.

- [x] **P3** Devolver la reserva si la llamada falla (B8)
  - `src/agent_review/adapters/tool_loop.py`: si `llm.complete` lanza, se llama a
    `commit(review_key, reserved, 0)` antes de relanzar, para que el reintento del
    nodo ([ADR-0007](../../adr/0007-reintento-en-dos-capas.md)) no reserve dos veces.
  - Aceptación: con un modelo que falla una vez y un presupuesto que registra los
    `commit`, la reserva del intento fallido queda a cero.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `src/agent_review/adapters/tool_loop.py`.

### Checkpoint 1 (tras P1–P3)

- [x] `make ci` en verde.
- [x] D21 cerrada en el código: `reserve(False)` corta y el error devuelve la reserva.

## Bloque 2 — Adaptador Redis y consumidores

- [x] **P4** `RedisBudget`: contador atómico con TTL
  - `src/agent_review/adapters/budget_redis.py`: `budget:{review_key}` con
    `INCRBY`/`DECRBY` y TTL; la reserva es una operación que respeta el tope (atómica
    respecto a las ramas en paralelo). `remaining` con la misma definición que P1. Si
    Redis no responde, degrada como el resto de adaptadores (sin Redis no hay review
    en `production`, [015](../015-puesta-en-marcha-real/spec.md) X4).
  - Aceptación: dos reservas concurrentes que juntas superan el tope: solo una pasa;
    el TTL se aplica; `remaining` coincide con el adaptador en memoria.
  - Verifica: `tests/test_budget.py` (fakeredis o doble de cliente).
  - Ficheros: `src/agent_review/adapters/budget_redis.py`.

- [x] **P5** `LlmReviewer` y `LlmFixer` contra el puerto
  - `src/agent_review/adapters/llm_reviewer.py` y `llm_fixer.py`: reciben
    `budget: BudgetPort | None` y `budget_limit` (tope blando por persona) en vez del
    entero; reservan antes de llamar, confirman con el `usage` y devuelven la reserva
    en el error. `worker/main.py` construye el puerto desde `Settings` y lo pasa.
  - Aceptación: la persona reserva/confirma contra el puerto; al agotarse, no llama al
    modelo (test con doble de puerto); el fixer usa la clave `budget:fix:{review_key}`.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_llm_fixer.py`,
    `tests/test_worker.py`.
  - Ficheros: `src/agent_review/adapters/llm_reviewer.py`,
    `src/agent_review/adapters/llm_fixer.py`, `src/agent_review/worker/main.py`.

- [x] **P6** Reparto por persona (`2 × total / N`) en `build_reviewers`
  - `src/agent_review/worker/main.py`: `build_reviewers` calcula el tope blando por
    persona (`2 * total // N`, `N` = personas LLM) y lo pasa a cada `LlmReviewer` (B10).
  - Aceptación: con 3 personas y total 300 000, cada una recibe 200 000; con una sola
    persona, el total entero.
  - Verifica: `tests/test_worker.py`.
  - Ficheros: `src/agent_review/worker/main.py`.

### Checkpoint 2 (tras P4–P6)

- [x] `make ci` en verde.
- [x] Los adaptadores Redis y memoria pasan el mismo test de contrato.
- [x] Ningún consumidor compara su propia llamada contra `budget_tokens_per_pr`.

## Bloque 3 — El hecho del corte y cierre

- [x] **P7** El hecho del corte deja rastro
  - `src/agent_review/domain/models.py`: `ReviewerResult` gana
    `budget_exhausted: bool` y `tokens_used: int`; `ReviewOutcome` gana
    `tokens_spent` y `tokens_remaining`, derivados del estado. `LlmReviewer` los
    rellena cuando corta por presupuesto.
  - Aceptación: una review con presupuesto agotado expone `budget_exhausted` en el
    resultado de la persona y `tokens_spent` en el `ReviewOutcome` (test).
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_worker.py`.
  - Ficheros: `src/agent_review/domain/models.py`,
    `src/agent_review/adapters/llm_reviewer.py`,
    `src/agent_review/graph/build.py`.

- [x] **P8** Documentación viva y cierre
  - `arquitectura.md` §5 (claves `budget:*` y TTL), §7 (**quita D17 y D21**);
    `operacion.md` §2 (semántica nueva de `LLM_BUDGET_TOKENS_PER_PR`: por review, no
    por llamada); `desarrollo.md` §3 (el puerto y sus adaptadores); `CONTEXT.md`
    («presupuesto por review», «reserva»). `docs/iniciativas/README.md`: la fila de 017
    pasa a `implementada`.
  - Aceptación: `make docs-check` en verde; cada documento describe lo entregado; D17
    y D21 ya no figuran en `arquitectura.md` §7.
  - Verifica: `make ci`.
  - Ficheros: `docs/arquitectura.md`, `docs/operacion.md`, `docs/desarrollo.md`,
    `CONTEXT.md`, `docs/iniciativas/README.md`.
