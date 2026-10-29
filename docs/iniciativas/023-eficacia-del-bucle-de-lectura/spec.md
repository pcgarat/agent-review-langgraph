Última modificación: 2026-10-17 | Creado: 2026-10-17

# Eficacia del bucle de lectura

> **Estado:** aprobada
>
> **Entrega objetivo:** que las tres herramientas que
> [014](../014-revisor-con-herramientas/spec.md) ya cablea (`read_file`, `grep`,
> `list_dir`) sean **eficaces**: que una búsqueda encuentre lo que el modelo pide,
> que una lectura larga se pueda acotar y **declare su corte**, y que el modelo no
> pague por lo que ya tiene. No añade herramientas: añade **modos** a las tres que
> hay.

---

## 1. Objetivo

014 dejó el bucle funcionando: 72 llamadas, 10 hallazgos publicados y cobertura 1.0
en el PR #3. La medición de su regrabación (2026-10-17) dice que las tres
herramientas rinden muy por debajo de lo que cuestan, y que el gasto **no está
donde parecía**. Esto no va de leer más, sino de que lo que se lee sirva.

Se quiere:

1. Que `grep` **encuentre lo que el modelo pide**. Hoy es literal
   (`git grep -F`, [016 Q1](../archivo/016-lector-de-contexto/spec.md)) y el modelo
   escribe expresiones regulares: **42 de 64** `grep` de la grabación llevan
   metacaracteres y devuelven **cero** coincidencias, quemando el turno entero.
2. Que una lectura **declare su corte** con el tamaño **real** y se pueda acotar.
   `read_file` sirve el fichero entero (43 de 55 sin rango; `worker/main.py`, 1.179
   líneas) y, cuando el presupuesto de bytes corta, **no lo dice**: el modelo
   concluye sobre medio fichero creyendo que lo vio entero.
3. Que no se **pierda trabajo pedido**: 14 peticiones se descartan en silencio por
   el tope de 3 por turno, en 11 de 52 turnos.
4. Que el coste se **mida antes de decidir**, y que la medida sea **posible**. El
   gasto del PR #3 es 744k tokens de *prompt* y 417k de *completion*, de los que
   384k son **razonamiento**: reenviar el contexto no sólo se paga en *prompt*, se
   vuelve a pagar en razonamiento turno a turno. Además ese reenvío consume
   presupuesto ([ADR-0019](../../adr/0019-presupuesto-de-tokens-por-review.md)) y
   puede dejar lotes sin revisar: no es sólo dinero, es **recall**.

## 2. No-objetivos

- **Herramientas nuevas.** `search` no existe como herramienta (es un alias de
  `grep`); no se añade una cuarta operación. Lo nuevo entra como **modo** de las
  tres que hay (`read_file(path, mode="outline")`), sin ensanchar el puerto. Ojo:
  **hoy el alias no está cableado** —`_serve_one` solo despacha `read_file`,
  `grep` y `list_dir`, y `_BASE_SYSTEM` no menciona `search`—, así que o se cablea
  `search → grep` (E15) o se retira la afirmación; lo que no vale es decir que
  «basta con aceptarlo en el *prompt*».
- **Ejecutar código (L3)** ni **verificar hallazgos (L2)**: son Q6 de 014 y
  [009](../archivo/009-verificador-adversarial/spec.md).
- **Cambiar el anclaje al diff ni L0** ([014 H7](../014-revisor-con-herramientas/spec.md)).
- **Especializar el *prompt* por rol (D20).** Es una carencia real —`correctness`,
  `security` y `arch` comparten `_SYSTEM` y sus hallazgos se prefijan
  `correctness`— pero no es eficacia de herramientas y hoy **no hay *gold set* que
  lo mida**: solo `correctness` está cableado en el harness. Se saca a su **propia
  iniciativa** (Q1).
- **Cambiar la estructura del contexto (mensajes, caché de prefijo, subgrafo de
  fases).** Es la iniciativa [024](../024-contexto-conversacional-del-bucle/spec.md)
  (aprobada): aquí se mide y se arregla la *interfaz* de las herramientas, no cómo
  se compone el *prompt*. 024 impone que el bloque de estado viaje **al final** del
  último mensaje de usuario (su E6) y que su bloque S anteceda a las corridas
  *live* de M1.
- **Añadir herramientas o cambiar la metodología de 006 por gusto.** `search` no
  entra como herramienta nueva (§2) y el matcher solo se toca por lo que §6 obliga:
  para poder medir. Su cambio va a ADR y a la spec de 006, pero se ejecuta **aquí**,
  como bloque 0, porque sin él esta iniciativa no tiene número (R1).

## 3. Contexto y evidencia

Medición de `tests/eval/pr_3/recorded_tools.json` (regrabación del 2026-10-17,
72 llamadas, `deepseek-v4-flash-revisor`, razonamiento medio):

| Observación | Medida |
|---|---|
| Lotes reales (`_batches`, `LLM_BATCH_MAX_CHARS=20 000`) | **19** |
| Llamadas al modelo / turnos con herramientas | 72 / 52 |
| `grep` con metacaracteres (hoy **0** coincidencias) | **42** de 64 |
| `read_file` sin rango / con rango | 43 / 12 |
| Rutas pedidas en **más de una llamada** | **22** (`domain/models.py` en 5) |
| Repeticiones **dentro** de un mismo turno | 0 |
| Peticiones descartadas por el tope de 3 | **14**, en 11 turnos |
| Tokens de *prompt* / *completion* (razonamiento) | 744k / 417k (384k) |
| *Prompt* base por lote (diff + L1) | 2,4k–18,5k tokens |

**Tres cautelas sobre esta tabla** (auditoría del 2026-10-17):

