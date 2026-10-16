# ADR-0019 — El presupuesto de tokens es un contador compartido por review, no un entero por llamada

- **Estado:** Aceptado
- **Fecha:** 2026-10-16

## Contexto

`LLM_BUDGET_TOKENS_PER_PR` prometía un tope **por PR**, pero el código comparaba cada
llamada contra el entero por su cuenta: `LlmReviewer` y `LlmFixer` miraban su propia
estimación y seguían. No había acumulación entre las personas del fan-out, entre el
revisor y el fixer, ni entre reintentos, así que el tope real era N veces el declarado
(deuda D17 de `docs/arquitectura.md` §7). Además, la reserva del bucle de herramientas
([016](../iniciativas/archivo/016-lector-de-contexto/spec.md)) descartaba el `bool` de
`reserve`, de modo que un puerto que devolviera `False` gastaba igual (deuda D21).

El obstáculo de fondo: las ramas `persona` del fan-out corren **en paralelo** dentro
del mismo paso del grafo y el `fix-worker` es otro proceso. Un campo de `ReviewState`
con reducer no se ve entre ramas hasta que el paso termina: no sirve como contador
compartido.

## Decisión

- El presupuesto es **por review** (`review_key`, [ADR-0005](0005-identidad-de-la-ejecucion.md))
  y lo comparten personas, lotes, bucles de herramientas y reintentos.
- El contador vive **fuera del grafo**: un `BudgetPort` (`reserve`, `commit`,
  `remaining`) con un adaptador Redis (script Lua atómico con TTL) y otro en memoria
  (tests y *replay*). Es lo único que ven a la vez las ramas paralelas y el
  `fix-worker`.
- Antes de cada llamada se **reserva** la estimación (`len(text)//4`); después se
  **confirma** el gasto real del `usage`. Sin reserva, varias ramas en paralelo
  pasarían la comprobación a la vez y la superarían entre todas.
- El resultado de `reserve` es **vinculante**: un `False` corta la llamada igual que
  un `remaining` corto (cierra la carrera entre consulta y reserva).
- `remaining` = `total − confirmado − reservado sin confirmar`. `commit(reserved, 0)`
  **devuelve** la reserva no gastada: es el camino del error, para que el reintento
  del nodo ([ADR-0007](0007-reintento-en-dos-capas.md)) no reserve dos veces.
- El reparto entre personas es «el primero que llega» sobre el contador atómico, con
  un tope blando por persona de `2 × total / N`. El puerto solo cuenta; el reparto lo
  calcula quien conoce `N` (`build_reviewers`).
- El fix tiene su **propio** contador (`budget:fix:<review_key>`): una aprobación
  humana puede cruzar el TTL de la review y una review cara no debe dejar sin
  presupuesto lo aprobado.
- Agotado el tope, la llamada **no se hace** y el corte se registra como un hecho
  (`budget_exhausted` en el `ReviewerResult`, `tokens_spent`/`tokens_remaining` en el
  `ReviewOutcome`). El **render** en la cobertura (`SkipReason.BUDGET`) es de
  [012](../iniciativas/012-cobertura-de-la-review/spec.md).

## Consecuencias

- El tope real coincide con el declarado, también con varias ramas en paralelo.
- Los consumidores no conocen Redis: solo el `BudgetPort`, y el *replay* sigue siendo
  determinista (adaptador en memoria).
- Un fallo transitorio de un modelo no infla el contador: la reserva se devuelve.
- Una review puede terminar con **menos cobertura** de la declarada, pero nunca caída
  ni con una factura desbocada. El hecho viaja para que 012 lo pinte.
- Sin Redis (`production` lo exige, [015](../iniciativas/015-puesta-en-marcha-real/spec.md))
  el adaptador cae a un contador local: la review sigue, sin tope global.

## Alternativas descartadas

- **Un campo de `ReviewState` con reducer.** No se ve entre ramas paralelas hasta que
  el paso termina, y el `fix-worker` ni lo comparte: no es un contador.
- **Cuota fija por persona (`total / N`).** Desperdicia lo que una persona no usa y es
  más rígido; el tope blando acota sin desperdiciar.
- **Un `commit` que recorta lo confirmado a lo reservado.** Escondería la imprecisión
  del estimador: el gasto real se cobra entero (el exceso descuenta) para que el
  contador no mienta hacia abajo.
- **Compartir el contador del fix con el de la review.** El TTL de la review puede
  vencer antes de la aprobación humana, o una review cara dejaría sin fix lo aprobado.
