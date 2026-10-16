Última modificación: 2026-10-16 | Creado: 2026-10-11

# Operación

Cómo se levanta, se configura, se verifica y se diagnostica el agente. La
arquitectura está en [`arquitectura.md`](arquitectura.md) y el vocabulario, en
[`CONTEXT.md`](../CONTEXT.md).

---

## 1. Arranque

```bash
scripts/bootstrap-env.sh          # crea .env desde .env.example (idempotente, permisos 600)
$EDITOR .env                      # rellena los secretos (§3)
cp ~/.local/bin/twg bin/twg       # solo con VCS_PROVIDER=twg; la imagen lo enlaza
docker compose up --build -d
docker compose logs -f worker
```

- `bootstrap-env.sh` no sobrescribe un `.env` existente y resume qué secretos faltan
  sin imprimir valores. Solo comprueba `twg` si el proveedor es `twg`.
- `REQUIRE_TWG=true` como `build-arg` hace que la imagen falle al construirse si
  falta el binario.
- `api` y `worker` llevan `restart: unless-stopped` y healthcheck. El del worker es
  el latido `/tmp/reviews-worker-heartbeat`: si tiene más de 60 s, el contenedor se
  considera enfermo.
- `scripts/sync-twg-credentials.sh` copia al `.env` la sesión `twg` de **tu**
  usuario. Es un atajo de desarrollo y **no** sirve para producción
  ([ADR-0012](adr/0012-vcs-tras-un-puerto.md)).

### Servicios de compose

| Servicio | Puerto | Monta | Notas |
|---|---|---|---|
| `api` | `8000` publicado | `./var` (lectura y escritura), `./config` (solo lectura) | Deposita los artefactos |
| `worker` | — | `./var` y `./config` (solo lectura) | Necesita `twg` en la imagen si el proveedor es `twg` |
| `fix-worker` | — | `./config` (solo lectura) | Consume la wave y verifica. **Sin** `env_file`: solo recibe las variables que lee (Redis, runner, timeouts, perfil). **Sin** `docker.sock`, credenciales de VCS, Postgres, clave del LLM ni `REVIEWS_DECISION_TOKEN` |
| `sandbox-runner` | `8099` solo interno | `./config` (solo lectura), `/run/user/1000/docker.sock` (demonio **rootless** propio) | Se construye con `SANDBOX_RUNNER=true`; recibe `TWG_BBC_TOKEN` vacío |
| `postgres` | — | volumen `pg_data` | Checkpoints |
| `redis` | — | — | Sin auth; solo red interna |

> El socket del runner es el del demonio rootless del **UID 1000** del host. Si el
> runner corre con otro usuario, hay que ajustar la ruta en `docker-compose.yml`.

---

## 2. Configuración

Toda la configuración entra por entorno (`settings.py`); `.env.example` es la
plantilla comentada. Además hay tres ficheros versionados:

- **[`config/autonomy.yaml`](../config/autonomy.yaml)**: la política
  ([ADR-0003](adr/0003-autonomia-como-politica-por-hallazgo.md)). `max_autonomy` es
  el dial de quien opera el agente:
  - `report` (por defecto): solo informa.
  - `fix_with_approval`: pausa y pide decisión humana.
  - `fix_auto`: además arregla sin preguntar lo que las reglas permitan.

  Se sube editando el YAML; `MAX_AUTONOMY_OVERRIDE` solo puede bajarlo.
- **[`config/repo_profile.emotivoo-v2.yaml`](../config/repo_profile.emotivoo-v2.yaml)**:
  el perfil del repo. Su contrato se valida al arrancar (`profile.validate_profile`):
  una clave desconocida o un tipo erróneo se registran como aviso.
