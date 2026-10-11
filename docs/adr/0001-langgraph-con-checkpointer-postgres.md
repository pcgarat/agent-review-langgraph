# ADR-0001 — LangGraph como motor, con checkpointer durable en Postgres

- **Estado:** Aceptado
- **Fecha:** 2026-10-10

## Contexto

El agente tiene que ejecutar varios revisores en paralelo, fusionar sus resultados,
aplicar una política y, en Fase 2, **pausarse días** esperando a una persona sin
perder el estado. Un reinicio del worker no puede perder una review pausada ni hacer
que se publique dos veces.

## Decisión

- El flujo es un `StateGraph` de LangGraph con estado tipado (`ReviewState`) y
  reducers explícitos: `scope → persona×N (Send) → merge → policy → hitl → fix →
  publish → notify`.
- El estado se persiste por hilo (`thread_id`) con `langgraph-checkpoint-postgres`.
  En producción, si Postgres no responde, el worker **no arranca**. Fuera de
  producción cae a `InMemorySaver`.
- El serializador tiene una *allowlist* explícita con los tipos de dominio que viajan
  por el estado (`checkpointer._STATE_TYPES`).
- La pausa humana es `interrupt()`. No hay `sleep` ni sondeo hecho a mano.

## Consecuencias

- La durabilidad del HITL y la no-repetición tras un reinicio salen gratis del checkpoint.
- El checkpoint es la **unidad de reintento**: un hilo a medias continúa con
  `invoke(None)` ([ADR-0007](0007-reintento-en-dos-capas.md)).
- Un tipo nuevo que viaje en el estado **debe** añadirse a `_STATE_TYPES`. Si no, el
  roundtrip de un hilo pausado depende del msgpack permisivo.
- `timeout=` de `add_node` no sirve: LangGraph solo lo aplica a nodos async y aquí
  son síncronos. El techo de tiempo se pone en cada adaptador (`TWG_TIMEOUT_S`).
- La retención de checkpoints se aísla tras `RetentionPolicy`, porque la API de poda
  de los savers oficiales está a medio implementar.

## Alternativas descartadas

- **Pipeline Python a mano + cola.** Habría que reimplementar fan-out, pausa durable
  y reanudación.
- **Invocar la skill `ce-code-review` como runtime.** Ver [ADR-0002](0002-contrato-de-ce-code-review-sin-su-runtime.md).
