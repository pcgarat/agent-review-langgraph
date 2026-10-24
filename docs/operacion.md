Última modificación: 2026-10-24 | Creado: 2026-10-11

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

El panel de operación (019) es un servicio aparte, opt-in por **perfil**:

```bash
docker compose --profile panel up -d      # añade `web` (panel) a lo de siempre
```

Sin el perfil, `docker compose up` arranca exactamente lo de siempre.

### Servicios de compose

| Servicio | Puerto | Monta | Notas |
|---|---|---|---|
| `api` | `8000` publicado | `./var` (lectura y escritura), `./config` (solo lectura) | Deposita los artefactos |
| `worker` | — | `./var` y `./config` (solo lectura) | Necesita `twg` en la imagen si el proveedor es `twg` |
| `fix-worker` | — | `./config` (solo lectura) | Consume la wave y verifica. **Sin** `env_file`: solo recibe las variables que lee (Redis, runner, timeouts, perfil). **Sin** `docker.sock`, credenciales de VCS, Postgres, clave del LLM ni `REVIEWS_DECISION_TOKEN` |
| `web` (perfil `panel`) | `PANEL_PORT` publicado | `./config` (solo lectura) | Panel de operación: lee estado de Redis, el recorrido del checkpointer y publica los diales (026). Monta `config/` para validar `MAX_AUTONOMY_OVERRIDE` contra `autonomy.yaml`; no lo edita. **Sin** `env_file`: no recibe credencial de VCS ni clave del LLM. Solo arranca con `--profile panel` |
| `teams-bot` | `3978` solo interno | — | Consume la outbox y sirve `POST /api/messages`. **Sin** `ports` (el ingress/TLS lo publica) ni `env_file`: solo recibe Redis e identidad del bot. **Sin** credenciales de VCS, LLM ni Postgres |
| `sandbox-runner` | `8099` solo interno | `./config` (solo lectura), `/run/user/1000/docker.sock` (demonio **rootless** propio) | Se construye con `SANDBOX_RUNNER=true`; recibe `TWG_BBC_TOKEN` vacío |
| `postgres` | — | volumen `pg_data` | Checkpoints |
| `redis` | — | — | Sin auth; solo red interna |

> El socket del runner es el del demonio rootless del **UID 1000** del host. Si el
> runner corre con otro usuario, hay que ajustar la ruta en `docker-compose.yml`.

---

## 2. Configuración

Toda la configuración entra por entorno (`settings.py`); `.env.example` es la
plantilla comentada y declara **todas** las variables. Cada secreto o identidad lleva
el placeholder `change-me`, que cuenta como «sin rellenar» en todas partes:
`make bootstrap` lo lista como pendiente y en producción el arranque falla (§3). Solo
quedan vacías a propósito las variables en las que el vacío significa algo
(`SCOPE_IGNORE`, `MAX_AUTONOMY_OVERRIDE`, `LLM_MODEL`, `LLM_BASE_URL`). Además hay tres
ficheros versionados:

- **[`config/autonomy.yaml`](../config/autonomy.yaml)**: la política
  ([ADR-0003](adr/0003-autonomia-como-politica-por-hallazgo.md)). `max_autonomy` es
  el dial de quien opera el agente:
  - `report` (por defecto): solo informa.
  - `fix_with_approval`: pausa y pide decisión humana.
  - `fix_auto`: además arregla sin preguntar lo que las reglas permitan.

  Se sube editando el YAML; `MAX_AUTONOMY_OVERRIDE` solo puede bajarlo.
  El bloque `severity_gate` ([ADR-0024](adr/0024-gate-de-severidad-en-la-politica.md))
  es el umbral compartido con L2 (`require_verified_for: [P0, P1]`) y la política
  de publicación: un `P0`/`P1` sin veredicto positivo se degrada a `P3` con acción
  `report`; un `refuted` con `on_refuted: drop` no se publica (sigue en `merged`
  para la métrica). Cambiar el bloque exige reiniciar el worker.
- **[`config/repo_profile.emotivoo-v2.yaml`](../config/repo_profile.emotivoo-v2.yaml)**:
  el perfil del repo. Su contrato se valida al arrancar (`profile.validate_profile`):
  una clave desconocida o un tipo erróneo se registran como aviso.