- **[`config/models.yaml`](../config/models.yaml)**: el registro de modelos
  ([ADR-0015](adr/0015-registro-declarativo-de-modelos.md)). Declara proveedores,
  modelos (con sus capacidades) y a qué modelo apunta cada **rol**. Un rol es la
  unidad de asignación: `correctness`, `security`, `arch` (personas del fan-out) y
  `fixer`. Cambiar de modelo es editar este fichero y **reiniciar el worker**: no
  hay recarga en caliente.

  ```yaml
  bindings:
    correctness: deepseek-v4-flash
    fixer: qwen3.6            # json_schema estricto para el parche
  ```

  Los secretos **no** van en el YAML, solo su referencia (`${LLM_API_KEY}`), que se
  resuelve contra los campos de `Settings`. Una referencia sin resolver cuenta como
  secreto inseguro y falla el arranque en producción. Para añadir un proveedor con
  su propia clave, se añade el campo a `Settings` y se referencia desde el YAML;
  el test `tests/test_live_models.py` (`-m live`) comprueba contra la API que las
  capacidades declaradas (`json_mode`, contexto) se cumplen.

  Sin registro declarativo (`MODELS_CONFIG` sin declarar o vacío, que es el valor
  por defecto), se sintetiza uno implícito desde `LLM_MODEL`/`LLM_BASE_URL`/
  `LLM_API_KEY`: un solo modelo para todos los roles. Es la vía de compatibilidad y
  no tiene fecha de retirada. `.env.example` declara `MODELS_CONFIG`, así que un
  `.env` nuevo usa el registro y uno existente con solo `LLM_*` no cambia.

  Al arrancar, el worker contrasta cada modelo bindeado con `GET /models` del
  proveedor. Un modelo que el proveedor no lista o una credencial rechazada
  (401/403) fallan el arranque; un proveedor que no responde solo deja un aviso.

### Variables por tema

| Tema | Variables (valor por defecto) |
|---|---|
| Entorno | `ENVIRONMENT` (`development`; `production` activa el *fail-closed*), `LOG_LEVEL` (`INFO`) |
| VCS | `VCS_PROVIDER` (`twg` \| `github`). Bitbucket: `BITBUCKET_WORKSPACE`, `BITBUCKET_REPO`, `TWG_BIN`, `TWG_BBC_TOKEN`, `TWG_SITE`, `TWG_USER`, `TWG_TOKEN` (Jira), `TWG_TIMEOUT_S` (60). GitHub: `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_TOKEN`, `GITHUB_API_URL` |
| Infraestructura | `REDIS_URL`, `DATABASE_URL`, `POSTGRES_PASSWORD` |
| Ingesta | `TOOLING_WEBHOOK_SECRET`, `TOOLING_OUTPUT_TEMPLATE` (`var/tooling/{pr_id}/{head_sha}/{tool}.json`), `TOOLING_OUTPUT_ROOT` (`.`), `EXPECTED_TOOLS` (`phpstan`; manda el perfil salvo override explícito), `INGEST_TTL_READY_S` (1200), `INGEST_TTL_PENDING_S` (86400), `MAX_TOOLING_PAYLOAD_BYTES` (5 MiB) |
| Alcance | `SCOPE_IGNORE` (patrones extra, separados por comas) |
| Política | `AUTONOMY_CONFIG`, `REPO_PROFILE_CONFIG`, `MAX_AUTONOMY_OVERRIDE` (solo puede bajar el techo) |
| Canal (Teams) | `TEAMS_ENABLED` (declara el canal), `TEAMS_NOTICES_KEY` (`reviews:notices`: outbox de avisos que consume el bot) |
| HITL | `REVIEWS_DECISION_TOKEN`, `DECISION_TTL_S` (7 días), `CHECKPOINT_RETENTION_DAYS` (30) |
| Handoff E7 | `FIX_VERIFY_TTL_S` (7200: una wave sin resolver vence → `NOT_VERIFIABLE`), `MAX_FIX_ATTEMPTS_JOB` (3), `FIX_POLL_S` (5), `FIX_SWEEP_S` (30), `FIX_WAIT_MAX_AGE_S` (259200: poda de hilos `fix_wait` vencidos, nunca `human`) |
| Reintentos | `MAX_REVIEW_ATTEMPTS` (3), `NODE_RETRY_MAX_ATTEMPTS` (3; `1` lo desactiva), `NODE_RETRY_INITIAL_INTERVAL_S` (0.5), `NODE_RETRY_BACKOFF_FACTOR` (2.0), `NODE_RETRY_MAX_INTERVAL_S` (8.0) |
| LLM | `LLM_REVIEWER_ENABLED` (`false`; sin él no se construye ninguna persona LLM ni el fixer), `MODELS_CONFIG` (vacío = vía `LLM_*`; `config/models.yaml` en `.env.example`: registro de modelos por rol), `LLM_MODEL` (vía de compatibilidad sin registro), `LLM_BASE_URL` (vacío = OpenAI), `LLM_API_KEY` (la referencia del YAML y la credencial de la vía implícita), `LLM_MAX_FINDINGS` (10), `LLM_BUDGET_TOKENS_PER_PR` (500000: no llama si la estimación lo supera) |
| Fix y sandbox | `SANDBOX_ENABLED`, `SANDBOX_RUNNER_URL` (p. ej. `http://sandbox-runner:8099`), `SANDBOX_RUNNER_TOKEN`, `SANDBOX_GIT_TOKEN`, `SANDBOX_REGISTRY_USER`/`_TOKEN`, `SANDBOX_DOCKER_BIN`, `SANDBOX_TIMEOUT_S` (1200; manda el perfil), `FIX_DELIVERY` (`diff` \| `suggestion`), `FIX_RECIPES` (solo si no hay perfil), `MAX_FIX_LINES` (30) |
| Handoff (fix-worker) | `REDIS_URL` (cola), `SANDBOX_RUNNER_URL`/`_TOKEN`, `MAX_FIX_ATTEMPTS_JOB` (3), `FIX_VERIFY_TTL_S` (7200), `FIX_POLL_S` (5), `FIX_SWEEP_S` (30) |

