# ADR-0008 — HITL por CLI/HTTP, cola única y reanudación en el worker

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

Teams solo tiene un Incoming Webhook: el agente puede avisar, pero **no** recibir
respuestas. Las opciones eran un bot de Azure con Adaptive Cards (opción A), decidir
por otro canal (opción B) o un puente de Power Automate (opción C).

## Decisión

- **Opción B.** Se decide por el CLI (`agent_review list`, `agent_review decide`) o
  por `POST /reviews/{review_key}/decision` (bearer `REVIEWS_DECISION_TOKEN`). Teams
  solo avisa. Los comentarios del PR como canal quedan para más adelante.
- **Cola única.** Una decisión es un `ReviewRequest` con `decision`. La API y el CLI
  **solo encolan**. El worker, que ya tiene el grafo y el checkpointer, hace
  `Command(resume=…)` sobre el mismo hilo y hereda reintentos y DLQ.
- **El inbox en Redis es índice, no verdad.** La verdad durable es el checkpoint. El
  worker repone en el inbox las pausas que se hayan perdido (reconciliación) y nunca
  poda un hilo pausado.
- **Hay que decidirlo todo de una vez.** Una decisión tiene que cubrir **todos** los
  pendientes y solo esos (si no, `409` o exit 2). Si llega una incompleta o ilegible,
  el nodo `hitl` vuelve a pausar en vez de lanzar, porque LangGraph reentregaría el
  mismo valor de resume en cada intento.
- `expired` lo emite solo el sistema: pasado `DECISION_TTL_S` (7 días), con
  `actor=system:ttl`.
- **Se publica al decidir, no al pausar.** Al pausar solo sale el aviso, sin marca.
- La decisión humana no vuelve a pasar por la política, pero tampoco salta las
  protecciones ([ADR-0009](0009-la-proteccion-manda-sobre-la-aprobacion.md)).
- No se extrae un `CheckpointerPort`: ningún otro proceso necesita el saver.

## Consecuencias

- Una reentrega del job mientras la review está pausada no re-ejecuta `scope` ni las
  personas.
- Una decisión que llega cuando el hilo ya terminó (por ejemplo, ganó el `expired`) se
  descarta con un aviso en el log.
- Quien tiene `REVIEWS_DECISION_TOKEN` decide sobre todos los pendientes: es un
  secreto de alto valor. En `development`, con `change-me`, el endpoint **no exige
  token**.
- Si Redis cae, la API y el CLI caen a almacenes en memoria que el worker no ve. Es un
  riesgo abierto (R1 de la revisión de `main`).

## Alternativas descartadas

- **Bot de Azure (A).** Necesita registro en Azure AD y un endpoint público. Se
  reconsidera cuando la opción B se quede corta.
- **Power Automate (C).** Depende de licencias y de un servicio difícil de probar.
- **Reanudar desde la API.** Obligaría a compartir el checkpointer entre procesos.
