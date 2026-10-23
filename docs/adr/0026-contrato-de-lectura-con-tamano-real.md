# ADR-0026 — La lectura devuelve un value object con el tamaño real, no un `str`

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

El puerto [`ContextReaderPort`](../../src/agent_review/ports/context.py) devuelve
`str | None` para `read_file` y `list[str]` para `grep` y `list_dir`. El bucle de
herramientas de [016](../iniciativas/archivo/016-lector-de-contexto/spec.md) solo
tiene entonces `len(content)` para decidir qué contar, y el valor ya viene
recortado por el adaptador:

- `FixtureContextReader` corta a `max_file_bytes` **antes** de aplicar el rango de
  líneas.
- `ReadWorkspace.show_file` corta a `_MAX_READ_BYTES` **antes** del rango, de modo
  que pedir `start=900` de un fichero grande devuelve vacío.
- `WorkspaceContextReader` recibe del runner solo `content`: el tamaño real se
  pierde en el salto HTTP.
- `grep` corta a `max_grep_matches` y `list_dir` también se recorta, y ninguna de
  las dos lo dice. El modelo lee «esto es todo lo que hay» cuando no lo es.

Un aviso de corte calculado con `len(content)` miente siempre que el adaptador ya
recortó. Y la media verdad es peor que un error: el modelo razona sobre lo que cree
que vio (E4 y R8 de [023](../iniciativas/023-eficacia-del-bucle-de-lectura/spec.md)).

Además, el recorte de líneas está triplicado (`context_fixture`, `context_vcs`,
`read_workspace`) y ninguno valida `start=0`, negativos ni `end < start`.

## Decisión

1. **`read_file` devuelve `ReadResult | None`**, con:
   - `content`: lo que se sirve, ya recortado.
   - `total_bytes`: tamaño del fichero **completo** en bytes UTF-8, antes de
     cualquier recorte o rango. `None` si la superficie no lo conoce.
   - `truncated`: el adaptador cortó `content`.
   - `served_range`: rango de líneas 1-based inclusivo servido, si se pidió rango.

2. **`grep` y `list_dir` devuelven `ToolResult`**: `items` (coincidencias
   `path:line: texto` o nombres), `total` (cuántos había antes del tope) y
   `truncated`. Un solo tipo para las dos operaciones: el significado de `items` lo
   fija el método, no la forma; dos clases casi idénticas no aportan nada.

3. **Las unidades se nombran en el campo**: `total_bytes` son bytes UTF-8, no
   `len(text)`. El recorte se hace en bytes y **no parte una línea**: la línea
   incompleta se descarta para que el modelo no la lea como entera. El rango de
   líneas se aplica **antes** del recorte, para que un `start` alto no caiga fuera
   de un contenido ya truncado.

4. **`None` significa «no medido», nunca cero.** Un adaptador que no puede contar
   el total (un runner viejo, un `.git` sin `cat-file`) lo declara `None`; el
   consumidor dirá «tamaño desconocido» en vez de inventar un número. `total_bytes=0`
   se reserva para un fichero realmente vacío.

5. **Un rango imposible es error.** `slice_lines` valida `start >= 1`, `end >= 1` y
   `end >= start` y lanza `ValueError`; los adaptadores validan antes de gastar una
   petición y el runner responde `400`. Los límites se recortan al tamaño del
   texto: pedir más allá del final sirve lo que hay, que no es ambiguo.

6. **El `ReadResult` viaja entero por HTTP.** `FileResponse` añade `total_bytes`,
   `truncated` y `served_range`; `GrepResponse` y `TreeResponse` añaden `total` y
   `truncated`. Un worker nuevo contra un runner viejo degrada a `None`/`False`
   (compatible), pero **no** finge un total.

## Consecuencias

- El aviso de corte de E4 puede decir «cortado a N de M bytes» con `M` real, y
  `grep`/`list_dir` pueden decir «mostrando N de M» sin suposiciones.
- Cambia el contrato del puerto: los tres adaptadores, el bucle, los dobles de
  test y la API del runner se migran en el mismo commit. Ningún
  `read_file(...) == "texto"` sobrevive.
- El rango se aplica antes del tope: deja de devolver vacío un `start` alto.
- `grep` recorre todas las coincidencias para contarlas aunque solo sirva las
  primeras; el coste ya estaba pagado por `git grep`, que devuelve todo el stdout.
- El tamaño se mide sobre el texto decodificado: si un byte no es UTF-8, la
  lectura usa `errors="replace"` y `total_bytes` es el del texto servible, no el
  del blob crudo. Sirve para el aviso (que habla de lo que se puede servir), no
  para auditar el blob.

## Alternativas descartadas

- **Mantener `str` y pasar los metadatos aparte** (un `last_result` en el lector,
  un `dict` en el bucle). Mete estado mutable en un puerto que hoy es puro y hace
  que dos hilos con el mismo lector se pisen.
- **Calcular `total_bytes` en el bucle con `len(content)`.** Es exactamente el
  error que se corrige: el adaptador ya recortó.
- **Devolver `total_bytes=0` cuando no se conoce.** Convierte «no medido» en un
  dato falso y hace que el aviso diga «0 de 0».
- **Una clase por operación (`GrepResult`, `DirResult`).** Tres tipos con los
  mismos campos y el mismo renderizado; el `items` genérico tiene menos superficie.
- **Cortar por caracteres en vez de bytes.** Un fichero con acentos ocupa más de lo
  que dice `len`; el tope de 60 000 «bytes» servía hasta un 30 % más.
