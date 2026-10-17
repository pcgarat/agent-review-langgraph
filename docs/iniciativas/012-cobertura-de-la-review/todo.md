Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Cobertura de la review

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17. Cierra
[D18](../../arquitectura.md) §7.

## Bloque 1 — Tipo, clases y `scope` (checkpoint C1)

- [ ] **CB1** `ReviewCoverage` y clasificación por *globs*
  - `src/agent_review/domain/models.py`: `SkipReason` (con `failed`), `FileClass` y
    `ReviewCoverage` con `partial` según C7 (spec §5.2).
  - `src/agent_review/scope.py`: clases por defecto de spec §5.4 y
    `classify(path, classes)`, en el orden `test` → `docs` → `config` → `code`.
  - `src/agent_review/profile.py`: clave `scope` (mapa con `classes`) en el contrato
    del perfil; las clases del perfil se suman a las de por defecto.
  - Aceptación: `docker-compose.yml`, `.env.example` y `src/x/settings.py` son
    `config`; `tests/config/a.yaml` es `test`; `docs/a.md` es `docs`; `partial` es
    cierto solo con `code`/`config` sin revisar.
  - Verifica: `tests/test_scope.py`, `tests/test_domain.py`, `tests/test_profile.py`.

- [ ] **CB2** `scope` sin `max_files_per_pr` (C8)
  - `config/autonomy.yaml`: fuera la guarda. `src/agent_review/graph/build.py`
    (nodo `scope`): fuera `limit_files`; prioriza por clase y churn (del propio diff)
    y reordena el diff; crea la cobertura con los `ignored`.
  - Aceptación: un diff con 51 ficheros llega entero y en orden de prioridad; la
    cobertura lista los ignorados; `limit_files` no existe.
  - Verifica: `tests/test_scope.py`, `tests/test_graph.py` (sustituye a
    `test_scope_aplica_max_files_per_pr`).

- [ ] **CB3** Retirar `get_diffstat`
  - Sale de `ports/vcs.py`, `adapters/github_vcs.py`, `adapters/twg_vcs.py` y de los
    dobles de test, si tras CB2 no lo usa nadie (`grep`).
  - Verifica: `make ci`.

- [ ] **CB4** La cobertura en el estado
  - `ReviewState.coverage` con *reducer* que combina por fichero (revisado si alguna
    persona LLM lo revisó). `ReviewerResult.coverage` opcional. `ReviewCoverage`,
    `SkipReason` y `FileClass` en `_STATE_TYPES` de `checkpointer.py`.
  - Aceptación: la cobertura sobrevive a una pausa HITL y a la reanudación.
  - Verifica: `tests/test_checkpointer.py`, `tests/test_graph.py`.

## Bloque 2 — Lotes y publicación (checkpoint C2)

- [ ] **CB5** Lotes de ficheros enteros en el `LlmReviewer` (C2, C3, C6)
  - `src/agent_review/adapters/llm_reviewer.py`: fuera `_MAX_DIFF_CHARS`; lotes hasta
    `LLM_BATCH_MAX_CHARS` en orden de prioridad, agrupando por directorio; `too_large`
    sin llamada; `reserve`/`commit` por lote contra 017; el resto como `budget` al
    primer `False`; lote con fallo permanente como `failed`; `max_findings` sobre el
    total, por severidad; `last_seen_files` como unión.
  - `src/agent_review/settings.py`, `.env.example` y `docs/operacion.md` §2:
    `LLM_BATCH_MAX_CHARS` (por defecto 60 000).
  - Aceptación: un diff mayor que un lote no se corta a mitad de fichero; con un
    contador que se agota en el segundo lote, el resto sale `budget`; un fichero
    mayor que el lote sale `too_large` sin gastar llamada.
  - Verifica: `tests/test_llm_reviewer.py`.

- [ ] **CB6** Cabecera de review parcial (spec §5.5)
  - `publish` en `src/agent_review/graph/build.py`: `complete` también depende de
    `coverage.partial`; cabecera con los ficheros de código y configuración sin
    revisar y su motivo; docs y tests sin revisar en una línea plegable.
  - Aceptación: configuración sin revisar → cabecera y marca `:partial`; solo docs
    sin revisar → lista plegable y review completa; la cabecera no cambia ninguna
    acción (Q3).
  - Verifica: `tests/test_graph.py`.

- [ ] **CB7** Cobertura en el `ReviewOutcome`
  - `ReviewOutcome.coverage`; el log `Review INCOMPLETA` de `worker/main.py` incluye
    los ficheros sin revisar.
  - Verifica: `tests/test_worker.py`.

## Bloque 3 — Medida y cierre (checkpoint C3)

- [ ] **CB8** La métrica de 006 lee `ReviewCoverage`
  - `tests/eval/harness.py`: `coverage_of` toma los revisados del `ReviewOutcome` en
    vez de `last_seen_files`; si nadie más lo usa, `last_seen_files` se retira.
  - Verifica: `tests/eval/test_eval_harness.py`.

- [ ] **CB9** *(Red)* Regrabar el *gold set*
  - `make eval-record` con una credencial válida (junto a V6 de 007 si coinciden);
    revisar el diff del baseline.
  - Aceptación: el *replay* de CI pasa con la grabación nueva.

- [ ] **CB10** Reproducción del PR #3 (criterio 1)
  - Con el fixture de 006 y el presupuesto por defecto: `docker-compose.yml`,
    `settings.py` y `.env.example` revisados; `worker/main.py` revisado o declarado
    `too_large`/`budget`.
  - Verifica: `tests/eval/`.

- [ ] **CB11** Documentos vivos y CI
  - `docs/arquitectura.md` §3–4 (estado `coverage`, nodo `scope`) y §7 (fuera D18).
  - `docs/operacion.md` §7: el diagnóstico de «0 hallazgos» deja de buscar
    `max_files_per_pr` y pasa a leer la cobertura.
  - `docs/desarrollo.md` §3: `ReviewCoverage` como tipo de estado nuevo.
  - `CONTEXT.md`: «cobertura», «review parcial»; «Scope» y «Guarda» sin
    `max_files_per_pr`.
  - Spec en `implementada`; fila de 012 en `docs/iniciativas/README.md`.
  - `make ci` y `make docs-check` en verde.
