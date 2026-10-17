Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Eficacia del bucle de lectura

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **No se empieza el bloque 2 (G1/R1/L1) hasta que K0 esté
verde**: sin D23, sin el matcher y sin la clasificación *replay*/*live*, cada
mejora hace salir el eval en verde con recall 0 (§3 de la spec).

## Bloque 0 — Base de medida (checkpoint K0)

- [ ] **T0** D23: el *replay* con *fingerprint* caducado **aborta** (E0, E0b, E0c)
  - `ports/llm.py`: mover `FingerprintMismatch` desde `tests/eval/harness.py:479`
    (tests → src). El harness la importa de ahí; no se duplica.
  - `adapters/llm_reviewer.py`: `except FingerprintMismatch: raise` **antes** del
    genérico en los **tres** sitios: `_call_once` (~:607), `_call_with_tools`
    (~:536) y **`_call_with_retries` (`:402`)**. El tercero es el que corre en
    producción con `llm_batch_retries=2`.
  - `_call_with_retries`: sustituir `except Exception` por `is_transient(exc)`; lo
    permanente sube (E0c: D23 es un caso de una clase más ancha).
  - Aceptación: con una grabación caducada, `make eval-tools` termina en **error
    ruidoso**, no en un informe con recall 0; la marca de D23 en
    `arquitectura.md` §7 pasa a resuelta.
  - Verifica: `tests/test_llm_reviewer.py` con **`batch_retries=2`** (un test con 0
    pasa aunque el bug siga vivo) y `tests/eval/test_eval_harness.py`.
  - Ficheros: `ports/llm.py`, `adapters/llm_reviewer.py`,
    `docs/arquitectura.md` §7.

- [ ] **F1** Flags de verdad: `Settings`, `.env.example` y *prompt* condicional
  (E11, criterio 9)
  - `settings.py`: las 8 variables de la spec §5.4 (hoy solo existen 4
    `REVIEW_TOOLS_*`), con su `Secret`/metadata si aplica.
  - `.env.example`: cada variable comentada, junto a las de 014.
  - `worker/main.py`: cableado hasta `ContextLimits`/`LlmReviewer`; ninguna flag
    leída solo en el bucle.
  - `adapters/tool_loop.py`: el texto de `_BASE_SYSTEM` (modos, estado, aviso) es
    **condicional** al flag. Con todo en default, el *prompt* es byte a byte el de
    hoy.
  - Aceptación: test que compara el *prompt* completo con todos los flags en su
    default contra el de `014` (criterio 9); `operacion.md` §2 con la tabla.
  - Verifica: `tests/test_settings.py` (o el de contrato de configuración),
    `tests/test_tool_loop.py`.
  - Ficheros: `settings.py`, `.env.example`, `worker/main.py`,
    `adapters/tool_loop.py`, `docs/operacion.md` §2.

- [ ] **T1** ADR: el matcher mide **defecto**, no redacción (E0d/R1/R1b)
  - ADR nuevo (`docs/adr/0025-…`): ancla por `file`(+`line`, tolerancia ADR-0014)
    como primaria y fragmentos como respaldo/desambiguación. Debe declarar la
    **regla de puntuación**: desempate entre etiquetas y qué pasa si un hallazgo
    casa una `expected` y una `must_not_report`.
  - `tests/eval/harness.py`: `_matches`/`_attribute` según el ADR; enmienda con
    fecha en la spec de [006](../archivo/006-eval-de-la-review/spec.md).
  - Aceptación: **test positivo** (dos redacciones del mismo defecto casan la misma
    etiqueta) y **test negativo** (dos defectos distintos del mismo fichero **no**
    casan la misma); una etiqueta con `file` sin `line` exige además todos sus
    fragmentos.
  - Verifica: `tests/eval/test_eval_harness.py`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/harness.py`, `docs/adr/`, spec de 006.

- [ ] **T2** Reancla de etiquetas y *baseline* firme (R1)
  - `tests/eval/pr_3/labels.yaml`: reanclar **contra el código de `008da57`**, no
    contra la grabación. La grabación solo descubre redacciones alternativas; no
    decide qué es un defecto. Casos a revisar: `docker-compose.yml:76` es
    `integracion-1` **y** `integracion-2` (¿el mismo defecto?); `build.py:1123` es
    `R3`; `worker/main.py:152` es `no-detectado-1`; `fixworker/main.py:95` es la
    etiqueta `A` (mal anclada: es un FP distinto del `A` real).
  - `expected_report.json`: refijar el *baseline* del *replay* sin herramientas tras
    T1 (el informe cambia de números porque el matcher cambia), comparando
    **etiqueta por etiqueta** contra el informe anterior para que el refijado no
    baje el listón sin que se note.
  - Aceptación: `make eval` y `make eval-tools` producen informes **estables** y
    reproducibles; el *baseline* queda registrado con la comparación.
  - Verifica: `make eval`, `make eval-tools`.
  - Ficheros: `tests/eval/pr_3/labels.yaml`, `tests/eval/pr_3/expected_report.json`.

- [ ] **M0** Clasificación *replay*/*live* por flag (E12, §5.5)
  - Anotar en la spec o aquí, por cada flag de §5.4, si cambia lo que recibe el
    modelo. Regla: si cambia coincidencias, contenido servido, avisos o número de
    turnos, **exige regrabación *live***; si sirve los mismos bytes, vale *replay*.
  - Aceptación: ningún flag de M1 queda sin clasificar; el informe de M1 declara
    *replay* o *live* por configuración.
  - Ficheros: `docs/iniciativas/023-…/spec.md` §5.5, `todo.md` (M1).

