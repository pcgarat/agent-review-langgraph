Última modificación: 2026-10-11

# Plan de implementación — Fase 1 (report-only)

> **Estado: entregado (2026-10-11).** Las Tasks 1–10 están cerradas, `make ci` en
> verde (`ruff` lint + formato, `mypy` strict, 182 tests). El código de las 10 tareas
> vive en git; este documento ya no repite su histórico. Lo que queda abierto son
> **verificaciones humanas o externas**, no código (ver "Pendiente").

El diseño de arquitectura está en `docs/diseno-agente-code-review-20261010.md`. El
registro de la auditoría que cerró los huecos de robustez, en
`docs/pendiente-fase1-20261011.md`.

---

## Overview

Cerrar el camino de valor del MVP: un evento de CI dispara una review real (diff vía
`twg`), al menos una persona produce hallazgos validados contra el contrato, la
política decide, y el resultado se publica como **comentarios inline en el diff**
(+ resumen general en el PR + Teams). Cero escritura de código en el repo del PR.
Sin HITL, sin sandbox, sin autofix.

---

## Decisiones de arquitectura

- **Report-only de verdad.** `max_autonomy: report` en `config/autonomy.yaml`. Las
  reglas que darían `escalate`/`fix_*` se degradan a `report` conservando la razón
  original. Un despliegue puede bajar el techo (`MAX_AUTONOMY_OVERRIDE`), nunca subirlo.
- **Persona estática antes que LLM.** Primero parsear la salida de PHPStan sobre el
  diff; valida el pipeline y el contrato sin coste de tokens. La persona LLM llega
  (opt-in) una vez el publish E2E está verde.
- **Cola mínima Redis.** La API hace `claim` + `RPUSH`; el worker hace `BLPOP` y
  ejecuta el grafo. Con reintentos y DLQ. Sin orquestador extra.
- **Contrato versionado en el repo.** El esquema de `ce-code-review` se copia a
  `contracts/` y se valida contra él; no se invoca su runtime.
- **Fixtures primero, PR real después.** Tests con diff golden de `emotivoo-v2`; el
  smoke contra Bitbucket es el Nivel 2, manual.
- **Inline primero, resumen después.** Cada hallazgo intenta anclarse a una línea
  añadida del diff; si falla, va al resumen (nunca se pierde). Tope
  `max_inline_comments` (20), prioridad P0→P3.
- **Ingesta diferida como puente.** El disparo es "el CI publicó el artefacto", no el
  PR. El webhook de PR se retiró. Es andamiaje desechable:
  `docs/ingesta-diferida-ci-20261011.md`.
- **Fuera de alcance Fase 1:** `interrupt`, Bot Teams, Jira, sandbox, fix, mutation
  testing. El checkpointer durable, aunque útil para el HITL de Fase 2, ya está
  montado porque resolvía el dedupe de re-push.

---

## Tareas (todas cerradas)

Cada tarea vive en git; aquí solo el mapa y su verificación.

| # | Tarea | Verificación |
|---|---|---|
| 1 | Clamp report-only en política | `tests/test_policy.py` |
| 2 | Contrato `Finding` versionado en `contracts/` | `tests/test_domain.py`, `tests/test_contracts.py` |
| 3 | Cola Redis entre API y worker | `tests/test_api.py`, `tests/test_queue.py` |
| 4 | Worker ejecuta el grafo | `tests/test_worker.py` |
| 5 | `TwgVcsAdapter` + filtros de scope + anclas inline | `tests/test_twg_vcs.py`, `tests/test_scope.py` (contrato verificado con `twg` 1.3.5 real, PR 417) |
| 6 | Persona `tooling` (`standards`) | `tests/test_tooling_reviewer.py`, `tests/test_tooling_source.py` |
| 7 | Publish: inline + resumen + Teams | `tests/test_graph.py`, `tests/test_teams.py` |
| 8 | Persona LLM `correctness` (opt-in) | `tests/test_llm_reviewer.py`, `tests/test_openai_llm.py` |
| 9 | Smoke E2E documentado | `docs/smoke-fase1-20261011.md` |
| 10 | Ingesta diferida + retirada del webhook de PR | `tests/test_pending.py`, `tests/test_api.py` |

Decisiones de implementación no obvias, documentadas aparte:

- **Task 6b** — la salida de PHPStan llega como **artefacto** leído por
  `ToolingOutputSource`; el agente no ejecuta la herramienta en Fase 1 (eso es el
  sandbox de Fase 3, otro adaptador del mismo puerto). Ampliado en el README.
- **Task 7** — el formato inline y el orden de publicación se fijan en
  `docs/decisiones-publish-inline-20261011.md`.
- **Task 10** — la ingesta diferida es un **puente desechable**; ver
  `docs/ingesta-diferida-ci-20261011.md`.

---

## Pendiente (no es código)

| # | Pendiente | Tipo |
|---|---|---|
| P1 | El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado | Externo, **opcional** (`docs/ingesta-diferida-ci-20261011.md` §3–4) |
| P2 | Nivel 2 del smoke: un PR real, verificar inline + resumen + Teams | ✅ Hecho con el PR 430 (`docs/smoke-fase1-20261011.md` §5); Teams queda en no-op sin webhook real |
| P3 | Revisión humana del formato inline + resumen | Humano (`docs/decisiones-publish-inline-20261011.md`) |
| P4 | Decisión go/no-go para Fase 2 (HITL Opción B) | Humano |
| P5 | Medir latencia real de una review (cola secuencial) | Medición orientativa hecha (~15 s, 4 hallazgos); falta carga real |
| P6 | Revisar la heurística de tema protegido con seguridad | Humano (Fase 3) |

---

## Riesgos (estado)

| Riesgo | Mitigación | Estado |
|---|---|---|
| Contrato `twg` distinto al adaptador | Goldens de stdout reales; `twg` 1.3.5 verificado | ✅ Resuelto |
| Persona LLM ruidosa / cara | Tooling primero; validación de contrato; corte de presupuesto | ✅ Controlado |
| Falla transitoria pierde la review | Reintento con contador + DLQ | ✅ Resuelto |
| Fallo transitorio dentro de un nodo (twg/LLM/Teams) rehace el grafo entero | `RetryPolicy` por nodo + `is_transient` (`docs/reintento-de-nodo-20261011.md`) | ✅ Resuelto |
| `twg` colgado bloquea el nodo (y el worker secuencial) | `TWG_TIMEOUT_S` en el `subprocess` → `TwgError` transitorio | ✅ Resuelto |
| Worker no sobrevive a una cola vacia | Redis sin deadline de socket + `dequeue` tolerante | ✅ Resuelto (smoke 2026-10-11) |
| Contrato fuera de la imagen | Ruta con candidatos + `contracts/` copiado al Dockerfile | ✅ Resuelto (smoke 2026-10-11) |
| Re-push duplica inlines | Checkpointer por hilo + marca de publicación por **ejecución** (coherente con el hilo) | ✅ Resuelto |
| Review sin datos del CI (carrera) | Ingesta diferida: encolar solo con el artefacto presente | ✅ Resuelto |
| Review larga reinicia el worker | Latido en hilo independiente | ✅ Resuelto |
| Ejecutar PHPStan sin sandbox | En Fase 1 solo artefacto; sandbox = Fase 3 | ✅ Fuera de alcance |
| Línea de Finding fuera del diff | Ancla sobre el diff; fallback a resumen | ✅ Cubierto por tests |
| Spam de inlines en PRs grandes | `max_inline_comments` + prioridad por severidad | ✅ Guard activo |
| CI nunca publica el artefacto | TTL → review degradada con aviso explícito | ⚠️ Aceptado (puente) |
| Estado del grafo perdido en reinicio | Checkpointer durable (Postgres) | ✅ Resuelto |

---

## Fase 2 (HITL) — especificada (2026-10-11)

La especificación completa vive en `docs/fase2-hitl-20261011.md`. Resumen de la
decisión y del plan:

- **Objetivo:** el bucle **pausa → decisión → reanudación** durable; el grafo pausa
  ante un hallazgo pendiente y reanuda el mismo `thread_id` sin duplicar publicación.
  Sigue **sin escribir código** (eso es Fase 3).
- **Decisiones:** go ya; canal **CLI + `POST …/decision`** (comentario de PR después);
  la decisión humana **manda** sobre el clamp y `never_auto_paths` (ratificado sin
  segunda señal, también en Fase 3: **riesgo asumido**, ver spec §2.1); transporte en
  **cola única** con el resume en el worker; inbox en **Redis**; techo
  **`fix_with_approval`**; publicar **al decidir**, no al pausar.