**Para activar la Fase 3** hacen falta las cinco cosas:

1. `max_autonomy` ≥ `fix_with_approval`.
2. `LLM_REVIEWER_ENABLED=true` y un modelo para los roles `correctness` y `fixer`
   (en `config/models.yaml`, o vía `LLM_MODEL`).
3. `SANDBOX_ENABLED=true` + `SANDBOX_RUNNER_URL`.
4. `ci_image_digest: sha256:…` en el perfil.
5. Haber cerrado P6.

Si falta cualquiera, el fix queda `NOT_VERIFIABLE`.

---

## 3. Secretos

Principio: la aplicación solo lee variables de entorno, y en producción no arranca
si falta un secreto obligatorio ([ADR-0013](adr/0013-secretos-por-entorno-fail-closed.md)).

| Variable | Qué es | Obligatoria | Dueño |
|---|---|---|---|
| `TWG_BBC_TOKEN` (+ `TWG_SITE`, `TWG_USER`) | API token *scoped* del **actor de servicio** de Bitbucket (lectura y escritura de PRs) | Con `twg` | Agente |
| `GITHUB_TOKEN` | PAT *fine-grained*: `pull_requests: write` + `contents: read` | Con `github` | Agente |
| `TOOLING_WEBHOOK_SECRET` | HMAC-SHA256 del `POST /webhook/tooling` | Siempre en producción | Agente **y** CI |
| `REVIEWS_DECISION_TOKEN` | Bearer del canal de decisión: quien lo tiene decide | Siempre en producción | Agente |
| `TEAMS_NOTICES_KEY` | Cola outbox de avisos al canal (`reviews:notices`); no es un secreto | Si `TEAMS_ENABLED` | Agente |
| `LLM_API_KEY` | Clave del proveedor LLM (la referencia `${LLM_API_KEY}` del registro de modelos) | Si `LLM_REVIEWER_ENABLED` | Agente |
| `SANDBOX_RUNNER_TOKEN` | Bearer compartido entre el `fix-worker` y el runner | Si `SANDBOX_ENABLED`; siempre para el `fix-worker` en producción | Agente |
| `SANDBOX_GIT_TOKEN` | Token de Bitbucket de **solo lectura** para clonar en el runner | Si `SANDBOX_ENABLED` | Agente |
| `SANDBOX_REGISTRY_USER` / `_TOKEN` | `pull` de la imagen del CI (`crg.apkg.io`) | Para verificar | Infra |
| `TWG_TOKEN` | API token de Atlassian para `twg jira` | Fase 4 | Agente |
| `POSTGRES_PASSWORD` | Credencial de Postgres | En producción | Infra |

El `fix-worker` tiene su propio contrato, mucho menor: en producción solo exige
`SANDBOX_RUNNER_URL` y un `SANDBOX_RUNNER_TOKEN` real, porque no recibe ningún otro
secreto (compose le pasa las variables una a una, sin `env_file`).

Reglas de higiene:

- `.env` con permisos `600`, fuera de git y de la imagen.
- Los secretos se copian a otra máquina por un canal seguro, nunca por el repo ni
  por chat.
- Los logs nunca muestran valores.

### Rotación