- **[`config/models.yaml`](../config/models.yaml)**: el registro de modelos
  ([ADR-0015](adr/0015-registro-declarativo-de-modelos.md)). Declara proveedores,
  modelos (con sus capacidades) y a qué modelo apunta cada **rol**. Un rol es la
  unidad de asignación: `correctness`, `security`, `arch` (personas del fan-out),
  `fixer` y `verifier` (L2 adversarial, 009). Cambiar de modelo es editar este
  fichero y **reiniciar el worker**: no hay recarga en caliente.

  ```yaml
  bindings:
    correctness: deepseek-v4-flash-revisor
    fixer: deepseek-v4-flash
    verifier: gemma4          # distinto de las personas; lee y cita (009 Q1)
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
| Canal (Teams) | `TEAMS_ENABLED` (declara el canal), `TEAMS_NOTICES_KEY` (`reviews:notices`), `TEAMS_BOT_ID` / `_TENANT_ID` / `_TOKEN` (identidad del bot; el tenant también filtra las acciones de tarjeta, §5), `TEAMS_BOT_PORT` (3978), `TEAMS_BOT_SHARED_SECRET` (bearer de `/healthz`), `TEAMS_BOT_POLL_S` (5) |
| HITL | `REVIEWS_DECISION_TOKEN`, `DECISION_TTL_S` (7 días), `CHECKPOINT_RETENTION_DAYS` (30) |
| Handoff E7 | `FIX_VERIFY_TTL_S` (7200: una wave sin resolver vence → `NOT_VERIFIABLE`), `MAX_FIX_ATTEMPTS_JOB` (3), `FIX_POLL_S` (5), `FIX_SWEEP_S` (30), `FIX_WAIT_MAX_AGE_S` (259200: poda de hilos `fix_wait` vencidos, nunca `human`) |
| Reintentos | `MAX_REVIEW_ATTEMPTS` (3), `NODE_RETRY_MAX_ATTEMPTS` (3; `1` lo desactiva), `NODE_RETRY_INITIAL_INTERVAL_S` (0.5), `NODE_RETRY_BACKOFF_FACTOR` (2.0), `NODE_RETRY_MAX_INTERVAL_S` (8.0) |
| Progreso y panel (019) | `PROGRESS_ENABLED` (`false`: activa el progreso del worker en Redis, cierra D16), `PROGRESS_TTL_S` (3600), `PROGRESS_KEY` (`reviews:active`; el panel de operación lee de aquí el nodo en curso), `CANCEL_TTL_S` (604800: vida de la marca de cancelación, que queda como *tombstone* tras abortar; debe superar `DECISION_TTL_S`) |
| Panel (019) | `PANEL_ENABLED` (`false`: declara el panel y exige su sesión en producción), `PANEL_PORT` (8090), `PANEL_SESSION_SECRET` (sesión `<usuario>:<lector\|operador>`; **no** es `REVIEWS_DECISION_TOKEN`; fail-closed en producción), `SANDBOX_CONSOLE_BUFFER_LINES` (2000: líneas del buffer de la consola del sandbox, bloque 4), `SANDBOX_RUNNER_URL` y `SANDBOX_CONSOLE_TOKEN` (proxy de la consola en vivo; sin ellos la consola da `404`) |
| Razonamiento (021) | `REASONING_STREAM_ENABLED` (`false`: el worker escribe en Redis lo que razonan los revisores en cada lote y el panel lo muestra en vivo en el detalle de la review), `REASONING_TTL_S` (864000: 10 días), `REASONING_MAX_BYTES` (10485760: tope aproximado por review) |
| LLM | `LLM_REVIEWER_ENABLED` (`false`; sin él no se construye ninguna persona LLM ni el fixer), `MODELS_CONFIG` (vacío = vía `LLM_*`; `config/models.yaml` en `.env.example`: registro de modelos por rol), `LLM_MODEL` (vía de compatibilidad sin registro), `LLM_BASE_URL` (vacío = OpenAI), `LLM_API_KEY` (la referencia del YAML y la credencial de la vía implícita), `LLM_MAX_FINDINGS` (10), `LLM_BUDGET_TOKENS_PER_PR` (2000000: tope de tokens **por review**, compartido por todas las personas, el fixer y el verificador L2; al agotarse, lo que falte se registra como `budget_exhausted` en vez de gastar de más, §2), `LLM_BATCH_MAX_CHARS` (20000: tamaño de un lote de la persona LLM en caracteres de diff; un fichero que no cabe se trocea por hunks y solo un hunk que no cabe sale `too_large`), `LLM_BATCH_RETRIES` (2: reintentos de un lote ante un corte del proveedor, un cierre **sin contenido** —un razonador que devuelve solo espacios— o un cierre **sin JSON** de hallazgos; cada reintento repite el lote con un aviso añadido al final del *prompt*, porque con razonamiento una repetición idéntica puede devolver el mismo vacío; agotados, el lote sale `failed` y la review sigue), `LLM_CONTEXT_MAX_CHARS` (40000: tope del bloque L1 —fichero completo + `context_docs` del perfil— por lote; el diff no se recorta; lo que no cabe se omite) |
| Verificador L2 (009) | `VERIFIER_MAX_FINDINGS` (5: tope de hallazgos por review, los más graves primero; el resto queda `unverified` sin llamada), `VERIFIER_MAX_TOOL_CALLS` (8) y `VERIFIER_MAX_CONTEXT_BYTES` (60000: topes del bucle *ReAct* por hallazgo), `VERIFIER_REQUIRE_VERIFIED_FOR` (`P0,P1`: respaldo si `autonomy.yaml` no declara `severity_gate.require_verified_for`; hoy el umbral vive en `severity_gate` —011/VF15— y L2 lo lee de ahí) |
| Revisor con herramientas (014/023) | `REVIEW_TOOLS_ROLES` (vacío: roles LLM que pueden leer ficheros y hacer `grep` mientras revisan; vacío = una llamada por lote como en 008). Topes y palancas del bucle: tabla siguiente |
| Fix y sandbox | `SANDBOX_ENABLED`, `SANDBOX_RUNNER_URL` (p. ej. `http://sandbox-runner:8099`), `SANDBOX_RUNNER_TOKEN`, `SANDBOX_GIT_TOKEN`, `SANDBOX_REGISTRY_USER`/`_TOKEN`, `SANDBOX_DOCKER_BIN`, `SANDBOX_TIMEOUT_S` (1200; manda el perfil), `SANDBOX_WORKSPACE_TTL_S` (1800: TTL de un workspace de lectura), `SANDBOX_MAX_WORKSPACES` (4: tope de clones abiertos a la vez), `FIX_DELIVERY` (`diff` \| `suggestion`), `FIX_RECIPES` (solo si no hay perfil), `MAX_FIX_LINES` (30) |
| Handoff (fix-worker) | `REDIS_URL` (cola), `SANDBOX_RUNNER_URL`/`_TOKEN`, `MAX_FIX_ATTEMPTS_JOB` (3), `FIX_VERIFY_TTL_S` (7200), `FIX_POLL_S` (5), `FIX_SWEEP_S` (30) |
| Panel, diales (026) | `PANEL_CONFIG_LOCK` (vacío: el operador puede publicar el catálogo). La página es `/config` |

