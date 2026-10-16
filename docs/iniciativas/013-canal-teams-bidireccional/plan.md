Última modificación: 2026-10-16 | Creado: 2026-10-16

# Plan: Canal bidireccional de Teams (bot con hilo por PR)

Reconstruye el canal de Teams sobre un **bot** (Azure Bot + Teams SDK Python) porque
el Incoming Webhook está retirado y porque ninguna vía sin bot da a la vez recepción,
hilo por PR y tarjeta editable. La especificación y las decisiones (C1–C11) viven en
[`spec.md`](spec.md); las tareas accionables, en [`todo.md`](todo.md).

## Overview

Un servicio nuevo `teams-bot` con dos caras: **ingress** (`POST /api/messages`, el
*messaging endpoint* del Bot Framework, validando el JWT) y **consumo de una outbox**
(`reviews:notices`), donde el worker deja los avisos. El bot **no conoce el grafo**:
traduce la interacción de la tarjeta a una `HitlDecision` con `build_decision` y la
**encola** en `reviews:jobs`, igual que el CLI y `POST /decision`. La «cola única» y la
reanudación en el worker del [ADR-0008](../../adr/0008-hitl-por-cli-y-http-con-cola-unica.md)
se mantienen intactas.

## Decisiones de arquitectura

- **Un solo escritor del canal.** El worker delega el egress en el bot; solo el bot
  posee el mapeo de hilos y edita la tarjeta. Es el mismo principio que el
  `fix-worker`/PR: una pluma por medio.
- **Egress por cola, no por llamada.** `ChatPort` pasa a **encolar** en
  `reviews:notices` (`OutboxChatAdapter`), replicando el patrón ya probado de
  `ReviewQueue`/`FixQueue` (Redis + DLQ + `InMemory` para tests). Un aviso con el bot
  caído no se pierde: se publica al arrancar. La capa HTTP interna queda como detalle
  del bot (`ChatTransport`), no del worker.
- **Aviso estructurado.** El `ChatNotice` de pausa lleva la `ApprovalRequest`, no el
  texto de `render_pause_notice`: el bot no interpreta prosa para pintar la tarjeta.
- **El wheel del grafo no cambia.** `ChatPort` sigue siendo el contrato; se retira
  `TeamsWebhookAdapter`. El worker no importa el SDK de Teams.
- **El bot no reanuda.** Solo encola. La decisión viaja **en** el `ReviewRequest` con
  `decision`; el worker hace `Command(resume=…)` sobre el mismo hilo (ADR-0008).
- **Decisión atómica (C3/C11).** La tarjeta se actualiza con cada interacción, pero
  Enviar está deshabilitado hasta que todo está marcado y la primera decisión es
  final. `build_decision` sigue siendo la última palabra. El **estado es por usuario**
  (C8): dos personas marcan en paralelo sin pisarse y cada una ve lo suyo; Enviar se
  habilita cuando *esa* persona ha cubierto todos los pendientes.
- **Hilo = UI, no verdad (C6).** El mapeo `repo:pr → {conversation_id,
  root_message_id}` vive en Redis y se puede perder: se crea otro hilo, no se
  corrompe estado. La verdad durable sigue en Postgres.
- **Retirada en el mismo cambio (C1/C4).** `TeamsWebhookAdapter` y
  `TEAMS_WEBHOOK_URL` desaparecen cuando entra la outbox; `TEAMS_ENABLED` se mantiene
  como interruptor del canal.

## Orden y dependencias

```
T1  contrato del aviso (ChatNotice) + OutboxChatAdapter (Redis + InMemory)
 └── T2  tarjeta de decisión (render + estado por usuario + Enviar bloqueado)
      └── T3  decisión → build_decision → encolar (sin Teams real, fakes)
 └── T4  hilo por PR (mapeo Redis, root message, reply, updateActivity)
      └── T5  servicio teams-bot: consumo de la outbox + ChatTransport interno
           └── T6  teams-bot en Compose + settings + healthcheck
                └── T7  ingress del Bot Framework (JWT) y traducción de acciones
 └── T8  retirada de TeamsWebhookAdapter y variables del webhook
      └── T9  E2E con fakes (pausa → decisión en tarjeta → reanudación → aviso)
T10 pendientes humanos (alta en Entra ID, consentimiento, ingress/TLS, sideload)
T11 documentación viva + README + índices
```

T1–T9 no requieren Azure real (fakes del SDK y de la cola). **T10 es la ruta
crítica de verdad**: sin el alta del bot y el endpoint público no se puede probar el
criterio de hecho nº 1. Existe un **atajo que evita Azure**: el *Teams Developer CLI*
(`teams app create`) crea un bot gestionado por Teams (o, con `--azure`, el Azure Bot
completo) y escribe las credenciales. T11 cierra la documentación en el mismo cambio
que el código.

## Checkpoints

- **Tras T1–T3:** el worker encola el aviso de una review publicada y de una pausa en
  `reviews:notices` (cola observable en un test, sin HTTP ni SDK); una decisión armada
  desde la tarjeta se encola en `reviews:jobs` con `build_decision`; un pendiente sin
  marcar no permite enviar. `make ci` en verde.
- **Tras T4–T7:** el bot consume la outbox, resuelve el hilo y publica/edita la
  tarjeta; el mismo `repo:pr` reutiliza el hilo. Con el SDK falso y tests que afirman
  el payload.
- **Tras T8:** `TeamsWebhookAdapter` y `TEAMS_WEBHOOK_URL` han desaparecido;
  ningún test depende de ellos.
- **Tras T9–T11:** E2E de fakes: review → pausa → aviso en la outbox → tarjeta →
  decisión → reanudación → aviso, sin duplicar publicación; un aviso con el bot caído
  se publica al arrancar el bot. Documentos vivos al día y `make docs-check` verde.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El canal muerto se queda muerto si el bot se retrasa | La outbox acumula; el bot publica al arrancar. Puente opcional con Workflows (spec §2) |
| Aviso perdido por un bot caído | Ya no se pierde: outbox durable (C10); un aviso de pausa vencida se descarta al drenar (C13) |
| Outbox que crece sin fin | Drenado con recorte/TTL (C13) |
| Alta del bot en Entra ID bloquea la validación | T10 explícito en el `todo.md` como pendiente humano; T1–T9 verificables con fakes |
| SDK de Teams v2 muy nuevo | Versión fijada; el plugin `microsoft-teams-botbuilder` permite convivir con BotBuilder |
| Carrera al re-decidir | La primera decisión es final (C11); rectificar por CLI/HTTP |
| Ruido de menciones en el canal | Pregunta abierta §9 de la spec, resuelta en T2 |
