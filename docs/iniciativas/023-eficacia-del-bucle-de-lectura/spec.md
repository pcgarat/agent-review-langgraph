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
  tres que hay (`read_file(path, mode="outline")`), sin ensanchar el puerto.
- **Ejecutar código (L3)** ni **verificar hallazgos (L2)**: son Q6 de 014 y
  [009](../archivo/009-verificador-adversarial/spec.md).
- **Cambiar el anclaje al diff ni L0** ([014 H7](../014-revisor-con-herramientas/spec.md)).
- **Especializar el *prompt* por rol (D20).** Es una carencia real —`correctness`,
  `security` y `arch` comparten `_SYSTEM` y sus hallazgos se prefijan
  `correctness`— pero no es eficacia de herramientas y hoy **no hay *gold set* que
  lo mida**: solo `correctness` está cableado en el harness. Se saca a su **propia
  iniciativa** (Q1).
- **Cambiar la estructura del contexto (mensajes, caché de prefijo, subgrafo de
  fases).** Es la iniciativa 024, pendiente de especificar: aquí se mide y se
  arregla la *interfaz* de las herramientas, no cómo se compone el *prompt*.
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
| `grep` con metacaracteres ERE (hoy **0** coincidencias) | **42** de 64 |
| `read_file` sin rango / con rango | 43 / 12 |
| Ficheros leídos en más de un lote | 16 (`domain/models.py` en 5) |
| Repeticiones **dentro** de un mismo turno | 0 |
| Peticiones descartadas por el tope de 3 | **14**, en 11 turnos |
| Tokens de *prompt* / *completion* (razonamiento) | 744k / 417k (384k) |
| *Prompt* base por lote (diff + L1) | 2,4k–18,5k tokens |

De ahí salen cuatro lecturas que ordenan el diseño:

- **El gasto dominante es el reenvío, no el catálogo.** Cada turno reenvía la
  conversación entera ([016 W2](../archivo/016-lector-de-contexto/spec.md): no hay
  lista de mensajes). El *prompt* base de un lote es una fracción del total; el
  resto es volver a mandar lo mismo turno a turno. Añadir herramientas lo agrava.
- **El razonamiento se paga dos veces.** 384k de los 417k de *completion* son
  razonamiento: volver a mandar el contexto obliga a volver a razonarlo. Es la
  palanca de coste más grande y no es de este bloque, es de 024.