### Configuración desde el panel (026)

El rol `operador` publica, en `/config`, los diales del catálogo de
[026](iniciativas/026-configuracion-desde-el-panel/spec.md). El `lector` ve la
página y no guarda. Cada control nace con el valor que `Settings` ya resolvió
(el entorno si la variable está puesta) y **Restaurar** lo devuelve a ese valor
sin escribir. **Guardar** publica el documento entero en Postgres
(`panel_config`); solo se guardan las claves que se apartan del valor inicial.

`PANEL_CONFIG_LOCK` es una lista de nombres de campo separada por comas. Esos
controles se ven, se pintan con su valor inicial y no se cambian: el navegador no
los envía y el servidor descarta un valor distinto con `400`. Un nombre que no
esté en el catálogo aborta el arranque del `web` y del worker. Vacío por defecto.

El `web` monta `config/` en solo lectura: `MAX_AUTONOMY_OVERRIDE` se valida
contra el `max_autonomy` de `autonomy.yaml` con el mismo motor de política,
solo cuando el formulario pide un valor distinto del inicial. Sin el montaje, un
guardado que cambie ese dial responde `400`; el resto de la página funciona.
Los valores iniciales viajan con el formulario, así que un cambio de default en
el despliegue con la página abierta responde `409` en vez de publicar el viejo.
Un candado que llega después de un override publicado gana: la página pinta el
valor del despliegue y el siguiente guardado descarta ese override, que
`apply_document` ya ignoraba.

La review que ya empezó sigue con la foto que fijó. La página nombra esa
revisión junto a la publicada. La siguiente, y un **Reintentar** del panel,
leen lo publicado. Secretos, arranque y los YAML de
`config/` no están en la página ([ADR-0029](adr/0029-configuracion-desde-el-panel.md)).

### Revisor con herramientas (023)

El *prompt* del bucle anuncia **siempre** todas las capacidades del puerto (modos,
`ref`, `files_only`, `depth`): estas banderas deciden el coste y la forma de servir,
nunca la disponibilidad. Con todas en su default, el comportamiento servido es el de
hoy.