- **La grabación no guarda el orden de ejecución.** `Recording.dump` serializa
  `calls` con `sort_keys=True` y la clave es el *fingerprint*: el JSON queda
  ordenado por huella, no por lote. Agrupar «hasta una respuesta con *findings*»
  produce grupos que **no** son lotes (en el primero, el *prompt* va 2 436 → 9 679
  → 2 482, imposible en una conversación que crece). Por eso la fila de «en más de
  un lote» se sustituye por **rutas pedidas en más de una llamada**, que no depende
  del orden. Instrumentar el orden es E0e y **bloquea M1**.
- **Los 19 lotes** no salen de la grabación: se calculan con `_batches` sobre el
  diff. Son correctos.
- **La grabación **no lleva L1**. El harness invoca el grafo sin `review_context`,
  así que `pack_l1_context` devuelve vacío: el *prompt* base del lote es el
  **diff** (2,4k–18,5k tokens), no el índice L1. Consecuencia: la afirmación de que
  «L1 reenviado en los 72 turnos son ~720k de los 744k» es **falsa en esta
  grabación** (los 19 *prompts* más pequeños suman 104 587 tokens, ~14 %). Ese
  reparto solo puede darse **en producción**, donde sí hay contexto. Y el corolario
  incómodo: **M1 no puede medir `PRECACHE_CONTEXT` con este *gold set***, porque el
  caso no tiene L1 que quitar; haría falta un caso con contexto o medirlo en
  producción (Q4).
- **`grep` con metacaracteres**: en la extracción que hice para auditar el
  dialecto (46 patrones, contando alguna repetición) todos usan solo construcciones
  portables —`.` `*` `+` `?` `|` `()` `[]` y `\d`/`\w`—; **0** usan clases POSIX
  `[[:…]]` y **0** usan *lookaround*. El error de dialecto de E2 es de diseño, no
  del corpus actual; el riesgo aparece cuando el modelo escriba lo que el *prompt*
  le permita escribir.

De ahí salen cuatro lecturas que ordenan el diseño:

- **El gasto dominante es el reenvío, no el catálogo.** Cada turno reenvía la
  conversación entera ([016 W2](../archivo/016-lector-de-contexto/spec.md): no hay
  lista de mensajes). El *prompt* base de un lote es una fracción del total; el
  resto es volver a mandar lo mismo turno a turno. Añadir herramientas lo agrava.
- **El razonamiento se paga dos veces.** 384k de los 417k de *completion* son
  razonamiento: volver a mandar el contexto obliga a volver a razonarlo. Es la
  palanca de coste más grande y no es de este bloque, es de 024.
- **El caché de E/S no ahorra *tokens*.** El lector se construye **por lote**
  (`_call_with_tools` llama a la *factory* en cada uno), así que dos lotes no
  comparten ni el lector ni su caché: un caché de turno no ahorraría nada y uno de
  *review* ahorra **E/S**, no *prompt*. El modelo necesita ver los bytes igual. Lo
  que sí ahorra *prompt* es **no precargar** lo que el bucle puede leer (Q3 de 014)
  y **reducir el número de lotes**. El **caché de prefijo del proveedor** es otro
  caché, no se mide hoy y no es de esta iniciativa: es 024.
- **El modelo pide bien; el puerto responde mal.** No es que le falten
  herramientas: usa `grep` 64 veces con sintaxis regex que el puerto no entiende y
  `read_file` sin rango 43 veces. El fallo está en la interfaz, no en el catálogo.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| E0 | **D23 se arregla antes y aparte**, en los **tres** niveles del camino de un lote: `_call_once`, `_call_with_tools` y `_call_with_retries` | El `except Exception` de `_call_with_retries` (`llm_reviewer.py:402`) también traga el aborto, y con `llm_batch_retries=2` por defecto lo **reintenta con espera** y acaba en «0 hallazgos». Arreglar solo los otros dos no arregla producción |