### Checkpoint K0

- [ ] Grabación caducada ⇒ error, no recall 0, **con `batch_retries=2`**.
- [ ] Un error permanente no se publica como «0 hallazgos».
- [ ] El informe no depende de la redacción del mismo defecto y **no** sube el
  recall con defectos distintos del mismo fichero.
- [ ] Los flags existen en `Settings` y `.env.example`; con todo en default, *prompt*
  idéntico al de hoy.
- [ ] Cada flag clasificado como *replay* o *live*.
- [ ] `make ci` en verde.

## Bloque 1 — Modos de las tres herramientas (checkpoint K1)

- [ ] **G1** `grep` con `mode`, `path`/`ref`, `files_only` y dialecto único
  (E2, E3)
  - `ports/context.py`: `grep(pattern, *, glob=None, path=None, ref="head",
    mode="literal", files_only=False)`. **Dialecto POSIX ERE** en los tres
    adaptadores; traducción declarada de `\d`/`\w`/`\s`; rechazo explícito de
    *lookaround* y retro-referencias.
  - `adapters/tool_loop.py`: `_serve_grep` acepta y traduce los parámetros; el
    *system prompt* documenta `mode` y su default **solo si el flag lo activa**.
  - Topes: `files_only` devuelve solo rutas; en `regex`, tope de coincidencias y de
    tiempo (mitiga R3 de 016).
  - **Patrón inválido o no soportado ⇒ `ERROR` explícito**, nunca «sin
    coincidencias» (hoy `git grep` devuelve código 2 y el adaptador lo traga).
  - `context_vcs.grep`: distinguir `head` de `base` (hoy recorre `self._cache` y
    mezcla refs) y honrar `path`.
  - Aceptación: `mode="regex"` casa `complete\(|model_for\(`; `mode` ausente es
    idéntico a hoy; un `path` acota de verdad; **el mismo patrón da el mismo
    resultado en `fixture`, `vcs` y `workspace`** (tabla de equivalencia).
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`.
  - Ficheros: `ports/context.py`, los tres adaptadores, `adapters/tool_loop.py`.
  - ADR nuevo (número siguiente libre): dialecto y política de traducción/rechazo.

- [ ] **R1** `read_file` con aviso de corte real y `mode="outline"` (E4, E5, R8)
  - Decidir el **origen de `total_bytes`**: el lector devuelve metadatos de la
    lectura (`total_bytes`, `truncado`, rango servido), no solo el texto. `M =
    len(content)` mentiría porque el adaptador ya recortó antes.
  - Arreglar `ReadWorkspace._max_bytes`: hoy se asigna y **nunca se usa**;
    `show_file` corta con `_MAX_READ_BYTES` (`:89`) y el manager no pasa el
    parámetro. Un tope por adaptador y otro por bucle, sin mezclarse.
  - `_budget_bytes`: aviso **por operación** (`start/end` en `read_file`,
    `path`/`glob` en `grep`, `glob`/`depth` en `list_dir`).
  - `mode="outline"`: índice de símbolos con rango de líneas (AST donde exista;
    heurística declarada donde no; cae a `full` si duda).
  - Aceptación: corte anunciado con el tamaño real del fichero; `outline` da rangos
    y el `read_file` posterior de ese rango da el cuerpo.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`,
    `tests/test_read_workspace.py`.
  - Ficheros: `ports/context.py`, adaptadores,
    `sandbox_runner/read_workspace.py`, `adapters/tool_loop.py`.

- [ ] **L1** `list_dir` con `glob` y `depth` (E6)
  - `ports/context.py`: `list_dir(path, *, glob=None, depth=1)`; recursión acotada.
  - Aceptación: `depth=2` lista dos niveles; `glob="*.py"` filtra; `depth=1` es
    idéntico a hoy.
  - Verifica: `tests/test_context_*.py`, `tests/test_tool_loop.py`.
  - Ficheros: `ports/context.py`, adaptadores.

### Checkpoint K1

- [ ] `mode`/`depth` ausentes ⇒ comportamiento idéntico a hoy (criterio 1).
- [ ] Mismo patrón ⇒ mismo resultado en los tres adaptadores; patrón inválido ⇒ error.
- [ ] El aviso de corte lleva el tamaño real y el consejo de su operación.
- [ ] `make ci` en verde.

