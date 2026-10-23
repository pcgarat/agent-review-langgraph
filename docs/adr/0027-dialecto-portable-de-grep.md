# ADR-0027 — `grep` habla un dialecto portable y rechaza lo que cada motor lee distinto

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

`grep` nació en [016](../iniciativas/archivo/016-lector-de-contexto/spec.md) como
búsqueda **literal**. El modelo, sin embargo, escribe patrones con metacaracteres
(46 en la extracción que hizo 023): `settings\.py`, `complete\(|model_for\(`,
`def _\w+\(`. En modo literal esos patrones no encuentran nada, y como la respuesta
es una lista vacía el modelo lee «aquí no hay nada»: la peor media verdad posible.
E2 de [023](../iniciativas/023-eficacia-del-bucle-de-lectura/spec.md) pide un modo
`regex` con default `literal`.

Regex tiene un problema de dialecto. Las cuatro superficies no ejecutan el mismo
motor:

- `git grep -E` (runner) implementa POSIX ERE con extensiones GNU.
- `FixtureContextReader` y `VcsContextReader` usan el módulo `re` de Python.

Y no coinciden en lo que parece obvio:

- `[[:space:]]` en `re` es una clase con los caracteres `[`, `:`, `s`, `p`, `a`,
  `c`, `e`: **no** un espacio.
- Dentro de una clase, GNU grep 3.11 lee `\t` como la letra `t`
  (`[ \t]` casa `t`, no un tabulador), mientras `re` lo lee como tabulador.
- `\b`, `\<` y `\>` son fronteras de palabra GNU; `re` solo entiende `\b`.
- `(?…)` (lookaround, grupos sin captura) no existe en ERE.

Traducir a dos emisores duplica la superficie de test y deja dos verdades para el
mismo patrón. Además había tres defectos que el modo `regex` habría amplificado:

1. `read_workspace.grep` trataba **cualquier** código de salida distinto de 0/1
   como «sin coincidencias». Un patrón inválido (código 2) se servía como lista
   vacía.
2. La semántica de `glob` no era la misma: el fixture y el VCS la medían con
   `globs.path_matches`, el runner la delegaba a un *pathspec* de `git`, donde `*`
   **cruza** directorios. El mismo `glob="*.py"` devolvía árboles distintos.
3. `VcsContextReader` mezclaba `head` y `base` en un mismo resultado (resuelto en
   C0b) y solo ve lo sembrado: es un lector **parcial**.

## Decisión

1. **Un subconjunto portable único, emitido por un solo normalizador.**
   `portable_pattern` vive en
   [`ports/context.py`](../../src/agent_review/ports/context.py) y emite **una**
   cadena que `re` y `git grep -E` interpretan igual. Acepta literales, `.`, `*`,
   `+`, `?`, `|`, `()`, `[]`, `^`, `$` y los escapes:

   | Entrada | Salida canónica |
   |---|---|
   | `\d` | `[0-9]` |
   | `\D` | `[^0-9]` |
   | `\w` | `[A-Za-z0-9_]` |
   | `\W` | `[^A-Za-z0-9_]` |
   | `\s` | `[<espacio><tab><CR><FF><VT>]` (caracteres literales) |
   | `\S` | la clase negada de la anterior |
   | `\.` `\*` `\+` `\?` `\|` `\(` `\)` `\[` `\]` `\{` `\}` `\^` `\$` `\\` `\/` `\-` | el mismo escape |

2. **`\s` se emite con caracteres literales, no con `[ \t\n\r\f\v]`.** Es una
   enmienda al texto de E2: dentro de una clase, GNU grep no reconoce `\t` y lo lee
   como `t`; y `\n` no puede aparecer nunca en una línea que devuelve `grep`. Los
   caracteres de control literales sí significan lo mismo en los dos motores
   (comprobado con GNU grep 3.11 y `re`). `[[:space:]]` se descarta por lo
   contrario: en `re` no es un espacio.

