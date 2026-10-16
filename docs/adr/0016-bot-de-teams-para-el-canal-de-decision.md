# ADR-0016 — Un bot de Teams es el canal de decisión, con un hilo por PR

- **Estado:** Propuesto
- **Fecha:** 2026-10-16

## Contexto

El [ADR-0008](0008-hitl-por-cli-y-http-con-cola-unica.md) eligió la Opción B del
diseño base: Teams **solo avisa** por Incoming Webhook y la decisión llega por CLI o
`POST /reviews/{review_key}/decision`. Se descartó el bot de Azure (Opción A) por
«cero infraestructura nueva».

Dos hechos invalidan ese punto de partida:

1. **El Incoming Webhook ya no existe.** Microsoft retiró los Office 365 Connectors
   entre el 18 y el 22 de mayo de 2026. Peor: los endpoints retirados siguen
   respondiendo `200` descartando el payload, así que
   [`TeamsWebhookAdapter`](../../src/agent_review/adapters/teams_webhook.py) puede
   «funcionar» sin publicar nada.
2. **Ninguna vía sin bot da las tres cosas a la vez** (recibir respuestas, hilo por
   PR y editar la tarjeta). Microsoft Graph exige permiso **delegado**
   (`ChannelMessage.Send`): un *service principal* no puede publicar en un canal en
   un escenario normal. El webhook de Power Automate *Workflows* permite responder,
   pero su trigger de canal **no dispara con replies** y su tarjeta interactiva es
   de un solo disparo dentro del flujo.

El SDK de Teams tiene soporte de **Python GA** y cubre el caso proactivo y de hilo:
`app.reply(conversationId, rootMessageId, …)` publica dentro del hilo de un PR sin
tener una actividad entrante a mano, y `updateActivity` permite editar en vez de
spamear.

## Decisión

- **El canal de Teams pasa a ser un bot** (Azure Bot + Teams SDK, Python). El
  Incoming Webhook se retira.
- **Ingress y egress viven en un servicio propio, `teams-bot`.** El *messaging
  endpoint* (`POST /api/messages`) valida el JWT del Bot Framework y traduce la
  interacción de la tarjeta a un `HitlDecision`; el tráfico exterior llega por un
  **único ingress** que enruta a `teams-bot` (el servicio `api` no cambia), y el
  egress llega desde el worker por **HTTP interno con bearer** (mismo patrón que el
  `sandbox-runner`).
- **La decisión se encola, no se reanuda en el bot.** El bot llama a `build_decision`
  y encola en `reviews:jobs`, exactamente como el CLI y la API. La «cola única» y la
  reanudación en el worker del [ADR-0008](0008-hitl-por-cli-y-http-con-cola-unica.md)
  se mantienen **intactas**: el grafo y el worker siguen sin saber que Teams existe
  (`ChatPort` es el único contrato).
- **Una decisión cubre todos los pendientes.** La tarjeta ofrece un botón por hallazgo
  y un «Enviar» que permanece **deshabilitado hasta que todos tengan decisión**; la UX
  es incremental (la tarjeta se actualiza con cada interacción), pero el `HitlDecision`
  se emite **atómico** al confirmar. No se relaja el contrato «hay que decidirlo todo
  de una vez», y `build_decision` sigue siendo la última palabra.
- **La primera decisión es final.** Tras decidir, la tarjeta pasa a «decidido por X» y
  deja de ser editable desde Teams; rectificar es cosa del CLI/HTTP. Evita la carrera
  con la reanudación del worker y la idempotencia extra.
- **Hilo por PR.** El bot mantiene el mapeo `repo:pr → {conversation_id,
  root_message_id}` en Redis; si falta, crea un hilo nuevo. La primera review del PR
  abre el hilo raíz; el resto va como reply y la tarjeta se **edita** al avanzar el
  grafo. El hilo es UI, no verdad: la verdad durable sigue en Postgres.
- **Autorización: se confía en el canal.** Cualquier miembro del canal puede decidir.
  Se asume y se documenta. El `actor` es la identidad de Entra ID que reporta el SDK,
  para que quede traza en la auditoría.
- **CLI y `POST /decision` se mantienen** como canal de respaldo.

## Consecuencias

- Hace falta un **endpoint HTTPS público** (ingress + TLS) y el alta de una app en
  Entra ID con consentimiento de admin y *sideload* del bot en el equipo: es el coste
  real del proyecto, más que el código.
- **El egress por HTTP no es durable.** Si el bot está caído, el aviso se pierde; no
  se garantiza entrega. Es aceptable porque hoy un fallo de Teams ya es no-op (nodo
  `notify` propio, B9), pero nadie debe asumir garantía de entrega.
- El bot pasa a ser **el único escritor** del canal: solo él posee el mapeo de hilos.
- `REVIEWS_DECISION_TOKEN` protege el canal de respaldo; el permiso de decidir desde
  Teams pasa a ser «pertenecer al canal», que es más débil. Sigue siendo bloqueo duro
  lo protegido: [ADR-0009](0009-la-proteccion-manda-sobre-la-aprobacion.md) no se
  toca, así que un `approved` en `never_auto_paths`/`protected_subject` no produce
  parche.
- `TEAMS_WEBHOOK_URL` y `TEAMS_ENABLED` desaparecen; entran las variables del bot.

## Alternativas descartadas

- **Migrar solo a un webhook de Workflows.** Mantiene la notificación viva pero no
  cierra el bucle (no hay trigger para replies) ni da tarjeta editable; obligaría a
  una segunda migración al bot. Se descarta salvo como medida puente si el bot se
  retrasa.
- **Microsoft Graph con token delegado u OBO.** Un *service principal* no puede
  publicar en un canal; el modo aplicación solo vale para migración
  (`Teamwork.Migrate.All`).
- **Un flujo de Power Automate como puente (Opción C del diseño base).** Depende de
  licencias, de un servicio difícil de probar y de un trigger que no ve replies.
- **Meter el *messaging endpoint* dentro del servicio `api`.** Reparte la
  responsabilidad de recibir tráfico del Bot Framework entre dos procesos y rompe el
  aislamiento del servicio que hoy no publica en el canal.
- **Decidir incrementalmente de verdad** (un hallazgo por reanudación). Relajaría
  «cubrir todos los pendientes», obligaría a cambiar `hitl` y el worker, y
  multiplicaría reanudaciones y publicaciones. Solo si se necesita que distintas
  personas decidan partes distintas.
