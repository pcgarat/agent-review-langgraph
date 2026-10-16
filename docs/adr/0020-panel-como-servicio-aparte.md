# ADR-0020 — El panel es un servicio aparte: *read model* + comandos encolados

- **Estado:** Aceptado
- **Fecha:** 2026-10-16

## Contexto

La observabilidad era **logs de texto** y endpoints de lectura suelta
([`operacion.md`](../operacion.md) §5): se podía saber *algo* preguntando a Redis o
mirando el latido del worker, pero no **ver** el sistema (cola, en vuelo, pausas,
descartes, gasto), ni seguir una verificación del sandbox, ni intervenir sobre una
review. La deuda estaba reconocida: D14 (sin métricas sobre tráfico real) y D16 (el
latido mide que el proceso vive, **no** que avanza).

Dos servicios ya publican puerto: la `api` (webhook del CI, en el camino crítico de
ingesta) y el `sandbox-runner` (aislado, ADR-0011). El worker es un bucle secuencial
de un solo escritor: procesa una review a la vez con `BLPOP` y `graph.invoke`
síncrono.

## Decisión

- **Servicio `web` propio**, opt-in por **perfil de compose** (`panel`), como el
  `tracing` de la 018. Ni UI dentro del worker ni un módulo más de la `api`: el
  webhook del CI no puede pagar el coste de un panel, y el worker no debe saber que
  existe.
- **CQRS-lite**: el panel **lee** (Redis para el estado operativo, el checkpointer
  para el recorrido) y **encola comandos** (`reviews:jobs`); **nunca** muta el estado
  del grafo. El worker sigue siendo el único escritor, y la decisión humana ya se
  encolaba así ([ADR-0008](0008-hitl-por-cli-y-http-con-cola-unica.md)).
- **Autenticación de sesión con rol** (`lector`/`operador`) para el panel, no el
  bearer de servicio de decisión: cancelar es una acción con consecuencias y el bearer
  autoriza a un servicio, no identifica a una persona.
- **El progreso del worker vive en Redis** (`reviews:active:<review_key>`, hash con
  TTL), no en el latido `HEARTBEAT_PATH`: el latido es el healthcheck de compose y no
  puede depender de Redis; el progreso sí lo necesita para verse desde fuera del
  worker. Cierra D16 en lo que respecta al worker de reviews.
- **Cancelación cooperativa en la frontera de nodo**: un único *seam* de envoltura
  (`graph/wrapper.py`) comprueba `reviews:cancel:<review_key>` **al empezar** cada
  nodo y, si está puesta, aborta. Una review **pausada** no pasa por ninguna
  frontera: su cancelación viaja como un `ReviewRequest` marcado y el worker la
  aplica al desencolar, **sin** reanudar el grafo.
- **La consola del sandbox es un buffer efímero en el runner** (`on_line` opcional en
  `ContainerRuntime.start`, SSE en `GET /verify/{run_key}/console`), con el mismo
  bearer. El runner **no** gana acceso a Redis: mantiene su aislamiento.

## Consecuencias

- Un servicio más que operar, pero apagado por defecto (sin el perfil `panel`, nada
  cambia). El panel se puede reiniciar sin tocar ninguna review.
- La latencia de la cancelación es **un nodo entero** (LangGraph no cancela un
  `invoke`); se declara en la UI y no se finge instantánea.
- Con el worker bloqueado en `graph.invoke`, un job de cancelación no se procesaría:
  por eso la señal duradera es la **marca** para lo que corre y el **job** para lo
  pausado. Borrar el hilo del checkpointer al abortar es la guarda frente a una
  reentrega que reanudaría un trabajo ya no querido.
  *Corrección (2026-10-16):* borrar el hilo no basta: sin él, un job rezagado
  arrancaría la review desde cero. La marca se conserva como *tombstone* hasta
  `CANCEL_TTL_S` (solo «Reintentar» la retira), y una pausa de fix no se marca.
- Cancelar **no** es reversible: si la review ya publicó, la UI solo ofrece «cancelar
  lo pendiente» y los comentarios del PR se quedan.
- El panel muestra diffs y salidas de PRs de terceros; mismo tratamiento que la 018
  §3.5 (self-hosted; la redacción de secretos sigue siendo un riesgo abierto).
- La consola del sandbox es *best-effort*: no persiste, no entra en el `VerifyReport`
  y su caída no afecta a la verificación.

## Alternativas descartadas

- **UI dentro del worker.** El worker es un bucle tonto y secuencial; meterle HTTP,
  sesiones y plantillas lo contamina y lo ata al ciclo de vida de las reviews.
- **Un módulo más de la `api`.** La `api` está en el camino crítico del webhook del
  CI y declara que **no** habla con el checkpointer
  ([`api/main.py`](../../src/agent_review/api/main.py)); meterle Postgres rompería esa
  separación.
- **Escritura directa del panel sobre el estado** (resolver la pausa o borrar el
  hilo desde la web). Sería un segundo escritor sobre el estado del grafo: la carrera
  entre el panel y el worker es exactamente lo que el diseño evita.
- **Progreso en el latido `HEARTBEAT_PATH`.** Acoplaría el healthcheck de compose a
  Redis: un Redis caído marcaría el worker como muerto.
- **Pub/sub asíncrono para la consola.** El runner verifica en un hilo bloqueante; un
  `asyncio.Queue` por suscriptor exigiría primitivas entre hilos. Leer el buffer con
  una espera corta es robusto y suficiente para una consola.
- **WebSocket en vez de SSE.** El flujo es unidireccional (servidor → navegador) y
  los comandos van por HTTP normal; SSE no añade protocolo ni estado de conexión.