- **El caché de E/S no ahorra *tokens*.** Las repeticiones son **entre lotes** (el
  lector se construye por lote en `_call_with_tools`), así que un caché de turno no
  ahorraría nada. Pero uno de *review* tampoco: cada lote es una conversación nueva
  y el modelo necesita ver los bytes igual. Ahorra E/S, no *prompt*. Lo que sí
  ahorra *prompt* es **no precargar** lo que el bucle puede leer (Q3 de 014) y
  **reducir el número de lotes**. El **caché de prefijo del proveedor** es otro
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
| E1 | El alcance es la **eficacia de las tres herramientas**, no el catálogo | La medición dice que el fallo es la interfaz (regex, rangos, avisos), no la falta de operaciones |
| E2 | `grep` gana `mode: "literal" \| "regex"`, con **default `literal`**, un **único dialecto** (POSIX ERE) probado en los tres adaptadores, **traducción declarada** de los escapes comunes (`\d`, `\w`, `\s`) y **error explícito** para lo no soportado y para el patrón inválido | Enmienda [016 Q1](../archivo/016-lector-de-contexto/spec.md). Metido a medias (`re` en memoria, `git grep -E` en el runner) el mismo patrón se comporta distinto: `\d` casaría en el *replay* y no en producción, y los números del *gold set* dejarían de valer. Un patrón inválido hoy devuelve «sin coincidencias», que el modelo lee como «aquí no hay nada» |
| E3 | `grep` se acota con `path` y `glob`, gana `files_only` y declara el **`ref`** (hoy `context_vcs` mezcla `head` y `base` en el mismo resultado) | El modelo ya manda `glob` (44 casos) pero no puede acotar a un subárbol; `files_only` evita traer 200 líneas para descubrir dos rutas. Un `grep` que mezcla refs puede servir una cita de `base` como si fuera `head` |
| E4 | `read_file` **declara el truncado** con el tamaño **real**: `…[cortado a N de M bytes; usa start/end]`, y el aviso depende de la operación | Hoy `_budget_bytes` corta en silencio y el modelo concluye sobre medio fichero. `M = len(content)` **mentiría**: el adaptador ya puede haber recortado antes (`context_fixture` a 200 000, `ReadWorkspace` a 500 000). El consejo de recuperación de `grep` y `list_dir` no es `start/end` |
| E5 | `read_file` gana `mode: "full" \| "outline"`: índice de símbolos con su rango | Evita leer 1.179 líneas para localizar una función; es el ahorro con mejor relación valor/coste de la lista |
| E6 | `list_dir` gana filtro por `glob` y recursión acotada (profundidad declarada) | El modelo lo usa 18 veces solo para orientarse |
| E7 | El tope de peticiones por turno **se mide**, no se sube a ciegas: `3` contra `4–5` con su flag | Subirlo baja turnos pero engorda cada uno (y el turno se reenvía): es una palanca a medir, no una mejora obvia |
| E8 | La **precarga de L1** en el bucle se vuelve conmutable (Q3 de 014) | Es la palanca grande de *prompt*: con herramientas activas, el CONTEXT precargado viaja en cada turno y el bucle puede leerlo. Se mide contra el *baseline* antes de fijarla |
| E9 | Hay **un único registro de lecturas por review** que alimenta a la vez el bloque de estado y la comprobación de citas; el caché de E/S vive también en la review y **se reinicia por review** | Hoy `served` vive en `_last_loop` y se resetea en cada lote (`llm_reviewer.py:315`, `:469`): un hallazgo que cita un fichero leído en un lote anterior se descarta como cita no servida, mientras `files_read` sí acumula. Dos verdades para el mismo hecho. Un estado por review sin reinicio filtra una review en la siguiente |
| E10 | Los modos son **parámetro de las tres operaciones**; el puerto **no** gana operaciones | Mantiene el contrato de 016 y el *fingerprint* acotado a lo que cambia |
| E11 | Cada mejora entra detrás de **su flag**, declarada en `Settings`, en `.env.example` y en `operacion.md`, **y el texto del *prompt* es condicional al flag** | Con todos los flags en su default el *prompt* debe ser **byte a byte** el de hoy: si el `_BASE_SYSTEM` anuncia los modos de forma incondicional, cambia el *fingerprint* y el criterio 9 deja de ser verdad. Sin tarea de `Settings` los flags no existen en producción |
| E12 | Cada flag se clasifica **antes de medir** en «medible con *replay*» o «exige regrabación *live*» | `ReplayLlm` indexa por *fingerprint* de **cada** llamada y el bucle mete los `<tool_result>` en la conversación: cualquier cambio en lo que devuelve una herramienta rompe la grabación. Presentar «una corrida por flag» con *replay* sería vender una medida que no existe |

## 5. Diseño o contrato

### 5.1 Modos sobre tres operaciones

```json
{"tools": [
  {"tool": "grep", "pattern": "complete\\(|model_for\\(", "mode": "regex", "glob": "src/**/*.py", "path": "src/agent_review", "ref": "head", "files_only": false},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "mode": "outline"},
  {"tool": "read_file", "path": "src/agent_review/worker/main.py", "start": 640, "end": 700},
  {"tool": "list_dir", "path": "src/agent_review", "glob": "*.py", "depth": 2}
]}
```

`mode` ausente equivale al comportamiento de hoy (`literal`, `full`), de modo que
una grabación vieja no cambia de significado. **Dialecto único:** el contrato del
puerto es POSIX ERE; el borde traduce los escapes comunes (`\d` → `[0-9]`,
`\w` → `[[:alnum:]_]`, `\s` → `[[:space:]]`) y **rechaza** lo que ERE no tiene
(*lookaround*, retro-referencias) con un error que el modelo ve. Los tres
adaptadores se prueban con la misma tabla de patrones (equivalencia
*fixture* ↔ *vcs* ↔ *workspace*).

### 5.2 Aviso de corte

`_budget_bytes` (o su sucesor) deja de cortar en silencio y recibe el tamaño real
del fichero, no el del trozo ya recortado por el adaptador:

```
<tool_result>read_file src/agent_review/worker/main.py
…[cortado a 12 000 de 45 701 bytes; vuelve a pedirlo con start/end]</tool_result>
```

`grep` avisa con `path`/`glob`/`files_only` y `list_dir` con `glob`/`depth`: el
consejo de recuperación no se comparte. Si no se puede servir lo pedido, es
preferible un error explícito a media verdad (el modelo **razona** sobre lo que cree
que leyó).

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

