# ADR-0021 — La verificación del hallazgo es una cascada barata→cara cuyo estado viaja en el contrato

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

La auditoría del PR #3 ([006](../iniciativas/archivo/006-eval-de-la-review/spec.md))
dejó 9 hallazgos publicados con 6 falsos positivos y uno con el fallo descrito al
revés. Los falsos positivos tienen una causa común: el revisor afirma sobre código que
no ve y **fabrica** el contexto que falta. El contrato heredado de `ce-code-review`
([ADR-0002](0002-contrato-de-ce-code-review-sin-su-runtime.md)) no dejaba expresar
cuánto se había comprobado una afirmación: `confidence` lo autoasigna el modelo y
nadie lo contrasta, y `requires_verification` no lo leía ningún código. La severidad
publicada era la que el modelo decía.

## Decisión

- **La verificación es una cascada de capas, de la más barata a la más cara:** L0
  determinista (anclaje, `base_sha`), L1 contexto, L2 adversarial (otro rol y otro
  modelo con lectura del repo) y L3 ejecutable (un test que falla en el sandbox).
  Cada capa solo procesa lo que la anterior no resolvió.
- **El estado de la cascada viaja en el hallazgo**, en el campo
  `verification` (`unverified | verified_static | verified_grounded |
  verified_executed | refuted`). Nace `unverified`; solo la cascada lo promueve o lo
  refuta. Ningún emisor se autopromueve: el `LlmReviewer` normaliza lo que devuelva
  el modelo a `unverified`, y el `ToolingReviewer` emite `verified_static` porque su
  hallazgo sale de una herramienta que corrió.
- **Un veredicto lleva su prueba:** `verification != unverified` exige
  `verification_evidence` (I2).
- **Una hipótesis grave lleva su escenario:** un `P0`–`P2` `unverified` exige
  `failure_scenario` (`input/estado → file:line → resultado`) (I1).
- **El revisor declara lo que no ve** con `context_scope` (`in_diff | outside_diff`).
  «No probado» no es un estado propio: es `unverified` con `outside_diff`.
- **Un `refuted` se conserva** en el estado: alimenta la métrica de falsos positivos
  y evita volver a proponerlo.
- **Los campos son hechos, no decisiones.** Ninguna capa cambia `severity`; la
  severidad publicada la calcula solo el gate de política de
  [011](../iniciativas/011-gate-de-severidad/spec.md), desde `autonomy.yaml`
  ([ADR-0003](0003-autonomia-como-politica-por-hallazgo.md)).
- `requires_verification` se retira del contrato: lo sustituye `verification`.

## Consecuencias

- El contrato publica cuatro campos nuevos con *default* y dos condiciones
  (`if/then`); el `required` de nivel superior no gana nada. Modelo y JSON cambian en
  el mismo commit (`schema_matches_domain`).
- Un checkpoint anterior sigue cargando: el serializador recurre a
  `model_construct` si la validación falla y `Finding` ignora campos extra.
- **El escenario obligatorio no filtra falsos positivos por sí solo.** En la
  regrabación del *gold set* (2026-10-17, `deepseek-v4-flash`) el modelo rellenó el
  escenario de los 7 hallazgos y los 7 eran falsos positivos con escenarios
  plausibles pero inalcanzables. El filtro real es la verificación (L2,
  [009](../iniciativas/009-verificador-adversarial/spec.md)); el escenario es lo que
  le da algo concreto que refutar.
- L2 y L3 añaden coste: se pagan contra el presupuesto por review
  ([ADR-0019](0019-presupuesto-de-tokens-por-review.md)).

## Alternativas descartadas

- **Seguir opinando con el mismo modelo** (pedirle más cuidado o más `confidence`):
  un sello de goma; el mismo modelo que fabricó el contexto lo confirma.
- **Publicar solo lo de `confidence` alto:** falsa precisión. `confidence` es el
  juicio del propio modelo, no una comprobación.
- **Que cada capa degrade la severidad por su cuenta:** nadie podría explicar por qué
  un hallazgo salió como `P3`. La severidad tiene un único dueño (011).
- **Un estado «no probado» propio en el enum:** duplicaría lo que ya dicen
  `verification` y `context_scope`.
