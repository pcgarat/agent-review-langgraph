Última modificación: 2026-10-16 | Creado: 2026-10-16

# Presupuesto de tokens por PR

> **Estado:** borrador
>
> **Entrega objetivo:** `LLM_BUDGET_TOKENS_PER_PR` es de verdad un tope **por
> review**. Lo comparten todas las personas, sus lotes, los bucles de herramientas,
> el verificador, el fixer y los reintentos. Al agotarse, lo que falta se registra
> en la cobertura en vez de fallar o de gastar de más.

---

## 1. Objetivo

Hoy cada `LlmReviewer` y el `LlmFixer` comparan **su propia** llamada contra
`budget_tokens_per_pr`. No se acumula nada entre personas, entre el revisor y el
fixer, ni entre reintentos (deuda D17 de [`arquitectura.md`](../../arquitectura.md)
§7). El tope real es N veces el declarado. La cascada lo agrava:

- [012](../012-cobertura-de-la-review/spec.md) acumula el gasto entre los lotes de
  **una** persona (C6), no entre personas.
- [009](../009-verificador-adversarial/spec.md) añade varias llamadas por hallazgo
  (su Q5 pregunta dónde se arregla D17).
- [014](../014-revisor-con-herramientas/spec.md) añade turnos por lote, y activar un
  segundo rol con herramientas exige antes resolver D17 (014 Q7).

Se quiere:

1. **Un presupuesto por review**, compartido por todo lo que llama a un modelo en esa
   review.
2. Que cuente el **gasto real** (`usage` de `LlmReply`) y no solo la estimación.
3. Que agotarlo **degrade** (cobertura `budget`) en vez de fallar.

## 2. No-objetivos

- **Coste en dinero.** Se cuentan tokens; el precio por modelo es otro asunto.
- **Presupuesto por repo o por día.** Solo por review.
- **Cambiar el estimador** (`len(text) // 4`). Sigue siendo la cota previa a cada
  llamada.

## 3. Contexto y evidencia

- Las ramas `persona` del fan-out corren **en paralelo** dentro del mismo paso del
  grafo: un campo de `ReviewState` con reducer no se ve entre ramas hasta que el paso
  termina, así que no sirve como contador compartido.
- El `LlmFixer` corre en otro proceso (`fix-worker`,
  [004](../archivo/004-fase3-e7-desacople/spec.md)), que solo tiene Redis.
- `LlmReply` ya trae el `usage` real del proveedor.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| B1 | El presupuesto es **por review** (`review_key`, [ADR-0005](../../adr/0005-identidad-de-la-ejecucion.md)) y lo comparten personas, lotes, bucles de herramientas, verificador y fixer | Es lo que el nombre de la variable ya promete |
| B2 | El contador vive en **Redis** (`INCRBY` atómico, con TTL) | Es lo único que ven a la vez las ramas paralelas del fan-out y el `fix-worker` |
| B3 | Antes de cada llamada se **reserva** la estimación; después se **ajusta** con el `usage` real | Sin reserva, varias ramas en paralelo pasan la comprobación a la vez y lo superan entre todas |
| B4 | Agotado el presupuesto, la llamada **no se hace**: los lotes pendientes van a la cobertura como `budget` ([012](../012-cobertura-de-la-review/spec.md) C6), el verificador deja `unverified` y el bucle de herramientas pide el turno final | Mejor menos hallazgos y avisado que una factura desbocada o una review caída |
| B5 | Los **reintentos cuentan** | Un reintento gasta tokens de verdad |
| B6 | Un puerto `BudgetPort` (`reserve`, `commit`, `remaining`) con un adaptador Redis y otro en memoria para tests y *replay* | Los consumidores no conocen Redis; el *replay* sigue siendo determinista |

## 5. Diseño o contrato

```python
@runtime_checkable
class BudgetPort(Protocol):
    def reserve(self, review_key: str, tokens: int) -> bool: ...   # False: no queda
    def commit(self, review_key: str, reserved: int, actual: int) -> None: ...
    def remaining(self, review_key: str) -> int: ...
```

- Clave `budget:{review_key}`, con el mismo TTL que la review. La fila nueva va a
  [`arquitectura.md`](../../arquitectura.md) §5.
- `LlmReviewer`, `LlmFixer`, el bucle de
  [016](../016-lector-de-contexto/spec.md) y el verificador de 009 reciben el
  `BudgetPort` en su construcción, en lugar del entero `budget_tokens_per_pr`.
- `ReviewOutcome` expone lo gastado y lo que quedó, para que
  [006](../archivo/006-eval-de-la-review/spec.md) mida el coste por review.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **Una persona se lo come todo** y las demás no revisan | Reparto entre personas (Q1) |
| R2 | **Redis caído** | Sin Redis no hay review en `production` ([015](../015-puesta-en-marcha-real/spec.md) X4); fuera de `production`, adaptador en memoria |
| R3 | **El fixer se queda sin presupuesto** después de una review cara | Q2: reserva propia para el fix o presupuesto aparte |
| R4 | **La estimación previa se queda corta** y el `usage` real supera la reserva | `commit` ajusta con el real; el exceso de una sola llamada está acotado por `max_tokens` |

## 7. Criterios de hecho

1. Con tres personas en paralelo y un presupuesto que solo da para dos, el gasto total
   no supera el tope (test con el adaptador en memoria y concurrencia real).
2. Un reintento descuenta del mismo presupuesto (test).
3. Agotado el presupuesto, la review termina y la cobertura lista los lotes `budget`
   (test).
4. El fixer descuenta del presupuesto de su review (test entre procesos con Redis
   falso).
5. D17 sale de [`arquitectura.md`](../../arquitectura.md) §7; `make ci` en verde, con
   `make docs-check`; [`operacion.md`](../../operacion.md) §2 explica la semántica
   nueva de `LLM_BUDGET_TOKENS_PER_PR`.

## 8. Preguntas abiertas

- **Q1 — ¿Cómo se reparte entre personas?** Una cuota fija por persona (justo, pero
  desperdicia lo que una no usa) o «el primero que llega» con un mínimo garantizado
  por persona.
- **Q2 — ¿El fix comparte el presupuesto de la review?** Compartirlo es coherente con
  B1; separarlo evita que una review cara deje sin fix lo que un humano aprobó.
- **Q3 — ¿Valor por defecto?** Hoy `500000` por llamada. Como tope por review, ¿se
  mantiene o se recalcula con el coste medido en 006?
