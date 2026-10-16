Última modificación: 2026-10-16 | Creado: 2026-10-16

# Todo: Canal bidireccional de Teams (bot con hilo por PR)

Checklist del bot. El diseño y las decisiones (C1–C11) están en [`spec.md`](spec.md);
el orden, en [`plan.md`](plan.md). Spec aprobada el 2026-10-16. Nada cerrado todavía.

## Bloque 0 — Spike de viabilidad (con el alta, antes de T1)

- [ ] **T0** Spike: validar la fricción y lo que de verdad es arriesgado
  - Objetivo: **no** un eco, sino reducir el riesgo allí donde el diseño puede caerse.
    Con el *Teams Developer CLI* (`teams app create`) y el canal estándar dedicado, un
    bot mínimo que:
    1. captura el **`conversation_id`** del canal al recibir la instalación
       (`install.add`) y lo persiste (C14);
    2. publica el **mensaje raíz** de un PR con una **tarjeta** que lleva un
       `Action.Execute` con datos (`review_key`, `finding_id`), guarda el
       `root_message_id` que devuelve la `SentActivity` y **responde dentro del mismo
       hilo** (`app.reply(conversation_id, root_message_id, …)`) — valida C6 y C7;
    3. registra `@app.on_card_action_execute("…")` y comprueba que la acción llega al
       `POST /api/messages` con su `action.data` — valida el **ingress** y la recepción;
    4. limpia la tarjeta con `updateActivity` cuando la "decisión" llega — valida C7 y
       C11.
  - Fuera de alcance: encolar en Redis, `build_decision`, la outbox, el estado por
    usuario. Eso es T1–T3.
  - Aceptación: se ven en el canal (a) el hilo raíz, (b) una respuesta en ese hilo y
    (c) la tarjeta actualizada tras pulsar el botón; el `conversation_id` y el
    `root_message_id` quedan apuntados.
  - Verifica: ejecución manual. **Salida esperada:** fijar (o corregir) el diseño con
    hechos; el spike se tira después.
  - Código ya escrito en `spikes/teams-bot/` (desechable, ignorado por git): `main.py`
    (los cuatro puntos), `pyproject.toml` y `README.md` con el guion de comprobación.
  - **Estado (2026-10-16):** el alta está hecha con `teams app create` (*Teams-managed*,
    sin Azure ni `az`). El bot tiene endpoint público en
    `https://spike.garatluque.com/api/messages` (ingress propio por `cloudflared` con
    `--protocol http2`; ver el README del spike) y responde **401** sin credenciales,
    que es lo correcto. Credenciales en `spikes/teams-bot/.env` (no versionado).
    **Bloqueado:** instalar la app en Teams — la política de *sideloading* del usuario
    está deshabilitada (`tenant: true`, `user: false`).
  - **Hallazgos que ya tocan el diseño:** el SDK exige que el `root_message_id` sea
    **numérico** (`ValueError` con cualquier otra cosa) y el *thread id* es
    `conversation_id;messageid=<root_message_id>`; ambos anotados en `spec.md` (C6).
  - **Validado sin Teams (2026-10-16):** con el mock del Bot Connector
    (`spikes/teams-bot/local-e2e.py`) se comprobó el ciclo completo —tarjeta raíz,
    recepción del `Action.Execute`, **reemplazo** de la tarjeta por id
    (`PUT .../activities/<id>`), reply con `;messageid=` y el **fallback
    `Action.Submit`**—. Quedó por validar en Teams solo la forma real del
    `conversation_id` y la actividad de instalación.
  - **Tres errores reales cazados por el spike** (anotados en `spec.md`, evitan
    retrabajo en T5/T7): el `service_url` hay que pasarlo en **toda** llamada proactiva
    (si no, el envío acaba en el Teams real y da 400); los adjuntos de tarjeta van
    envueltos con `card_attachment(...)`; y los efectos cosméticos (reply del hilo,
    repintado) deben ir blindados para no tumbar el registro de la decisión.
  - **El Playground no sirve como arnés** para el clic de la tarjeta: avisa de que
    ciertas funciones de Adaptive Card no están soportadas y su conectador no
    implementa bien las respuestas en hilo. Para eso, el mock.

## Bloque 1 — Contrato, tarjeta y decisión (sin Teams real)

