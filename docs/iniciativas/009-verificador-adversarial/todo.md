Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Verificador adversarial (L2)

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-16; el plan aterriza sobre el código
de hoy (016, 017 y 007 ya entregadas).

**Tras VF4 (esqueleto del nodo):** el veredicto vive solo en
`state["verifications"]` y, con puerto inyectado, el nodo llama para **todo** lo
no grounded. Eso lo cierran VF6 (mapeo al `Finding`) y VF7 (filtro/topes). **No
empezar VF8** (cablear el verificador real en el worker) hasta cerrar C2: cablear
sin filtro ni mapeo pagaría de más y dejaría el `Finding` sin veredicto.

## Bloque 1 — Contrato (checkpoint C1)

- [x] **VF1** `VerifierPort`, `Verdict` y `VerificationResult`
  - `src/agent_review/ports/verifier.py`: `Verdict` (`CONFIRMED`/`REFUTED`/`UNPROVEN`),
    `VerificationResult` (`finding_id`, `verdict`, `failure_scenario`, `evidence`,
    `files_read`, `reason`) y el `VerifierPort` de §5.2.
  - `ReviewerPort` (`ports/reviewer.py`) gana `grounded: bool` con `False` por
    defecto, documentado como «de quién es la hipótesis» (V7). `ToolingReviewer` lo
    declara `True`; `LlmReviewer` y `NullReviewer` no lo declaran.
  - Aceptación: un doble sin `grounded` se lee como no grounded
    (`getattr(reviewer, "grounded", False)`); el `ToolingReviewer` es grounded.
  - Verifica: `tests/test_domain.py` (los tipos), `tests/test_tooling_reviewer.py`.

- [x] **VF2** Rol `verifier` y *fail-fast* de independencia (V2, §5.3)
  - `src/agent_review/models_config.py`: `ROLES` gana `"verifier"`; el
    *fail-fast* vive en `ModelsConfig._check_verifier_independence` (al cargar el
    YAML): si `bindings["verifier"]` coincide con el de cualquier
    `REVIEW_ROLES` (`correctness`/`security`/`arch`), el registro no valida.
    Sin binding `verifier` no es error (paso directo). Se puso en el contrato del
    YAML —no en `validate_models` del worker— para fallar antes y sin red.
  - `config/models.yaml`: declara `verifier` con un modelo distinto de
    `correctness`, `security` y `arch` (Q1: se elige midiendo contra el *gold set*).
  - Aceptación: arrancar con `verifier = correctness` lanza; un typo en el rol lanza;
    sin `verifier` el registro valida.
  - Verifica: `tests/test_models_config.py`, `tests/test_models_registry.py`.

- [x] **VF3** *Grounding*: atributo, estampa del merge y estado (V7, §5.1)
  - `_merge_findings` (`graph/build.py`) recibe las personas grounded y devuelve
    también los ids *grounded*; el nodo `merge` escribe `grounded_findings` en el
    estado (`Annotated[list[str], ...]`, sustituye, §5.6).
  - Aceptación: un grupo donde participa el `ToolingReviewer` queda grounded aunque
    el representante de evidencia más rica sea la copia del LLM; un hallazgo solo
    del LLM nunca queda grounded.
  - Verifica: `tests/test_graph.py`.

- [x] **VF4** El nodo `verify` entre `merge` y `policy` (V1)
  - `graph/build.py`: `build_graph` acepta `verifier: VerifierPort | None = None`; se
    registra el nodo `verify` (con `RetryPolicy`, es una llamada externa) y la arista
    pasa a `merge → verify → policy`.
  - Sin `verifier` (o sin binding), `verify` es un **paso directo**: no llama a
    nadie y todo queda `unverified`; el grafo se comporta como hoy.
  - Aceptación: el orden `merge → verify → policy` se afirma en un test; sin
    verificador no hay ni una llamada al puerto; un hallazgo refutado **sigue en
    `merged`**.
  - Verifica: `tests/test_graph.py`.
  - *Huecos deliberados del esqueleto (no son deuda nueva):* con puerto, llama a
    todo lo no grounded (filtro → VF7); el veredicto no se escribe en el `Finding`
    (mapeo → VF6).