- **Cambio respecto al boceto:** el `CheckpointerPort` **sale del alcance**: con el
  resume en el worker, ningún otro proceso necesita el saver. Se reabre si se mueve.
  No confundir con **T10** (seam de retención): aquel puerto abstraía *todo* el saver
  sin segundo cliente; T10 solo aísla la parte **volátil** (la decisión de podar) y
  mantiene el acoplamiento donde no hay alternativa.
- **Hallazgo verificado (LangGraph 1.2.14):** una reentrega del job estando **pausado**
  no pierde la decisión (vuelve a pausar) pero **re-ejecuta `scope` y personas**; hay
  que tratar el estado pausado explícitamente en `process_request`.

Tareas en `tasks/todo.md` (T1–T9 + **T10**) y riesgos en el spec §7.

- **T10 (deuda cerrada):** seam de **Strategy** para la retención de checkpoints. Los
  cuatro backends oficiales declaran `copy_thread`/`delete_for_runs`/`prune` como
  `NotImplementedError` (verificado en los wheels): la API que tocamos es volátil y
  hoy está soldada al saver. T10 la confina en un adaptador y saca la decisión a una
  `RetentionPolicy` testeable, sin cambiar el comportamiento. No reabre el
  `CheckpointerPort`.

Estado: **T1–T10 cerradas** (228 tests en verde). Fase 2 completa; queda el smoke
contra infra real (Redis/Postgres/Bitbucket) y la validación P3 del formato inline.

---

## Fase 3 (fix + verificación) — especificada y en curso (2026-10-11)

La especificación completa vive en `docs/fase3-fix-verificacion-20261011.md`
(aprobada con enmiendas) y las tareas en `tasks/todo-fase3.md`.

- **Objetivo:** un hallazgo `fix_with_approval` **aprobado** produce un parche, se
  verifica en un sandbox con la imagen del CI **por digest** y, solo si pasa, se
  publica como sugerencia inline. Si no pasa, no deja rastro y escala con el log.
  El agente **sigue sin escribir** el repo.
- **Enmiendas de arranque:**
  - **E6 (seam de entrega):** el formato (```` ```suggestion ```` vs ```` ```diff ````)
    vive tras un `DeliveryFormatter`; R1 (¿Bitbucket renderiza `suggestion`?)
    deja de bloquear. Verificado que `twg` no expone ningún concepto de `suggestion`.
  - **E7 (verificación desacoplada):** separar la verificación de la ruta de la
    review es requisito de v1, no optimización (revisión de R2).
  - **Q1:** runner rootless **en esta máquina** (herramienta rootless y user
    namespaces verificados; el demonio actual no es rootless).
  - **Q4:** P6 pasa a ser **precondición del runner** (ejecuta código de PRs de
    terceros), no "ya no bloquea".
- **Cerrado (Bloque 1, base pura):** `F1`–`F6`. Contrato de fix, validación local
  del parche, `LlmFixer`, selección de candidatos, nodo `fix` con el seam de
  entrega, y render/dedupe. **320 tests** en verde; camino pausa → decisión → fix →
  sugerencia verificada cubierto con fakes (`tests/test_fix_e2e.py`).
- **Cerrado (Bloque 2, runner):** `F7`–`F10`. **Enmienda de contrato (2026-10-11):**
  el runner es **síncrono** (`POST /verify` bloqueante con `run_key` idempotente),
  no `POST /runs` + sondeo. El adaptador HTTP, el servicio `sandbox_runner`
  (FastAPI interno, sin `docker.sock`, con runtime/workspace inyectables), la
  validación de `{targets}`, el `ci_image_digest` y el compose/secretos están
  hechos. Ver `tests/test_http_sandbox.py`, `tests/test_sandbox_runner.py`,
  `tests/test_compose.py`.
- **Pendiente (Bloque 3 + precondiciones):** `F11`–`F13` (spike R1 real, smoke
  real, docs) y el desacople **fuerte** de E7 (cola/consumidor de fixes aparte):
  el adaptador síncrono **no** lo cumple, solo lo simplifica. Smoke real del runner
  bloqueado por **P6** (precondición de seguridad).
