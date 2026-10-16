Última modificación: 2026-10-16 | Creado: 2026-10-16

# Presupuesto de tokens por PR

> **Estado:** aprobada *(2026-10-16)*
>
> **Entrega objetivo:** `LLM_BUDGET_TOKENS_PER_PR` es de verdad un tope **por
> review**. Lo comparten todas las personas, sus lotes, los bucles de herramientas,
> el verificador, el fixer (con su propio contador) y los reintentos. Al agotarse, lo
> que falta se registra como un hecho (`budget_exhausted`) en vez de fallar o de
> gastar de más; [012](../012-cobertura-de-la-review/spec.md) lo pinta en la cobertura.

---

## 1. Objetivo

Hoy cada `LlmReviewer` y el `LlmFixer` comparan **su propia** llamada contra
`budget_tokens_per_pr`. No se acumula nada entre personas, entre el revisor y el
fixer, ni entre reintentos (deuda D17 de [`arquitectura.md`](../../arquitectura.md)
§7). El tope real es N veces el declarado. La cascada lo agrava:

- [012](../012-cobertura-de-la-review/spec.md) acumula el gasto entre los lotes de
  **una** persona (C6), no entre personas; y es quien **pinta** el corte por
  presupuesto en la `ReviewCoverage` (`SkipReason.BUDGET`), que aún no existe.
- [009](../009-verificador-adversarial/spec.md) añade varias llamadas por hallazgo
  (su Q5 pregunta dónde se arregla D17).
- [014](../014-revisor-con-herramientas/spec.md) añade turnos por lote, y activar un
  segundo rol con herramientas exige antes resolver D17 (014 Q7).

Se quiere:

1. **Un presupuesto por review**, compartido por todo lo que llama a un modelo en esa
   review.
2. Que cuente el **gasto real** (`usage` de `LlmReply`) y no solo la estimación.
3. Que agotarlo **degrade** en vez de fallar: 017 emite el **hecho** del corte
   (`budget_exhausted`, `tokens_spent`); el **render** en la cobertura es de 012, y 017
   no depende de ella para entregar el mecanismo.

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
| B1 | El presupuesto es **por review** (`review_key`, [ADR-0005](../../adr/0005-identidad-de-la-ejecucion.md)) y lo comparten personas, lotes, bucles de herramientas y verificador | Es lo que el nombre de la variable ya promete |
| B2 | El contador vive en **Redis** (`INCRBY` atómico, con TTL) | Es lo único que ven a la vez las ramas paralelas del fan-out y el `fix-worker` |
| B3 | Antes de cada llamada se **reserva** la estimación; después se **ajusta** con el `usage` real | Sin reserva, varias ramas en paralelo pasan la comprobación a la vez y lo superan entre todas |
| B4 | Agotado el presupuesto, la llamada **no se hace**: el verificador deja `unverified`, el bucle de herramientas pide el turno final y el `LlmReviewer` corta los lotes que falten **anotando el hecho** (`budget_exhausted` en `ReviewerResult`); [012](../012-cobertura-de-la-review/spec.md) es quien lo pinta como `SkipReason.BUDGET` en la `ReviewCoverage` | Mejor menos hallazgos y avisado que una factura desbocada o una review caída. 017 entrega el **hecho**; el **render** es de 012 (que aún no existe) |
| B5 | Los **reintentos cuentan** | Un reintento gasta tokens de verdad |
| B6 | Un puerto `BudgetPort` (`reserve`, `commit`, `remaining`) con un adaptador Redis y otro en memoria para tests y *replay* | Los consumidores no conocen Redis; el *replay* sigue siendo determinista |
| B7 | El **resultado de `reserve` es vinculante**: un `False` corta la llamada igual que un `remaining` corto | Es el único punto que cierra la carrera entre `remaining` y `reserve`; sin él, el adaptador real de Redis puede dejarse gastar (enmienda 2026-10-16, deuda D21) |
| B8 | La reserva se **libera** si la llamada no llega a gastar: `commit(review_key, reserved, 0)` en el camino de error | Sin la devolución, un `TransientError` reintentado por el nodo ([ADR-0007](../../adr/0007-reintento-en-dos-capas.md)) deja la primera reserva colgada para siempre y el contador se infla solo |
| B9 | `remaining` devuelve el saldo **descontando lo reservado y no confirmado** | Si solo descontara lo confirmado, dos ramas en paralelo verían el mismo saldo y ambas pasarían la comprobación: justo la carrera que B3 cierra |
| B10 | El reparto entre personas es **«el primero que llega»** sobre el contador atómico, con un tope blando por persona de `2 × total / N` (`N` = personas LLM del fan-out) | Sin cuota fija no se desperdicia lo que una persona no usa; sin tope, una persona con *prompt* grande se lo come todo (R1) |
| B11 | El fix tiene su **propio contador** por review (clave distinta), no el de la review | La aprobación humana puede tardar más que el TTL de la review (R3): compartir la clave dejaría al fix sin presupuesto, o lo recrearía con saldo entero |
| B12 | Si `reserve` devuelve `False`, **no se llama a `commit`**: el `commit` va condicionado a `reserve is True` | Sin reserva no hay nada que ajustar; un `commit(key, 0, actual)` cobraría un gasto que la reserva rechazó (§8 Q4) |

