# ADR-0004 — La review se dispara por el artefacto del CI, no por el PR

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

El webhook de PR llegaba **antes** de que el CI produjera la salida de PHPStan. El
worker no encontraba el artefacto y publicaba «0 hallazgos»: un fallo de integración
disfrazado de PR limpio. «PR creado» y «datos de la review listos» son dos eventos
distintos.

## Decisión

- Se retira el webhook de PR. El CI del repo revisado hace `POST /webhook/tooling`
  con el artefacto, firmado con HMAC-SHA256 (`X-Reviews-Signature`).
- La API guarda el artefacto y lo registra en un `PendingReview`. **Solo encola** la
  review cuando han llegado todas las herramientas esperadas del perfil (la barrera).
- Hay dos claves de idempotencia. `ingest_key` (por artefacto) evita ingerir el mismo
  evento dos veces. `review_key` (por ejecución) evita encolar la misma review dos
  veces.
- Si falla el paso que protege una clave (persistir, `observe`, encolar), la clave se
  **libera** antes de devolver el error, para que el reintento del CI entre de nuevo.
- Un `repo` distinto del configurado se rechaza con `400`. El adaptador de VCS siempre
  usa el repo configurado, así que sin este filtro se comentaría un PR ajeno.
- Si vence el TTL sin todos los artefactos, se lanza una **pasada degradada** que
  publica avisando de que la review está incompleta.
- El `POST` del CI **no** es un check de merge: va con `|| true` y su fallo no pone el
  pipeline en rojo.

## Consecuencias

- La carrera desaparece por construcción.
- **El artefacto es un puente desechable.** En `emotivoo-v2`, `make stan` es
  bloqueante: un pipeline verde produce un artefacto vacío. La señal útil está en
  ejecutar (personas LLM, checks fuera del CI, verificar fixes), no en transportar.
  **No se añaden capacidades a la ingesta.**
- El puente se retira cuando: (1) exista un `CheckRunner` que ejecute las recetas del
  perfil en el sandbox, (2) la verificación lo use y (3) se mida que la señal es igual
  o mejor.
- Si el CI nunca manda el `POST`, todas las reviews salen degradadas por TTL. Es un
  riesgo aceptado.

## Alternativas descartadas

- **Mantener el webhook de PR y reintentar la lectura del artefacto.** Sigue siendo
  una carrera, solo que más lenta.
- **Descargar el artefacto de Bitbucket Pipelines.** `twg` no descarga artefactos de
  pipeline.