| Variable | Default | Qué hace | Apagado ⇒ qué pasa |
|---|---|---|---|
| `REVIEW_TOOLS_MAX_TURNS` | `4` | turnos con herramientas por lote (sin contar el final forzado) | — |
| `REVIEW_TOOLS_MAX_CALLS` | `8` | total de peticiones por lote | — |
| `REVIEW_TOOLS_MAX_BYTES` | `60000` | bytes servidos por lote | — |
| `REVIEW_TOOLS_CALLS_PER_TURN` | `3` | peticiones atendidas en un mismo turno | se sube junto con `MAX_CALLS`: con más por turno y el mismo total, el lote se agota antes |
| `REVIEW_TOOLS_GREP_MODE` | `literal` | modo por defecto de `grep` (`literal` \| `regex`) | el adaptador sirve literal aunque el *prompt* anuncie `regex`; pedirlo explícito **sí** funciona |
| `REVIEW_TOOLS_READ_OUTLINE` | `off` | habilita `mode="outline"` en `read_file` | pedirlo cae a `full` y se avisa |
| `REVIEW_TOOLS_READ_STATE` | `off` | bloque de lecturas de la review en el *prompt* | no viaja el bloque; las citas se siguen validando con el registro |
| `REVIEW_TOOLS_TRUNCATION_NOTICE` | `on` | aviso de corte con el tamaño real del fichero | el corte se aplica igual pero **no** se anuncia: solo para reproducir el *prompt* viejo |
| `REVIEW_TOOLS_STRICT_CALLS` | `off` | error explícito por petición no atendida | el excedente se descarta con un `AVISO` en el `<tool_result>` |
| `REVIEW_TOOLS_PRECACHE_CONTEXT` | `on` | L1 precargado en el bucle | baja el *prompt* base del lote; el modelo debe leer lo que necesite (cuesta turnos) |
| `REVIEW_TOOLS_IO_CACHE` | `off` | caché de E/S por review (ahorra E/S, no *prompt*) | cada lote vuelve a leer del repositorio |

Desde 023, todas estas banderas **actúan**: `GREP_MODE` y `CALLS_PER_TURN` fijan
defaults, `STRICT_CALLS` cambia el descarte por un error explícito, `READ_OUTLINE`,
`READ_STATE`, `TRUNCATION_NOTICE`, `PRECACHE_CONTEXT` e `IO_CACHE` cambian lo
servido o el *prompt* con el sentido de la tabla. Con `GREP_MODE=literal` (el
default) un `mode="regex"` explícito del modelo se atiende igual; el flag solo fija
el default. Ninguna cambia lo que el *prompt* anuncia que existe.

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
Vacío y `change-me` (el placeholder de `.env.example`) son igual de «falta»
(`settings.is_missing`).

**El contrato se declara en el modelo, no en una lista**
([ADR-0022](adr/0022-contrato-de-secretos-declarado.md), iniciativa 020): cada campo
secreto de `Settings` lleva un descriptor `Secret` en su `metadata` que dice en qué
procesos aplica, qué condición lo activa y qué se espera del valor; el contrato se
deriva con `settings.secrets_for(settings, proceso)`. Añadir un campo secreto y
olvidar su comprobación deja de ser posible, y el valor de ejemplo se declara una sola
vez: API, web y el `sandbox-runner` comparten el predicado `settings.is_missing`, que
encapsula la lista `settings.PLACEHOLDER_VALUES`.

| Variable | Qué es | Obligatoria | Dueño |
|---|---|---|---|
| `TWG_BBC_TOKEN` (+ `TWG_SITE`, `TWG_USER`) | API token *scoped* del **actor de servicio** de Bitbucket (lectura y escritura de PRs) | Con `twg` | Agente |
| `GITHUB_TOKEN` | PAT *fine-grained*: `pull_requests: write` + `contents: read` | Con `github` | Agente |
| `TOOLING_WEBHOOK_SECRET` | HMAC-SHA256 del `POST /webhook/tooling` | Siempre en producción | Agente **y** CI |
| `REVIEWS_DECISION_TOKEN` | Bearer del canal de decisión: quien lo tiene decide | Siempre en producción | Agente |
| `TEAMS_NOTICES_KEY` | Cola outbox de avisos al canal (`reviews:notices`); no es un secreto | Si `TEAMS_ENABLED` | Agente |
| `TEAMS_BOT_ID` / `_TENANT_ID` / `_TOKEN` | Identidad y secreto del bot (Teams App / Azure Bot). Los lee **solo** `teams-bot` | Si `TEAMS_ENABLED` en producción | Agente |
| `TEAMS_BOT_SHARED_SECRET` | Bearer del `/healthz` del bot (ingress interno) | Si `TEAMS_ENABLED` en producción | Agente |
| `LLM_API_KEY` | Clave del proveedor LLM (la referencia `${LLM_API_KEY}` del registro de modelos) | Si `LLM_REVIEWER_ENABLED` | Agente |
| `SANDBOX_RUNNER_TOKEN` | Bearer compartido entre el `fix-worker` y el runner | Si `SANDBOX_ENABLED`; siempre para el `fix-worker` en producción | Agente |
| `SANDBOX_CONSOLE_TOKEN` | Bearer de **solo lectura** de la consola del runner: lo usa el `web` y solo abre `GET /verify/{run_key}/console`, no `POST /verify` | Para la consola en vivo del panel | Agente |
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
| `SANDBOX_CONSOLE_TOKEN` | 90 días o ante sospecha | Nuevo valor en `.env` → `docker compose --profile panel up -d web sandbox-runner` (**a la vez**) |
| `SANDBOX_GIT_TOKEN` | 90 días o ante sospecha | Nuevo token de solo lectura → runner |
| `REVIEWS_DECISION_TOKEN` | 90 días o al salir alguien con acceso | Nuevo valor → `api` → avisar a quien decide |
| `TEAMS_NOTICES_KEY` | — (no es un secreto: es una clave de cola) | Cambiarla requiere reiniciar worker y bot a la vez |
| `TEAMS_BOT_TOKEN` / `_SHARED_SECRET` | 90 días o ante sospecha | Nuevo valor en `.env` → `docker compose up -d teams-bot` |
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
| `base_sha` | *(Opcional.)* SHA de la base del PR. Si falta, el worker lo pide al VCS al consumir el job (008) |
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

