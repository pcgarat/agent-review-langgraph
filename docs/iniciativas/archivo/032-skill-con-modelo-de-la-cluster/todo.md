Última modificación: 2026-10-29 | Creado: 2026-10-29

# Todo: la skill con el modelo del clúster

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md).

## Bloque S — Spike (checkpoint S-K)

- [x] **S1** Gateway local pineado, `curl` a `/v1/messages` contra `deepseek-v4-flash`
  - Resultado 2026-10-29: `ghcr.io/berriai/litellm:v1.106.6` (`sha256:37e2dafb088a`). HTTP 200, `type: message`, `stop_reason: end_turn`. Con `max_tokens: 64` el bloque `thinking` se come la salida y `text` llega vacío; con 256, el texto es `pong`.
- [x] **S2** `claude -p` contra ese gateway emite `type: result` y `subtype: success`
  - Resultado 2026-10-29: Claude Code 2.1.308, `subtype: success`, resultado `pong`. Avisa `unrecognized_model` y sigue. Un `Bash` (`git diff --stat`) dentro de la jaula (`allowedDomains: nan-gateway-spike`) devolvió 18, que es el diff real.
- [x] **S3** Una review real con `mode:agent` pasa `adapt_agent_mode` y una persona reconoce la skill. Si no, se retira la iniciativa y no se abre G
  - Resultado 2026-10-29: PR #21, head `c12427a`, base `ad89ab9`. `adapt_agent_mode` acepta el objeto (`status: complete`, veredicto «Ready with fixes», 7 hallazgos, 3 descartados por título de más de 100 caracteres). El artefacto trae el procedimiento: personas correctness, testing, maintainability, api-contract, julik-frontend-races y adversarial, dispatch y hoja de cierre. Las 37 peticiones llevan `model: deepseek-v4-flash`. El clúster responde 404 a `/v1/responses/input_tokens`; las completiones siguieron.

## Bloque G — Gateway y destino (checkpoint G-K)

- [x] **G1** Servicio `nan-gateway` en el perfil `claude`, LiteLLM pineado, egress solo a `api.nan.builders`
  - 2026-10-29: imagen `v1.106.6`, modelos `deepseek-v4-flash` y `glm5.3-flash`. El egress lo cierra `nan-gateway-egress` (NET_ADMIN en el sidecar, no en LiteLLM): OUTPUT solo a `api.nan.builders:443`.
- [x] **G2** `CLAUDE_GATEWAY_URL`, `CLAUDE_GATEWAY_TOKEN` y `CLAUDE_GATEWAY_MODEL` en `Settings`. `NAN_API_KEY` solo en el entorno de `nan-gateway`
- [x] **G3** `claude-runner` lee el destino en cada `POST /run` y arma `extra_env` y `allowedDomains`. Sin elección, el hijo queda como hoy
- [x] **G4** Test de paridad del argv y del entorno cuando el destino es Anthropic
- [x] **G5** `docs/operacion.md` §2 y §3: variables, quién ve el diff, y la clave del clúster que no sale del gateway

## Bloque Q — 429 (checkpoint Q-K)

- [x] **Q1** El gateway reintenta un 429 unas pocas veces dentro de la llamada
  - 2026-10-29: `RateLimitErrorRetries: 3`. El resto de errores del router queda en cero.
- [x] **Q2** Si sigue, el worker reencola el job y no lo parkea como `quota`
- [x] **Q3** El desenlace de cupo de Claude Code, con destino Anthropic, sigue parkeando

## Bloque P — Página del destino (checkpoint P-K)

- [x] **P1** La elección `claude` | `deepseek-v4-flash` | `glm5.3-flash` vive junto a la sesión. Si nadie ha elegido, manda `CLAUDE_UPSTREAM`
- [x] **P2** `/claude` ofrece esas tres. Con `claude` sigue el formulario de sesión. Con un modelo del clúster no hay sesión que pedir. `llm` no se ofrece
- [x] **P3** Cambiar de destino no mata ningún proceso y `web` no recibe el socket de Docker

## Bloque J — Hallazgos de la review del PR #22 (checkpoint J-K)

- [x] **J1** La marca de egress nombra el netns del arranque: el sidecar la borra al empezar y el gateway no arranca con una marca vieja del volumen
  - 2026-10-29: la marca guarda `readlink /proc/self/ns/net`; el sidecar sale si su netns se queda sin interfaces, y `restart` lo une al nuevo.
- [x] **J2** `claude-runner` no espera a `nan-gateway`, y el sidecar reintenta el DNS en vez de salir
- [x] **J3** `web` recibe `CLAUDE_UPSTREAM` y `CLAUDE_GATEWAY_MODEL` con los mismos defectos que el runner
- [x] **J4** `/claude` pinta solo la consulta, no el resultado de la última mutación
- [x] **J5** Tests de `/session` y `/run` con destino clúster: health del gateway, URL o token ausentes
- [x] **J6** `CLAUDE_GATEWAY_TOKEN` y `CLAUDE_GATEWAY_TOKEN_FILE` en `.env.example` y en `docs/operacion.md` §2 y §3; `/api/claude/backend` en §5
- [x] **J7** Elegir un modelo del clúster borra el enfriamiento del cupo y suelta `quota-wait`
- [x] **J8** Tests de comportamiento del sidecar de egress, del entrypoint y del token, con `iptables` y `getent` de mentira
- [x] **J9** El master key del gateway con modo `0600`, fuera de la jaula (`denyRead`) y redactado también con destino Anthropic
- [x] **J10** El gateway no arranca sin clave del clúster: sin health, las reviews esperan en vez de acabar en la DLQ
- [x] **J11** El refresco del egress cambia de cadena de una vez (`NAN_EGRESS_A`/`_B`), sin vaciar la activa
- [x] **J12** Panel: escribir un 0 en los turnos no desmonta el campo; el formulario del token no parpadea mientras carga el destino; test de `DialControl`
- [x] **J13** `current_backend()` compartido por panel y runner; un test exige los mismos modelos del clúster en `Settings`, el gateway y el panel
- [x] **J14** Un 429 persistente del clúster aparca como `cluster-wait` (300 s) y vuelve solo, sin gastar intentos ([ADR-0038](../../../adr/0038-el-cluster-se-espera.md))
- [x] **J15** Con el gateway caído, `GET /session` dice `reason: gateway` y el job aparca con su propio aviso; vuelve cuando el gateway responde
- [x] **J16** Volver a Claude suelta `gateway` y `cluster-wait`; el panel los explica en «Aparcados»
- [x] **J17** Riesgos aceptados en ADR-0037: DNS hacia el resolver de Docker y `master_key` en el hijo