3. **Rechazo explícito de lo no portable.** `ToolError` con mensaje accionable
   para clases POSIX `[[:…:]]`, fronteras de palabra `\b`/`\<`/`\>`, `(?…)`
   (lookaround, grupos sin captura, flags), retro-referencias `\1`, cualquier `\`
   dentro de una clase, letras escapadas que no sean `dDwWsS` y patrones inválidos
   (clase o grupo sin cerrar). Un error visible es mejor que dos resultados
   distintos según la superficie.

4. **Patrón inválido o no portable nunca es «sin coincidencias».** El
   normalizador se ejecuta en el borde (adaptador y runner); el runner responde
   `400` y el adaptador traduce ese `400` a `ToolError`, que el bucle sirve como
   `ERROR` dentro del `<tool_result>`. `git grep` con código de salida ≥ 2 también
   es error; un timeout del subproceso también, con el consejo de acotar.

5. **`mode` es parámetro del puerto, no una operación nueva.** Default `literal`
   (comportamiento de 016: `mode` ausente no cambia nada). El flag
   `REVIEW_TOOLS_GREP_MODE` fija el **default del bucle**; si el modelo pide un
   modo, manda el suyo: el flag no oculta la capacidad (E11b).

6. **`glob` se mide contra la ruta relativa a `path`** (o a la raíz si no hay
   `path`), con `*` que no cruza `/` y `**` que sí. Se aplica en Python en las
   cuatro superficies; el runner deja de delegarlo a los *pathspec* de `git`,
   cuyo glob tiene otra semántica. `depth` cuenta niveles desde `path`, igual que
   `list_dir`.

7. **Sin timeout dentro del proceso.** Los lectores en memoria no pueden abortar
   un `re` que retrocede en exceso sin matar el hilo que sirve la review (Q8).
   Riesgo acotado por: default `literal`, `path`/`glob` que reducen el texto,
   tope de coincidencias y el `timeout_s` del subproceso `git grep` del runner,
   que sí corta. El `regex` se aplica línea a línea sobre texto ya leído, no sobre
   el árbol entero de una vez.

8. **El runner anuncia `grep.mode`.** Un runner anterior a 023 no lo declara y el
   adaptador rechaza `mode="regex"` en vez de servirlo literal en silencio
   (E0g, R3c).

9. **`VcsContextReader` se declara parcial.** La equivalencia entre superficies se
   predica sobre la intersección de ficheros visibles (lo sembrado más el diff):
   un `grep` vacío en el VCS no significa «no hay nada en el repositorio».

## Consecuencias

- El mismo patrón canónico da el mismo resultado en el fixture, el VCS, el
  workspace y la API del runner (`tests/test_context_ports.py` y
  `tests/test_read_workspace.py`).
- El modelo puede escribir `def \w+\(` y `needle = \d` sin adivinar el motor.
- Un patrón no portable cuesta un turno (el modelo recibe el error y lo corrige),
  pero no cuesta una medida falsa.
- `\s` deja de casar el salto de línea. En un `grep` línea a línea da igual.
- Las clases POSIX y las fronteras de palabra no están disponibles; el mensaje
  dice la alternativa (`path`/`glob`, `[^A-Za-z0-9_]`).
- Un `regex` costoso en memoria puede tardar: se acepta y se acota por diseño.
  El runner, que es el camino normal, sí tiene timeout.
- `glob="*.py"` en la raíz ya solo casa ficheros de la raíz. Para todo el árbol,
  `**/*.py` (lo que anuncia el *prompt*).

## Alternativas descartadas

- **Traducir el patrón a dos variantes** (una para `re`, otra para `git grep`).
  Dos emisores, dos tablas y dos pruebas para el mismo concepto; y la variante
  que se sirve deja de ser auditable en la grabación.
- **Usar `[[:space:]]` para `\s`.** En `re` no significa espacio: sería el error
  de dialecto que este ADR evita, con una pinta de correcto.
- **`git grep -P` (PCRE) en el runner.** Depende de que el binario de git se
  compilase con libpcre; si no, falla en tiempo de ejecución. `-E` está siempre.
- **Delegar `glob` al *pathspec* de `git`.** `*` cruza `/` allí y no en
  `globs.path_matches`; el mismo argumento devolvería árboles distintos.
- **Un timeout con `signal.alarm` en los adaptadores en memoria.** Solo funciona
  en el hilo principal y el worker sirve el bucle en otro hilo; dejaría el código
  con una protección que no protege.
- **Aceptar todo lo que compile en `re`.** `(?=…)`, `\b` y `\1` compilan en Python
  y no existen en ERE; aceptarlos es prometer un resultado que la otra superficie
  no da.
