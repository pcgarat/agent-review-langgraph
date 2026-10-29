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