| E0b | **La excepción vive en producción** (`ports/llm.py`), y el harness la importa de ahí | Hoy `FingerprintMismatch` está en `tests/eval/harness.py:479`: producción no puede capturarla sin depender de los tests. La dirección correcta es tests → src |
| E0c | **Solo se reintenta lo transitorio**; cualquier otro fallo sube o se propaga | El `except Exception` actual convierte un error permanente (un 400 de formato) en una review limpia tras dos reintentos. D23 es un caso de una clase |
| E0d | **El matcher mide defecto, no redacción**, y se corrige aquí (bloque 0: T1 + T2), con **tests negativos** y anclando **contra el código**, no contra la grabación | Anclar contra la grabación es circular: se ajustan las etiquetas a la salida que se quiere medir. Un mismo defecto redactado distinto puntúa distinto (`build.py:1123` es `R3` en el *baseline* y «0» con herramientas); y sin test negativo, un defecto **distinto** del mismo fichero puede casar por fragmentos y **subir** el recall |
| E0e | **La grabación guarda el orden y la unicidad de las llamadas**: `seq` global, `batch_index`, `turn` y un `call_id` (el *fingerprint* deja de ser la clave única) | Hoy `calls` es un `dict` con `sort_keys=True` y clave = *fingerprint*: el JSON pierde el orden de ejecución y **colapsa dos llamadas con el mismo prompt** (la segunda sobrescribe la primera; `ReplayLlm` ya falla con «fingerprint ya consumido»). Sin orden no se puede medir nada por lote, ni auditar el 16/5, ni reproducir M1. Se lee un `list` y se conserva el `dict` como índice |
| E0f | **La lectura devuelve un `ReadResult`**, no un `str`: `content`, `total_bytes`/`total_lines`, `truncated`, rango servido. El mismo value object sirve `read_file`, `grep` y `list_dir` (`ToolResult`), y el bucle renderiza el aviso según la operación | El tamaño real se pierde **en el adaptador** (`context_fixture` corta a `_max_bytes`; `ReadWorkspace.show_file` corta a `_MAX_READ_BYTES`; el runner devuelve solo `content`). Cambiar `read_file -> str` es un **cambio de contrato**, no un detalle: toca el puerto, los tres adaptadores, la API HTTP del runner (`FileResponse`, `GrepRequest`, `tree`), `context_workspace` y los dobles de test. Sin esto, el aviso de corte de E4 no puede ser honesto |
| E0g | **Hay cuatro superficies, no tres adaptadores**: *fixture*, VCS, workspace **y la API HTTP del runner**. Los argumentos nuevos (`path`, `ref`, `mode`, `files_only`, `glob`, `depth`) viajan de punta a punta, con **negociación de capacidad** entre worker y runner | Un worker nuevo contra un runner viejo **ignoraría `mode` en silencio**: el modelo creería que buscó en regex y recibiría literal. Eso es peor que no tener el modo. Además `_serve_*` hoy **descarta** los argumentos nuevos (`_serve_grep` solo lee `pattern`/`glob`, `_serve_read_file` no lee `mode`, `_serve_list_dir` no lee `glob`/`depth`): cambiar los adaptadores sin *forwarding* no cambia nada |
| E0h | **El matcher no resuelve solapamientos por prioridad**: una etiqueta que casa a la vez `expected` y `must_not_report` (o dos `expected`) es **error al cargar el caso**, y toda etiqueta con `file` exige al menos un **fragmento discriminante** | `_attribute` consume `expected` antes que `must_not` y `LINE_TOLERANCE=5`: un hallazgo que casa ambos **nunca** cuenta como FP. Y un test negativo con dos defectos a más de 5 líneas pasa trivialmente. El test negativo tiene que poner los dos defectos **en la misma línea** (o exigir el fragmento) |
| E1 | El alcance es la **eficacia de las tres herramientas**, no el catálogo | La medición dice que el fallo es la interfaz (regex, rangos, avisos), no la falta de operaciones |
| E2 | `grep` gana `mode: "literal" \| "regex"`, con **default `literal`**, un **subconjunto portable único** (la **misma** cadena para `git grep -E` y para `re`: literales, `.` `*` `+` `?` `\|` `()` `[]` `^` `$`, con `\d`→`[0-9]`, `\w`→`[A-Za-z0-9_]`, `\s`→ una clase con los caracteres de espacio literales), **rechazo explícito** de `[[:…]]`, `\b`, `<`/`>`, *lookaround* y retro-referencias, y **error explícito** para patrón inválido | Enmienda [016 Q1](../archivo/016-lector-de-contexto/spec.md). POSIX ERE puro **no** es un dialecto único: `[[:space:]]` en `re` es una clase con los caracteres `[ : s p a c e`, no un espacio, y `\b`/`\<` son extensiones GNU que `re` interpreta distinto. Traducir a dos emisores duplica la superficie de test; un subconjunto portable es **una** cadena, **una** tabla y **dos** pruebas. Un patrón inválido hoy devuelve «sin coincidencias», que el modelo lee como «aquí no hay nada». Enmienda 2026-10-17: `\s` no se emite como `[ \t\n\r\f\v]` —dentro de una clase, GNU grep lee `\t` como `t`, y una línea de `grep` nunca contiene `\n`— sino con esos caracteres **literales**; ver [ADR-0027](../../adr/0027-dialecto-portable-de-grep.md) |
| E3 | `grep` se acota con `path` y `glob`, gana `files_only` y declara el **`ref`** (hoy `context_vcs` mezcla `head` y `base` en el mismo resultado) | El modelo ya manda `glob` (44 casos) pero no puede acotar a un subárbol; `files_only` evita traer 200 líneas para descubrir dos rutas. Un `grep` que mezcla refs puede servir una cita de `base` como si fuera `head` |
| E4 | `read_file` **declara el corte** con el tamaño **real** que devuelve el lector (`ReadResult`), el rango servido y el consejo **propio de la operación**; `grep` y `list_dir` también declaran `mostrando N de M` y cómo acotar | Hoy `_budget_bytes` corta en silencio; `grep` corta a 200 coincidencias (`_DEFAULT_MAX_MATCHES`) y `list_dir` también se recorta, y ninguno lo dice. `M = len(content)` **mentiría**: el adaptador ya recortó antes. La media verdad es peor que un error: el modelo razona sobre lo que cree que vio |
| E5 | `read_file` gana `mode: "full" \| "outline"`: índice de símbolos con su rango | Evita leer 1.179 líneas para localizar una función; es el ahorro con mejor relación valor/coste de la lista |
| E6 | `list_dir` gana filtro por `glob` y recursión acotada (profundidad declarada) | El modelo lo usa 18 veces solo para orientarse |
| E7 | El tope de peticiones por turno **se mide**, no se sube a ciegas: `3` contra `4–5` con su flag | Subirlo baja turnos pero engorda cada uno (y el turno se reenvía): es una palanca a medir, no una mejora obvia |
| E8 | La **precarga de L1** en el bucle se vuelve conmutable (Q3 de 014) | Es la palanca grande de *prompt*: con herramientas activas, el CONTEXT precargado viaja en cada turno y el bucle puede leerlo. Se mide contra el *baseline* antes de fijarla |
| E9 | Hay **un único registro de lecturas por review** que alimenta a la vez el bloque de estado y la comprobación de citas; el caché de E/S vive también en la review y **se reinicia por review** | Hoy `served` vive en `_last_loop` y se resetea en cada lote (`llm_reviewer.py:315`, `:469`): un hallazgo que cita un fichero leído en un lote anterior se descarta como cita no servida, mientras `files_read` sí acumula. Dos verdades para el mismo hecho. Un estado por review sin reinicio filtra una review en la siguiente |
| E9b | El registro guarda **`Reading(path, ref, kind, ranges)`**, con `kind ∈ {full, range, outline, grep}` y rangos fusionados; la cita se valida por tipo y ref, y la **cobertura solo cuenta `full`/`range`** | `served` es hoy `dict[path, ref]`: un `outline` (símbolos sin cuerpo) valida una cita como si se hubiera leído, una lectura de `base` valida una cita de `head`, y `files_read` —que se deriva de `served`— cuenta un `grep` como fichero leído. Degrada H7 de 014 y la cobertura de 012 |
| E9c | Para que el bloque de estado **viaje dentro del lote**, el registro se **pasa al bucle** (`state_provider`/`ReadingRegistry`), no se renderiza en el *prompt* inicial del lote | El `LlmReviewer` construye el *prompt* inicial por lote; solo `run_tool_loop` añade los `<tool_result>`. Sin una costura, el registro de la review no puede actualizarse dentro del lote. El bloque se inserta **al final del último mensaje de usuario**, tras el `<tool_result>`, para no romper el prefijo cacheable de 024 (E6) |
| E10 | Los modos son **parámetro de las tres operaciones**; el puerto **no** gana operaciones | Mantiene el contrato de 016 y el *fingerprint* acotado a lo que cambia |
| E11 | Cada mejora entra detrás de **su flag**, declarada en `Settings`, en `.env.example` y en `operacion.md`. El *prompt* **anuncia siempre las capacidades del puerto** (modos, `ref`, `files_only`, `depth`); el flag decide **qué hace el adaptador**, no lo que el modelo sabe que existe. El criterio de compatibilidad pasa a ser **comportamiento por defecto idéntico**, no *prompt* byte a byte, y se paga **una** regrabación controlada | El criterio anterior («*prompt* byte a byte») y «el modelo usa `mode="regex"`» son **incompatibles**: si el *prompt* no anuncia el modo, los 46 patrones con metacaracteres siguen fallando y la iniciativa no arregla nada; si lo anuncia, el *fingerprint* cambia. No se puede tener las dos cosas. Se elige que la herramienta sirva, se regraba una vez (con `cached_tokens` de 024 en la misma pasada) y el diff de la grabación es revisable (ADR-0017) |
| E11b | La tabla de §5.4 declara, por flag, **«apagado ⇒ qué pasa»**, no solo su default | Un flag con default `off` que además oculta una capacidad deja la mejora sin efecto aunque esté implementada. El default define el **coste** (avisos, estado, reenvío), no la **disponibilidad** |
| E12b | Se congela un **`expected_report_tools.json`** (variante con herramientas) además del `expected_report.json` (sin herramientas) | Hoy `make eval-tools` no tiene test de regresión: solo el *replay* sin herramientas está congelado. Sin baseline de herramientas, las configuraciones de M1 no tienen contra qué compararse y una regresión en el bucle pasa en verde |
| E13 | El `CachingReader` cachea **solo `read_file`** (inmutable para un `ref` fijo) o exige al lector una **clave de pureza/semilla**; `grep`/`list_dir` no se cachean sin ella | En `VcsContextReader`, `grep` y `list_dir` dependen del *seed* y del diff de cada lote: cachear entre lotes un «sin coincidencias» del lote 1 haría perder ficheros en el 2 |
| E14 | `REVIEW_TOOLS_CALLS_PER_TURN` (por turno) y `REVIEW_TOOLS_MAX_CALLS` (total por lote) **se suben juntos** y se nombran sin ambigüedad | Hoy 3 por turno contra 8 por lote: subir a 5 agota el total en dos turnos y el flag se convierte en un espejismo. `max_calls` tiene que acompañar a la palanca que se mide |
| E15 | `search` se **cablea como alias de `grep`** en `_serve_one` (y se documenta), o **se retira** de la spec | Hoy no está en el dispatch ni en `_BASE_SYSTEM`: un modelo que emita `search` recibe «herramienta desconocida». Decir que «basta con aceptarlo en el *prompt*» es falso |
| E12 | Cada flag se clasifica **antes de medir** en «medible con *replay*» o «exige regrabación *live*» | `ReplayLlm` indexa por *fingerprint* de **cada** llamada y el bucle mete los `<tool_result>` en la conversación: cualquier cambio en lo que devuelve una herramienta rompe la grabación. Presentar «una corrida por flag» con *replay* sería vender una medida que no existe |