## Bloque 2 — Palancas transversales (checkpoint K2)

- [ ] **P1** Tope de peticiones por turno como palanca medida (E7)
  - `_MAX_CALLS_PER_TURN` deja de ser constante: `ContextLimits.calls_per_turn` +
    ajuste `REVIEW_TOOLS_CALLS_PER_TURN` (default 3).
  - Aceptación: con 4–5 el aviso de descarte desaparece; la corrida mide *prompt*
    y recall contra el valor 3 (con *live*, M0).
  - Verifica: `tests/test_tool_loop.py`; informe anotado (M1).
  - Ficheros: `adapters/tool_loop.py`, `ports/context.py`, `settings.py`.

- [ ] **S1** No descartar en silencio + estado + `served` único por review (E9)
  - `REVIEW_TOOLS_STRICT_CALLS`: cada petición no atendida recibe un
    `<tool_result>ERROR: no atendida…</tool_result>` explícito.
  - **Un registro de lecturas por review** (rutas, refs y rangos) que alimente a la
    vez el bloque `LECTURAS (esta review)` y la comprobación de citas de 009 V4.
    Hoy `served` vive en `_last_loop` y se resetea por lote (`:315`, `:469`), así
    que una cita a un fichero de otro lote se marca como no servida mientras
    `files_read` sí acumula.
  - **Reinicio explícito por review**: el `LlmReviewer` vacía el registro al
    empezar; si no, una review valida citas de la anterior.
  - Aceptación: error explícito por petición no atendida; un fichero leído en un
    lote anterior cuenta como servido; el bloque aparece tras una lectura, no crece
    sin límite y se reinicia entre reviews.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/tool_loop.py`, `adapters/llm_reviewer.py`.

- [ ] **C1** Caché de E/S por review y precarga de L1 conmutable (E8, E9)
  - `CachingReader` (decorador de `ContextReaderPort`) instanciado en el
    `LlmReviewer` (el lector se construye por lote), `REVIEW_TOOLS_IO_CACHE`.
    **Ahorra E/S, no *prompt*** (la spec lo dice).
  - `REVIEW_TOOLS_PRECACHE_CONTEXT`: `on`/`off` de la precarga L1 en el bucle.
  - Aceptación: con el caché, un mismo fichero en dos lotes se lee una vez (contador
    de E/S); con `precache=off` el *prompt* base del lote baja.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/llm_reviewer.py`, adaptadores de contexto.

### Checkpoint K2

- [ ] Cada palanca con su flag; con todos en default, comportamiento de hoy.
- [ ] `served` único por review y reinicio entre reviews.
- [ ] `make ci` en verde.

## Bloque 3 — Medida, docs y cierre (checkpoint K3)

- [ ] **M1** Una corrida por configuración, con *replay*/*live* declarado
  (criterio 11, R7 de la spec)
  - Por cada flag de §5.4: `make eval-tools` si es *replay* (caché de E/S) o
    regrabación *live* si cambia lo que recibe el modelo (M0), y los números
    anotados aquí o en la spec.
  - El informe desglosa ***prompt***, ***completion*** y **razonamiento**, no solo
    `usage`; y cuenta **lecturas por modo** (cuántas veces el modelo pide `outline`
    o `regex`), para saber si la palanca se usa.
  - Anotar **lotes omitidos por presupuesto** (ADR-0019): el reenvío también cuesta
    recall.
  - Aceptación: informe con *prompt*, *completion*, razonamiento, recall y lotes
    omitidos por configuración; la decisión de cada default se justifica con el
    número.
  - *Humano / medida:* fijar los defaults de §5.4 tras leer el informe.
  - Verifica: `make eval-tools` / `make eval-live`, informe anotado.

- [ ] **D1** Documentos vivos y cierre (criterio 15)
  - `operacion.md` §2: las variables de §5.4.
  - `.env.example`: las mismas, comentadas (regla del repo al tocar `Settings`).
  - `desarrollo.md` §2 (zonas de tests: equivalencia de adaptadores, aviso de
    corte, estado por review) y §3 (modos del puerto y `CachingReader`).
  - `CONTEXT.md`: «modo» de lectura si es término nuevo; «estado de lecturas».
  - `arquitectura.md` §7: D23 resuelta (T0); `_max_bytes` y `served` por lote
    (D24/D25) resueltas; §3–§5 si la firma del puerto de contexto cambia.
  - [014](../014-revisor-con-herramientas/spec.md): enmendar con fecha sus Q3/Q4 y
    anotar RH11.
  - Spec → `implementada`; fila de 023 en `docs/iniciativas/README.md`.
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–15 de la spec
    §7 verificados uno a uno.

### Checkpoint K3

- [ ] Informe por configuración anotado, con *replay*/*live* declarado y
  razonamiento desglosado.
- [ ] Docs al día; iniciativa cerrable.
