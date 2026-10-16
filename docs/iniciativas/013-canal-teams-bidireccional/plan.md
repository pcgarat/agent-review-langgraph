Última modificación: 2026-10-16 | Creado: 2026-10-16

# Plan: Canal bidireccional de Teams (bot con hilo por PR)

Reconstruye el canal de Teams sobre un **bot** (Azure Bot + Teams SDK Python) porque
el Incoming Webhook está retirado y porque ninguna vía sin bot da a la vez recepción,
hilo por PR y tarjeta editable. La especificación y las decisiones (C1–C11) viven en
[`spec.md`](spec.md); las tareas accionables, en [`todo.md`](todo.md).

## Overview

Un servicio nuevo `teams-bot` con dos caras: **ingress** (`POST /api/messages`, el
*messaging endpoint* del Bot Framework, validando el JWT) y **egress interno**
(HTTP con bearer, desde el worker). El bot **no conoce el grafo**: traduce la
interacción de la tarjeta a una `HitlDecision` con `build_decision` y la **encola** en
`reviews:jobs`, igual que el CLI y `POST /decision`. La «cola única» y la reanudación
en el worker del [ADR-0008](../../adr/0008-hitl-por-cli-y-http-con-cola-unica.md) se
mantienen intactas.

## Decisiones de arquitectura

- **Un solo escritor del canal.** El worker delega el egress en el bot; solo el bot
  posee el mapeo de hilos y edita la tarjeta. Es el mismo principio que el
  `fix-worker`/PR: una pluma por medio.
- **El wheel del grafo no cambia.** `ChatPort` sigue siendo el contrato; se añade
  `TeamsBotChatAdapter` (HTTP) y se retira `TeamsWebhookAdapter`. El worker no
  importa el SDK de Teams.
- **El bot no reanuda.** Solo encola. La decisión viaja **en** el `ReviewRequest` con
  `decision`; el worker hace `Command(resume=…)` sobre el mismo hilo (ADR-0008).
- **Decisión atómica (C3/C11).** La tarjeta se actualiza con cada interacción, pero
  Enviar está deshabilitado hasta que todo está marcado y la primera decisión es
  final. `build_decision` sigue siendo la última palabra.
- **Hilo = UI, no verdad (C6).** El mapeo `repo:pr → {conversation_id,
  root_message_id}` vive en Redis y se puede perder: se crea otro hilo, no se
  corrompe estado. La verdad durable sigue en Postgres.
- **Retirada en el mismo cambio (C1/C4).** `TeamsWebhookAdapter`, `TEAMS_WEBHOOK_URL`
  y `TEAMS_ENABLED` desaparecen cuando entra el bot.

## Orden y dependencias

```
T1  contrato y esqueleto del adaptador (ChatPort → TeamsBotChatAdapter, fakes)
 └── T2  tarjeta de decisión (render + estado por usuario + Enviar bloqueado)
      └── T3  decisión → build_decision → encolar (sin Teams real, fakes)
 └── T4  hilo por PR (mapeo Redis, root message, reply, updateActivity)
      └── T5  egress interno del bot (notify / notify_pending + bearer)
           └── T6  servicio teams-bot + Compose + settings + ingress
                └── T7  ingress del Bot Framework (JWT) y traducción de acciones
 └── T8  retirada de TeamsWebhookAdapter y variables del webhook
      └── T9  E2E con fakes (pausa → decisión en tarjeta → reanudación → aviso)
T10 pendientes humanos (alta en Entra ID, consentimiento, ingress/TLS, sideload)
T11 documentación viva + README + índices
```

T1–T9 no requieren Azure real (fakes del SDK y del transporte). **T10 es la ruta
crítica de verdad**: sin el alta del bot y el endpoint público no se puede probar el
criterio de hecho nº 1. T11 cierra la documentación en el mismo cambio que el código.

## Checkpoints

- **Tras T1–T3:** existe el contrato del bot y una decisión armada desde la tarjeta se
  encola en `reviews:jobs` con `build_decision`; un pendiente sin marcar no permite
  enviar. `make ci` en verde.
- **Tras T4–T7:** el worker publica avisos por el bot; el mismo `repo:pr` reutiliza el
  hilo; la tarjeta se edita al avanzar el grafo. Sin Teams real, con el transporte
  falso y tests que afirman el payload.
- **Tras T8:** `TeamsWebhookAdapter` y las variables del webhook han desaparecido;
  ningún test depende de ellas.
- **Tras T9–T11:** E2E de fakes: review → pausa → tarjeta → decisión → reanudación →
  aviso, sin duplicar publicación. Documentos vivos al día y `make docs-check` verde.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El canal muerto se queda muerto si el bot se retrasa | Puente opcional: migrar el egress a un webhook de Workflows mientras tanto (spec §2) |
| Egress no durable (bot caído → aviso perdido) | Se documenta; el estado no se pierde y el CLI/HTTP es el respaldo real (C9) |
| Alta del bot en Entra ID bloquea la validación | T10 explícito en el `todo.md` como pendiente humano; T1–T9 verificables con fakes |
| SDK de Teams v2 muy nuevo | Versión fijada; el plugin `microsoft-teams-botbuilder` permite convivir con BotBuilder |
| Carrera al re-decidir | La primera decisión es final (C11); rectificar por CLI/HTTP |
| Ruido de menciones en el canal | Pregunta abierta §9 de la spec, resuelta en T2 |