## 5. Diseño o contrato

### 5.1 Modos sobre tres operaciones

```json
{"tools": [
  {"tool": "grep", "pattern": "complete\\(|model_for\\(", "mode": "regex", "glob": "src/**/*.py", "path": "src/agent_review", "ref": "head", "files_only": false},
  {"tool": "search", "pattern": "model_for", "glob": "src/**/*.py"},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "mode": "outline"},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "start": 640, "end": 700},
  {"tool": "list_dir", "path": "src/agent_review", "glob": "*.py", "depth": 2}
]}
```

`mode` ausente equivale al comportamiento de hoy (`literal`, `full`), de modo que
una grabación vieja no cambia de significado. `search` es **alias** de `grep` y se
resuelve en `_serve_one` (E15), no en el puerto.

**Dialecto único y portable.** El patrón que acepta el puerto es un subconjunto
cuya semántica es **la misma** en `git grep -E` y en `re`: literales, `.`, `*`, `+`,
`?`, `|`, `()`, `[]`, `^`, `$`, y los escapes `\d` → `[0-9]`, `\w` →
`[A-Za-z0-9_]`, `\s` → una clase con los caracteres de espacio **literales**
(espacio, tabulador, CR, FF y VT). Un solo normalizador (`portable_pattern`) emite
**una** cadena canónica que ambos motores interpretan igual. Se **rechazan** con
error visible: `[[:alpha:]]` y demás clases POSIX, `\b`, `\<`, `\>`, *lookaround* y
retro-referencias. La tabla de equivalencia se prueba con el **mismo** patrón
canónico en las cuatro superficies (fixture, VCS, workspace y API del runner);
`VcsContextReader` se declara **lector parcial** y la equivalencia se predica sobre
la intersección de ficheros visibles (E0g, R3b). El contrato completo, incluida la
decisión de no emitir `[ \t\n\r\f\v]` (GNU grep lee `\t` como `t` dentro de una
clase), está en [ADR-0027](../../adr/0027-dialecto-portable-de-grep.md).

