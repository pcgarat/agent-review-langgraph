Última modificación: 2026-10-16 | Creado: 2026-10-16

# Plan: Presupuesto de tokens por review

El *qué y por qué* vive en [`spec.md`](spec.md); las tareas, en [`todo.md`](todo.md).
Este plan fija el orden y las dependencias. 017 sustituye el entero
`budget_tokens_per_pr` por un **puerto con contador compartido** y cierra la deuda
[D17](../../arquitectura.md) §7 y [D21](../../arquitectura.md) §7.

## Overview

Cuatro piezas:

1. `BudgetPort` afinado en [`ports/budget.py`](../../../src/agent_review/ports/budget.py)
   (016 ya dejó el protocolo; 017 fija la semántica de `remaining` y la devolución de
   reservas).
2. Dos adaptadores: `RedisBudget` (contador atómico con TTL) e `InMemoryBudget`
   (tests y *replay*), con la **misma** definición de `remaining`.
3. Los consumidores dejan de comparar su propia llamada contra un entero y pasan a
   reservar/confirmar contra el puerto: `LlmReviewer`, `LlmFixer` y el bucle de 016.
4. El **hecho** del corte (`budget_exhausted`, `tokens_spent`/`tokens_remaining`)
   viaja en `ReviewerResult` y `ReviewOutcome`. El render en la cobertura es de 012.

## Decisiones de implementación

- **Claves separadas** (B11): `budget:{review_key}` para la review y
  `budget:fix:{review_key}` para el fix. El fix corre en otro proceso, tras una
  aprobación humana que puede cruzar el TTL de la review.
- **`remaining` descuenta lo reservado y no confirmado** (B9). Es la definición que
  hace que dos ramas en paralelo no vean el mismo saldo. Si el adaptador Redis y el de
  memoria difieren aquí, el *replay* deja de ser fiel: un test lo fija.
- **`commit(reserved, actual)` con `actual=0` devuelve la reserva** (B8). El bucle y
  el `LlmReviewer` lo llaman en el `except` antes de relanzar lo transitorio.
- **`reserve(...) is False` corta la llamada** (B7, cierra D21). El `bool` deja de
  descartarse en `_reserve` de `tool_loop.py`.
- **Un `reserve` rechazado no lleva `commit`** (B12): si `reserve` devuelve `False`,
  no se llama a `commit` (no hay reserva que ajustar). El `commit` va condicionado a
  `reserve is True`.
- **El reparto es del consumidor que conoce N** (B10): `build_reviewers` calcula
  `2 * total // N` y se lo pasa al `LlmReviewer` como tope blando. El puerto solo
  cuenta.
- **Sin puerto, comportamiento actual**: `budget=None` sigue siendo «no se consulta».
  El *replay* y los tests que no miden coste no cambian.
- **El fixer en otro proceso** usa el adaptador Redis (B2); el `ReviewOutcome` del
  worker de reviews no ve el gasto del fix. El contador es la fuente de verdad, no el
  estado del grafo.

## Dependency graph

```
P1  presupuesto en memoria fiel (remaining con reservas)
     └── P2  cubrir D21: reserve(False) corta el bucle (tool_loop)
          └── P3  devolver la reserva en el error (reserve/commit, bucle + reviewer)

P4  adaptador Redis (INCRBY + TTL + reservas pendientes)
     └── P5  LlmReviewer y LlmFixer contra el puerto (no el entero)
          └── P6  reparto por persona (2*total/N) en build_reviewers

P7  hecho del corte: ReviewerResult.budget_exhausted + ReviewOutcome tokens
     └── P8  documentación viva y cierre (D17 y D21 fuera de §7)
```

P1–P3 son el núcleo del puerto y cierran D21 sin tocar el worker. P4–P6 cablean los
consumidores reales. P7 expone el hecho. P8 documenta y cierra.

## Task list

Ver [`todo.md`](todo.md) (P1–P8). El puerto y su semántica primero; los consumidores
después; el hecho y los docs al final, en el mismo commit que el código.

## Checkpoints

- **Tras P1–P3:** el bucle respeta `reserve(False)` y devuelve la reserva en el error;
  `make ci` en verde.
- **Tras P4–P6:** `LlmReviewer` y `LlmFixer` no conocen el entero; el reparto por
  persona está probado; `make ci` en verde.
- **Tras P7–P8:** el corte por presupuesto deja rastro medible; D17 y D21 fuera de
  `arquitectura.md` §7; `make ci` y `make docs-check` en verde.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El adaptador Redis diverge del de memoria en `remaining` | Definición única (B9); test de contrato paramétrico sobre los dos |
| `INCRBY` + TTL por reserva no es atómico y dos ramas se cuelan | Una sola operación Lua o `INCRBY` dentro de la misma transacción; el tope blando por persona acota el daño |
| El `commit` con `actual=0` deja el contador negativo si la reserva ya se había devuelto | El adaptador ignora una devolución mayor que lo reservado; test de doble `commit` |
| El `commit` con `actual > reserved` no descuenta el exceso y el gasto real queda invisible | El adaptador resta `reserved` de lo pendiente y suma `actual` a lo confirmado (equivale a `pending += actual − reserved`), **sin** recortar lo confirmado a lo reservado; `remaining` puede quedar negativo (R4). Test de `commit(reserved, actual > reserved)` |
| El fixer y el worker no ven el mismo TTL | Claves separadas con el mismo TTL que la review; el fix renueva al leer |
| Cambiar la firma de `LlmReviewer` rompe los tests del *gold set* | El parámetro nuevo es opcional (`budget: BudgetPort | None`), con el entero como camino heredado hasta P5 |

## Dependencias externas

- [016](../../iniciativas/archivo/016-lector-de-contexto/spec.md) dejó el
  `BudgetPort` y su consumo en `run_tool_loop`; 017 afina la semántica.
- [012](../012-cobertura-de-la-review/spec.md) consume el hecho del corte para la
  `ReviewCoverage`, pero 017 **no** la espera: entrega el mecanismo y el dato.
- [ADR-0007](../../adr/0007-reintento-en-dos-capas.md): el reintento del nodo es lo
  que obliga a devolver la reserva en el error (B8).
- [ADR-0005](../../adr/0005-identidad-de-la-ejecucion.md): `review_key` es la clave
  del contador.
