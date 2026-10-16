Última modificación: 2026-10-16 | Creado: 2026-10-16

# Todo: Evaluación de la review (gold set y métricas)

Checklist de la medición. El diseño y las decisiones están en [`spec.md`](spec.md); el
orden, en [`plan.md`](plan.md). Spec aprobada el 2026-10-16. Los doce bloques están
cerrados (E1–E12).

## Bloque 1 — Harness puro (sin red, sin grafo)

- [x] **E1** Esqueleto del harness y del caso
  - `tests/eval/harness.py`: carga de un caso (`labels.yaml` + `diff.patch`), tipos
    del informe (`Case`, `Report`, `Label`) y normalización de texto. Sin
    `__init__.py`; el módulo se importa hermano desde el test.
  - Aceptación: un caso de juguete mínimo (en memoria) se carga y expone
    procedencia, `scope` y las tres listas de etiquetas.
  - Verifica: `tests/eval/test_eval_harness.py`. ✅
  - Ficheros: `tests/eval/harness.py`, `tests/eval/test_eval_harness.py`.

- [x] **E2** Matcher 1:1 determinista
  - `match` conjuntivo sobre `title` + `why_it_matters` normalizados; `file`/`line`
    opcionales con tolerancia de 5 líneas (ADR-0014); orden canónico (severidad,
    línea, `id`) y el primer match no consumido gana.
  - Aceptación: dos casos que casan el mismo `expected` no lo consumen dos veces;
    un hallazgo sin etiqueta queda `unmatched`; el resultado no depende del orden de
    entrada.
  - Verifica: `tests/eval/test_eval_harness.py`.
  - Ficheros: `tests/eval/harness.py`.

- [x] **E3** Métricas y JSON determinista
  - Precisión y recall **excluyendo** `subset: tooling` (E14) y por subconjunto;
    *accuracy* de severidad; falsos `P0`/`P1`; coste en tokens del `usage` (E13);
    `tp_inverted` por `must_mention` fallido (E16). Serialización con claves
    ordenadas y floats a 4 decimales.
  - Aceptación: con los números de la auditoría (9 publicados, A e I `expected`, H
    `acceptable`, 6 falsos) la precisión es 0,25 y el recall 0,25; el JSON es estable
    entre dos serializaciones.
  - Verifica: `tests/eval/test_eval_harness.py`.
  - Ficheros: `tests/eval/harness.py`.

- [x] **E4** `harness.compare` (dominancia, E6)
  - Tres veredictos: **mejora** si precisión, recall y cobertura no bajan, los falsos
    `P0`/`P1` no suben y algo sube estrictamente; **empeora** si lo contrario en
    cualquier métrica; **no concluyente** en el resto.
  - Aceptación: los tres veredictos tienen test, incluido «sube recall y bajan los
    `P0`/`P1` pero cae la precisión» → no concluyente; `coverage=None` no cuenta como
    caída.
  - Verifica: `tests/eval/test_eval_harness.py`.
  - Ficheros: `tests/eval/harness.py`.

## Bloque 2 — Caso y grabación

- [x] **E5** Caso `pr_3`: `labels.yaml` + `diff.patch`
  - Captura una vez el diff del PR #3 (`a8ead73..008da57`, 51 ficheros) y versiona
    `labels.yaml` con procedencia, `scope` (E10: `max_files_per_pr: 10`), `expected`
    (los 7 hallazgos reales: A en `P2`, I, integración 1–3 y no-detectado 1–3),
    `must_not_report` (B–G, con motivo) y `acceptable` (H), más el `must_mention` de A.
  - Aceptación: `labels.yaml` cubre los 9 hallazgos publicados (`fp_unknown == 0`)
    y los 6 hallazgos reales no detectados (3 `P1` de integración y 3 `P2` de código);
    `diff.patch` es el del PR, no un fixture de juguete.
  - Verifica: `make eval` (informe) y revisión humana de las etiquetas.
  - Ficheros: `tests/eval/pr_3/labels.yaml`, `tests/eval/pr_3/diff.patch`.