### 5.2 Aviso de corte

El lector devuelve un **`ReadResult`** (E0f), no un `str`:

```python
class ReadResult:
    content: str
    total_bytes: int | None   # tamaño REAL en bytes UTF-8, antes de rango y recorte
    truncated: bool           # el adaptador ya recortó
    served_range: tuple[int, int] | None  # rango 1-based servido, si lo hay
```

`grep` y `list_dir` devuelven un **`ToolResult(items, total, truncated)`**: `items`
lleva las coincidencias `path:line: texto` o los nombres, `total` cuenta lo que había
antes del tope y `None` significa «no medido». Se declara en
[ADR-0026](../../adr/0026-contrato-de-lectura-con-tamano-real.md).

El bucle renderiza el aviso con ese dato; si `total_bytes` no está disponible
(runner viejo, operación que no lo devuelve), lo **dice** en vez de inventarlo:

```
<tool_result>read_file src/agent_review/worker/main.py
…[cortado a 12 000 de 45 701 bytes; vuelve a pedirlo con start/end]</tool_result>
```

`grep` y `list_dir` usan el mismo `ToolResult`: `grep` avisa `mostrando 200 de N;
acota con path/glob/files_only` y `list_dir` `mostrando N de M; usa depth/glob`. El
consejo de recuperación no se comparte. Si no se puede servir lo pedido, es
preferible un error explícito a media verdad (el modelo **razona** sobre lo que cree
que leyó). El cambio toca las **cuatro superficies** de E0g: puerto, adaptadores,
`FileResponse`/`GrepRequest`/`tree` del runner y `context_workspace`; hay un test de
ida y vuelta worker ↔ runner.

### 5.3 Estado de lecturas, citas y caché

Un **único** registro por review, con lo ya servido (rutas, refs y rangos), del que
salen dos consumidores:

- el **bloque de estado** que viaja en cada turno:

```
<tool_result>LECTURAS (esta review): src/agent_review/worker/main.py [1-1179],
src/agent_review/ports/llm.py [1-120], config/models.yaml [1-192]</tool_result>
```

- la **comprobación de citas** (009 V4): «¿el revisor leyó este fichero?», no «¿lo
  leyó en este lote?».

Coste del bloque: decenas de tokens, acotado (número de entradas y caracteres).
Dueño: el `LlmReviewer` (vida de la review), no el bucle (vida del lote), porque es
donde se repiten las lecturas. Pero para que el bloque **viaje dentro del lote**, el
registro se pasa al bucle (E9c) y se inserta **al final del último mensaje de
usuario**, tras el `<tool_result>`, no en la cabeza: así el prefijo que se reenvía
sigue siendo cacheable (024 E6). El caché de E/S va en un **decorador** sobre
`ContextReaderPort` (`CachingReader`) envuelto por review y reiniciado por review:
el puerto y sus adaptadores no se tocan; solo cachea `read_file` (E13).

El `Reading` que guarda el registro incluye tipo y ref (E9b):

```python
Reading(path="src/agent_review/worker/main.py", ref="head",
        kind="range", ranges=[(640, 700)])
```

La cita se valida contra `(path, ref)` y contra el `kind`: un `outline` no da por
leído el cuerpo, un `grep` no cuenta como lectura para cobertura y una lectura de
`base` no valida una cita de `head`.

### 5.4 Variables nuevas

| Variable | Default propuesto | Qué acota | Apagado ⇒ qué pasa |
|---|---|---|---|
| `REVIEW_TOOLS_GREP_MODE` | `literal` | `literal` \| `regex` por defecto en el bucle | el adaptador sirve literal aunque el *prompt* anuncie `mode="regex"`; pedirlo explícito **sí** funciona |
| `REVIEW_TOOLS_READ_OUTLINE` | `off` | habilita `mode="outline"` | pedir `outline` cae a `full` y se avisa; el modelo no gasta una llamada en balde sin saberlo |
| `REVIEW_TOOLS_READ_STATE` | `off` | bloque de lecturas de 5.3 | no viaja el bloque; las citas se siguen validando con el registro (E9) |
| `REVIEW_TOOLS_TRUNCATION_NOTICE` | `on` | aviso de corte de 5.2 | el corte se sigue aplicando, pero **no se anuncia**; solo aceptable en una medida que quiera reproducir el *prompt* viejo |
| `REVIEW_TOOLS_STRICT_CALLS` | `off` | no descartar en silencio: error explícito por petición no atendida | el excedente se descarta con un `AVISO` en el `<tool_result>` (comportamiento de hoy) |
| `REVIEW_TOOLS_CALLS_PER_TURN` | `3` | la palanca E7 (3 contra 4–5) | — |
| `REVIEW_TOOLS_MAX_CALLS` | `8` | total de peticiones por lote; se sube junto con la anterior (E14) | — |
| `REVIEW_TOOLS_PRECACHE_CONTEXT` | `on` | L1 precargado en el bucle (E8); `off` = el bucle lee lo que necesita | el *prompt* base del lote baja; el modelo debe leerlo (cuesta turnos) |
| `REVIEW_TOOLS_IO_CACHE` | `off` | caché de E/S por review (E9, E13) | cada lote vuelve a leer del repositorio |

