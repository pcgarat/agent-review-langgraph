Última modificación: 2026-10-16 | Creado: 2026-10-16

# Todo: Canal bidireccional de Teams (bot con hilo por PR)

Checklist del bot. El diseño y las decisiones (C1–C11) están en [`spec.md`](spec.md);
el orden, en [`plan.md`](plan.md). Spec aprobada el 2026-10-16. Nada cerrado todavía.

## Bloque 1 — Contrato, tarjeta y decisión (sin Teams real)

- [ ] **T1** `ChatPort` al bot: adaptador y fakes
  - `TeamsBotChatAdapter` (HTTP + bearer) que implementa `ChatPort` con `notify` y
    `notify_decision_pending`; `build_chat` en worker usa el adaptador cuando hay URL
    del bot. Un transporte falso para los tests, sin SDK real.
  - Aceptación: con URL vacía el canal es no-op (como hoy sin webhook); un fallo HTTP
    transitorio no tumba la review (reintento del nodo `notify`, B9).
  - Verifica: `tests/test_teams.py` (adaptado) y `tests/test_worker.py`.
  - Ficheros: `src/agent_review/adapters/teams_bot.py`, `src/agent_review/ports/chat.py`.

- [ ] **T2** Tarjeta de decisión: render, estado por usuario y Enviar bloqueado
  - Render de la tarjeta a partir de `ApprovalRequest` (un botón por hallazgo:
    `aprobar`/`rechazar`/`noted`) con `Action.Execute`; el estado marcado por usuario
    se guarda en `reviews:teams:card:<review_key>`. Enviar deshabilitado mientras
    quede un pendiente sin marcar (C3). Contexto mostrado por hallazgo: resolver §9 de
    la spec (título, severidad, fichero:línea; `evidence` solo si no alarga de más).
  - Aceptación: la tarjeta refleja todos los pendientes del `ApprovalRequest`; la
    acción de un hallazgo devuelve la tarjeta con esa marca conservada.
  - Verifica: `tests/test_teams.py` (payload de la tarjeta y estado).
  - Ficheros: `src/agent_review/adapters/teams_bot.py`, tests.

- [ ] **T3** Decisión → `build_decision` → encolar
  - Traducción de las marcas de la tarjeta a `HitlDecision` con `build_decision`
    (mismo camino que el CLI): lee el inbox `reviews:paused:<key>`, valida que cubre
    todos los pendientes y encola en `reviews:jobs`. `actor` = identidad de Entra ID.
  - Aceptación: una decisión incompleta se rechaza sin tocar el grafo (misma regla
    que el `409` de la API); una completa reanuda el mismo hilo sin duplicar.
  - Verifica: `tests/test_teams.py`, `tests/test_hitl_e2e.py` (con fakes).
  - Ficheros: `src/agent_review/adapters/teams_bot.py`, `src/agent_review/decision.py` (si hace falta un ctor).

## Bloque 2 — Hilo por PR y egress

- [ ] **T4** Hilo por PR: mapeo, root message, reply y edición
  - Clave `reviews:teams:thread:<repo>:<pr>` → `{conversation_id, root_message_id}`.
    La primera review del PR abre el hilo raíz; el resto va como **reply**. La tarjeta
    se **edita** (`updateActivity`) al avanzar el grafo (C7). Si falta el mapeo, hilo
    nuevo sin efectos sobre el estado durable (C6).
  - Aceptación: dos avisos del mismo `repo:pr` caen en el mismo hilo; borrar la clave
    de Redis crea otro hilo y no rompe la reanudación.
  - Verifica: `tests/test_teams.py`; revisión humana del hilo en un canal de prueba.
  - Ficheros: `src/agent_review/adapters/teams_bot.py`, `src/agent_review/settings.py`.

- [ ] **T5** Egress interno del bot (`/notify`, `/notify_pending`)
  - Endpoints internos con bearer (`TEAMS_BOT_SHARED_SECRET`), hermanos del
    `sandbox-runner`: el worker los llama; el bot es el único que habla con Teams.
  - Aceptación: sin bearer → `401`; con bearer, el bot resuelve el hilo (T4) y
    publica/edita la tarjeta.
  - Verifica: `tests/test_teams_bot_service.py` (cliente HTTP falso).
  - Ficheros: servicio `teams-bot`, `src/agent_review/settings.py`.