## 5. Diseño o contrato

```python
@runtime_checkable
class BudgetPort(Protocol):
    def reserve(self, review_key: str, tokens: int) -> bool: ...   # False: no queda
    def commit(self, review_key: str, reserved: int, actual: int) -> None: ...
    def remaining(self, review_key: str) -> int: ...
```

- Clave `budget:{review_key}` para la review y `budget:fix:{review_key}` para el fix
  (B11), con el mismo TTL que la review. Las filas nuevas van a
  [`arquitectura.md`](../../arquitectura.md) §5.
- `commit(reserved, actual=0)` **devuelve** la reserva no gastada (B8): es el camino
  que usa el bucle y el `LlmReviewer` si la llamada lanza. Sin él, el reintento
  reserva dos veces. Con `actual > reserved` **cobra el gasto real** (equivale a
  `pending += actual − reserved`): el exceso descuenta del saldo, no se recorta, así
  que `remaining` puede quedar negativo si la estimación se quedó corta (R4).
- `remaining` = `total − confirmado − reservado sin confirmar` (B9). El adaptador en
  memoria y el de Redis deben coincidir en esa definición; el test de concurrencia
  (§7.1) la fija.
- El **reparto** (B10) vive en el consumidor que conoce el número de personas
  (`build_reviewers` en el worker): calcula `tope_persona = 2 * total // N` y pasa al
  `LlmReviewer` su tope blando además del puerto compartido. El puerto no conoce el
  reparto: solo cuenta.
- `LlmReviewer`, `LlmFixer`, el bucle de
  [016](../archivo/016-lector-de-contexto/spec.md) y el verificador de 009 reciben el
  `BudgetPort` en su construcción, en lugar del entero `budget_tokens_per_pr`.
- **El hecho, no el render.** `LlmReviewer` expone en `ReviewerResult` que se cortó
  por presupuesto (`budget_exhausted`) y los tokens que gastó; `ReviewOutcome` expone
  `tokens_spent` y `tokens_remaining`. Convertir eso en `ReviewCoverage`/`SkipReason.BUDGET`
  es de [012](../012-cobertura-de-la-review/spec.md) C6, que aún no existe: 017 no la
  espera para entregar el mecanismo.
- **Enmienda 2026-10-16 (deuda D21).** `reserve` devuelve `bool` pero hoy nadie lo
  mira: en el bucle de [016](../archivo/016-lector-de-contexto/spec.md) la guarda es
  solo `remaining`; `_reserve` descarta el `False`. Al traer el adaptador real hay
  que tratar `reserve(...) is False` como presupuesto agotado (B7) y cubrirlo con un
  test donde `reserve` devuelva `False` con `remaining` aún alto (el `MemoryBudget`
  actual siempre devuelve `True`). Afecta a `_reserve` en
  `src/agent_review/adapters/tool_loop.py`.