Los flags definen **coste y comportamiento**, no **disponibilidad**: el *prompt*
anuncia siempre las capacidades del puerto (E11). Cada una se declara en `Settings`,
se documenta comentada en `.env.example` y en la tabla de `operacion.md` §2, y se
cablea en `worker/main.py`. La compatibilidad se mide como **comportamiento por
defecto idéntico**; el *prompt* cambia una vez y se regraba (criterio 9).

### 5.5 Clasificación de la medida (E12)

Cada flag se clasifica **antes** de medir. La regla: si cambia lo que recibe el
modelo (coincidencias, contenido servido, avisos, número de turnos o *prompt* base)
exige **regrabación *live***; si sirve los mismos bytes, vale ***replay*** sobre la
misma grabación.

| Flag | ¿Cambia lo que recibe el modelo? | Cómo se mide |
|---|---|---|
| **Anuncio de capacidades en el *prompt*** (E11) | **Sí, una vez**: el *prompt* base cambia respecto a hoy | **Una** regrabación *live* de referencia, compartida por todas las configuraciones; el diff es revisable (ADR-0017) y trae `cached_tokens` de 024 |
| `REVIEW_TOOLS_GREP_MODE` | **Sí**: cambia las coincidencias que devuelve `grep` | *Live*: el modelo reacciona a otro `tool_result` |
| `REVIEW_TOOLS_READ_OUTLINE` | **Sí**: `outline` sirve un índice (o un aviso) en vez de `full` | *Live* |
| `REVIEW_TOOLS_READ_STATE` | **Sí**: añade el bloque `LECTURAS` al último mensaje | *Live* |
| `REVIEW_TOOLS_TRUNCATION_NOTICE` | **Sí**: añade el aviso de corte al `tool_result` | *Live* |
| `REVIEW_TOOLS_STRICT_CALLS` | **Sí**: cambia el `<tool_result>` de una petición no atendida | *Live* |
| `REVIEW_TOOLS_CALLS_PER_TURN` | **Sí**: cambia cuántas peticiones se atienden por turno | *Live* (va junto con `MAX_CALLS`, E14) |
| `REVIEW_TOOLS_MAX_CALLS` | **Sí**: cambia cuándo se agota el lote | *Live* |
| `REVIEW_TOOLS_PRECACHE_CONTEXT` | **Sí**: cambia el *prompt* base del lote | *Live* |
| `REVIEW_TOOLS_IO_CACHE` | **No**: sirve los mismos bytes, solo evita la E/S | *Replay* sobre la misma grabación, con contador de E/S |

El informe de M1 declara, por configuración, si es *replay* o *live*; presentar un
*replay* como medida de una mejora que cambia los resultados es medir el pasado. La
comparación se hace contra **dos** baselines congelados (E12b): sin herramientas
(`expected_report.json`) y con herramientas (`expected_report_tools.json`).

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **La medida no mide lo que cree.** El *baseline* sin herramientas da `recall 0.375` (3 TP de 8, `expected_report.json`, 19 llamadas); la variante **con herramientas** da `0.125` (1 TP, `recorded_tools.json`, 72 llamadas, reproducible con `make eval-tools`). Ese descenso es el problema de la iniciativa, pero su valor exacto no es fiable mientras el matcher esté anclado a redacción | **Antes de medir** (bloque 0): T1 cambia el ancla del matcher a ubicación/identidad con su ADR y enmienda la spec de 006, y T2 reancla las etiquetas **contra el código del caso (`008da57`, `fixtures/head`)** y refija los **dos** *baselines*. La grabación solo descubre redacciones, no decide qué es un defecto. **No se sustituye 0.125 por 0.375**: son configuraciones distintas |
| R1b | **El ancla estructural sube el recall por la puerta de atrás**: un defecto distinto del mismo fichero casa por fragmentos, o `expected` gana a `must_not` por orden de atribución | **Tests negativos** en T1 con los dos defectos **en la misma línea**; toda etiqueta con `file` exige fragmento discriminante; solapamiento `expected`/`must_not` es **error al cargar** (E0h) |
| R2 | **D23**: una grabación caducada se lee como recall 0 en verde | E0; `make eval-tools` debe fallar ruidosamente si el *fingerprint* no casa, **también** con `batch_retries > 0` |
| R2b | Otro error permanente se confunde con una medida mala: en *replay* acaba en `FAILED`, baja la cobertura y `make eval` sale en verde; en producción se publica «0 hallazgos» sin decir que el lote no se revisó | E0c + **R2c**: en producción el lote sale `FAILED` y la review **parcial** (021 R14, 012 C7), con el log corregido a «lote no revisado»; en *replay* el eval **aborta** si algo queda `FAILED` |
| R2c | **Un `FAILED` en *replay* es indistinguible de una caída real de recall** | T0c: el invariante se declara en el borde de la evaluación (`Run.skipped`), no con un flag de producción; `FAILED` aborta y `BUDGET` no (es legítimo y se cuenta) |
| R3 | **Regex caro** (R4 de 016): un patrón que explota en el `git grep` del runner | Default `literal`; `mode: "regex"` explícito, acotado por `path`/`glob` y con tope de coincidencias y de tiempo |
| R3b | **Dialecto divergente**: el *replay* casa y producción no (o al revés) | E2: **subconjunto portable único** (misma cadena para `re` y `git grep -E`), rechazo explícito de POSIX y extensiones GNU, y tabla de equivalencia probada en las **cuatro** superficies |
| R3c | **El runner viejo ignora los campos nuevos** y el modelo cree que buscó en regex | E0g: negociación de capacidad worker ↔ runner; si el runner no soporta un campo, **error explícito**, no degradación silenciosa |
| R4 | **`outline` opina mal** en lenguajes sin parser (YAML, Dockerfile) | Índice por AST donde exista y por heurística declarada donde no; si no puede indexar, **no cae a `full`**: lo dice y sugiere `start/end` |
| R5 | **Más avisos, más *prompt*** (E4, E9): el bloque de estado viaja en cada turno | Bloques acotados (decenas de tokens) y medidos con 006; cada uno detrás de su flag |
| R6 | **Invalidación de grabaciones**: cada flag cambiar el *prompt* obliga a regrabar | Es el comportamiento de [ADR-0017](../../adr/0017-replay-determinista-con-fingerprint.md); E12 separa lo que exige *live* de lo que se mide con *replay*, y cada configuración medida se declara |
| R7 | **La mejora no se nota** y el bucle sigue igual de caro | La entrega se juzga por el informe de cada flag contra el *baseline*, no por el número de mejoras |
| R8 | **El aviso de corte miente**: `M` es el tamaño ya recortado por el adaptador | E0f: `ReadResult` con `total_bytes` real desde el lector; el runner lo devuelve; test de ida y vuelta worker ↔ runner |
| R9 | **El estado por review filtra entre reviews** | E9: dueño el `LlmReviewer`, reinicio explícito por review (test) |
| R10 | **La medida por lote no es reproducible**: la grabación no guarda orden ni unicidad | E0e: `seq`/`batch_index`/`turn`/`call_id`; hasta entonces, M1 no se declara hecho y la tabla de §3 usa solo medidas que no dependen del orden |
| R11 | **El *forwarding* se olvida**: el adaptador soporta el modo y el bucle lo descarta | E0g: test de contrato que verifica que **cada** argumento documentado llega al adaptador |
| R12 | **Una cita de `outline` o de `base` se valida como lectura completa de `head`** | E9b: `Reading(path, ref, kind, ranges)`; cobertura solo con `full`/`range` |
| R13 | **El eco del bloque de estado engorda el *prompt*** | Tope de entradas y caracteres, inserción al final (E9c) y medida con 006 |

