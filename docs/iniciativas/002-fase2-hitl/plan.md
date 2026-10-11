Última modificación: 2026-10-11 | Creado: 2026-10-11

# Plan: Fase 2 (HITL)

La especificación completa vive en `docs/iniciativas/002-fase2-hitl/spec.md`. Resumen de la
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

Tareas en `docs/iniciativas/002-fase2-hitl/todo.md` (T1–T9 + **T10**) y riesgos en el spec §7.

- **T10 (deuda cerrada):** seam de **Strategy** para la retención de checkpoints. Los
  cuatro backends oficiales declaran `copy_thread`/`delete_for_runs`/`prune` como
  `NotImplementedError` (verificado en los wheels): la API que tocamos es volátil y
  hoy está soldada al saver. T10 la confina en un adaptador y saca la decisión a una
  `RetentionPolicy` testeable, sin cambiar el comportamiento. No reabre el
  `CheckpointerPort`.

Estado: **T1–T10 cerradas** (228 tests en verde). Fase 2 completa; queda el smoke
contra infra real (Redis/Postgres/Bitbucket) y la validación P3 del formato inline.