- [ ] **T6** Servicio `teams-bot` + Compose + settings
  - Proceso propio con Teams SDK (versión fijada); servicio en `docker-compose.yml`
    en la red interna; variables `TEAMS_BOT_ID`, `TEAMS_BOT_TENANT_ID`,
    `TEAMS_BOT_TOKEN`, `TEAMS_BOT_URL`, `TEAMS_BOT_SHARED_SECRET`. `.env.example`
    actualizado (comentado) y `insecure_secrets` para el arranque *fail-closed*.
  - Aceptación: el servicio levanta y responde `/healthz`; en producción, sin los
    secretos del bot, el arranque falla nombrando el que falta.
  - Verifica: `tests/test_settings.py`; `docker compose config`.
  - Ficheros: `docker-compose.yml`, `.env.example`, `src/agent_review/settings.py`,
    árbol del servicio `teams-bot`.

## Bloque 3 — Ingress y retirada

- [ ] **T7** Ingress del Bot Framework (`POST /api/messages`) y traducción de acciones
  - Valida el JWT del Bot Framework; enruta por un **único ingress** a `teams-bot`
    (`api` no cambia, C2). Traduce `Action.Execute`/`Action.Submit` a las marcas de la
    tarjeta y dispara T3.
  - Aceptación: un JWT inválido se rechaza; una acción válida llega a T3 y encola.
  - Verifica: `tests/test_teams_bot_service.py`; `docs/operacion.md` §5 (endpoint).
  - Ficheros: servicio `teams-bot`, `docs/operacion.md`.

- [ ] **T8** Retirada de `TeamsWebhookAdapter` y variables del webhook
  - Borrar `TeamsWebhookAdapter`, sus tests y `TEAMS_WEBHOOK_URL`/`TEAMS_ENABLED` del
    código, `.env.example` y `insecure_secrets`. Sustituir por el bot en `build_chat`.
  - Aceptación: no queda referencia al webhook; `make ci` en verde.
  - Verifica: `rg -i "teams_webhook|TEAMS_WEBHOOK_URL|TEAMS_ENABLED"`.
  - Ficheros: `src/agent_review/adapters/teams_webhook.py` (borrado), `settings.py`,
    `.env.example`, `tests/test_teams.py`, `src/agent_review/worker/main.py`.

- [ ] **T9** E2E con fakes
  - Review → pausa → tarjeta de decisión → decisión desde la tarjeta → reanudación →
    aviso, con el transporte falso del bot. Caso bot caído: la review no se rompe y el
    CLI sigue funcionando.
  - Aceptación: no se duplica publicación; con el bot caído, la decisión por CLI
    reanuda con normalidad.
  - Verifica: `tests/test_hitl_e2e.py`.
  - Ficheros: `tests/test_hitl_e2e.py`.

## Bloque 4 — Pendientes humanos y documentación

- [ ] **T10** Alta del bot (humano)
  - Registro de app en Entra ID, consentimiento de admin, bot en Azure Bot Service,
    *sideload* del bot en el equipo, canal dedicado, ingress + TLS apuntando a
    `POST /api/messages`. Sin esto no se prueba el criterio de hecho nº 1.
  - Aceptación: el bot publica y recibe una tarjeta en el canal real.
  - Verifica: ejecución manual (smoke, fuera de CI).
  - Ficheros: —.

- [ ] **T11** Documentación viva e índices
  - ADR-0016 pasa a `Aceptado`; `arquitectura.md` (§1 diagrama, §2 capas, §4.3, §5
    claves nuevas, §6 fases, §7 deuda); `operacion.md` (§1 compose, §2 variables, §3
    secretos, §5 endpoint, §6 smoke); `desarrollo.md` (§2 tests, §3 extensión);
    `CONTEXT.md` («canal», «hilo», «bot», retirada de «Incoming Webhook»); README si
    procede; índice de iniciativas (013 → aprobada/implementada).
  - Aceptación: `make docs-check` en verde; ningún documento vivo describe el webhook.
  - Verifica: `make ci` y `make docs-check`.
  - Ficheros: `docs/adr/0016-*.md`, `docs/adr/README.md`, `docs/arquitectura.md`,
    `docs/operacion.md`, `docs/desarrollo.md`, `CONTEXT.md`, `README.md`,
    `docs/iniciativas/README.md`.