### Panel de operación (`web`, perfil `panel`)

El panel es un proceso aparte (`python -m agent_review.web.main`), opt-in por el
perfil de compose `panel`. Lee el estado operativo de Redis y el recorrido del
checkpointer con un pool de **solo lectura**; no recibe credencial de VCS ni clave
del LLM (solo lee estado y encola comandos). La sesión va en la cabecera
`X-Panel-Session: <usuario>:<rol>` (`rol` `lector` u `operador`) o en la cookie
`panel_session` (el navegador no puede mandar cabeceras en una navegación normal);
**no** es `REVIEWS_DECISION_TOKEN`.

| Método | Ruta | Auth | Respuestas |
|---|---|---|---|
| `GET` | `/healthz` | — | `200` |
| `GET` | `/api/state` | Sesión | Estado operativo: cola (`reviews:jobs`), en vuelo (`reviews:active:*`), pausas por `kind`, descartes (`reviews:jobs:dead`) y presupuesto (`budget:*`) |
| `GET` | `/api/trace/{review_key}` | Sesión | Recorrido de la review (nodos, orden real y `next`); `404` si no existe |
| `GET` | `/api/history` | Sesión | Reviews terminadas, la más reciente primero (021): resultado (completa, parcial o incompleta), hallazgos por severidad, tokens, ficheros revisados y si hubo razonamiento. Filtros `repo`, `pr_id` y `limit`; la página HTML es `/history` |
| `GET` | `/api/reviews/{review_key}/reasoning` | Sesión | Razonamiento de los revisores por SSE (021): repite lo guardado y sigue en vivo; `404` si no hay |
| `POST` | `/api/reviews/{review_key}/cancel` | `operador` | `200` con el efecto; `403` para `lector` o para un comando por cookie que no sea JSON del mismo origen; `409` si no aplica; `503` si Redis no responde |
| `POST` | `/api/reviews/{review_key}/retry` | `operador` | Saca el descarte de la DLQ (atómico), retira la marca de cancelación si la hay y lo reencola con `attempts=0`; `409` si no está en la DLQ (un segundo clic no encola otra copia). Si reencolar falla, el descarte vuelve a la DLQ |
| `POST` | `/api/reviews/{review_key}/resume` | `operador` | Reencola la reanudación de una pausa humana resolviendo todo como `expired`; `409` si no está pausada. La UI ya **no** lo ofrece en pausas humanas: decidir es la acción (022) |
| `POST` | `/api/reviews/{review_key}/decision` | `operador` | Decisión humana explícita (022): cuerpo `{"items": [{"finding_id", "outcome", "note"}]}` con `approved`/`rejected`/`noted`; la construye `build_decision` (misma que CLI/API/Teams) y la encola. El `actor` es el de la sesión. `400` cuerpo mal formado; `409` si no está pausada, no es humana o no cubre **todos** los pendientes; `503` si Redis no responde |
| `GET` | `/api/reviews/{review_key}/console` | Sesión | SSE de la consola del sandbox (proxy del runner); `404` si no hay verificación en curso, si el runner no tiene su buffer o si no responde (se comprueba antes de abrir el stream) |
| `GET` | `/api/config` | Sesión | Documento de diales vigente (026): `revision`, `values` (solo las claves que se apartan del inicial), `initial`, `locked` y quién lo publicó. `503` si Postgres no responde |
| `POST` | `/config` | `operador` | Publica el formulario entero (026): cuerpo `{"revision", "values", "initial"}`, con `initial` opcional. `400` con `{"detail": {"field"}}` si un valor no encaja; `409` con la revisión vigente si otro guardó antes o si `initial` ya no coincide con este proceso; `503` si Postgres no responde |
| `GET` | `/config` | Sesión | Página HTML de diales (026): una sección por grupo, ayuda por control y un **Restaurar** por control |
| `GET` | `/` y `/review/{review_key}` | Sesión | Vistas HTML (global y detalle con el recorrido y la consola) |

Los comandos **encolan** y no tocan el grafo (spec §5.4/§5.5): cancelar marca la review
(o encola el `request` marcado si estaba pausada) y el worker aborta en la frontera de
nodo o al desencolar. Una pausa de fix no se marca: su cancelación encola la wave como
no verificable y el hilo completa. La marca sobrevive al aborto hasta `CANCEL_TTL_S`
para abortar también los jobs rezagados de la review; reintentar la retira. La UI solo
ofrece cada botón en el estado en que aplica y nunca ofrece «cancelar la review» si ya
publicó (solo «cancelar lo pendiente»).

