# ADR-0024 — La severidad publicada la decide un gate de política, no el modelo

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

La cascada de verificación ([ADR-0021](0021-cascada-de-verificacion-del-hallazgo.md))
deja hechos en el hallazgo (`verification`, evidencia, `pre_existing`,
`context_scope`) pero **no** cambia `severity`: L0/L1/L2 solo emiten hechos. La
política ([ADR-0003](0003-autonomia-como-politica-por-hallazgo.md)) elegía la
**acción** a partir de la severidad que el modelo decía. Un `P1` inventado se
publicaba como `P1`. El bloque `severity_gate` de `autonomy.yaml` ya era el umbral
de candidatos de L2; faltaba aplicarlo a la publicación
([011](../iniciativas/archivo/011-gate-de-severidad/spec.md)).

## Decisión

- Antes de las reglas de acción, el motor aplica un **gate de severidad** declarado
  en `config/autonomy.yaml` (`severity_gate`):
  - `require_verified_for` + `verified`: un hallazgo en el umbral sin veredicto
    positivo se **degrada** (`degrade_to`, por defecto `P3`) y solo puede tener
    acción `report`.
  - `on_refuted`: `drop` (no se publica; sigue en `merged` para la métrica) o
    `summary` (solo resumen, nunca inline).
- La severidad **efectiva**, el motivo y el modo de publicación viajan en
  `Decision` (`severity`, `severity_reason`, `publication`). El `Finding.severity`
  no se muta.
- El gate es el **único** sitio que decide la severidad publicada. El umbral
  `require_verified_for` es el mismo que usa L2 para elegir candidatos.
- El veredicto se muestra en inline/resumen solo en el umbral, en degradados y en
  refutados publicados; no en un `P2`/`P3` nativo sin tocar.

## Consecuencias

- Un `P0`/`P1` sin verificar deja de ensuciar la cola de críticos.
- Cambiar el umbral o `on_refuted` es editar el YAML y reiniciar el worker; no hay
  que desplegar lógica nueva.
- ADR-0003 sigue mandando sobre la **acción**; este ADR manda sobre la
  **severidad publicada** previa a esa acción.
- `on_unverified: possibles` queda declarado como alternativa futura; hoy el motor
  solo acepta `degrade`.

## Alternativas descartadas

- **Clave `verification` en el `when` con `severity_override`.** Mezcla acción y
  publicación en las mismas reglas; el umbral de L2 tendría otra fuente.
- **Que L0/L2 bajen la severidad.** Tres dueños, umbrales contradictorios y sin
  auditoría única.
- **Enmendar ADR-0003.** El gate es una decisión distinta (severidad vs acción);
  un ADR aceptado no se reescribe.
