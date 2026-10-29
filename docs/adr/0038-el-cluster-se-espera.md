# ADR-0038 — Lo que falla en el clúster se espera, no se reintenta

- **Estado:** Aceptado
- **Fecha:** 2026-10-29

Sustituye el punto 5 de [ADR-0037](0037-la-skill-con-un-backend-ajeno.md) y su
consecuencia sobre el gateway caído. El resto de ADR-0037 sigue vigente. Spec:
[032](../iniciativas/archivo/032-skill-con-modelo-de-la-cluster/spec.md).

## Contexto

ADR-0037 trataba dos fallos del destino clúster como transitorios del job:

- Un 429 que el gateway no absorbía con sus tres reintentos salía como
  `ClaudeProcessCrashed`. El worker reencolaba de inmediato, sin espera, y a
  los `MAX_REVIEW_ATTEMPTS` la review acababa en la DLQ aunque no tuviera nada
  roto. Cada intento era una pasada completa del CLI.
- Con el gateway caído, `GET /session` respondía «no», y el worker aparcaba
  como `session`. El aviso de Teams decía «Inicia sesión en el panel», cosa
  que con el clúster no existe.

Ninguno de los dos es un fallo de la review. Se arreglan solos cuando el
clúster deja de limitar o el gateway vuelve.

## Decisión

1. **Un 429 persistente del clúster espera un enfriamiento.** El runner
   responde `503` (no `429`, que sigue siendo el cupo de Anthropic) y el
   cliente lo traduce a `ClaudeClusterLimited`. El worker aparca ese job y los
   siguientes con el motivo `cluster-wait` y un aviso que caduca a los 300 s.
   Al caducar, el barrido los devuelve a `reviews:jobs`. No gasta intentos ni
   va a la DLQ.
2. **El gateway caído tiene su propio motivo.** `GET /session` responde
   `{"active", "reason"}`, donde `reason` es `session`, `gateway` o `null`. El
   worker aparca con ese motivo y avisa una vez. En cuanto el gateway responde,
   suelta lo aparcado por `gateway` y borra el aviso, para que la siguiente
   caída avise otra vez. Un runner que no manda `reason` cuenta como `session`.
3. **Cambiar de destino suelta lo que esperaba al otro.** Elegir un modelo del
   clúster suelta `session` y `quota-wait` y borra el enfriamiento del cupo.
   Elegir Claude suelta `gateway` y `cluster-wait` y borra su enfriamiento.
   `quota` y `ceiling` no se sueltan nunca solos.

## Consecuencias

- `ParkReason` gana `gateway` y `cluster-wait`. El panel los pinta en
  «Aparcados» con su explicación.
- Un clúster que limita sin parar no llena la DLQ. La review vuelve a
  intentarse cada cinco minutos, sin límite, como `quota-wait` con el cupo.
- El contrato de `GET /session` crece, pero `active` sigue presente. Un worker
  viejo contra un runner nuevo funciona igual que antes.
- Un gateway sin clave del clúster no arranca, así que no da health, y sus
  reviews esperan como `gateway` en vez de fallar una a una.

## Alternativas descartadas

- **Reencolar con *backoff* y contar intentos.** La cola no tiene entrega
  diferida. Esperar dentro del worker bloquearía el bucle para el resto de
  reviews, y contar intentos acabaría igualmente en la DLQ con algo que no es
  un fallo.
- **Reutilizar `quota-wait` para el clúster.** Mezcla dos causas: el panel no
  podría decir si esperar al cupo de Anthropic o al clúster, y volver a Claude
  soltaría una espera que sí le aplica.
- **Que el worker conozca el destino.** El worker no ve el modelo (ADR-0037).
  Basta con que el runner diga el motivo.
