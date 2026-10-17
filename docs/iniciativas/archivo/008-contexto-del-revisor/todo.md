Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Contexto del revisor (L0 + L1)

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — Cimientos y L0 (checkpoint C1)

- [x] **CX1** `base_sha` en el modelo y en los VCS (D10)
  - `ports/vcs.py`: `PullRequestInfo.base_sha: str = ""`.
  - `domain/models.py`: `ReviewRequest.base_sha: str = ""` (viaja con la petición).
  - `adapters/twg_vcs.py`: `destination.commit.hash`; `adapters/github_vcs.py`:
    `base.sha`.
  - Quien construye el `ReviewRequest` (`api/main.py`, worker, tests) copia el
    `base_sha` del PR cuando lo tenga.
  - Aceptación: un PR de GitHub/twg con base conocida rellena el campo; vacío no
    rompe validación.
  - Verifica: `tests/test_github_vcs.py`, `tests/test_twg_vcs.py`, `tests/test_domain.py`.
  - *Hecho 2026-10-17:* el webhook acepta `base_sha` opcional; el worker lo completa
    con `_with_base_sha` si llega vacío (`get_pull_request`). No entra en la clave
    de idempotencia.

- [x] **CX2** Índice del lado viejo del diff (D1, paso a de `pre_existing`)
  - `adapters/diff_parser.py`: `FileDiff` gana `context` y `removed`
    (`dict[int, str]` del lado viejo / números del lado nuevo para contexto).
  - `parse_unified_diff` indexa ` ` y `-` además de `+`; `number_diff` y el anclaje
    actual no cambian de contrato hacia fuera.
  - Aceptación: un hunk con contexto y borradas se consulta por número de línea;
    las añadidas siguen igual.
  - Verifica: `tests/test_diff_parser.py` (nuevo).

- [x] **CX3** L0: `pre_existing` calculado (D2, D14)
  - `src/agent_review/verification/l0.py` (nuevo paquete): función pura que, dada la
    cita, el `FileDiff` y un `base_content` opcional, decide `pre_existing` (paso a:
    lado viejo del diff; paso b: fichero a `base_sha`).
  - Sin `base_sha` o si `get_file` falla: solo paso a, con log (R3).
  - Aceptación: el caso F (cita idéntica en la base) sale `pre_existing=True`; una
    línea solo añadida, `False`.
  - Verifica: `tests/test_verification_l0.py` (nuevo).

- [x] **CX4** L0: anclaje estricto y cita contrastada (D1, D9)
  - En `verification/l0.py`: (1) si `line`/cita apuntan a contexto o borrada, **no**
    llamar a `best_line` por cercanía; (2) cita que no casa con ninguna línea del
    fichero → descartar; (3) cita que casa con contexto/borrada → hallazgo al
    resumen (`line` en esa línea, no en una añadida).
  - Aceptación: los tres casos del criterio 1 pasan con test.
  - Verifica: `tests/test_verification_l0.py`.
  - *Hecho 2026-10-17 (CX2–CX4):* módulo puro sin cablear al `LlmReviewer` (CX5)
    para no chocar con 009 en `graph/build.py`.