- **Si `reserve` devuelve `False`, no se llama a `commit`** (§8 Q4): no hubo reserva
  que ajustar, así que un `commit(key, 0, actual)` cobraría contra el contador un
  gasto que la reserva rechazó. El `commit` del consumidor queda **condicionado a
  `reserve is True`**; un `commit` sobre una clave sin reserva es un error del
  consumidor, no un cobro.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **Una persona se lo come todo** y las demás no revisan | Reparto «el primero que llega» con tope blando por persona (B10) |
| R2 | **Redis caído** | Sin Redis no hay review en `production` ([015](../015-puesta-en-marcha-real/spec.md) X4); fuera de `production`, adaptador en memoria |
| R3 | **El fixer se queda sin presupuesto** después de una review cara, o la clave caducó antes de la aprobación | Contador propio del fix (B11), con su clave y su TTL |
| R4 | **La estimación previa se queda corta** y el `usage` real supera la reserva | `commit` ajusta con el real; el exceso de una sola llamada está acotado por `max_tokens` |
| R5 | **La reserva se filtra** en un fallo transitorio y el contador se infla con cada reintento | `commit(reserved, 0)` en el camino de error (B8); test de reintento (§7.2) |
| R6 | **Dos implementaciones de `remaining` discrepan** (memoria vs Redis) y el bucle se comporta distinto en *replay* | Definición única (B9) y el test de concurrencia (§7.1) corre contra ambas |

## 7. Criterios de hecho

1. Con tres personas en paralelo y un presupuesto que solo da para dos, el gasto total
   no supera el tope (test con el adaptador en memoria y concurrencia real).
2. Un reintento descuenta del mismo presupuesto y **no** deja la reserva del intento
   fallido colgada (test con un modelo que falla una vez; cierra B8).
3. Agotado el presupuesto, la review termina anotando `budget_exhausted` en la persona
   y `tokens_spent`/`tokens_remaining` en el `ReviewOutcome`; el render como
   `SkipReason.BUDGET` de [012](../012-cobertura-de-la-review/spec.md) lo consume
   cuando 012 exista (test del hecho, no del render).
4. El fixer descuenta de **su** contador (`budget:fix:{review_key}`), no del de la
   review (test entre procesos con Redis falso; cierra B11).
5. Un `reserve` que devuelva `False` con `remaining` alto detiene la llamada igual que
   un `remaining` corto, **y no se llama a `commit`** (test con un doble que lo fuerce;
   cierra D21 y §8 Q4).
6. `D17` sale de [`arquitectura.md`](../../arquitectura.md) §7 y `D21` también (la
   enmienda la cierra); `make ci` en verde, con `make docs-check`;
   [`operacion.md`](../../operacion.md) §2 explica la semántica nueva de
   `LLM_BUDGET_TOKENS_PER_PR` (por **review**, no por llamada).

## 8. Preguntas abiertas

Todas resueltas el 2026-10-16. Se conservan con su respuesta para la traza:

- **Q1 — ¿Cómo se reparte entre personas?** *Resuelta:* «el primero que llega» sobre el
  contador atómico, con tope blando por persona de `2 × total / N` (B10). Reparto justo
  sin desperdiciar, y una persona no puede monopolizar.
- **Q2 — ¿El fix comparte el presupuesto de la review?** *Resuelta:* no; contador
  propio (`budget:fix:{review_key}`, B11). La aprobación humana puede cruzar el TTL de
  la review y una review cara no debe dejar sin fix lo aprobado.
- **Q3 — ¿Valor por defecto?** *Resuelta:* se mantiene `500000`, ahora por review
  (antes era por llamada). El coste medido en 006 (PR #3: ≈20 700 tokens, 25× por
  debajo) deja aire de sobra; bajarlo crearía falsos `budget` en la primera review
  real. El knob se recalcula cuando 006 tenga datos de producción.
- **Q4 — ¿Qué hace `commit` si `reserve` devolvió `False`?** *Resuelta:* **no se
  llama**. El `commit` del consumidor queda condicionado a `reserve is True`: sin
  reserva no hay nada que ajustar, y un `commit(key, 0, actual)` cobraría un gasto que
  la reserva rechazó. Un `commit` sobre una clave sin reserva es un error del
  consumidor, no un cobro. Nace de la enmienda D21 (§5).