| Secreto | Cadencia | Procedimiento |
|---|---|---|
| `TWG_BBC_TOKEN` | 90 días o ante sospecha | Nuevo token del actor de servicio → `.env` → `docker compose up -d worker` |
| `TOOLING_WEBHOOK_SECRET` | 90 días | **A la vez** en el agente y en la variable *secured* del CI |
| `SANDBOX_RUNNER_TOKEN` | 90 días | Nuevo valor en `.env` → `docker compose up -d fix-worker sandbox-runner` (**a la vez**) |
| `SANDBOX_GIT_TOKEN` | 90 días o ante sospecha | Nuevo token de solo lectura → runner |
| `REVIEWS_DECISION_TOKEN` | 90 días o al salir alguien con acceso | Nuevo valor → `api` → avisar a quien decide |
| `TEAMS_NOTICES_KEY` | — (no es un secreto: es una clave de cola) | Cambiarla requiere reiniciar worker y bot a la vez |
| `LLM_API_KEY` | Según el proveedor | Rotar en el proveedor |
| `POSTGRES_PASSWORD` | 180 días | `ALTER ROLE` + secreto + recrear los contenedores |

---

## 4. Contrato con el CI del repo revisado

Es la única pieza que vive fuera de este repo y es **opcional**
([ADR-0004](adr/0004-ingesta-diferida-por-artefacto-del-ci.md)). Sin ella, cada review
sale degradada por TTL. El paso de PHPStan del CI hace:

```bash
# El cuerpo se construye UNA vez: la firma es sobre los bytes exactos que se envían.
jq -n \
  --arg repo "$BITBUCKET_REPO_FULL_NAME" \
  --argjson pr_id "$BITBUCKET_PR_ID" \
  --arg head_sha "$BITBUCKET_COMMIT" \
  --arg pipeline_id "$BITBUCKET_BUILD_NUMBER" \
  --arg payload "$(cat var/tooling/report.json)" \
  '{repo: $repo, pr_id: $pr_id, head_sha: $head_sha, pipeline_id: $pipeline_id,
    tool: "phpstan", payload: $payload}' > /tmp/agent-body.json

SIGNATURE=$(openssl dgst -sha256 -hmac "$TOOLING_WEBHOOK_SECRET" -r /tmp/agent-body.json | cut -d' ' -f1)

curl -sS -X POST "$REVIEWS_URL/webhook/tooling" \
  -H "Content-Type: application/json" \
  -H "X-Reviews-Signature: $SIGNATURE" \
  --data-binary @/tmp/agent-body.json || true
```

| Campo | Regla |
|---|---|
| `repo` | `workspace/repo`. Debe coincidir con el configurado; si no, `400` |
| `pr_id` | `BITBUCKET_PR_ID`. No existe en builds de rama; sin él, `400` |
| `head_sha` | `BITBUCKET_COMMIT` |
| `pipeline_id` | `BITBUCKET_BUILD_NUMBER`: distingue una re-ejecución del mismo commit |
| `tool` | Una de las `expected_tools` del perfil |
| `payload` | Salida **cruda** de la herramienta, como string |
| Firma | HMAC-SHA256 del cuerpo crudo en `X-Reviews-Signature` |
| Fallo | `\|\| true`: el agente caído no puede poner rojo el pipeline |

---

## 5. API y CLI

| Método | Ruta | Auth | Respuestas |
|---|---|---|---|
| `GET` | `/healthz` | — | `200` |
| `POST` | `/webhook/tooling` | HMAC | `202 aceptado` (encolada), `202 pendiente` (faltan artefactos), `200 duplicado`, `400`, `401`, `413` |
| `GET` | `/pending` | — | Aplica los TTL de ingesta y devuelve las reviews degradadas |
| `GET` | `/reviews/paused` | — | Inbox: pendientes y orden para decidir |
| `GET` | `/reviews/{review_key}` | — | `PAUSED` o `404` (la API no consulta el checkpointer) |
| `POST` | `/reviews/{review_key}/decision` | Bearer | `202`; `409` si no está pausada o si la decisión no cubre exactamente los pendientes |
| `GET` | `/profile/{repo}` | — | Ruta del perfil cargado (ignora `{repo}`) |

Cuerpo de una decisión:
`{"actor": "ana", "items": [{"finding_id": "…", "outcome": "approved|rejected|noted", "note": ""}]}`.

```bash
agent_review list                                       # reviews pausadas y orden sugerida
agent_review decide '<review_key>' --actor ana \
  --approve <id> --reject <id> --note "falso positivo" --noted <id>
```

