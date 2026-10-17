Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Eficacia del bucle de lectura

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **No se empieza el bloque 2 (G1/R1/L1) hasta que K0 esté
verde**: sin D23 y sin el matcher, cada mejora hace salir el eval en verde con
recall 0 (§3 de la spec).

## Bloque 0 — Base de medida (checkpoint K0)

- [ ] **T0** D23: el *replay* con *fingerprint* caducado **aborta** (E0)
  - `adapters/llm_reviewer.py`: un `except FingerprintMismatch: raise` (o marca en
    la excepción) **antes** del `except Exception` en `_call_once` (~:607) y en
    `_call_with_tools` (~:536). El harness no cambia.
  - Aceptación: con una grabación caducada, `make eval-tools` termina en **error
    ruidoso**, no en un informe con recall 0; la marca de D23 en
    `arquitectura.md §7` pasa a resuelta.
  - Verifica: `tests/test_llm_reviewer.py` (nuevo test de `FingerprintMismatch`
    propagada) y `tests/eval/test_eval_harness.py`.
  - Ficheros: `adapters/llm_reviewer.py`, `docs/arquitectura.md` §7.

- [ ] **T1** ADR: el matcher mide **defecto**, no redacción (E11/R1)
  - ADR nuevo (`docs/adr/0025-…`): ancla por `file`(+`line`, tolerancia ADR-0014)
    como primaria y fragmentos como respaldo/desambiguación.
  - `tests/eval/harness.py`: `_matches`/`_attribute` según el ADR; enmienda con
    fecha en la spec de [006](../archivo/006-eval-de-la-review/spec.md).
  - Aceptación: un mismo defecto redactado distinto casa la **misma** etiqueta
    (test con dos textos sintéticos); una etiqueta con `file` sin `line` sigue
    casando por fichero.
  - Verifica: `tests/eval/test_eval_harness.py`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/harness.py`, `docs/adr/`, spec de 006.

- [ ] **T2** Reancla de etiquetas y *baseline* firme (R1)
  - `tests/eval/pr_3/labels.yaml`: reanclar contra la grabación con herramientas.
    Hechos verificados: `docker-compose.yml:76` es `integracion-1` **y**
    `integracion-2`; `build.py:1123` es `R3`; `worker/main.py:152` es
    `no-detectado-1`; `fixworker/main.py:95` es la etiqueta `A` (mal anclada: es un
    FP distinto del `A` real).
  - `expected_report.json`: refijar el *baseline* del *replay* sin herramientas tras
    T1 (el informe cambia de números porque el matcher cambia).
  - Aceptación: `make eval` y `make eval-tools` producen informes **estables** y
    reproducibles; el *baseline* queda registrado.
  - Verifica: `make eval`, `make eval-tools`.
  - Ficheros: `tests/eval/pr_3/labels.yaml`, `tests/eval/pr_3/expected_report.json`.

### Checkpoint K0

- [ ] Grabación caducada ⇒ error, no recall 0.
- [ ] El informe no depende de la redacción del mismo defecto.
- [ ] `make ci` en verde.

## Bloque 1 — Modos de las tres herramientas (checkpoint K1)

- [ ] **G1** `grep` con `mode`, `path`/`glob` y `files_only` (E2, E3)
  - `ports/context.py`: `grep(pattern, *, glob=None, path=None, mode="literal",
    files_only=False)`. `mode="regex"` en los tres adaptadores: `git grep -E` en el
    runner, `re` en los dos en memoria.
  - `adapters/tool_loop.py`: `_serve_grep` acepta y traduce los parámetros; el
    *system prompt* documenta `mode` y su default.
  - Topes: `files_only` devuelve solo rutas; en `regex`, tope de coincidencias y de
    tiempo (mitiga R3 de 016).
  - Aceptación: `mode="regex"` casa `complete\(|model_for\(`; `mode` ausente es
    idéntico a hoy; un `path` acota de verdad.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`.
  - Ficheros: `ports/context.py`, los tres adaptadores, `adapters/tool_loop.py`.

- [ ] **R1** `read_file` con aviso de corte y `mode="outline"` (E4, E5)
  - `_budget_bytes`: si corta, anexa `…[cortado a N de M bytes; usa start/end]`.
  - `mode="outline"`: índice de símbolos con rango de líneas (AST donde exista;
    heurística declarada donde no; cae a `full` si duda).
  - Aceptación: corte anunciado con tamaño real; `outline` da rangos y el `read_file`
    posterior de ese rango da el cuerpo.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`.
  - Ficheros: `ports/context.py`, adaptadores, `adapters/tool_loop.py`.

- [ ] **L1** `list_dir` con `glob` y `depth` (E6)
  - `ports/context.py`: `list_dir(path, *, glob=None, depth=1)`; recursión acotada.
  - Aceptación: `depth=2` lista dos niveles; `glob="*.py"` filtra; `depth=1` es
    idéntico a hoy.
  - Verifica: `tests/test_context_*.py`, `tests/test_tool_loop.py`.
  - Ficheros: `ports/context.py`, adaptadores.

### Checkpoint K1

- [ ] `mode`/`depth` ausentes ⇒ comportamiento idéntico a hoy (criterio 7).
- [ ] `make ci` en verde.

## Bloque 2 — Palancas transversales (checkpoint K2)

- [ ] **P1** Tope de peticiones por turno como palanca medida (E7)
  - `_MAX_CALLS_PER_TURN` deja de ser constante: `ContextLimits.calls_per_turn` +
    ajuste `REVIEW_TOOLS_CALLS_PER_TURN` (default 3).
  - Aceptación: con 4–5 el aviso de descarte desaparece; la corrida mide *prompt*
    y recall contra el valor 3.
  - Verifica: `tests/test_tool_loop.py`; informe anotado (M1).
  - Ficheros: `adapters/tool_loop.py`, `ports/context.py`, `settings.py`.

- [ ] **S1** No descartar en silencio + estado de lecturas (E9, §5.3)
  - `REVIEW_TOOLS_STRICT_CALLS`: cada petición no atendida recibe un
    `<tool_result>ERROR: no atendida…</tool_result>` explícito.
  - Bloque `LECTURAS (esta review)` con rutas y rangos, en el `LlmReviewer` (vida de
    la review). `REVIEW_TOOLS_READ_STATE`.
  - Aceptación: error explícito por petición no atendida; el bloque aparece tras una
    lectura y no crece sin límite.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/tool_loop.py`, `adapters/llm_reviewer.py`.

- [ ] **C1** Caché de E/S por review y precarga de L1 conmutable (E8, E9)
  - `CachingReader` (decorador de `ContextReaderPort`) a nivel de review,
    `REVIEW_TOOLS_IO_CACHE`. **Ahorra E/S, no *prompt*** (la spec lo dice).
  - `REVIEW_TOOLS_PRECACHE_CONTEXT`: `on`/`off` de la precarga L1 en el bucle.
  - Aceptación: con el caché, un mismo fichero en dos lotes se lee una vez (contador
    de E/S); con `precache=off` el *prompt* base del lote baja.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/llm_reviewer.py`, adaptadores de contexto.

### Checkpoint K2

- [ ] Cada palanca con su flag; con todos en default, comportamiento de hoy.
- [ ] `make ci` en verde.

## Bloque 3 — Medida, docs y cierre (checkpoint K3)

- [ ] **M1** Una corrida por flag contra el *baseline* (criterio 8, R7)
  - `make eval-tools` con cada configuración (regex, outline, estado, strict,
    calls_per_turn 3 vs 4–5, precache on/off, caché) y los números anotados.
  - Aceptación: informe con *prompt*, *completion* y recall por configuración; la
    decisión de cada default se justifica con el número.
  - *Humano / medida:* fijar los defaults de §5.4 tras leer el informe.
  - Verifica: `make eval-tools`, informe anotado aquí o en la spec.

- [ ] **D1** Documentos vivos y cierre (criterio 10)
  - `operacion.md` §2: las variables de §5.4.
  - `desarrollo.md` §3: modos del puerto y `CachingReader`.
  - `CONTEXT.md`: «modo» de lectura si es término nuevo; «estado de lecturas».
  - `arquitectura.md` §7: D23 resuelta (T0); anotar lo que 023 cierre.
  - Spec → `implementada`; fila de 023 en `docs/iniciativas/README.md`.
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–10 de la spec
    §7 verificados uno a uno.

### Checkpoint K3

- [ ] Informe por flag anotado; docs al día; iniciativa cerrable.