- [x] **T1** Contrato del aviso (`ChatNotice`) y outbox del chat
  - `ChatNotice` estructurado (`kind=published|pause`, `review_key`, `repo`, `pr_id`,
    `link`, y la `ApprovalRequest` en el caso de pausa). `OutboxChatAdapter` que
    implementa `ChatPort` y **encola** en `reviews:notices`; `RedisChatOutbox` +
    `InMemoryChatOutbox` imitando `ReviewQueue`/`FixQueue` (incluida DLQ). El worker
    deja de llamar por HTTP.
  - Aceptación: `notify` y `notify_decision_pending` dejan una entrada observable en
    la outbox (test sin red); con Redis caído el worker no se rompe; el aviso de
    pausa lleva la `ApprovalRequest` y no texto.
  - Verifica: `tests/test_chat_outbox.py` y `tests/test_worker.py`.
  - Ficheros: `src/agent_review/domain/*`, `src/agent_review/ports/chat.py`,
    `src/agent_review/chat_outbox.py`, `src/agent_review/worker/main.py`.
  - **Hecho (2026-10-16).** El contrato `ChatPort` cambió para llevar la identidad
    del PR (`repo`/`pr_id`/`review_key`, necesaria para resolver el hilo: C6) y la
    `ApprovalRequest` estructurada en el aviso de pausa (C12). Como el puerto ya no
    encajaba con el webhook retirado, se adelantó aquí la **retirada de
    `TeamsWebhookAdapter` y `TEAMS_WEBHOOK_URL`** (T8, C1/C4): es atómico con el
    cambio de contrato, no un extra. `TEAMS_ENABLED` se mantiene como interruptor
    del canal y nace `TEAMS_NOTICES_KEY`. El aviso de pausa del worker queda
    **blindado** (un fallo del canal no tumba la pausa). `render_pause_notice`
    sigue existiendo para el comentario de PR de Fase 2, pero ya no lo usa el worker.

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

- [ ] **T5** Servicio `teams-bot`: consumo de la outbox y `ChatTransport`
  - `ChatTransport` (capa HTTP interna del bot, con bearer) y el bucle que consume
    `reviews:notices` (`BLPOP`) y despacha por `kind`: publica el aviso de review
    (`published`) o la tarjeta de decisión (`pause`, desde la `ApprovalRequest`
    estructurada). Drenado con recorte (C13): un aviso de una pausa ya vencida se
    descarta; una reentrega no duplica tarjeta.
  - Aceptación: un aviso deja la tarjeta/mensaje en el transporte falso; un aviso de
    pausa vencida no publica; reiniciar el bot con avisos pendientes los publica.
  - Verifica: `tests/test_teams_bot_service.py` (transporte y cola falsos).
  - Ficheros: servicio `teams-bot`, `src/agent_review/settings.py`.

- [ ] **T6** `teams-bot` en Compose + settings
  - Proceso propio con Teams SDK (versión fijada); servicio en `docker-compose.yml`
    en la red interna; variables `TEAMS_BOT_ID`, `TEAMS_BOT_TENANT_ID`,
    `TEAMS_BOT_TOKEN`, `TEAMS_BOT_URL`, `TEAMS_BOT_SHARED_SECRET`,
    `TEAMS_NOTICES_KEY`. `.env.example` actualizado (comentado) e `insecure_secrets`
    para el arranque *fail-closed*.
  - Aceptación: el servicio levanta, consume la outbox y responde `/healthz`; en
    producción, sin los secretos del bot, el arranque falla nombrando el que falta.
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

- [x] **T8** Retirada de `TeamsWebhookAdapter` y variables del webhook
  - Borrar `TeamsWebhookAdapter`, sus tests y `TEAMS_WEBHOOK_URL` del
    código, `.env.example` y `insecure_secrets`. Sustituir por la outbox en `build_chat`.
  - Aceptación: no queda referencia al webhook; `make ci` en verde.
  - Verifica: `rg -i "teams_webhook|TEAMS_WEBHOOK_URL|TEAMS_ENABLED"`.
  - Ficheros: `src/agent_review/adapters/teams_webhook.py` (borrado), `settings.py`,
    `.env.example`, `tests/test_teams.py`, `src/agent_review/worker/main.py`.
  - **Hecho dentro de T1 (2026-10-16).** El cambio de contrato de `ChatPort` dejaba
    el adaptador inservible; retirarlo aquí evita un estado intermedio incoherente
    (C1/C4). `TEAMS_ENABLED` **no** se retira: pasa a declarar el canal. Queda
    pendiente solo la parte de Compose (T6), donde `teams-bot` sustituye al webhook
    como publicador real.