## Bloque 2 — El verificador (checkpoint C2)

- [x] **VF5** `LlmVerifier` sobre el bucle de 016 (V3, §5.4)
  - `src/agent_review/adapters/llm_verifier.py`: implementa `VerifierPort` con
    `run_tool_loop` (`adapters/tool_loop.py`) y un lector `ContextReaderPort`
    inyectado; el *prompt* del sistema **refuta** (§5.5) y el objeto final es el
    veredicto (`is_final`).
  - El *prompt* incluye el hallazgo, su hunk y los ficheros del diff; el resultado de
    cada herramienta llega ya delimitado como dato por el bucle (016 W4, R11).
  - Aceptación: con un `LlmPort` falso que pide `read_file` y luego emite veredicto,
    el resultado cita un fichero servido; sin veredicto en el tope, `UNPROVEN`.
  - Verifica: `tests/test_llm_verifier.py` (nuevo).

- [x] **VF6** Adjudicación mecánica y mapeo al `Finding` (§5.5, V4, V5, V12)
  - Cierra el hueco de VF4: el veredicto deja de vivir solo en
    `state["verifications"]` y se **estampa** en el `Finding` de `merged`.
  - `graph/build.py`: el nodo comprueba (a) **cita servida** con
    `LoopResult.cite_served` —una cita a un fichero no servido fuerza `UNPROVEN`— y
    (b) **escenario exigido** —`CONFIRMED` sin `failure_scenario` de tres partes
    (`entrada/estado → file:line → resultado`) cae a `UNPROVEN`—.
  - Mapeo al contrato de 007 (`verification` + `verification_evidence`) sobre el
    hallazgo en `merged` por `model_copy`, sin tocar `severity`; en `CONFIRMED` se
    adopta el `failure_scenario` del verificador (V12). Un `REFUTED`/`UNPROVEN`
    también deja su marca en el `Finding` (el hallazgo **sigue** en `merged`).
  - Aceptación: un `CONFIRMED` sin escenario queda `unverified`; una cita no servida
    queda `UNPROVEN`, nunca `verified_grounded`; `severity` no cambia en ningún caso;
    tras un veredicto, `merged[i].verification` refleja el mapeo (no solo el dict
    `verifications`).
  - Verifica: `tests/test_graph.py`.

- [x] **VF7** Topes y configuración (V8, V11) — **antes de VF8**
  - Cierra el hueco de VF4: hoy, con puerto, el nodo verifica **todo** lo no
    grounded; sin este filtro cablear el `LlmVerifier` real (VF8) pagaría de más.
  - `src/agent_review/settings.py`, `.env.example` y `docs/operacion.md` §2:
    `VERIFIER_MAX_FINDINGS` (por defecto 5), `VERIFIER_MAX_TOOL_CALLS`,
    `VERIFIER_MAX_CONTEXT_BYTES` y `VERIFIER_REQUIRE_VERIFIED_FOR` (`[P0, P1]`).
  - El nodo elige candidatos: `require_verified_for` (de `severity_gate` de la
    política si existe; si no, el ajuste) **y** no grounded **y** hasta el tope, los
    más graves primero; el resto queda `unverified` **sin llamada**.
  - El presupuesto se **comparte** con la review (017 B1/B7): `reserve` en `False`
    corta y deja `UNPROVEN`.
  - Aceptación: un hallazgo fuera del umbral, uno grounded y uno más allá del tope
    **no** llaman al verificador (contador de llamadas); los topes de contexto
    cortan y marcan `UNPROVEN`.
  - Verifica: `tests/test_graph.py`, `tests/test_llm_verifier.py`.

## Bloque 3 — Cableado, estado y salida (checkpoint C3)