Coste del bloque: decenas de tokens. Dueño: el `LlmReviewer` (vida de la review),
no el bucle (vida del lote), porque es donde se repiten las lecturas. El caché de
E/S va en un **decorador** sobre `ContextReaderPort` (`CachingReader`) envuelto por
review y reiniciado por review: el puerto y sus tres adaptadores no se tocan.

### 5.4 Variables nuevas

| Variable | Default propuesto | Qué acota |
|---|---|---|
| `REVIEW_TOOLS_GREP_MODE` | `literal` | `literal` \| `regex` por defecto en el bucle |
| `REVIEW_TOOLS_READ_OUTLINE` | `off` | habilita `mode="outline"` |
| `REVIEW_TOOLS_READ_STATE` | `off` | bloque de lecturas de 5.3 |
| `REVIEW_TOOLS_TRUNCATION_NOTICE` | `on` | aviso de corte de 5.2 |
| `REVIEW_TOOLS_STRICT_CALLS` | `off` | no descartar en silencio: error explícito por petición no atendida |
| `REVIEW_TOOLS_CALLS_PER_TURN` | `3` | la palanca E7 (3 contra 4–5) |
| `REVIEW_TOOLS_PRECACHE_CONTEXT` | `on` | L1 precargado en el bucle (E8); `off` = el bucle lee lo que necesita |
| `REVIEW_TOOLS_IO_CACHE` | `off` | caché de E/S por review (E9) |

Cada una se declara en `Settings`, se documenta comentada en `.env.example` y en la
tabla de `operacion.md` §2, y se cablea en `worker/main.py`. El **texto del
*prompt*** es condicional: con todos los flags en su default, el *prompt* enviado
al modelo es idéntico al de hoy (criterio 9).

### 5.5 Clasificación de la medida (E12)

| Flag | ¿Cambia lo que recibe el modelo? | Cómo se mide |
|---|---|---|
| `GREP_MODE`, `READ_OUTLINE`, `TRUNCATION_NOTICE`, `READ_STATE`, `STRICT_CALLS`, `CALLS_PER_TURN` | **Sí**: cambia coincidencias, contenido servido, avisos o número de turnos | **Regrabación *live*** por configuración (ADR-0017) |
| `IO_CACHE` | No: sirve los mismos bytes | *Replay* sobre la misma grabación, con contador de E/S |
| `PRECACHE_CONTEXT` | **Sí**: cambia el *prompt* base del lote | **Regrabación *live*** |

El informe de M1 declara, por configuración, si es *replay* o *live*; presentar un
*replay* como medida de una mejora que cambia los resultados es medir el pasado.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **La medida no mide lo que cree.** Con el matcher anclado a redacción, dos corridas del mismo defecto puntúan distinto; el `recall` de la variante con herramientas (0.125) es hoy un artefacto | **Antes de medir** (bloque 0): T1 cambia el ancla del matcher a ubicación/identidad con su ADR y enmienda la spec de 006, y T2 reancla las etiquetas **contra el código de `008da57`** y refija el *baseline*. La grabación solo descubre redacciones, no decide qué es un defecto |
| R1b | **El ancla estructural sube el recall por la puerta de atrás**: un defecto distinto del mismo fichero casa por fragmentos | **Tests negativos** obligatorios en T1: dos defectos distintos del mismo fichero no casan la misma etiqueta; una etiqueta con `file` sin `line` exige además todos sus fragmentos |
| R2 | **D23**: una grabación caducada se lee como recall 0 en verde | E0; `make eval-tools` debe fallar ruidosamente si el *fingerprint* no casa, **también** con `batch_retries > 0` |
| R2b | Otro error permanente se lee como «0 hallazgos» por el mismo `except Exception` | E0c: solo se reintenta lo transitorio; el resto sube |
| R3 | **Regex caro** (R4 de 016): un patrón que explota en el `git grep` del runner | Default `literal`; `mode: "regex"` explícito, acotado por `path`/`glob` y con tope de coincidencias y de tiempo |
| R3b | **Dialecto divergente**: el *replay* casa y producción no (o al revés) | E2: POSIX ERE único, traducción declarada, rechazo explícito y tabla de equivalencia en los tres adaptadores |
| R4 | **`outline` opina mal** en lenguajes sin parser (YAML, Dockerfile) | Índice por AST donde exista y por heurística declarada donde no; si duda, cae a `full` |
| R5 | **Más avisos, más *prompt*** (E4, E9): el bloque de estado viaja en cada turno | Bloques acotados (decenas de tokens) y medidos con 006; cada uno detrás de su flag |
| R6 | **Invalidación de grabaciones**: cada flag cambiar el *prompt* obliga a regrabar | Es el comportamiento de [ADR-0017](../../adr/0017-replay-determinista-con-fingerprint.md); E12 separa lo que exige *live* de lo que se mide con *replay*, y cada configuración medida se declara |
| R7 | **La mejora no se nota** y el bucle sigue igual de caro | La entrega se juzga por el informe de cada flag contra el *baseline*, no por el número de mejoras |
| R8 | **El aviso de corte miente**: `M` es el tamaño ya recortado por el adaptador | E4: el lector devuelve metadatos (`total_bytes`, `truncado`, rango servido), no solo el texto |
| R9 | **El estado por review filtra entre reviews** | E9: dueño el `LlmReviewer`, reinicio explícito por review (test) |

