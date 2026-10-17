# ADR-0025 — El matcher del *gold set* mide el defecto, no la redacción

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

El matcher de [006](../iniciativas/archivo/006-eval-de-la-review/spec.md) atribuye
un hallazgo a una etiqueta con `match` **conjuntivo**: todas las frases deben
aparecer en `title` + `why_it_matters`, con `file` y `line` opcionales. Nació para
un *gold set* de juguete y se rompe al medir de verdad:

- El **mismo defecto** redactado distinto puntúa distinto. `build.py:1123` casa la
  etiqueta `R3` en el *baseline* sin herramientas y queda sin casar en el *replay*
  con herramientas, no porque el defecto cambie, sino porque el modelo lo nombra
  con otras palabras.
- Una etiqueta **anclada solo por `file`** puede casar dos defectos distintos del
  mismo fichero: el fragmento se elige para describir uno y el otro lo comparte.
- `_attribute` consume `expected` **antes** que `must_not_report` y la atribución
  es 1:1 por orden de lista: un hallazgo que casa ambas cosas **nunca** cuenta como
  falso positivo. La prioridad silenciosa esconde el solapamiento en vez de
  declararlo.
- La tolerancia de ±5 líneas ([ADR-0014](0014-criterio-de-merge-entre-personas.md))
  sola tampoco distingue dos defectos **en la misma línea**.

Sin un ancla estable no se puede comparar una configuración con otra: el número
mide la redacción del modelo, no lo que el modelo encontró (R1 y R1b de
[023](../iniciativas/023-eficacia-del-bucle-de-lectura/spec.md)).

## Decisión

El matcher tiene **dos anclas**: la localización cuando existe y el contenido como
respaldo. Una etiqueta casa un hallazgo si se cumplen **las dos**.

1. **Localización.** `file` debe coincidir. Si se declara `line`, la distancia es
   ≤ `LINE_TOLERANCE` (5, la misma del merge). `line` sin `file` es un **error de
   carga**: una línea de otro fichero no ancla nada.
2. **Contenido.** `match` son fragmentos sobre `title` + `why_it_matters`
   normalizados (minúsculas y espacios colapsados, como hoy):
   - `file` **y** `line` declarados: **basta un** fragmento. La localización
     identifica el defecto y los fragmentos lo desambiguan de otro defecto en el
     mismo sitio.
   - `file` sin `line`, o etiqueta sin `file`: **todos** los fragmentos, como en
     006. Sin línea, ni el fichero ni un fragmento suelto identifican el defecto;
     el conjunto es la única ancla.
3. **Fragmento discriminante.** Toda etiqueta con `file` y `line` declara al menos
   un fragmento. Sin él, `file`+`line` (±5) casaría dos defectos distintos de la
   misma línea y el matcher subiría el recall por la puerta de atrás.
4. **Atribución 1:1 con puntuación.** Se evalúan todos los pares
   (etiqueta, hallazgo) y se asignan de mayor a menor número de fragmentos
   casados. El empate lo rompe el orden declarado de las etiquetas (primero
   `expected`, luego `must_not_report`, luego `acceptable`) y, después, el orden
   canónico del hallazgo (severidad, línea, `id`). Dos etiquetas nunca se quedan
   con el mismo hallazgo.
5. **Solapamiento declarado, no prioridad.** Si un hallazgo casa a la vez una
   etiqueta `expected` y una `must_not_report`, el caso es ambiguo y la medida
   **falla** con el `id` de las dos etiquetas: el mismo hallazgo no puede ser a la
   vez acierto y falso positivo. La carga del caso rechaza además dos etiquetas
   indistinguibles (`id` repetido, o mismo anclaje y mismos fragmentos) entre
   `expected` y `must_not_report`.
6. **`must_mention` no participa.** Sigue siendo la comprobación posterior de
   «descrito al revés» (E16 de 006): un `tp_inverted` es un acierto y no mueve
   precisión ni recall.

Las etiquetas se reanclan **contra el código del `head_sha`**, no contra la
grabación: la grabación descubre redacciones alternativas, pero no decide qué es un
defecto.

## Consecuencias

- Dos redacciones del mismo defecto casan la misma etiqueta si comparten
  localización y un fragmento discriminante; no hay que enumerar sinónimos.
- Dos defectos distintos en la misma línea del mismo fichero **no** casan la misma
  etiqueta si cada uno declara su fragmento.
- Un defecto sin línea canónica (un cableado que falta, una omisión) se ancla por
  fichero y contenido conjuntivo, que es más estricto. Anclarlo a una línea
  arbitraria mediría la puntería del modelo, no el defecto.
- Los *baselines* cambian: hay que refijar los dos (sin y con herramientas) y
  comparar etiqueta por etiqueta para que el refijado no baje el listón en
  silencio (023 T2).
- Un caso mal etiquetado falla al cargar o al medir, con el `id` en el mensaje, en
  vez de esconder un FP.

## Alternativas descartadas

- **Fragmentos disyuntivos también con `file` sin `line`.** Medido: el hallazgo de
  `settings.py:230` casaba `integracion-2`, `integracion-3` y `R6` a la vez. Un
  fichero no es un ancla; el conjunto de fragmentos sí.
- **Prioridad `expected` > `must_not`.** Es lo que hay hoy y convierte un
  solapamiento en un acierto silencioso.
- **Anclar solo por línea.** Los defectos de ausencia no tienen una línea y el
  modelo ancla en el hunk que le parece; mediría su puntería.
- **Ajustar las etiquetas a la salida grabada.** Circular: se mide la redacción que
  ya se ha visto en vez del defecto.
