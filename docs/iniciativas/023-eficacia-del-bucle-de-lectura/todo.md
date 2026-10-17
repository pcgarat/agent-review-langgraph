Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Eficacia del bucle de lectura

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **No se empieza el bloque 0.5 hasta que K0 esté verde** (sin
D23, sin orden en la grabación, sin matcher y sin la clasificación *replay*/*live*,
cada mejora hace salir el eval en verde con recall 0), y **no se empieza el bloque 1
hasta que K0.5 esté verde** (sin *forwarding* y sin `ReadResult`, los modos no llegan
al adaptador y el aviso de corte no tiene tamaño real que anunciar).

## Bloque 0 — Base de medida (checkpoint K0)

- [x] **T0** D23: el *replay* con *fingerprint* caducado **aborta** (E0, E0b, E0c)
  - `ports/llm.py`: mover `FingerprintMismatch` desde `tests/eval/harness.py:479`
    (tests → src). El harness la importa de ahí; no se duplica.
  - `adapters/llm_reviewer.py`: `except FingerprintMismatch: raise` **antes** del
    genérico en los **tres** sitios: `_call_once` (~:607), `_call_with_tools`
    (~:536) y **`_call_with_retries` (`:402`)**. El tercero es el que corre en
    producción con `llm_batch_retries=2`.
  - `_call_with_retries`: sustituir `except Exception` por la política correcta:
    `FingerprintMismatch` **relanza**; un error permanente **no se reintenta** y
    deja `_last_error` + `None` (el lote sale `FAILED`, la review queda parcial: 021
    R14 y 012 C7). **No** relanzar todo lo permanente: el nodo `persona` lo
    re-relanza (`graph/build.py:787`) y tumba la review entera, perdiendo los demás
    lotes. Lo transitorio se reintenta como hoy.
  - Los dos `logger.warning(... "0 hallazgos")` de `_call_once`/`_call_with_tools`
    dicen «0 hallazgos» cuando el lote **no se revisó**: corregir el mensaje a
    «lote no revisado (FAILED)».
  - Aceptación: con una grabación caducada, `make eval-tools` termina en **error
    ruidoso**, no en un informe con recall 0; la marca de D23 en
    `arquitectura.md` §7 pasa a resuelta.
  - Verifica: `tests/test_llm_reviewer.py` con **`batch_retries=2`** (un test con 0
    pasa aunque el bug siga vivo) y `tests/eval/test_eval_harness.py`.
  - Ficheros: `ports/llm.py`, `adapters/llm_reviewer.py`,
    `docs/arquitectura.md` §7.

- [x] **T0c** En *replay*, un `FAILED` **aborta** la medida (R2c, criterio 13)
  - El invariante va en el **borde de la evaluación**, no en un flag de producción:
    `Run` (harness) pasa a llevar `skipped`; `evaluate`/`scripts/eval.py` **abortan
    con mensaje** si algún fichero queda en `SkipReason.FAILED` en modo `replay`.
  - `SkipReason.BUDGET` **no** aborta: es legítimo y ya se cuenta (criterio 12).
  - Motivo: hoy un error permanente distinto de `FingerprintMismatch` (grabación
    corrupta, `KeyError`, `ValueError` de parseo) acaba en `FAILED`, baja
    `coverage` y `make eval-tools` sale con código 0. Es la misma clase que D23 y
    T0 no la cubre.
  - Aceptación: un *replay* con un lote forzado a `FAILED` termina en error; un
    *replay* con `BUDGET` no.
  - Verifica: `tests/eval/test_eval_harness.py`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/harness.py`, `scripts/eval.py`.

- [x] **T0d** Invariante de producción: un error permanente **no** tumba la review
  - Test que fija el comportamiento deliberado: un fallo permanente en **un** lote
    de varios lo deja `FAILED` y parcial, y **los demás lotes siguen** publicando
    sus hallazgos; la review **no** se cae.
  - Test complementario: `FingerprintMismatch` **sí** aborta (es fallo de medida).
  - Aceptación: los dos comportamientos quedan clavados por test, para que nadie
    «arregle» el primero convirtiéndolo en aborto.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_graph_*.py` (cobertura
    parcial).
  - Ficheros: `tests/test_llm_reviewer.py`, `tests/test_graph_*.py`.

- [x] **T3** La grabación guarda **orden y unicidad** (E0e, R10)
  - `tests/eval/harness.py`: además de `calls` (índice por *fingerprint*), guardar
    una lista ordenada `order` con `{call_id, fingerprint, role, batch_index, turn}`
    por llamada. `call_id` único (contador), no el *fingerprint*: dos llamadas con el
    mismo *prompt* hoy se **colapsan** en el `dict` y el *replay* falla con
    «fingerprint ya consumido».
  - `Recording.dump`/`load`: `sort_keys=True` puede quedarse para el índice, pero la
    lista de orden se serializa tal cual; `load` de una grabación sin `order` la
    marca como **no medible por lote** (no se inventa).
  - Aceptación: una grabación reconoce los **19 lotes** y el orden dentro de cada
    uno; el *replay* consume cada llamada una vez; el informe puede agrupar por
    `batch_index`.
  - Verifica: `tests/eval/test_eval_harness.py`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/harness.py`, `tests/eval/pr_3/recorded_tools.json` (nuevo
    campo), `docs/arquitectura.md` §7 (D28).
  - Hecho: el formato, el *replay* por llamada y el `BatchTracker` (escucha el
    `start` del lote, sin tocar el *prompt* ni el *fingerprint*). Las grabaciones
    actuales siguen en el formato viejo y se declaran **no medibles por lote**; la
    regrabación que estrena el formato es la **única** de F1.

- [x] **F1** Flags de verdad: `Settings`, `.env.example` y *prompt* de capacidades
  (E11, E11b, criterio 9)
  - `settings.py`: las 9 variables de la spec §5.4 (hoy solo existen 4
    `REVIEW_TOOLS_*`; faltan `MAX_CALLS`, `CALLS_PER_TURN`, `STRICT_CALLS`,
    `READ_STATE`, `PRECACHE_CONTEXT`, `IO_CACHE`, `READ_OUTLINE`,
    `TRUNCATION_NOTICE`, `GREP_MODE`), con su `Secret`/metadata si aplica.
  - `.env.example`: cada variable comentada, junto a las de 014.
  - `worker/main.py`: cableado hasta `ContextLimits`/`LlmReviewer`; ninguna flag
    leída solo en el bucle.
  - `adapters/tool_loop.py`: `_BASE_SYSTEM` **anuncia siempre las capacidades del
    puerto** (modos, `ref`, `files_only`, `depth`); los flags cambian el
    **comportamiento** del adaptador, no lo que el modelo sabe que existe. Esto
    cambia el *prompt* **una vez** y exige **una** regrabación de referencia.
  - Aceptación: test de **comportamiento por defecto** (mismos resultados para los
    mismos argumentos), no de *prompt* byte a byte; el diff de la grabación de
    referencia queda revisado y anotado; `operacion.md` §2 con la tabla completa
    (incluida la columna «apagado ⇒ qué pasa»).
  - Verifica: `tests/test_settings.py`, `tests/test_tool_loop.py`.
  - Ficheros: `settings.py`, `.env.example`, `worker/main.py`,
    `adapters/tool_loop.py`, `docs/operacion.md` §2.
  - Hecho: los topes (`ContextLimits`, con `calls_per_turn`) y el comportamiento
    (`ReviewToolsOptions`) van separados; `worker/main.py` es el único que lee
    `Settings` y lo pasa al `LlmReviewer`. `CALLS_PER_TURN` y `STRICT_CALLS` ya
    actúan; `GREP_MODE`, `READ_OUTLINE`, `READ_STATE`, `TRUNCATION_NOTICE`,
    `PRECACHE_CONTEXT` e `IO_CACHE` quedan declarados y cableados, pero su efecto
    llega con G1/R1/S1/C1 (anotado en `operacion.md` §2). El *prompt* de
    capacidades cambia **una vez** (anuncia `mode`, `ref`, `files_only` y `depth`):
    la grabación de `pr_3` queda **caducada** a propósito y `make eval-tools` falla
    con `FingerprintMismatch` (T0) hasta la **única** regrabación *live*, que es la
    corrida compartida de [024 LB1](../024-contexto-conversacional-del-bucle/todo.md)
    tras su bloque S. Los tests no tocan byte a byte el *prompt*: fijan el
    **comportamiento** con los defaults y que el anuncio no dependa de los flags.

- [x] **T1** ADR: el matcher mide **defecto**, no redacción (E0d, E0h, R1/R1b)
  - ADR nuevo con el **siguiente número libre al escribirlo** (hoy sería el 0025):
    ancla por `file`(+`line`, tolerancia ADR-0014)
    como primaria y fragmentos como respaldo/desambiguación. Debe declarar la
    **regla de puntuación**: desempate entre etiquetas y qué pasa si un hallazgo
    casa una `expected` y una `must_not_report` (**error al cargar el caso**, no
    prioridad silenciosa: hoy `_attribute` consume `expected` antes que `must_not`
    y un solapamiento nunca cuenta como FP).
  - Toda etiqueta con `file` exige al menos un **fragmento discriminante**: sin él,
    `file`+`line` (±5) basta para casar dos defectos distintos.
  - `tests/eval/harness.py`: `_matches`/`_attribute` según el ADR; validación del
    caso al cargar; enmienda con fecha en la spec de
    [006](../archivo/006-eval-de-la-review/spec.md).
  - Aceptación: **test positivo** (dos redacciones del mismo defecto casan la misma
    etiqueta), **negativo** (dos defectos distintos **en la misma línea** del mismo
    fichero **no** casan la misma) y **de carga** (solapamiento
    `expected`/`must_not` falla con un mensaje claro).
  - Verifica: `tests/eval/test_eval_harness.py`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/harness.py`, `docs/adr/`, spec de 006.
  - Hecho (2026-10-17): ADR-0025. `file`+`line` (±5) es el ancla primaria y basta
    **un** fragmento discriminante; sin línea el `match` sigue siendo conjuntivo
    (un fichero solo no ancla: el hallazgo de `settings.py:230` casaba
    `integracion-2`, `integracion-3` y `R6` a la vez). La carga valida `id` único,
    `line` sin `file`, etiqueta sin fragmento y `expected`/`must_not` indistinguibles;
    al medir, un hallazgo que casa ambas clases **falla** con los dos `id` (no hay
    prioridad de `expected`). La atribución 1:1 puntúa por fragmentos casados y
    desempata por orden declarado y orden canónico del hallazgo. Tests: dos
    redacciones del mismo defecto, dos defectos distintos en la misma línea y las
    tres validaciones de carga. Enmienda con fecha en la spec de 006 y §2 de
    `desarrollo.md`.

- [ ] **T2** Reancla de etiquetas y **dos** *baselines* firmes (R1, E12b)
  - `tests/eval/pr_3/labels.yaml`: reanclar **contra el código de `008da57`** (el
    `head_sha` del caso, servido por `fixtures/head`), no contra la grabación. La
    grabación solo descubre redacciones alternativas; no decide qué es un defecto.
    Casos a revisar: `docker-compose.yml:76` es `integracion-1` **y**
    `integracion-2` (¿el mismo defecto?); `build.py:1123` es `R3`;
    `worker/main.py:152` es `no-detectado-1`; `fixworker/main.py:95` es la etiqueta
    `A` (hoy `match: [mark_done]` + `must_mention`: reforzar el fragmento para el
    ancla nueva).
  - `expected_report.json`: refijar el *baseline* **sin herramientas** tras T1,
    comparando **etiqueta por etiqueta** contra el informe anterior para que el
    refijado no baje el listón sin que se note.
  - `expected_report_tools.json`: congelar el *baseline* **con herramientas** (hoy
    no existe). Su `recall` actual es **0.125** (1 TP de 8, reproducible con
    `make eval-tools`), frente a **0.375** del *replay* sin herramientas: son
    configuraciones distintas y **no se sustituye uno por otro**.
  - Aceptación: `make eval` y `make eval-tools` producen informes **estables**,
    reproducibles y comparables contra **su** *baseline*; un test falla si el
    *replay* con herramientas se desvía.
  - Verifica: `make eval`, `make eval-tools`, `tests/eval/test_eval_pr_3.py`.
  - Ficheros: `tests/eval/pr_3/labels.yaml`, `tests/eval/pr_3/expected_report.json`,
    `tests/eval/pr_3/expected_report_tools.json`, `tests/eval/test_eval_pr_3.py`.
  - Hecho (2026-10-17), a medias: etiquetas reancladas **contra `fixtures/head`**:
    `A` (`fixworker/main.py:95`, entrada de `_consume`), `I` (`:166`, `except
    OSError`), `integracion-1` (`docker-compose.yml:72`, `env_file`),
    `integracion-2` (`:76`, `TWG_BBC_TOKEN` vacío), `integracion-3`
    (`settings.py:155`), `no-detectado-1` (`worker/main.py:152`),
    `no-detectado-2` (`openai_llm.py:122`) y `R6` (`settings.py:230`).
    `integracion-1` e `integracion-2` **no** son el mismo defecto: uno es fuga de
    secretos y el otro un fallo de arranque; comparten línea de síntoma, no causa, y
    por eso van anclados en líneas distintas. `expected_report.json` no cambió
    **ni un byte**: el informe etiqueta por etiqueta es idéntico al anterior
    (`matched`, `missing`, `counts` y `metrics`), así que el refijado no baja el
    listón. `expected_report_tools.json` **sigue bloqueado**: `recorded_tools.json`
    quedó caducada a propósito con el *prompt* de capacidades (F1) y la única
    regrabación *live* es la corrida compartida de 024 LB1 tras su bloque S; C0c
    hereda la tarea cuando exista esa grabación.

- [x] **M0** Clasificación *replay*/*live* por flag (E12, §5.5)
  - Anotar en la spec o aquí, por cada flag de §5.4, si cambia lo que recibe el
    modelo. Regla: si cambia coincidencias, contenido servido, avisos o número de
    turnos, **exige regrabación *live***; si sirve los mismos bytes, vale *replay*.
  - Aceptación: ningún flag de M1 queda sin clasificar; el informe de M1 declara
    *replay* o *live* por configuración.
  - Ficheros: `docs/iniciativas/023-…/spec.md` §5.5, `todo.md` (M1).
  - Hecho (2026-10-17): §5.5 clasifica **cada** flag de §5.4 con su fila: los nueve
    que cambian lo que recibe el modelo (`GREP_MODE`, `READ_OUTLINE`, `READ_STATE`,
    `TRUNCATION_NOTICE`, `STRICT_CALLS`, `CALLS_PER_TURN`, `MAX_CALLS`,
    `PRECACHE_CONTEXT` y el anuncio de capacidades) exigen *live*; solo `IO_CACHE`
    se mide en *replay* con contador de E/S.

### Checkpoint K0

- [x] Grabación caducada ⇒ error, no recall 0, **con `batch_retries=2`**.
- [x] Un error permanente **nunca** se publica como review limpia: en *replay*
  aborta la medida (T0c) y en producción el lote queda `FAILED` y la review
  **parcial** (T0d), sin tumbar los demás lotes.
- [x] `_call_with_retries` **no** relanza lo permanente: solo `FingerprintMismatch`.
- [x] La grabación conserva el **orden** (19 lotes) y la **unicidad** (dos llamadas
  con el mismo *prompt* no se colapsan).
- [x] El informe no depende de la redacción del mismo defecto y **no** sube el
  recall con defectos distintos **en la misma línea**.
- [x] Los flags existen en `Settings` y `.env.example`; con todo en default, el
  **comportamiento servido** es el de hoy y la regrabación del *prompt* de
  capacidades es única y está anotada.
- [x] Cada flag clasificado como *replay* o *live*.
- [x] `make ci` en verde.

## Bloque 0.5 — Contrato de lectura (checkpoint K0.5)

- [ ] **C0** `ReadResult`/`ToolResult` + ADR de contrato (E0f, R8)
  - `ports/context.py`: `ReadResult(content, total_bytes, truncated,
    served_range)`; `grep`/`list_dir` devuelven `ToolResult(matches|entries,
    total, truncated)` con el mismo patrón.
  - ADR nuevo (siguiente número libre): por qué un value object y no un `str`; qué
    pasa cuando `total_bytes` no está disponible (se declara `None`, **no** se
    inventa).
  - **Unidades explícitas**: `total_bytes` cuenta **bytes UTF-8**, no `len(text)`
    (hoy los tres topes y el aviso mezclan caracteres con bytes). Se nombra y se
    documenta; el corte no parte una línea a mitad.
  - **`_slice` se unifica**: hoy está triplicado (`context_fixture`, `context_vcs`,
    `read_workspace`) y no valida `start=0`, negativos ni `end < start`. Una sola
    función compartida con tests en las tres superficies.
  - Migrar el puerto, los tres adaptadores y los dobles; el bucle renderiza con el
    resultado, no con `len(content)`.
  - Aceptación: el puerto solo devuelve value objects; ningún `read_file(...) ==
    "texto"` sobrevive en tests; `make ci` en verde.
  - Verifica: `tests/test_context_ports.py`, `tests/test_context_*.py`,
    `tests/test_tool_loop.py`.
  - Ficheros: `ports/context.py`, `adapters/context_*.py`,
    `adapters/tool_loop.py`, dobles de test.

- [ ] **C0b** *Forwarding* y las **cuatro** superficies (E0g, R3c, R11)
  - `adapters/tool_loop.py`: `_serve_grep` lee `mode`, `path`, `ref`, `glob`,
    `files_only`; `_serve_read_file` lee `mode`; `_serve_list_dir` lee `glob`,
    `depth`. Hoy los **descarta**: cambiar el adaptador no basta.
  - `sandbox_runner/main.py`: `GrepRequest` gana `path`, `ref`, `mode`,
    `files_only`; `FileResponse` gana `total_bytes`; `tree` gana `glob` y `depth`.
  - `adapters/context_workspace.py`: envía y lee los campos nuevos.
  - **Negociación de capacidad**: si el runner no soporta un campo, `ERROR`
    explícito; nunca degradar en silencio.
  - **Errores tipados**: `_serve_one` hoy convierte cualquier excepción en «la
    herramienta X falló». Un `ToolError` con mensaje útil llega al modelo; el
    `except Exception` genérico se reserva para lo inesperado (y se registra).
  - `search` → `grep` en `_serve_one`, o se retira de la spec (E15).
  - Aceptación: test de contrato que verifica que **cada** argumento documentado
    llega al adaptador y de ahí al runner; test de ida y vuelta worker ↔ runner.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_workspace.py`,
    `tests/test_sandbox_runner.py` (o equivalente).
  - Ficheros: `adapters/tool_loop.py`, `sandbox_runner/main.py`,
    `adapters/context_workspace.py`, `ports/context.py`.

- [ ] **C0c** *Baseline* congelado con herramientas (E12b)
  - `tests/eval/pr_3/expected_report_tools.json` generado con `make eval-tools`
    tras T1/T2 (matcher arreglado y etiquetas reancladas).
  - Test de regresión: `make eval-tools` reproduce el *baseline* byte a byte; si no,
    falla con el diff etiqueta por etiqueta.
  - Aceptación: existe y se compara; el 0.125 queda **declarado** como la variante
    con herramientas (frente a 0.375 sin herramientas), no como sustituto.
  - Verifica: `tests/eval/test_eval_pr_3.py`, `make eval-tools`.
  - Ficheros: `tests/eval/pr_3/expected_report_tools.json`,
    `tests/eval/test_eval_pr_3.py`.

### Checkpoint K0.5

- [ ] `read_file` devuelve el **tamaño real** desde el runner (test de ida y vuelta).
- [ ] Cada argumento documentado llega al adaptador (test de contrato por argumento).
- [ ] `search` resuelve a `grep` o no se anuncia en el *prompt*.
- [ ] Hay **dos** *baselines* congelados y `make eval-tools` falla si se desvía.
- [ ] `make ci` en verde.

## Bloque 1 — Modos de las tres herramientas (checkpoint K1)

- [ ] **G1** `grep` con `mode`, `path`/`ref`, `files_only` y dialecto portable
  (E2, E3, E15)
  - `ports/context.py`: `grep(pattern, *, glob=None, path=None, ref="head",
    mode="literal", files_only=False)`. **Subconjunto portable único**: el
    normalizador emite **una** cadena válida en `re` y en `git grep -E`
    (`\d`→`[0-9]`, `\w`→`[A-Za-z0-9_]`, `\s`→`[ \t\n\r\f\v]`). **Rechazo
    explícito** de `[[:…]]`, `\b`, `\<`/`\>`, *lookaround* y retro-referencias.
    **No** traducir `\s` a `[[:space:]]`: en `re` no significa espacio.
  - `adapters/tool_loop.py`: `_serve_grep` acepta y **reenvía** los parámetros (C0b).
    El *system prompt* anuncia `mode` siempre (F1); el flag `GREP_MODE` decide el
    default de la operación, no la disponibilidad.
  - Topes: `files_only` devuelve solo rutas; en `regex`, tope de coincidencias y de
    tiempo (mitiga R3 de 016). Si en memoria no hay timeout (Q8), se decide y se
    documenta.
  - **Patrón inválido o no portable ⇒ `ERROR` explícito**, nunca «sin
    coincidencias» (hoy `git grep` devuelve código 2 y el adaptador lo traga).
  - `context_vcs.grep`: distinguir `head` de `base` (hoy recorre `self._cache` y
    mezcla refs) y honrar `path`. Declarar el lector como **parcial**.
  - **Semántica de `glob` unificada**: hoy `context_fixture` usa `matches_any`,
    `context_vcs` usa `path_matches` y el runner usa `pathspec` de git. Se define
    si es relativo a `path` o a la raíz y se prueba la **matriz** (`glob` solo,
    `path` + `glob`, `**`) en las cuatro superficies.
  - Aceptación: `mode="regex"` casa `complete\(|model_for\(`; `mode` ausente es
    idéntico a hoy; un `path` acota de verdad; **el mismo patrón canónico da el
    mismo resultado en las cuatro superficies** (VCS sobre la intersección visible).
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`.
  - Ficheros: `ports/context.py`, los tres adaptadores, `adapters/tool_loop.py`,
    `sandbox_runner/main.py`, `adapters/context_workspace.py`.
  - ADR nuevo (número siguiente libre): dialecto portable y política de
    rechazo.

- [ ] **R1** `read_file` con aviso de corte real y `mode="outline"` (E4, E5, R8)
  - El **origen de `total_bytes`** es C0: el lector devuelve `ReadResult` con el
    tamaño real, no `len(content)`. El runner (`FileResponse`) lo transporta desde
    `ReadWorkspace` **antes** de recortar.
  - Arreglar `ReadWorkspace._max_bytes`: hoy se asigna y **nunca se usa**;
    `show_file` corta con `_MAX_READ_BYTES` (`:89`) y el manager no pasa el
    parámetro. Un tope por adaptador y otro por bucle, sin mezclarse (D24).
  - `_budget_bytes`: aviso **por operación** (`start/end` en `read_file`,
    `path`/`glob`/`files_only` en `grep`, `glob`/`depth` en `list_dir`).
  - `grep` y `list_dir` también declaran `mostrando N de M`: hoy `grep` corta a 200
    coincidencias y nadie lo dice.
  - `mode="outline"`: índice de símbolos con rango de líneas (AST donde exista;
    heurística declarada donde no). Si no puede indexar, **lo dice** y sugiere
    `start/end`; **no** sirve el fichero entero como sustituto silencioso.
  - Aceptación: corte anunciado con el tamaño real del fichero; `outline` da rangos
    y el `read_file` posterior de ese rango da el cuerpo; fichero no indexable ⇒
    aviso, no lectura completa.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_context_*.py`,
    `tests/test_read_workspace.py`, `tests/test_sandbox_runner.py`.
  - Ficheros: `ports/context.py`, adaptadores,
    `sandbox_runner/read_workspace.py`, `sandbox_runner/main.py`,
    `adapters/context_workspace.py`, `adapters/tool_loop.py`.

- [ ] **L1** `list_dir` con `glob` y `depth` (E6, M4)
  - `ports/context.py`: `list_dir(path, *, glob=None, depth=1)`; recursión acotada.
  - Filtrar `is_denied` en **todos** los niveles (hoy no se aplica en `list_dir` ni
    en `tree`) y distinguir **directorio vacío** de **inexistente** (hoy ambos
    devuelven `[]` y el bucle dice «directorio vacío o inexistente»).
  - Aceptación: `depth=2` lista dos niveles; `glob="*.py"` filtra; `depth=1` es
    idéntico a hoy; una ruta vetada no aparece; vacío ≠ inexistente.
  - Verifica: `tests/test_context_*.py`, `tests/test_tool_loop.py`.
  - Ficheros: `ports/context.py`, adaptadores, `sandbox_runner/main.py`.

### Checkpoint K1

- [ ] `mode`/`depth` ausentes ⇒ comportamiento idéntico a hoy (criterio 1).
- [ ] Mismo patrón canónico ⇒ mismo resultado en las **cuatro** superficies (VCS
  sobre la intersección); patrón inválido o no portable ⇒ error.
- [ ] El aviso de corte lleva el tamaño real y el consejo de su operación; `grep` y
  `list_dir` declaran `mostrando N de M`.
- [ ] `outline` no cae a `full` en silencio cuando no puede indexar.
- [ ] `make ci` en verde.

## Bloque 2 — Palancas transversales (checkpoint K2)

- [ ] **P1** Tope de peticiones por turno como palanca medida (E7, E14)
  - `_MAX_CALLS_PER_TURN` deja de ser constante: `ContextLimits.calls_per_turn` +
    ajuste `REVIEW_TOOLS_CALLS_PER_TURN` (default 3).
  - **`REVIEW_TOOLS_MAX_CALLS` (total por lote, hoy 8) se sube junto con el tope por
    turno**: con 5 por turno y 8 totales, el límite se agota en dos turnos y la
    palanca no mide lo que dice. Nombres sin ambigüedad: `CALLS_PER_TURN` frente a
    `MAX_CALLS`.
  - Aceptación: con 4–5 el aviso de descarte desaparece **y** el total no se agota
    antes; la corrida mide *prompt* y recall contra el valor 3 (con *live*, M0).
  - Verifica: `tests/test_tool_loop.py`; informe anotado (M1).
  - Ficheros: `adapters/tool_loop.py`, `ports/context.py`, `settings.py`,
    `.env.example`.

- [ ] **S1** No descartar en silencio + estado + `Reading` por review (E9, E9b, E9c)
  - `REVIEW_TOOLS_STRICT_CALLS`: cada petición no atendida recibe un
    `<tool_result>ERROR: no atendida…</tool_result>` explícito.
  - **Un registro de lecturas por review** (`Reading(path, ref, kind, ranges)`,
    `kind ∈ {full, range, outline, grep}`) que alimente a la vez el bloque
    `LECTURAS (esta review)` y la comprobación de citas de 009 V4. Hoy `served` es
    `dict[path, ref]` y se resetea por lote (`:315`, `:469`): una cita a un fichero
    de otro lote se marca como no servida, un `outline` valida una cita de cuerpo y
    un `grep` cuenta como fichero leído.
  - **Costura dentro del lote**: el registro se pasa a `run_tool_loop`
    (`state_provider`/`ReadingRegistry`); el bloque se inserta **al final del último
    mensaje de usuario**, tras el `<tool_result>`, no en el *prompt* inicial (024
    E6: prefijo cacheable).
  - **Reinicio explícito por review**: el `LlmReviewer` vacía el registro al
    empezar; si no, una review valida citas de la anterior.
  - Cobertura (`files_read`) solo con `kind` `full`/`range`.
  - Aceptación: error explícito por petición no atendida; un fichero leído en un
    lote anterior cuenta como servido; `outline`/`grep`/`base` **no** validan una
    cita de cuerpo de `head`; el bloque aparece tras una lectura, va al final, no
    crece sin límite y se reinicia entre reviews.
  - Verifica: `tests/test_tool_loop.py`, `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/tool_loop.py`, `adapters/llm_reviewer.py`,
    `ports/context.py` (si el registro entra ahí).

- [ ] **C1** Caché de E/S por review y precarga de L1 conmutable (E8, E13)
  - `CachingReader` (decorador de `ContextReaderPort`) instanciado en el
    `LlmReviewer` (el lector se construye por lote), `REVIEW_TOOLS_IO_CACHE`.
    **Ahorra E/S, no *prompt*** (la spec lo dice).
  - **Cachea solo `read_file`** (inmutable para un `ref` fijo) o exige una clave de
    pureza/semilla al lector. En `VcsContextReader`, `grep`/`list_dir` dependen del
    *seed* de cada lote: cachear un «sin coincidencias» haría perder ficheros.
  - `REVIEW_TOOLS_PRECACHE_CONTEXT`: `on`/`off` de la precarga L1 en el bucle.
  - Aceptación: con el caché, un mismo fichero en dos lotes se lee una vez (contador
    de E/S); **dos lotes con semillas distintas no comparten `grep`**; con
    `precache=off` el *prompt* base del lote baja.
  - Verifica: `tests/test_llm_reviewer.py`.
  - Ficheros: `adapters/llm_reviewer.py`, adaptadores de contexto.

### Checkpoint K2

- [ ] Cada palanca con su flag; con todos en default, comportamiento de hoy.
- [ ] `calls_per_turn` y `max_calls` se mueven juntos; subir uno solo no agota el
  total.
- [ ] `Reading` distingue `full`/`range`/`outline`/`grep` y `ref`; cobertura solo
  `full`/`range`.
- [ ] El caché de E/S no comparte `grep` entre lotes con semillas distintas.
- [ ] `make ci` en verde.

## Bloque 3 — Medida, docs y cierre (checkpoint K3)

- [ ] **M1** Una corrida por configuración, con *replay*/*live* declarado
  (criterio 11, R7 de la spec)
  - **Prerrequisitos:** T3 (orden y unicidad de la grabación), C0c (baseline con
    herramientas) y el bloque S de
    [024](../024-contexto-conversacional-del-bucle/todo.md) (instrumentar
    `cached_tokens`). Sin T3 no se puede agrupar por lote; sin C0c no hay contra qué
    comparar la variante con herramientas; sin 024 S el caché queda en `no medido`.
    La corrida con todos los flags en default es **la misma** que la de 024 LB1:
    una sola pasada, anotada en los dos sitios.
  - Por cada flag de §5.4: `make eval-tools` si es *replay* (caché de E/S) o
    regrabación *live* si cambia lo que recibe el modelo (M0), y los números
    anotados aquí o en la spec. La **regrabación de referencia** del *prompt* de
    capacidades (F1) se hace una vez y se usa como punto de partida.
  - **Aviso de alcance**: el *gold set* **no** tiene `review_context`, así que
    `PRECACHE_CONTEXT` no se puede medir con él (Q4). O se añade un caso con
    contexto, o ese flag se mide en producción y el informe lo declara `no medido`.
  - El informe desglosa ***prompt***, ***completion*** y **razonamiento**, no solo
    `usage`; y cuenta **lecturas por modo** (cuántas veces el modelo pide `outline`
    o `regex`), para saber si la palanca se usa.
  - Anotar **lotes omitidos por presupuesto** (ADR-0019): el reenvío también cuesta
    recall.
  - Aceptación: informe con *prompt*, *completion*, razonamiento, recall y lotes
    omitidos por configuración; la decisión de cada default se justifica con el
    número; la tabla declara `config | replay/live | prompt | completion |
    reasoning | recall | lotes omitidos`.
  - *Humano / medida:* fijar los defaults de §5.4 tras leer el informe.
  - Verifica: `make eval-tools` / `make eval-live`, informe anotado.

- [ ] **D1** Documentos vivos y cierre (criterio 20)
  - `operacion.md` §2: las variables de §5.4 con la columna «apagado ⇒ qué pasa».
  - `.env.example`: las mismas, comentadas (regla del repo al tocar `Settings`).
  - `desarrollo.md` §2 (zonas de tests: equivalencia de las cuatro superficies,
    aviso de corte, `Reading` por review, forwarding) y §3 (modos del puerto,
    `ReadResult`/`ToolResult` y `CachingReader`).
  - `CONTEXT.md`: «modo» de lectura, «estado de lecturas», «lector parcial»,
    «capacidad del runner».
  - `arquitectura.md` §7: D23 resuelta (T0); D24 (`_max_bytes`), D25 (`served` por
    lote), D26 (`usage` agregado), D27 (conversación sin turnos del *assistant*),
    D28 (orden de grabación) y D29 (contrato de lectura) resueltas o reclasificadas;
    §3–§5 si la firma del puerto de contexto cambia.
  - [014](../014-revisor-con-herramientas/spec.md): enmendar con fecha sus Q3/Q4 y
    anotar RH11.
  - Spec → `implementada`; fila de 023 en `docs/iniciativas/README.md`.
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–20 de la spec
    §7 verificados uno a uno.

### Checkpoint K3

- [ ] Informe por configuración anotado, con *replay*/*live* declarado, razonamiento
  desglosado y comparado contra el baseline que corresponda.
- [ ] Docs al día; iniciativa cerrable.
