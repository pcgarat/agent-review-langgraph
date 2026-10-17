Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Verificador adversarial (L2)

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-16; el plan aterriza sobre el código
de hoy (016, 017 y 007 ya entregadas).

## Bloque 1 — Contrato (checkpoint C1)

- [ ] **VF1** `VerifierPort`, `Verdict` y `VerificationResult`
  - `src/agent_review/ports/verifier.py`: `Verdict` (`CONFIRMED`/`REFUTED`/`UNPROVEN`),
    `VerificationResult` (`finding_id`, `verdict`, `failure_scenario`, `evidence`,
    `files_read`, `reason`) y el `VerifierPort` de §5.2.
  - `ReviewerPort` (`ports/reviewer.py`) gana `grounded: bool` con `False` por
    defecto, documentado como «de quién es la hipótesis» (V7). `ToolingReviewer` lo
    declara `True`; `LlmReviewer` y `NullReviewer` no lo declaran.
  - Aceptación: un doble sin `grounded` se lee como no grounded
    (`getattr(reviewer, "grounded", False)`); el `ToolingReviewer` es grounded.
  - Verifica: `tests/test_domain.py` (los tipos), `tests/test_tooling_reviewer.py`.

- [ ] **VF2** Rol `verifier` y *fail-fast* de independencia (V2, §5.3)
  - `src/agent_review/models_config.py`: `ROLES` gana `"verifier"`.
  - `src/agent_review/worker/main.py`: `build_registry`/`validate_models` fallan si
    `bindings["verifier"]` coincide con el binding de cualquier rol de
    `LLM_REVIEW_ROLES`; sin binding `verifier` no es error (paso directo).
  - `config/models.yaml`: declara `verifier` con un modelo distinto de
    `correctness`, `security` y `arch` (Q1: se elige midiendo contra el *gold set*).
  - Aceptación: arrancar con `verifier = correctness` lanza; un typo en el rol lanza;
    sin `verifier` el registro valida.
  - Verifica: `tests/test_models_config.py`, `tests/test_models_registry.py`.

- [ ] **VF3** *Grounding*: atributo, estampa del merge y estado (V7, §5.1)
  - `_merge_findings` (`graph/build.py`) recibe las personas grounded y devuelve
    también los ids *grounded*; el nodo `merge` escribe `grounded_findings` en el
    estado (`Annotated[list[str], ...]`, sustituye, §5.6).
  - Aceptación: un grupo donde participa el `ToolingReviewer` queda grounded aunque
    el representante de evidencia más rica sea la copia del LLM; un hallazgo solo
    del LLM nunca queda grounded.
  - Verifica: `tests/test_graph.py`.

- [ ] **VF4** El nodo `verify` entre `merge` y `policy` (V1)
  - `graph/build.py`: `build_graph` acepta `verifier: VerifierPort | None = None`; se
    registra el nodo `verify` (con `RetryPolicy`, es una llamada externa) y la arista
    pasa a `merge → verify → policy`.
  - Sin `verifier` (o sin binding), `verify` es un **paso directo**: no llama a
    nadie y todo queda `unverified`; el grafo se comporta como hoy.
  - Aceptación: el orden `merge → verify → policy` se afirma en un test; sin
    verificador no hay ni una llamada al puerto; un hallazgo refutado **sigue en
    `merged`**.
  - Verifica: `tests/test_graph.py`.

## Bloque 2 — El verificador (checkpoint C2)

- [ ] **VF5** `LlmVerifier` sobre el bucle de 016 (V3, §5.4)
  - `src/agent_review/adapters/llm_verifier.py`: implementa `VerifierPort` con
    `run_tool_loop` (`adapters/tool_loop.py`) y un lector `ContextReaderPort`
    inyectado; el *prompt* del sistema **refuta** (§5.5) y el objeto final es el
    veredicto (`is_final`).
  - El *prompt* incluye el hallazgo, su hunk y los ficheros del diff; el resultado de
    cada herramienta llega ya delimitado como dato por el bucle (016 W4, R11).
  - Aceptación: con un `LlmPort` falso que pide `read_file` y luego emite veredicto,
    el resultado cita un fichero servido; sin veredicto en el tope, `UNPROVEN`.
  - Verifica: `tests/test_llm_verifier.py` (nuevo).