Una pausa humana se **decide** desde el detalle de la review (022): el formulario muestra
cada hallazgo pendiente y envía la `HitlDecision` al endpoint de decisión, que la
construye con `build_decision` y la encola; el worker reanuda el mismo hilo (ADR-0008).
La UI ya no ofrece «Reanudar» en pausas humanas: decidir es la acción y `expired` es del
sistema (TTL).

Con la sesión en la cookie, un comando solo se acepta como `fetch` JSON del mismo
origen (`Content-Type: application/json`, y `Origin`/`Sec-Fetch-Site` del propio panel
si vienen): es la defensa contra CSRF, porque el navegador manda la cookie también en un
formulario que dispare otra web. Con la cabecera `X-Panel-Session` no se exige.

El **actor** auditado de un comando (logs y `HitlDecision.actor` de una reanudación)
es siempre el usuario de la sesión; el cuerpo de la petición no puede cambiarlo.

Sin `PANEL_SESSION_SECRET` configurado (dev/test) la sesión se relaja; con uno real,
la cabecera es obligatoria (`401` sin ella) y un rol `lector` recibe `403` en
cualquier comando (bloque 5). La `review_key` lleva `/`: el endpoint de recorrido la
acepta entera.

### Messaging endpoint del bot (`teams-bot`)

El *messaging endpoint* del Bot Framework vive en el proceso `teams-bot`, **no** en
`api`. Un único ingress/TLS enruta `POST /api/messages` hacia él (C2). El SDK
valida el JWT del Bot Framework: un token inválido o ausente responde `401` antes
de llegar a los handlers.

| Método | Ruta | Auth | Respuestas |
|---|---|---|---|
| `POST` | `/api/messages` | JWT del Bot Framework | `200` (actividad procesada); `401` si el JWT no es válido |
| `GET` | `/healthz` | Bearer `TEAMS_BOT_SHARED_SECRET` | `200`; `401` sin credencial |

Las acciones de la tarjeta (`Action.Execute` / fallback `Action.Submit`) llegan a
`/api/messages`, se traducen a marcas o a una `HitlDecision` con `build_decision` y
se **encolan** en `reviews:jobs`. El bot no reanuda el grafo.

Solo se aceptan acciones que vengan del **canal capturado** (`reviews:teams:channel`)
y, si `TEAMS_BOT_TENANT_ID` está fijado, de ese tenant. Las demás reciben «Esta
conversación no es el canal de decisión de las reviews.». El canal es la primera
conversación (no personal) que ve el bot; si el bot está en otra, se registra y se
ignora. Para mover el canal: `redis-cli DEL reviews:teams:channel` y una actividad
del bot en el canal nuevo.

### Workspaces del `sandbox-runner`

El runner expone, además de `/verify`, los endpoints de **workspace de lectura**
([016](iniciativas/archivo/016-lector-de-contexto/spec.md), [ADR-0018](adr/0018-workspace-de-lectura-en-el-runner.md)).
Los usa el worker (y en el futuro el verificador de 009) con el mismo bearer
`SANDBOX_RUNNER_TOKEN`; no están publicados al exterior (el runner no publica
puertos). Una petición sin bearer —cuando hay uno configurado— da `401`.

| Método | Ruta | Cuerpo / query | Respuestas |
|---|---|---|---|
| `POST` | `/workspaces` | `{repo, head_sha, base_sha}` | `200 {workspace_id, expires_at}`. Idempotente por `(repo, head_sha)` |
| `GET` | `/workspaces/{id}/file` | `path`, `ref=head\|base`, `start`, `end` | `200 {content}`; `404` si no existe; `403` si la ruta está vetada |
| `POST` | `/workspaces/{id}/grep` | `{pattern, glob?, max_matches?}` | `200 {matches}` (`path:line: texto`) |
| `GET` | `/workspaces/{id}/tree` | `path` | `200 {entries}` |
| `DELETE` | `/workspaces/{id}` | — | `200 {deleted}` |

Un `{id}` desconocido (caducado, desalojado o perdido en un reinicio) da `410` en
`file`, `grep` y `tree`; el `404` de `file` es solo «el fichero no existe en el
commit». Un workspace caduca por TTL (`SANDBOX_WORKSPACE_TTL_S`) y se relanza con otra llamada
a `POST /workspaces`; el lector del worker lo hace solo, una vez, al recibir `410`. El veto de rutas (`context_denylist:` del perfil) lo aplica el
runner: una ruta vetada da `403` y no aparece en `grep`.

### Consola en vivo del `sandbox-runner`