## 7. Criterios de hecho

1. `grep` con `mode="regex"` casa `complete\(|model_for\(` en el *gold set* y en el
   *fixture*; con `mode` ausente el resultado es idéntico a hoy (test).
2. El **mismo patrón canónico** da el mismo resultado en las **cuatro superficies**
   (*fixture*, VCS, workspace y API del runner), probado con la misma tabla; sobre
   `VcsContextReader` la equivalencia se predica de la **intersección** de ficheros
   visibles (lector parcial declarado); un patrón inválido, no portable (`[[:…]]`,
   `\b`, *lookaround*, retro-referencias) o no soportado devuelve **error
   explícito**, no «sin coincidencias» (test).
3. Un `grep` acotado por `path`/`glob`/`ref` no devuelve coincidencias de fuera ni
   mezcla `head` con `base`; con `files_only` devuelve solo rutas (test).
4. Un `read_file` cortado por presupuesto lleva su aviso de corte con el **tamaño
   real** del fichero y el rango servido; `grep` y `list_dir` declaran
   `mostrando N de M` y el consejo de **su** operación (test).
5. `read_file(mode="outline")` devuelve los símbolos con su rango y una lectura
   posterior del rango devuelve el cuerpo; si no puede indexar, lo dice y **no**
   sirve el fichero entero como sustituto silencioso (test).
6. Un fichero leído en un lote **anterior** de la misma review cuenta como servido
   para la cita, y el estado **se reinicia** entre reviews (test).
7. El bloque de estado aparece en el turno siguiente a una lectura, va **al final
   del último mensaje de usuario** (prefijo cacheable) y no crece sin límite (test).
8. Con `REVIEW_TOOLS_STRICT_CALLS`, una petición de más recibe error explícito y
   **no** se descarta en silencio (test).
9. **Con todos los flags en su default, el comportamiento servido es idéntico al de
   hoy** (mismos resultados para los mismos argumentos), y el *prompt* base cambia
   **una sola vez** por el anuncio de capacidades: esa regrabación está declarada y
   su diff es revisable. No se exige «*prompt* byte a byte» (E11).
10. Cada flag está en `Settings`, en `.env.example` y en `operacion.md` §2, y
    cableado en `worker/main.py`; la tabla de §5.4 dice qué pasa con cada uno
    **apagado** (test de contrato de configuración si existe).
11. El informe de 006 queda anotado por configuración, **declarando si es *replay*
    o *live***, con *prompt*, *completion*, **razonamiento** y recall; y con las
    **lecturas por modo** (cuántas veces el modelo pide `outline` o `regex`).
12. El informe dice cuántos lotes se **omiten por presupuesto** (ADR-0019): el
    reenvío es también un riesgo de recall.
13. **D23 resuelto y verificado**: con una grabación caducada, `make eval-tools`
    termina en error —también con `batch_retries=2`— y no en un informe con recall
    0 (test).
13b. **Un error permanente no se publica como review limpia**, en ninguno de los
    dos contextos: en *replay* el eval **aborta** si algún fichero queda en
    `SkipReason.FAILED` (un `BUDGET` no aborta: se cuenta, criterio 12); en
    producción el lote queda `FAILED`, la review es **parcial** y los demás lotes
    siguen publicando (tests de T0c y T0d). `_call_with_retries` solo relanza
    `FingerprintMismatch`; el resto va a `_last_error` + lote fallido.