La `review_key` lleva `/` y `:` (`cqm/emotivoo-v2:430:<sha>:<pipeline>`): ponla entre
comillas. Exit `1` = no está pausada; exit `2` = decisión rechazada (falta algún
pendiente, un id es ajeno o se usó `expired`).

---

## 6. Verificación

| Nivel | Comando | Qué demuestra |
|---|---|---|
| CI local | `make ci` | `ruff check`, `ruff format --check`, `mypy` estricto, `pytest` (`live` excluido) y `docs-check` |
| Smoke nivel 1 (sin red) | `uv run pytest tests/test_e2e_tooling.py tests/test_worker.py tests/test_graph.py tests/test_hitl_e2e.py tests/test_fix_e2e.py -v` | Artefacto → grafo → inline/resumen; pausa → decisión → fix → sugerencia, con fakes |
| Lectura real | `REVIEWS_LIVE_PR=417 uv run pytest tests/test_live_twg.py -m live` | El contrato de `twg` contra Bitbucket (solo lectura) |
| Smoke nivel 2 | `scripts/smoke-nivel2.sh <PR_ID> [PIPELINE_ID]` | Levanta el stack, firma y deposita el artefacto, y espera la publicación real |

**Criterio de aceptación del nivel 2.** En el PR deben aparecer:

- un inline por hallazgo anclado (≤ 20, los más graves primero);
- **un** resumen con la marca `:ok`;
- **una** tarjeta en Teams.

Reenviar el mismo `pipeline_id` no vuelve a publicar; un `pipeline_id` nuevo sí. Una
firma inválida o ausente da `401`.

> Última ejecución real: 2026-10-11, PR 430 de `cqm/emotivoo-v2`. Resultado: 4 inline
> + resumen; scope de 67 → 10 ficheros; ~15 s por review. Teams quedó en no-op por no
> tener webhook real.

---

## 7. Diagnóstico

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| El POST del CI devuelve `202 pendiente` y no pasa nada más | Faltan herramientas esperadas | `GET /pending` tras el TTL; revisar `expected_tools` del perfil |
| La review se publica como «incompleta» | El artefacto no llegó a tiempo o no se pudo leer | Revisar el `POST` del CI y la plantilla `TOOLING_OUTPUT_TEMPLATE` |
| «0 hallazgos» sospechoso | La review está incompleta, o el scope recortó ficheros | Buscar `Review INCOMPLETA` o `superan max_files_per_pr` en los logs |
| `200 duplicado` en un reintento legítimo | La clave se reclamó en un intento anterior que terminó bien | Usar un `pipeline_id` nuevo; las claves caducan a los 7 días |
| Job en la DLQ | Se agotaron los `MAX_REVIEW_ATTEMPTS` | `redis-cli LRANGE reviews:jobs:dead 0 -1`; corregir la causa y reinyectar `request` con `RPUSH reviews:jobs` |
| Review pausada que nadie ve | El inbox se perdió (Redis) | La reconciliación horaria la repone; también basta reentregar el job |
| Decisión «descartada: el hilo ya estaba completado» | Ganó el `expired` del TTL | Es esperado: la review ya se publicó como «sin decisión» |
| Worker reiniciándose en bucle | Faltan secretos en producción, falta `twg` o Postgres no responde | Mirar el primer error del arranque: dice el **nombre** del secreto que falta |
| Fix `not_verifiable` | Sin digest, sin runner, sin fixer, o el runner rechazó la petición | La razón aparece en el inline y en el log `Fase 3:`. Si vino de un TTL (`FIX_VERIFY_TTL_S`), revisa que el `fix-worker` esté arriba |
| La review pausa "en verificación" y no cierra | La wave no vuelve: `fix-worker` caído o runner sin responder | `docker compose logs -f fix-worker`; la wave vence por TTL y queda `NOT_VERIFIABLE` |
| Worker «sano» pero sin avanzar | `twg` lento o una review larga (LLM) | El latido solo mide que el proceso vive; mirar el último `Procesando review`. El sandbox ya no bloquea al worker (E7) |

Fallos históricos que solo afloraron con Docker y ya están corregidos:

- `redis-py` 8 imponía un `socket_timeout` de 5 s que mataba el `BLPOP`.
- `contracts/` no se copiaba a la imagen.