El runner expone también `GET /verify/{run_key}/console`, la **consola en vivo** de una
verificación ([019](iniciativas/archivo/019-panel-de-operacion/spec.md) §5.6, W5/W8/W9): un
*stream* SSE (`text/event-stream`) con el `stdout`/`stderr` del contenedor **a medida que
se ejecuta**, que el `web` proxya y el panel pinta. Acepta el bearer compartido
`SANDBOX_RUNNER_TOKEN` o el de **solo lectura** `SANDBOX_CONSOLE_TOKEN`, que es el que
recibe el `web`: así el panel sigue una verificación sin poder lanzar otras.

| Evento | `data` | Cuándo |
|---|---|---|
| `hello` | `{run_key}` | Al engancharse al stream |
| `line` | `{seq, stream, text}` (`stream` = `stdout` \| `stderr`) | Por cada línea emitida |
| `end` | `{run_key}` | Al terminar la verificación; el stream cierra |

El buffer vive **en la memoria del runner**, indexado por `run_key` y con tope de líneas
y de verificaciones terminadas; es **efímero**: no se persiste, **no** entra en el
`VerifyReport` y un reinicio del runner lo pierde. Por eso un `run_key` desconocido
(nunca visto, desalojado o perdido en un reinicio) da `404`, no un informe alterado. El
`run_key` lleva `/` y `:`, así que va tal cual (el parámetro es *path*). El canal fiable
de la verificación sigue siendo el `POST /verify` bloqueante: la consola es
*best-effort* y su caída no afecta a la verificación (W8).

---

## 6. Verificación

| Nivel | Comando | Qué demuestra |
|---|---|---|
| CI local | `make ci` | `ruff check`, `ruff format --check`, `mypy` estricto, `pytest` (`live` excluido) y `docs-check` |
| Smoke nivel 1 (sin red) | `uv run pytest tests/test_e2e_tooling.py tests/test_worker.py tests/test_graph.py tests/test_hitl_e2e.py tests/test_teams_bot_service.py tests/test_fix_e2e.py -v` | Artefacto → grafo → inline/resumen; pausa → tarjeta/CLI → reanudación → outbox, con fakes |
| Lectura real | `REVIEWS_LIVE_PR=417 uv run pytest tests/test_live_twg.py -m live` | El contrato de `twg` contra Bitbucket (solo lectura) |
| Smoke nivel 2 | `scripts/smoke-nivel2.sh <PR_ID> [PIPELINE_ID]` | Levanta el stack, firma y deposita el artefacto, y espera la publicación real |

**Criterio de aceptación del nivel 2.** En el PR deben aparecer:

- un inline por hallazgo anclado (≤ 20, los más graves primero);
- **un** resumen con la marca `:ok`;
- **una** tarjeta en Teams.

Reenviar el mismo `pipeline_id` no vuelve a publicar; un `pipeline_id` nuevo sí. Una
firma inválida o ausente da `401`.

> Última ejecución real: 2026-10-11, PR 430 de `cqm/emotivoo-v2`. Resultado: 4 inline
> + resumen; scope de 67 → 10 ficheros; ~15 s por review. Teams quedó en no-op: el
> bot (iniciativa 013) aún no está instalado en el tenant (T10).

---