14. **El matcher mide defecto**: dos redacciones distintas del mismo defecto casan
    la **misma** etiqueta y dos defectos distintos **en la misma línea** del mismo
    fichero **no** casan la misma; una etiqueta que casa a la vez `expected` y
    `must_not_report` es **error al cargar** (test positivo, negativo y de carga);
    las etiquetas del PR #3 reancladas **contra el código del caso** dan un informe
    estable y los **dos** *baselines* quedan refijados con comparación etiqueta por
    etiqueta (T2).
15. **La grabación conserva el orden y la unicidad**: `seq`, `batch_index`, `turn`
    y `call_id`; un *replay* reconstruye los 19 lotes y dos llamadas con el mismo
    *prompt* **no** se colapsan (test).
16. **Cada argumento documentado llega al adaptador**: test de contrato de
    *forwarding* para `grep` (`mode`, `path`, `ref`, `glob`, `files_only`),
    `read_file` (`mode`, `start`, `end`) y `list_dir` (`glob`, `depth`); `search`
    resuelve a `grep` o no se anuncia.
17. **El tamaño real cruza las cuatro superficies**: un `read_file` en el runner
    devuelve `total_bytes` real (no el recortado), y el test de ida y vuelta
    worker ↔ runner lo comprueba.
18. **La cita distingue tipo y ref**: un `outline` no valida una cita de cuerpo, un
    `grep` no cuenta como fichero leído para cobertura y una lectura de `base` no
    valida una cita de `head` (test).
19. **Hay dos *baselines* congelados**, sin herramientas y con herramientas
    (`expected_report_tools.json`), y `make eval-tools` falla si el *replay* con
    herramientas se desvía (test de regresión).
20. `make ci` y `make docs-check` en verde; documentos vivos al día
    ([`arquitectura.md`](../../arquitectura.md) §7 si D23 se cierra,
    [`operacion.md`](../../operacion.md) §2 con las variables de §5.4,
    `.env.example`, [`desarrollo.md`](../../desarrollo.md) §2–§3 y las preguntas
    abiertas de [014](../014-revisor-con-herramientas/spec.md) que aquí se
    resuelven).

## 8. Preguntas abiertas

- **Q1 — ¿El *prompt* por rol entra aquí o es su propia iniciativa?**
  *Resuelta (2026-10-17):* **su propia iniciativa**. D20 está registrada «sin
  iniciativa»: `correctness`, `security` y `arch` comparten `_SYSTEM` y el hallazgo
  se prefija `correctness` para las tres. Es una carencia real, pero el *harness*
  solo corre `correctness`, así que especializar `security`/`arch` **no se puede
  medir** con el *gold set* de hoy. La vía es la que no engorda esta spec: hacer el
  *prompt* **declarativo por rol** (como `config/models.yaml` declara el modelo por
  rol) con su caso de *gold set* propio.
- **Q2 — ¿`outline` necesita el runner o basta el `VcsContextReader`?** Depende de
  dónde se construya el índice (AST en memoria sobre los bytes ya servidos, o en el
  workspace). Decide si `outline` funciona en la degradación de 014 H3.
- **Q3 — ¿El tope de peticiones por turno se mide con 3 y 4–5, o se fija por
  evidencia previa?** Recomendación: medir las dos; es la palanca que más fácil se
  supone y la que más fácil engorda el *prompt*.
- **Q4 — ¿`REVIEW_TOOLS_PRECACHE_CONTEXT=off` (E8) sube o baja el recall?** Es la
  pregunta de 014 Q3, ahora con número. **Aviso (2026-10-17):** con el *gold set*
  actual **no se puede responder**: el harness corre el grafo sin `review_context`,
  así que no hay L1 que quitar y el flag no cambia el *prompt*. Para medirlo hace
  falta un caso con contexto (o medir en producción). Si baja el recall más de lo
  que baja el coste, se queda en `on` y el ahorro hay que buscarlo en el fan-out.
- **Q5 — ¿Cuánto del coste de medir 023 se lleva 024?**
  *Resuelta (2026-10-17):* **la instrumentación del caché de 024 (su bloque S) va
  antes que las corridas *live* de M1.** S no cambia el *prompt* ni el *fingerprint*
  (solo añade un contador al `usage` y una columna al informe), así que no invalida
  ninguna grabación; hacerla primero convierte las corridas de pago de M1 en la
  medida de caché de 024 **y** en el *baseline* de esta iniciativa, con una sola
  pasada. Si se hicieran al revés, habría que repetirlas o el número de caché se
  quedaría en `no medido`. El cambio de puerto de 024 (bloque A) sigue esperando a
  K3, porque ese sí mueve el *baseline*.
- **Q6 — ¿El aviso de corte debe incluir también el rango ya servido** (`[1-400]`)
  para que el modelo no vuelva a pedir lo mismo? Es barato y evita una llamada.
  Se decide con el informe de M1.
- **Q7 — ¿`ReadResult` o un `stat_file` aparte?**
  *Recomendación:* `ReadResult`/`ToolResult` común (E0f). Un `stat_file` ahorra
  tocar decenas de tests que comparan `read_file(...) == "texto"`, pero deja dos
  fuentes de verdad para el tamaño y no resuelve el aviso de `grep`/`list_dir`.
  El coste de migrar los tests es mecánico; el de arrastrar dos APIs no.
- **Q8 — ¿`mode="regex"` en memoria necesita timeout?** `re` no lo tiene. Las
  opciones son `regex` con timeout, un subproceso, o **llevar todo el `grep` al
  runner** y que los adaptadores en memoria deleguen. Se decide con el coste de
  la tabla de equivalencia: la tercera opción elimina el dialecto divergente de
  raíz, pero hace que `fixture`/`vcs` dependan del binario de git.