## 7. Criterios de hecho

1. `grep` con `mode="regex"` casa `complete\(|model_for\(` en el *gold set* y en el
   *fixture*; con `mode` ausente el resultado es idéntico a hoy (test).
2. El mismo patrón da **el mismo resultado** en los tres adaptadores (tabla de
   equivalencia); un patrón inválido o no soportado devuelve **error explícito**,
   no «sin coincidencias» (test).
3. Un `grep` acotado por `path`/`glob`/`ref` no devuelve coincidencias de fuera ni
   mezcla `head` con `base`; con `files_only` devuelve solo rutas (test).
4. Un `read_file` cortado por presupuesto lleva su aviso de corte con el **tamaño
   real** del fichero y el rango servido; `grep` y `list_dir` llevan el consejo de
   su operación (test).
5. `read_file(mode="outline")` devuelve los símbolos con su rango y una lectura
   posterior del rango devuelve el cuerpo (test).
6. Un fichero leído en un lote **anterior** de la misma review cuenta como servido
   para la cita, y el estado **se reinicia** entre reviews (test).
7. El bloque de estado aparece en el turno siguiente a una lectura y no crece sin
   límite (test).
8. Con `REVIEW_TOOLS_STRICT_CALLS`, una petición de más recibe error explícito y
   **no** se descarta en silencio (test).
9. **Con todos los flags en su default, el *prompt* enviado al modelo es byte a
   byte el de hoy** (test de regresión, criterio 1 de 014): el anuncio de los modos
   va condicionado al flag.
10. Cada flag está en `Settings`, en `.env.example` y en `operacion.md` §2, y
    cableado en `worker/main.py` (test de contrato de configuración si existe).
11. El informe de 006 queda anotado por configuración, **declarando si es *replay*
    o *live***, con *prompt*, *completion*, **razonamiento** y recall; y con las
    **lecturas por modo** (cuántas veces el modelo pide `outline` o `regex`).
12. El informe dice cuántos lotes se **omiten por presupuesto** (ADR-0019): el
    reenvío es también un riesgo de recall.
13. **D23 resuelto y verificado**: con una grabación caducada, `make eval-tools`
    termina en error —también con `batch_retries=2`— y no en un informe con recall
    0 (test).
14. **El matcher mide defecto**: dos redacciones distintas del mismo defecto casan
    la **misma** etiqueta y dos defectos distintos del mismo fichero **no** casan
    la misma (test positivo y negativo); las etiquetas del *gold set* del PR #3
    reancladas **contra el código** dan un informe estable y el *baseline* queda
    refijado con comparación etiqueta por etiqueta (T2).
15. `make ci` y `make docs-check` en verde; documentos vivos al día
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
  pregunta de 014 Q3, ahora con número. Si baja el recall más de lo que baja el
  coste, se queda en `on` y el ahorro hay que buscarlo en el fan-out.
- **Q5 — ¿Cuánto del coste de medir 023 se lleva 024?** Si 024 cambia el puerto a
  mensajes, las regrabaciones *live* de §5.5 se tiran. Recomendación: ejecutar el
  bloque 0 y los flags que **no** cambian el *prompt* (caché de E/S) antes de
  decidir si las regrabaciones *live* se hacen contra el puerto viejo o el nuevo.
  No bloquea esta iniciativa, pero ordena el dinero.
- **Q6 — ¿El aviso de corte debe incluir también el rango ya servido** (`[1-400]`)
  para que el modelo no vuelva a pedir lo mismo? Es barato y evita una llamada.
  Se decide con el informe de M1.