## 7. Diagnóstico

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| El POST del CI devuelve `202 pendiente` y no pasa nada más | Faltan herramientas esperadas | `GET /pending` tras el TTL; revisar `expected_tools` del perfil |
| La review se publica como «incompleta» | El artefacto no llegó a tiempo o no se pudo leer | Revisar el `POST` del CI y la plantilla `TOOLING_OUTPUT_TEMPLATE` |
| «0 hallazgos» sospechoso | La review está incompleta o es parcial (código o configuración sin leer por presupuesto, tamaño o fallo del modelo) | El resumen lleva la cabecera «Review parcial» con cada fichero y su motivo; en los logs, `Review INCOMPLETA` o `review parcial`. Si el motivo es `too_large`, subir `LLM_BATCH_MAX_CHARS`; si es `budget`, `LLM_BUDGET_TOKENS_PER_PR` |
| `200 duplicado` en un reintento legítimo | La clave se reclamó en un intento anterior que terminó bien | Usar un `pipeline_id` nuevo; las claves caducan a los 7 días |
| Job en la DLQ | Se agotaron los `MAX_REVIEW_ATTEMPTS` | `redis-cli LRANGE reviews:jobs:dead 0 -1`; corregir la causa y reinyectar `request` con `RPUSH reviews:jobs` |
| Review pausada que nadie ve | El inbox se perdió (Redis) | La reconciliación horaria la repone; también basta reentregar el job |
| Decisión «descartada: el hilo ya estaba completado» | Ganó el `expired` del TTL | Es esperado: la review ya se publicó como «sin decisión» |
| Worker reiniciándose en bucle | Faltan secretos en producción, falta `twg` o Postgres no responde | Mirar el primer error del arranque: dice el **nombre** del secreto que falta |
| Fix `not_verifiable` | Sin digest, sin runner, sin fixer, o el runner rechazó la petición | La razón aparece en el inline y en el log `Fase 3:`. Si vino de un TTL (`FIX_VERIFY_TTL_S`), revisa que el `fix-worker` esté arriba |
| La review pausa "en verificación" y no cierra | La wave no vuelve: `fix-worker` caído o runner sin responder | `docker compose logs -f fix-worker`; la wave vence por TTL y queda `NOT_VERIFIABLE` |
| Worker «sano» pero sin avanzar | `twg` lento o una review larga (LLM) | El latido solo mide que el proceso vive; mirar el último `Procesando review`. El sandbox ya no bloquea al worker (E7) |
| «¿En qué nodo está ahora?» | El progreso del worker (019) | Con `PROGRESS_ENABLED=true`: `redis-cli HGETALL reviews:active:<review_key>` da el nodo en curso, su `started_at` y cuántas ramas del fan-out siguen. Sin marca y con el latido fresco, el worker vive pero no está ejecutando ningún nodo (entre reviews). Un `started_at` muy antiguo con la marca viva delata un nodo colgado, no un worker muerto |
| «¿Qué está pensando el revisor?» | El razonamiento de los revisores (021) | Con `REASONING_STREAM_ENABLED=true`: pestaña «Razonamiento» del detalle en el panel, o `redis-cli XRANGE reviews:reasoning:<review_key> - +`. Un lote con `fallo` lleva el motivo (sin presupuesto, error del modelo) |
| El panel no arranca o da `401` en todo | Falta el perfil `panel`, o `PANEL_SESSION_SECRET` es real y no se manda sesión | Levantar con `--profile panel`; en producción la sesión es obligatoria (`X-Panel-Session: <usuario>:<rol>` o cookie `panel_session`). En dev/test sin secreto, la sesión se relaja |
| El panel ve la cola pero no el recorrido | El pool de lectura de Postgres no abrió (`DATABASE_URL`) | El estado de Redis es independiente: `/api/state` funciona y `/api/trace/{key}` da `404` con el fallo en el log del `web` |
| Cancelar no surte efecto | La marca no llegó, o la review no pasa por una frontera | `redis-cli EXISTS reviews:cancel:<review_key>`; si está, la review corre: la cancelación se aplica al empezar el **siguiente nodo** (un nodo entero de latencia). Si estaba pausada, el comando encola el `request` marcado y el worker lo aborta al desencolar |
| Un reintento termina cancelado, o un job de la review se descarta solo | Queda la marca de cancelación (*tombstone*, `CANCEL_TTL_S`) | Es esperado: aborta los rezagados de una review cancelada. «Reintentar» desde el panel la retira; a mano, `redis-cli DEL reviews:cancel:<review_key>` |
| Un comando da `403` con un operador | La sesión va por cookie y la petición no es JSON del mismo origen | Usar la UI del panel o mandar la cabecera `X-Panel-Session` |
| Cancelada pero el PR ya tiene comentarios | Publicó antes de cancelar | Es esperado (W6/R2): cancelar no despublica. La UI solo ofrece «cancelar lo pendiente» en ese estado |
| La consola del sandbox da `404` | El `run_key` no está en el buffer del runner | Es efímero (W5): un reinicio del runner lo pierde y una verificación terminada se desaloja. El informe sigue en `POST /verify`; la consola es *best-effort* |
| La consola no muestra nada | El runner no responde, o `SANDBOX_RUNNER_URL`/`SANDBOX_CONSOLE_TOKEN` vacíos en el `web` (o el token no coincide con el del runner: el runner da `401`) | El proxy degrada a `404` sin tumbar el panel; revisar `docker compose logs -f sandbox-runner` |
| No llegan avisos a Teams | `teams-bot` caído, sin canal capturado (C14) o `TEAMS_ENABLED=false` | `docker compose logs -f teams-bot`; `LLEN reviews:notices` (la outbox acumula); instalar el bot en el canal dedicado (T10) |
| La tarjeta no reanuda y el CLI sí | Acción no llegó al ingress o JWT inválido | Mirar `401` en `/api/messages`; el CLI/HTTP es el respaldo (C9) |
| La tarjeta responde «no es el canal de decisión» | La acción viene de otra conversación o de otro tenant, o aún no hay canal capturado | Buscar `Accion de tarjeta de … rechazada` en los logs del bot; `GET reviews:teams:channel` dice cuál es el canal. Si está mal, ver §5 (mover el canal) |
| Aviso perdido en la outbox | Payload ilegible (no valida como `ChatNotice`) | Está en `reviews:notices:dead` con el texto en `raw` |

Fallos históricos que solo afloraron con Docker y ya están corregidos:

- `redis-py` 8 imponía un `socket_timeout` de 5 s que mataba el `BLPOP`.
- `contracts/` no se copiaba a la imagen.
