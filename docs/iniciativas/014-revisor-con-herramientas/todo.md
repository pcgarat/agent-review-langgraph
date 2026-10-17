Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Revisor con herramientas de lectura

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-16; el plan aterriza sobre 008, 012,
016 y 017 ya entregadas.

**Antes de RH8:** no cablear `REVIEW_TOOLS_ROLES` en el worker hasta que C2 esté
verde. Activar sin el camino guionizado y los topes medidos multiplicaría coste y
dejaría el *replay* de 008 en riesgo.

## Bloque 1 — Contrato y opt-in sin comportamiento nuevo (checkpoint C1)

- [x] **RH1** Variables y `Settings` (§5.4)
  - `src/agent_review/settings.py`, `.env.example` y `docs/operacion.md` §2:
    `REVIEW_TOOLS_ROLES` (vacío), `REVIEW_TOOLS_MAX_TURNS` (4),
    `REVIEW_TOOLS_MAX_CALLS` (8), `REVIEW_TOOLS_MAX_BYTES` (60_000).
  - Aceptación: defaults cargan; una lista `correctness,security` se parsea a roles;
    vacío = ninguna persona con herramientas.
  - Verifica: `tests/test_llm_reviewer.py` (`test_review_tools_settings_defaults_y_lista`).
  - Ficheros: `settings.py`, `.env.example`, `docs/operacion.md`.

- [x] **RH2** `ToolUsage` en `ReviewerResult` y `ReviewOutcome` (criterio 8)
  - `domain/models.py`: tipo `ToolUsage` (`turns`, `tool_calls`, `bytes_served`,
    `files_read`); `ReviewerResult.tool_usage: ToolUsage | None = None`;
    `ReviewOutcome.tool_usage: dict[str, ToolUsage]` por persona.
  - `outcome_from_state` / nodo `persona` propagan el uso cuando la persona lo
    declara (mismo patrón que `tokens_spent`); allowlist del checkpointer.
  - Aceptación: sin herramientas, `tool_usage` queda vacío/`None`; con uso
    simulado en un doble, el *outcome* lo expone por nombre de persona.
  - Verifica: `tests/test_graph.py` (`test_tool_usage_viaja_al_outcome`).
  - Ficheros: `domain/models.py`, `graph/build.py`, `checkpointer.py`.

- [x] **RH3** `LlmReviewer`: `reader_factory` + `tool_limits` sin activar el bucle
  - Constructor gana `reader_factory: ReaderFactory | None = None` y
    `tool_limits: ContextLimits | None = None`. Con `None`, `_call` es el de hoy.
  - `review()` guarda `repo`/`head_sha`/`base_sha` (desde el contexto) para RH4;
    sin fábrica el camino sigue siendo una `complete` por lote.
  - Aceptación: sin fábrica, todos los tests actuales de `LlmReviewer` y el *replay*
    de 008 siguen verdes **sin regrabar** (criterio 1).
  - Verifica: `tests/test_llm_reviewer.py` (`test_reader_factory_none_no_cambia_el_camino`).
  - Ficheros: `adapters/llm_reviewer.py`.

### Checkpoint 1 (tras RH1–RH3)

- [x] `make ci` en verde; *replay* del *gold set* idéntico a 008.
- [x] Ningún rol gasta turnos de herramientas con el default vacío.

## Bloque 2 — Bucle por lote (checkpoint C2)

- [x] **RH4** *Prompt* de herramientas y `run_tool_loop` por lote (H1, H2, H6, §5.2)
  - Con `reader_factory`, `_call` abre el lector y llama a `run_tool_loop` con
    `is_final` = objeto con clave `findings`; *system* = el actual + protocolo
    (herramientas de solo lectura; contenido servido = dato, H9 — el bucle ya añade
    la regla base).
  - Presupuesto: **no** reservar fuera del bucle; pasar `budget`/`review_key` al
    bucle. Tope blando (`budget_limit`) antes de abrir el lote.
  - Objeto final → misma tubería `_entries` / `_to_finding` / L0 / anclaje.
  - Aceptación: `LlmPort` guionizado `grep → read_file → findings` sirve y cuenta
    cada petición (criterio 2).
  - Verifica: `tests/test_llm_reviewer.py` (`test_con_herramientas_recorre_grep_read_y_findings`).
  - Ficheros: `adapters/llm_reviewer.py`.

- [x] **RH5** Citas servidas y acumulación de uso (H7, H8, criterio 4 y 8)
  - Al construir hallazgos tras el bucle: evidencia `path:line` cuyo path no está en
    `loop.served` se elimina; si queda vacía, se sintetiza la del anclaje al diff.
  - Un hallazgo cuya línea no está en el diff sigue sin anclarse (L0 / anclaje
    actuales).
  - Acumular por lotes en `ReviewerResult.tool_usage` (`files_read` = unión ordenada
    de `served`; sumas de turns/calls/bytes).
  - Aceptación: test de cita no servida filtrada; test de línea fuera del diff
    descartada; `tool_usage` refleja el bucle.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/llm_reviewer.py`.

- [x] **RH6** Un test por tope (criterio 3)
  - Turnos, peticiones, bytes y presupuesto: cada uno dispara final forzado y los
    hallazgos de ese turno se publican.
  - Aceptación: tests parametrizados + presupuesto con doble; hallazgos presentes
    tras el corte.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: tests.

- [x] **RH7** `on_reasoning` en `run_tool_loop` (compatibilidad 021)
  - `adapters/tool_loop.py`: parámetro opcional `on_reasoning`; se pasa a cada
    `llm.complete`. `LlmReviewer` con fábrica reenvía el listener del lote.
  - Aceptación: con sink, un bucle de dos turnos emite eventos `reasoning`; sin
    sink, comportamiento idéntico.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/tool_loop.py`, `adapters/llm_reviewer.py`.