- [x] **E6** `LlmPort` de *replay* con fingerprint
  - Un puerto que devuelve `text` y `usage` grabados por **rol y llamada** y aborta si
    el fingerprint del prompt (incluye el rol y el `max_findings`) o del caso no
    coincide; el mismo puerto sirve para el modelo real (la única variable es el
    puerto, E7). El fingerprint del caso cubre prompt + `diff` + `scope` (E4).
  - Aceptación: un prompt cambiado (p. ej. `max_findings`) aborta con un mensaje que
    nombra el fingerprint; un caso distinto también.
  - Verifica: `tests/eval/test_eval_harness.py`.
  - Ficheros: `tests/eval/harness.py`.

- [x] **E7** Grabación `recorded.json` y `make eval-record`
  - Graba contra el modelo real (marca `live`) las respuestas del rol `correctness`
    para el caso, con su fingerprint.
  - Aceptación: `make eval-record` produce `recorded.json` con una entrada por rol y
    llamada; regrabar sin cambiar nada da el mismo fingerprint.
  - Verifica: ejecución real (`make eval-record`), fuera de CI.
  - Ficheros: `tests/eval/pr_3/recorded.json`, `scripts/eval.py`.

- [x] **E8** Regresión en CI y baseline congelado
  - `tests/eval/test_eval_pr_3.py` corre el caso en *replay* dentro de `pytest` y
    compara contra `expected_report.json`; dos ejecuciones dan el mismo JSON.
  - Aceptación: `make test` en verde sin red ni credenciales; el baseline de la spec
    §3.3 queda registrado, incluida la cobertura.
  - Verifica: `make test`; `make eval` con delta vacío contra el baseline.
  - Ficheros: `tests/eval/test_eval_pr_3.py`, `tests/eval/pr_3/expected_report.json`.

## Bloque 3 — CLI, cobertura y corrida real

- [x] **E9** CLI `scripts/eval.py` y comandos `make`
  - `make eval` (informe *replay*; `EVAL_COMPARE` añade el delta), `make eval-live`
    (marca `live`) y `make eval-record`.
  - Aceptación: `make eval` imprime el informe con el delta contra el baseline;
    `make eval-live` corre con `-m live`.
  - Verifica: los tres comandos a mano; `make ci` en verde.
  - Ficheros: `scripts/eval.py`, `Makefile`.

- [x] **E10** Cobertura instrumentada del `LlmReviewer` (E11)
  - Campo de solo lectura `last_seen_files` con los ficheros que entraron en el
    prompt; el harness calcula la cobertura sobre las clases de código/configuración
    del PR.
  - Aceptación: la cobertura refleja el recorte real (4/20 = 0,20 sobre código y
    configuración) y no depende del corte de `_MAX_DIFF_CHARS` como constante externa.
  - Verifica: `tests/test_llm_reviewer.py` (el campo expone lo enviado) y `make eval`.
  - Ficheros: `src/agent_review/adapters/llm_reviewer.py`, `tests/eval/harness.py`.

- [x] **E11** Corrida real (`make eval-live`)
  - Corre el caso contra el modelo real y compara con el baseline; n=1, sin
    repeticiones, con la varianza declarada como **no medida** (E15).
  - Aceptación: `make eval-live` emite el informe y el delta sin bloquear CI (test
    `live` deseleccionado por defecto).
  - Verifica: ejecución manual; `make ci` intacto sin credenciales.
  - Ficheros: `scripts/eval.py`, `tests/eval/test_live_eval.py`.

## Bloque 4 — Documentación

- [x] **E12** ADR y documentos vivos
  - ADR nuevo (siguiente número libre) para E3/E4 (replay en CI + fingerprint);
    `desarrollo.md` §1–2 (comandos y zona `eval`); `arquitectura.md` §7 (D14 pasa a
    parcial); `CONTEXT.md` si nace «*gold set*», «caso de evaluación» o «mejora
    (dominancia)»; fila de 006 en `docs/iniciativas/README.md`.
  - Aceptación: `make docs-check` en verde; todos los documentos vivos describen el
    harness resultante.
  - Verifica: `make ci` y `make docs-check`.
  - Ficheros: `docs/adr/NNNN-*.md`, `docs/adr/README.md`, `docs/desarrollo.md`,
    `docs/arquitectura.md`, `CONTEXT.md`, `docs/iniciativas/README.md`.