- [x] **VF8** `build_verifier` en el worker (§5.2)
  - **Depende de C2 (VF6+VF7).** Sin el filtro y el mapeo, un binding real
    multiplicaría llamadas y el `Finding` seguiría sin veredicto publicado.
  - `src/agent_review/worker/main.py`: construye el `LlmVerifier` con
    `registry.port_for("verifier")` y un lector de contexto de
    `build_reader_factory` (016); `build_runtime` lo pasa a `build_graph` (nuevo
    parámetro, patrón de `fixer`).
  - Aceptación: sin binding `verifier`, `build_graph` recibe `None` y el grafo no
    llama; con binding, el puerto llega construido.
  - Verifica: `tests/test_worker.py`.

- [x] **VF9** El estado y la idempotencia (V9, §5.6)
  - `verifications` ya viaja en `ReviewState` y `Verdict`/`VerificationResult` ya
    están en `_STATE_TYPES` (VF1/VF4); aquí se **cierra** con test de pausa HITL +
    reanudación y de que un reintento del nodo no re-verifica lo de `existing`.
  - Aceptación: `verifications` sobrevive a una pausa HITL y a la reanudación; un
    reintento del nodo no re-verifica lo ya verificado.
  - Verifica: `tests/test_checkpointer.py`, `tests/test_graph.py`.

- [x] **VF10** `ReviewOutcome` y log (§5.6)
  - `domain/models.py`: `ReviewOutcome.verifications`; `outcome_from_state` lo
    deriva del estado.
  - Aceptación: el log de cierre distingue el recuento
    `confirmed`/`refuted`/`unproven`; el *outcome* lo expone sin releer el estado.
  - Verifica: `tests/test_worker.py`.

## Bloque 4 — Casos, medida y cierre (checkpoint C4)

- [x] **VF11** Caso C: bucle de `fix_wait` **refutado** (criterio 3)
  - Fixture del bucle `fix_wait → fix_dispatch → fix_wait` con
    `FixtureContextReader`; el verificador traza la ruta, ve que cada vuelta consume
    un intento (`fix_max_attempts`) y **refuta**.
  - Aceptación: veredicto `REFUTED`; el hallazgo se conserva en `merged`.
  - Verifica: `tests/test_llm_verifier.py` (o un módulo de casos del verificador).

- [x] **VF12** Caso A: dirección fijada, sin refutar (criterio 4, V12)
  - El verificador fija la dirección con un `failure_scenario` de tres partes, marca
    `verified_grounded` y el comentario publicado lleva **ese** escenario, no el del
    proponente.
  - Aceptación: el `Finding` sale `verified_grounded` con el escenario del
    verificador y su `verification_evidence` servido.
  - Verifica: `tests/test_graph.py`.

- [x] **VF13** Documentos vivos y CI (criterio 11)
  - `docs/arquitectura.md` §3–4 (nodo `verify`, estado `verifications` y
    `grounded_findings`) y §7 si nace deuda.
  - `docs/operacion.md` §2 (rol `verifier` y las variables nuevas).
  - `docs/desarrollo.md` §3 (puerto `VerifierPort`, adaptador y tipo de estado).
  - `CONTEXT.md`: «verificación adversarial», «hallazgo refutado», «escenario de
    fallo».
  - Spec en `implementada`; fila de 009 en `docs/iniciativas/README.md`.
  - `make ci` y `make docs-check` en verde.

## Bloque 5 — Dependencias externas (humanas)

- [ ] **VF14** *(Humano)* Elegir el modelo de `verifier` con el *gold set* (Q1)
  - Comparar `qwen3.6` y `gemma4` contra el caso del PR #3; fijar el binding.
- [ ] **VF15** *(Humano, con 011)* Cerrar el umbral y `on_refuted`
  - 011 Q1/Q2 deciden qué se publica ante `unverified` y `refuted`; el nodo ya lee
    su clave, así que no cambia código, solo el YAML.