- [ ] **VF6** Adjudicación mecánica en el nodo (§5.5, V4, V5)
  - `graph/build.py`: el nodo comprueba (a) **cita servida** con
    `LoopResult.cite_served` —una cita a un fichero no servido fuerza `UNPROVEN`— y
    (b) **escenario exigido** —`CONFIRMED` sin `failure_scenario` de tres partes
    (`entrada/estado → file:line → resultado`) cae a `UNPROVEN`—.
  - Mapeo al contrato de 007 (`verification` + `verification_evidence`), sin tocar
    `severity`; en `CONFIRMED` se adopta el `failure_scenario` del verificador por
    `model_copy` (V12).
  - Aceptación: un `CONFIRMED` sin escenario queda `unverified`; una cita no servida
    queda `UNPROVEN`, nunca `verified_grounded`; `severity` no cambia en ningún caso.
  - Verifica: `tests/test_graph.py`.

- [ ] **VF7** Topes y configuración (V8, V11)
  - `src/agent_review/settings.py`, `.env.example` y `docs/operacion.md` §2:
    `VERIFIER_MAX_FINDINGS` (por defecto 5), `VERIFIER_MAX_TOOL_CALLS`,
    `VERIFIER_MAX_CONTEXT_BYTES` y `VERIFIER_REQUIRE_VERIFIED_FOR` (`[P0, P1]`).
  - El nodo elige candidatos: `require_verified_for` (de `severity_gate` de la
    política si existe; si no, el ajuste) **y** no grounded **y** hasta el tope, los
    más graves primero; el resto queda `unverified` sin llamada.
  - El presupuesto se **comparte** con la review (017 B1/B7): `reserve` en `False`
    corta y deja `UNPROVEN`.
  - Aceptación: un hallazgo fuera del umbral, uno grounded y uno más allá del tope
    **no** llaman al verificador (contador de llamadas); los topes de contexto
    cortan y marcan `UNPROVEN`.
  - Verifica: `tests/test_graph.py`, `tests/test_llm_verifier.py`.

## Bloque 3 — Cableado, estado y salida (checkpoint C3)

- [ ] **VF8** `build_verifier` en el worker (§5.2)
  - `src/agent_review/worker/main.py`: construye el `LlmVerifier` con
    `registry.port_for("verifier")` y un lector de contexto de
    `build_reader_factory` (016); `build_runtime` lo pasa a `build_graph` (nuevo
    parámetro, patrón de `fixer`).
  - Aceptación: sin binding `verifier`, `build_graph` recibe `None` y el grafo no
    llama; con binding, el puerto llega construido.
  - Verifica: `tests/test_worker.py`.

- [ ] **VF9** El estado y la idempotencia (V9, §5.6)
  - `graph/build.py`: `verifications: Annotated[dict[str, VerificationResult], ...]`
    (sustituye) escrita por `verify`, que lee lo ya verificado como `existing` y no
    re-verifica.
  - `src/agent_review/checkpointer.py`: `Verdict` y `VerificationResult` entran en
    `_STATE_TYPES`.
  - Aceptación: `verifications` sobrevive a una pausa HITL y a la reanudación; un
    reintento del nodo no re-verifica lo ya verificado.
  - Verifica: `tests/test_checkpointer.py`, `tests/test_graph.py`.

- [ ] **VF10** `ReviewOutcome` y log (§5.6)
  - `domain/models.py`: `ReviewOutcome.verifications`; `outcome_from_state` lo
    deriva del estado.
  - Aceptación: el log de cierre distingue el recuento
    `confirmed`/`refuted`/`unproven`; el *outcome* lo expone sin releer el estado.
  - Verifica: `tests/test_worker.py`.

## Bloque 4 — Casos, medida y cierre (checkpoint C4)

- [ ] **VF11** Caso C: bucle de `fix_wait` **refutado** (criterio 3)
  - Fixture del bucle `fix_wait → fix_dispatch → fix_wait` con
    `FixtureContextReader`; el verificador traza la ruta, ve que cada vuelta consume
    un intento (`fix_max_attempts`) y **refuta**.
  - Aceptación: veredicto `REFUTED`; el hallazgo se conserva en `merged`.
  - Verifica: `tests/test_llm_verifier.py` (o un módulo de casos del verificador).

- [ ] **VF12** Caso A: dirección fijada, sin refutar (criterio 4, V12)
  - El verificador fija la dirección con un `failure_scenario` de tres partes, marca
    `verified_grounded` y el comentario publicado lleva **ese** escenario, no el del
    proponente.
  - Aceptación: el `Finding` sale `verified_grounded` con el escenario del
    verificador y su `verification_evidence` servido.
  - Verifica: `tests/test_graph.py`.

- [ ] **VF13** Documentos vivos y CI (criterio 11)
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
