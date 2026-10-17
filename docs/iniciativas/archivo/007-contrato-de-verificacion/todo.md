Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Contrato de verificación del hallazgo

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — El contrato (checkpoint C1)

V1, V2 y V3 van en el mismo commit: `schema_matches_domain` no permite separarlos.
*Nota 2026-10-17:* V4 y V5 entraron también en ese commit. Con I1 activo, el
`ToolingReviewer` emitía `P2` `unverified` sin escenario y el `LlmReviewer` no leía
`failure_scenario`: ambos dejaban de producir hallazgos válidos hasta tener sus
campos. El `if/then` del JSON no lleva `required: [verification]` en el `if`
(a diferencia del borrador de spec §5.5): con `verification` ausente vale su
*default* `unverified`, y la regla debe disparar igual.

- [x] **V1** `Finding` gana los campos y los invariantes
  - `src/agent_review/domain/models.py`: enums `Verification` y `ContextScope`;
    campos `failure_scenario`, `verification`, `verification_evidence` y
    `context_scope` con sus *defaults* (spec §5.2); `model_validator` con I1–I3.
  - Los helpers de los tests que construyen `P0`–`P2` rellenan `failure_scenario`.
  - Aceptación: un `P1` `unverified` sin escenario se rechaza; un `verified_static`
    sin evidencia se rechaza; un `P3` sin escenario pasa; un `P2` `verified_static`
    sin escenario pasa (Q5).
  - Verifica: `tests/test_domain.py`, `tests/test_contracts.py`.

- [x] **V2** El contrato JSON
  - `contracts/findings-schema.json`: las cuatro propiedades, `$defs.Verification`,
    `$defs.ContextScope` y el `if/then` condicional de `failure_scenario` (spec §5.5).
  - Aceptación: `schema_matches_domain()` en verde; el `required` de nivel superior
    no gana ningún campo.
  - Verifica: `tests/test_contracts.py`.

- [x] **V3** Retirar `requires_verification` (D12)
  - Sale de `Finding`, de `properties` y `required` del JSON, de
    `tooling_reviewer.py`, del `_SCHEMA_HINT` y de `_to_finding` en `llm_reviewer.py`, y
    de los tests que lo pasan.
  - Los fixtures grabados de `tests/eval/` no se tocan: son respuestas del modelo y
    el campo extra se ignora.
  - Aceptación: un `Finding` serializado con `requires_verification` y un `P1` sin
    escenario, guardados en un checkpoint de antes de 007, se reanudan sin error;
    `grep requires_verification src contracts` vacío.
  - Verifica: `tests/test_checkpointer.py`.

## Bloque 2 — Los emisores (checkpoint C2)

- [x] **V4** `ToolingReviewer` declara su nivel
  - `src/agent_review/adapters/tooling_reviewer.py`: `verification: verified_static`,
    `verification_evidence` con la cita `file:line texto`, y `context_scope`:
    `in_diff` si la línea reportada es añadida, `outside_diff` si `best_line` reancla
    (Q3).
  - Aceptación: los dos casos de `context_scope` probados; sus `P2` no necesitan
    escenario.
  - Verifica: `tests/test_tooling_reviewer.py`.

- [x] **V5** `LlmReviewer` pide, lee y normaliza
  - `src/agent_review/adapters/llm_reviewer.py`: el `_SCHEMA_HINT` pide
    `failure_scenario` (formato `input/estado → file:line → resultado`) y
    `context_scope`; `_to_finding` los lee y fija `verification: unverified` aunque el
    modelo diga otra cosa.
  - Aceptación: un `P1` sin escenario se descarta por el contrato; un
    `verified_grounded` del modelo sale `unverified` y sin evidencia de verificación;
    un `context_scope` desconocido cae a `None`.
  - Verifica: `tests/test_llm_reviewer.py`.

- [x] **V6** *(Red)* Regrabar el *gold set*
  - `make eval-record` con una credencial válida; revisar el diff del baseline
    congelado ([ADR-0017](../../../adr/0017-replay-determinista-con-fingerprint.md)).
  - Aceptación: el *replay* de CI pasa con la grabación nueva; el informe anota
    cuántos `P0`–`P2` descartó I1 (dato para revisar Q5 y R1).
  - *Resultado 2026-10-17* (`deepseek-v4-flash`, una muestra): 7 hallazgos, todos
    con `failure_scenario` y `context_scope: in_diff`; **I1 no descartó ninguno**.
    Los 7 son FP: dos ya etiquetados (F, G) y seis nuevos (J–O en `labels.yaml`),
    verificados contra `008da57`. Baseline: precisión 0.0, recall 0.0, cobertura 0.2
    (antes 0.25 / 0.25 / 0.2). Lectura: el modelo rellena escenarios plausibles pero
    inalcanzables (R1 se materializa); el escenario no filtra FP por sí solo y la
    verificación de [009](../009-verificador-adversarial/spec.md) es la que debe
    hacerlo. Los TP de la auditoría (A, I) no salieron en esta muestra: con una sola
    grabación no se separa el efecto del *prompt* del ruido del muestreo.

## Bloque 3 — Cierre (checkpoint C3)

- [x] **V7** ADR-0021 — Cascada de verificación del hallazgo
  - `docs/adr/0021-cascada-de-verificacion-del-hallazgo.md` con el contenido de spec
    §5.6, y su fila en `docs/adr/README.md`.

- [x] **V8** Documentos vivos y CI
  - `CONTEXT.md`: «verificación del hallazgo», «escenario de fallo», «alcance de
    contexto».
  - `docs/desarrollo.md` §4: I1–I3 como invariantes del contrato.
  - `docs/arquitectura.md`: donde describa el contrato de hallazgo.
  - Spec en `implementada`; fila de 007 en `docs/iniciativas/README.md`.
  - `make ci` y `make docs-check` en verde.
