Última modificación: 2026-10-11 | Creado: 2026-10-11

# Plan: Fase 1 (report-only)

> **Estado: entregado (2026-10-11).** Las Tasks 1–10 están cerradas, `make ci` en
> verde (`ruff` lint + formato, `mypy` strict, 182 tests). El código de las 10 tareas
> vive en git; este documento ya no repite su histórico. Lo que queda abierto son
> **verificaciones humanas o externas**, no código (ver "Pendiente").

El diseño de arquitectura está en `docs/iniciativas/archivo/001-diseno-base/spec.md`. El
registro de la auditoría que cerró los huecos de robustez, en
`docs/arquitectura.md`.

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
  `docs/adr/0004-ingesta-diferida-por-artefacto-del-ci.md`.
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
| 9 | Smoke E2E documentado | `docs/operacion.md` |
| 10 | Ingesta diferida + retirada del webhook de PR | `tests/test_pending.py`, `tests/test_api.py` |

Decisiones de implementación no obvias, documentadas aparte:

- **Task 6b** — la salida de PHPStan llega como **artefacto** leído por
  `ToolingOutputSource`; el agente no ejecuta la herramienta en Fase 1 (eso es el
  sandbox de Fase 3, otro adaptador del mismo puerto). Ampliado en el README.
- **Task 7** — el formato inline y el orden de publicación se fijan en
  `docs/adr/0006-publicacion-inline-primero-resumen-al-final.md`.
- **Task 10** — la ingesta diferida es un **puente desechable**; ver
  `docs/adr/0004-ingesta-diferida-por-artefacto-del-ci.md`.

---

## Pendiente (no es código)

| # | Pendiente | Tipo |
|---|---|---|
| P1 | El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado | Externo, **opcional** (`docs/adr/0004-ingesta-diferida-por-artefacto-del-ci.md` §3–4) |
| P2 | Nivel 2 del smoke: un PR real, verificar inline + resumen + Teams | ✅ Hecho con el PR 430 (`docs/operacion.md` §5); Teams queda en no-op sin webhook real |
| P3 | Revisión humana del formato inline + resumen | Humano (`docs/adr/0006-publicacion-inline-primero-resumen-al-final.md`) |
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
| Fallo transitorio dentro de un nodo (twg/LLM/Teams) rehace el grafo entero | `RetryPolicy` por nodo + `is_transient` (`docs/adr/0007-reintento-en-dos-capas.md`) | ✅ Resuelto |
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