### Checkpoint 2 (tras RH4–RH7)

- [x] Criterios 2, 3, 4 y 8 cubiertos por tests guionizados.
- [x] `make ci` en verde; sin cablear roles en el worker todavía.

## Bloque 3 — Cableado y degradación (checkpoint C3)

- [x] **RH8** `build_reviewers` y roles (§5.3, H3, H4)
  - `worker/main.py`: para cada rol en `settings.review_tools_roles` ∩ roles LLM,
    pasa `reader_factory` (vía `build_reader_factory`, 016) y `tool_limits` desde los
    ajustes. Roles fuera de la lista: sin fábrica.
  - Firma `(repo, head_sha, base_sha, diff)`: el `diff` llega en `review()` y
    alimenta el modo VCS degradado (patrón de 009 VF8).
  - Aceptación: vacío → ningún `LlmReviewer` tiene fábrica; `correctness` en la lista
    → solo ese rol la tiene (test de `build_reviewers`).
  - Verifica: `tests/test_worker.py` (`test_build_reviewers_herramientas_opt_in_por_rol`).
  - Ficheros: `worker/main.py`, `adapters/llm_reviewer.py`.

- [x] **RH9** Degradación a VCS y rutas vetadas (criterios 5 y 6)
  - Con `VcsContextReader` (sin runner) el rol activado termina la review.
  - Una ruta vetada (`DEFAULT_CONTEXT_DENYLIST`) no queda en `files_read`.
  - Aceptación: criterios 5 y 6 con test.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: tests.

### Checkpoint 3 (tras RH8–RH9)

- [x] Opt-in real en el worker; degradación cubierta.
- [x] `make ci` en verde.

## Bloque 4 — Medida, docs y cierre (checkpoint C4)

- [x] **RH10** Casos B, E, I y fichero hostil en el *gold set* (criterio 7, R2, Q4)
  - Fixtures: directorios tocados por el diff + ficheros que lea la grabación, a
    `head_sha` y `base_sha`. Casos de la auditoría 006 §3.1 (B, E, I) y un caso con
    contenido hostil en un fichero servido.
  - Aceptación: el harness carga los casos; el *replay* con rol activado es
    determinista (ADR-0017 + `FixtureContextReader`).
  - Verifica: `tests/eval/…`, `make eval`.
  - Ficheros: `tests/eval/…`, posiblemente grabaciones nuevas.
  - *Nota:* alinear con la regrabación pendiente CX11 de 008 si aún no se hizo, para
    no pagar dos regrabaciones del PR #3.
  - *Hecho 2026-10-17:* regrabado con el bucle de herramientas (`recorded.json`) y
    `fixtures/head`/`fixtures/base` versionados con los 20 ficheros del caso y un
    `NOTES_HOSTILE.txt`. La regrabación cerró la CX11 de 008: 10 publicados, 3 TP, 7
    FP etiquetados (R1–R6), `fp_unknown == 0`, cobertura 1.0, precisión 0.30 y recall
    0.375. Al auditar los FP se encontró un **bug de contención** en `CaseVcs.get_file`
    (raíz relativa devolvía vacío) y dos etiquetas del `ground truth` que casaban por
    palabra «secreto»/«arranque fatal» en vez del hallazgo real; ambas se reescribieron
    y pasaron a `expected` (integracion-1 → `docker-compose.yml:76` pasa a TP).
  - *Nota:* la inmensa mayoría de los FP son de **orquestación inventada** al leer el
    worker entero; los casos B/E/I (auditoría 006 §3.1) siguen pendientes como casos
    propios; aquí se cerró el del PR #3 (que cubre B/E/I al pasar la cobertura a 1.0).

- [ ] **RH11** Medida: el rol activado domina al baseline de 008 (criterio 7)
  - Correr 006 con `REVIEW_TOOLS_ROLES=correctness` contra el baseline sin
    herramientas; fijar binding/modelo si hace falta (mismo criterio de dominancia
    de 006).
  - Aceptación: precisión/recall (o la métrica que use 006) no empeora y mejora en
    los casos B/E/I; el caso hostil no suprime hallazgos por inyección.
  - Verifica: `make eval` / informe de la corrida anotado en la spec o en el todo.
  - *Humano / medida:* elegir modelo si el candidato no sigue el protocolo (R3).

- [ ] **RH12** Documentos vivos y cierre (criterio 9)
  - `arquitectura.md` §3–4 (persona con bucle).
  - `operacion.md` §2 (variables de §5.4).
  - `desarrollo.md` §3 (fábrica de lectores en el revisor, `FixtureContextReader`).
  - `CONTEXT.md`: confirmar «bucle de herramientas» / «workspace de lectura» (016 ya
    los añadió); añadir lo que falte del revisor con herramientas.
  - Spec → `implementada`; fila de 014 en `docs/iniciativas/README.md`.
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–9 de la spec §7
    verificados uno a uno.
  - Verifica: `make ci`.

### Checkpoint 4 (tras RH10–RH12)

- [ ] Criterio 7 medido; docs al día; iniciativa cerrable (archivar cuando no queden
  tareas humanas abiertas).