- [x] **CX5** Cablear L0 en `LlmReviewer`; dejar de copiar `pre_existing` (D2, D13)
  - `adapters/llm_reviewer.py`: tras anclar, invoca L0; el valor del modelo en
    `pre_existing` se ignora; un descarte de L0 no entra en `findings`.
  - `publish` (`graph/build.py`): si `finding.line` no está en las líneas añadidas
    del diff → `summary_only` sin llamar a `resolve_inline_anchor`.
  - Aceptación: test con doble de modelo que miente en `pre_existing` → gana L0;
    cita inventada no sale; cita de contexto no se ancla inline.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_graph.py`.
  - *Hecho 2026-10-17:* L0 en `_to_finding`; publish endurecido; `FakeVcs` sintetiza
    hunks por ancla para que los tests de inline sigan anclando líneas añadidas.

## Bloque 2 — ReviewContext y L1 (checkpoint C2)

- [x] **CX6** `ReviewContext` y ensamblaje en `scope` (D4, D14)
  - Tipo inmutable (p. ej. en `domain/` o `scope.py`) con diff indexado, ficheros
    completos al `head_sha` y documentos de contexto.
  - `graph/build.py` `scope`: lee con `vcs.get_file` (cache por `(path, ref)`);
    falla de lectura → sin ese fichero, log, no tumba (R3).
  - Estado: clave `review_context` (o nombre acordado) que sobrevive al fan-out;
    `persona`/`ReviewerPort.review` reciben `context=...`.
  - Aceptación: una review con dos personas llama a `get_file` una vez por ruta.
  - Verifica: `tests/test_graph.py`.
  - *Hecho 2026-10-17:* `ReviewContext` (Pydantic frozen) con `contents`/`docs`;
    el diff indexado sigue en `state["diff"]` (checkpointer). `assemble_review_context`
    en `scope.py`; worker pasa `context_doc_paths(...)`.

- [x] **CX7** `context_docs:` en el perfil (D5, D11) — validación
  - `profile.py`: valida `context_docs` opcional (`glossary`, `decisions: [str]`,
    `extra: [str]`); rutas relativas al repo revisado.
  - `context_docs(profile)` extrae el mapa o `{}` si es inválido.
  - *Hecho con CX6:* `scope` carga las rutas de `context_doc_paths`.
  - Aceptación: perfil sin clave → OK; perfil con lista inválida → warning y
    extractor vacío, no tumba.
  - Verifica: `tests/test_profile.py`.
  - *Hecho 2026-10-17:* contrato del perfil + lectura en `scope` (CX6).

- [x] **CX8** L1 por lote + tope de config (D8, D12)
  - `settings.py` + `.env.example` + `operacion.md` §2: `LLM_CONTEXT_MAX_CHARS`.
  - `LlmReviewer`: cada lote incluye fichero completo de sus paths y los docs, con
    prioridad diff → fichero → docs; el contexto que no cabe se omite y la
    cobertura lo anota; el diff del lote **no** se recorta.
  - `_estimate_tokens` cuenta el contexto.
  - Aceptación: test de que el contexto no desplaza diff; tope respetado.
  - Verifica: `tests/test_llm_reviewer.py`.
  - *Hecho 2026-10-17:* `pack_l1_context`; `ReviewCoverage.context_omitted`;
    secciones `#N` en `assemble_review_context`.

- [x] **CX9** Regla de humildad en el *system prompt* (D6)
  - Texto explícito: claim que depende de código no visible →
    `context_scope: outside_diff`, sin inventar semántica.
  - `_to_finding` ya respeta `context_scope` del modelo (007); test con entrada que
    lo declara.
  - Aceptación: el *system* contiene la regla; un hallazgo con
    `outside_diff` sobrevive y no cambia `severity`.
  - Verifica: `tests/test_llm_reviewer.py`.
  - *Hecho 2026-10-17:* regla en `_SYSTEM`; test de supervivencia sin tocar severidad.

## Bloque 3 — Cierre (checkpoint C3)

- [x] **CX10** Render: etiqueta «preexistente» (D7)
  - Inline y resumen muestran la etiqueta cuando `pre_existing` es verdadero; no
    cambian severidad ni acción.
  - Aceptación: snapshot o aserción de texto; `severity` intacta.
  - Verifica: tests de render del grafo / fix_render según donde viva.
  - *Hecho 2026-10-17:* también «fuera del diff» si `context_scope=outside_diff`.

- [x] **CX11** Casos de la auditoría y medida (criterios 1, 7)
  - Tests unitarios del caso F (`pre_existing`) y de humildad/contexto para B, D, E,
    G, H (fixture de prompt o de L0/L1, sin red).
  - Si se regraba el *gold set* en esta iniciativa: anotar baseline provisional;
    si no, dejar nota de que la regrabación conjunta con 009 cierra el criterio 7.
  - Aceptación: L0/L1 no empeoran los invariantes de test; `severity` nunca muda.
  - Verifica: `tests/test_verification_l0.py`, `tests/test_llm_reviewer.py`; opcional
    `make eval-record`.
  - *Hecho 2026-10-17 (parcial):* F y humildad cubiertos en unitarios; el *gold set*
    del PR #3 queda en `xfail` hasta `make eval-record` conjunto con 009 (criterio 7).

- [x] **CX12** Documentos vivos y CI
  - `docs/arquitectura.md` §3–4 (persona, `ReviewContext`, L0).
  - `docs/operacion.md` §2 (`LLM_CONTEXT_MAX_CHARS`, `context_docs` en el perfil).
  - `docs/desarrollo.md` §2–3 (tests de L0; cómo añadir `context_docs`).
  - `CONTEXT.md`: «L0», «L1», «ReviewContext» si no están.
  - Spec en `implementada`; fila de 008 en el índice.
  - `make ci` y `make docs-check` en verde.