- [ ] **T9** E2E con fakes
  - Review → pausa → aviso en la outbox → tarjeta de decisión → decisión desde la
    tarjeta → reanudación → aviso, con la cola y el transporte falsos. Casos: bot
    caído (el aviso se publica al arrancar), aviso de pausa vencida (se descarta) y
    CLI con el bot caído (reanuda con normalidad).
  - Aceptación: no se duplica publicación; con el bot caído, la decisión por CLI
    reanuda con normalidad; un aviso de pausa vencida no genera tarjeta.
  - Verifica: `tests/test_hitl_e2e.py`.
  - Ficheros: `tests/test_hitl_e2e.py`.

## Bloque 4 — Pendientes humanos y documentación

- [ ] **T10** Alta del bot (humano; camino recomendado: Teams Developer CLI)
  - **0. Prerrequisitos:** Node.js 20+, cuenta M365 *work/school*, **sideloading
    habilitado** (`teams status` debe decir `Sideloading: enabled`; si no, lo habilita
    el administrador: *Upload custom apps*), y un **canal estándar** dedicado (los
    privados no admiten publicación del bot).
  - **1. Registro sin Azure (recomendado):** `npm i -g @microsoft/teams.cli`;
    `teams app create --name agent-review --endpoint https://<host>/api/messages
    --env .env --azure --subscription <sub> --resource-group <rg>`. Escribe
    `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` e imprime el **Teams App ID** y el enlace
    *Install in Teams*. El SDK elige el método de auth por entorno: `CLIENT_ID` +
    `CLIENT_SECRET` → *client secret*; `CLIENT_ID` sin secreto → *managed identity*.
  - **1-bis. Camino manual (equivalente):** App Registration en Entra ID (+ *client
    secret*) → recurso **Azure Bot** con `Microsoft App ID` = *Single Tenant*, *Use
    existing app registration* → *Settings → Configuration → Messaging endpoint*
    `https://<host>/api/messages` → *Settings → Channels → Microsoft Teams*.
    Alternativa sin secretos: **identidad asignada por el usuario** (UAMI) con
    `MANAGED_IDENTITY_CLIENT_ID` en lugar de `CLIENT_SECRET`. **No** crear bot
    *multi-tenant*: está deprecado desde 2025-07-31.
  - **2. Endpoint público:** ingress + TLS apuntando a `POST /api/messages` de
    `teams-bot` (en local, DevTunnels/ngrok; en despliegue, el host real).
  - **3. Instalar el bot:** dos vías según para qué:
    - *Desarrollo / probar (sideloading):* subir el `.zip` para uno mismo. Requiere
      que un admin active **Upload custom apps** en tu *app setup policy*
      (`Teams apps → Setup policies`) y, si aplica, **Let users interact with custom
      apps in preview** en `Teams apps → Manage apps → Actions → Org-wide app settings`.
      También hay que ser **owner del equipo** al que se sube.
    - *Producción (catálogo de la organización):* un admin sube el `.zip` una vez en
      `Teams apps → Manage apps → Upload new app`; queda en el catálogo sin aprobación
      y se distribuye por *app centric management* (`Manage apps → app → Users and
      groups → Availability`), sin depender del sideloading de nadie. Es la vía del
      servicio real.
    - En ambos casos, instalar en el **equipo** no añade el bot a cada canal: hay que
      **añadirlo al canal dedicado** explícitamente.
  - **4. Manifiesto:** el CLI lo arma por ti (`teams app manifest download|upload`,
    `teams app package download` genera el `.zip` con `manifest.json` + iconos).
    `bots[].botId` = Teams App ID, `scopes` con `team`, y `validDomains` con el host
    del *messaging endpoint*.
  - Aceptación: el bot publica un mensaje y una tarjeta en el canal real y **recibe**
    una acción (llega a `POST /api/messages`).
  - Verifica: ejecución manual (smoke, fuera de CI).
  - Ficheros: `manifest.json` + iconos, `.env` (no versionado).

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
