Última modificación: 2026-10-16 | Creado: 2026-10-11

# Desarrollo

Cómo trabajar en el repo y cómo extenderlo sin romper los invariantes. Antes de
tocar nada, lee [`CONTEXT.md`](../CONTEXT.md) y los ADR de la zona que vas a cambiar.

---

## 1. Puesta en marcha

```bash
make install     # uv sync --extra dev (Python 3.12; el CI fija UV_FROZEN=true)
make ci          # ruff check + ruff format --check + mypy (strict) + pytest + docs-check
```

`make ci` ejecuta lo mismo y en el mismo orden que `.github/workflows/ci.yml`. Los
tests `live` (contra Bitbucket real) quedan excluidos por defecto (`-m 'not live'`).

`tests/conftest.py` aísla la suite del host: desactiva el `.env` en `Settings` y
borra del proceso las variables que el modelo conoce. Los tests parten siempre de los
valores por defecto. Añadir un campo a `Settings` no obliga a tocar el `conftest`.

### Evaluación de la review (gold set)

El caso del PR #3 (`tests/eval/pr_3/`) se mide en *replay* dentro de `make test` (sin
red). Los comandos son:

```bash
make eval                  # informe replay por consola
EVAL_COMPARE=tests/eval/pr_3/expected_report.json make eval   # + delta por dominancia
make eval-live             # corrida real (marca live, a mano, fuera de CI)
make eval-record           # regraba recorded.json contra el modelo real
```

`make eval-record` es el **único** camino que produce grabaciones. Un cambio que
altera lo que recibe el modelo invalida el fingerprint y CI **falla** pidiendo
regrabar (ver [ADR-0017](adr/0017-replay-determinista-con-fingerprint.md)). El
harness vive en `tests/eval/harness.py` (sin paquete); la CLI, en `scripts/eval.py`.

## 2. Mapa de tests

| Zona | Tests |
|---|---|
| Contrato y dominio | `test_domain`, `test_contracts`, `test_fix_domain` |
| Política | `test_policy` |
| Ingesta y API | `test_api`, `test_pending`, `test_queue` |
| Grafo y publicación | `test_graph`, `test_scope`, `test_e2e_tooling` |
| Reintentos | `test_retry`, `test_revision_main` (regresiones B1–B10) |
| HITL | `test_hitl`, `test_hitl_e2e`, `test_hitl_persistence`, `test_paused`, `test_cli`, `test_checkpointer` |
| Recorrido y panel (019) | `test_trace` (recorrido reconstruido del `saver` sin grafo: completado, pausado humano/fix y a medias, contrastado contra `get_state_history`; lector sin DDL y sin saver), `test_progress` (semántica del `ProgressPort` sobre `fakeredis` y envoltura de nodo: firma/`Runtime`/`RetryPolicy` preservados), `test_cancel` (cancelación: `Cancelled` no se reintenta, la frontera aborta sin ejecutar el nodo, el worker borra el hilo y limpia el inbox), `test_web` (panel: endpoints `/api/state` y `/api/trace` con un read model falso, read model real sobre `fakeredis`, sesión con rol `401`/`403` y fail-closed de `PANEL_SESSION_SECRET`), `test_web_ui` (vistas HTML, botones por estado y rol, escapado de datos del PR), `test_web_commands` (comandos por estado: cancelar antes/después de publicar, reintentar, reanudar); `test_compose` cubre el perfil `panel` |
| Fix | `test_fix_selection`, `test_fix_validation`, `test_fix_node`, `test_fix_render`, `test_fix_e2e`, `test_llm_fixer` |
| Handoff (E7) | `test_fix_worker` (verificación e idempotencia del `fix-worker`), `test_queue` (colas `reviews:fixes`), `test_compose` (aislamiento del servicio) |
| Sandbox | `test_http_sandbox`, `test_sandbox_runner`, `test_sandbox_console` (consola en vivo: `on_line` no altera el `VerifyReport`, `Popen` separa `stdout`/`stderr` y emite al vuelo, buffer circular y SSE, `404` sin `run_key`), `test_compose` |
| Lectura de contexto (016) | `test_context_ports` (contrato paramétrico de los tres adaptadores y `FixtureContextReader`), `test_context_vcs`, `test_context_workspace`, `test_read_workspace` (workspace *bare* del runner y endpoints), `test_context_factory` (elección y degradación), `test_tool_loop` (bucle, topes, citas y presupuesto) |
| Presupuesto (017) | `test_budget` (contrato paramétrico de `InMemoryBudget` y `RedisBudget` sobre `fakeredis`, reserva/confirmación y el consumo desde el bucle), `test_llm_reviewer` y `test_llm_fixer` (reserva, tope blando, corte y devolución en el error), `test_worker` (reparto `2×total/N` en `build_reviewers`), `test_graph` (el hecho del corte en el `ReviewOutcome`) |
| Adaptadores | `test_twg_vcs`, `test_github_vcs`, `test_openai_llm`, `test_llm_reviewer`, `test_tooling_reviewer`, `test_tooling_source`, `test_text_utils` |
| Canal (chat) | `test_chat_outbox` (contrato `ChatNotice` y outbox), `test_teams` (tarjeta, estado por usuario, `build_decision`), `test_teams_threads` (hilo por PR), `test_teams_bot_service` (consumo de la outbox), `test_teams_bot_settings` (secretos y Compose), `test_teams_bot_ingress` (acciones de tarjeta → encolado; wiring del SDK con fakes), `test_hitl_e2e` (ciclo pausa → tarjeta → reanudación → aviso, bot caído, pausa vencida, CLI) |
| Modelos | `test_models_config` (contrato del YAML), `test_models_registry` (bindings y `probe`) |
| Perfil y worker | `test_profile`, `test_worker` |
| Real (opt-in) | `test_live_twg` (`-m live`, `REVIEWS_LIVE_PR`), `test_live_models` (`-m live`, `LLM_API_KEY`; comprueba las capacidades declaradas) |
| Evaluación (gold set) | `test_eval_harness` (matcher, métricas, dominancia, replay), `test_eval_pr_3` (regresión del caso PR #3, *replay*), `test_live_eval` (`-m live`, `LLM_API_KEY`; corrida real comparativa) |

Regla de estilo: ningún test depende de red, de `twg` real ni de Docker. Los puertos
se sustituyen por fakes (por ejemplo, el `DiffLineVcs` de los e2e).

## 3. Cómo extender

### Un proveedor de VCS nuevo

1. Implementa `VcsPort` (`ports/vcs.py`) en `adapters/`.
2. Los fallos recuperables heredan de `TransientError` y el resto, de
   `PermanentError`. Eso basta para que el grafo reintente bien
   ([ADR-0007](adr/0007-reintento-en-dos-capas.md)).
3. `resolve_inline_anchor` devuelve `None` si no hay coincidencia. **Nunca** inventa
   una línea. Si el host no resuelve anclas, usa `diff_parser.resolve_added_line`.
4. `list_review_markers` tiene que paginar: una marca no vista significa una review
   duplicada.
5. `file_url` devuelve el permalink al fichero anclado a una línea (lo pinta el aviso
   de pausa); `None` si el host no lo soporta. Nunca aborta.
6. Añade el valor a `settings._VCS_PROVIDERS`, ramifica `worker.build_vcs` y
   `api._expected_repo`, y cubre la credencial en `insecure_secrets`.

### Una persona nueva

1. Implementa `ReviewerPort` (`name` + `review(...) -> ReviewerResult`).
2. Valida cada hallazgo con `domain.contracts.validate_finding` y ancla solo en
   líneas **añadidas** del diff.
3. Si depende de una fuente externa, devuelve `tooling_status`. Si no, `None`.
4. Regístrala en `worker.build_reviewers` y añade su rol a `LLM_REVIEW_ROLES` si es
   LLM. El merge la tratará como una persona distinta para la fusión
   ([ADR-0014](adr/0014-criterio-de-merge-entre-personas.md)).

### Un proveedor de modelo nuevo

1. Declara el proveedor y sus modelos en `config/models.yaml`: `protocol`,
   `base_url`, `api_key` (referencia `${VAR}`) y, por modelo, sus capacidades
   (`json_mode`, `context_window`).
2. Añade el campo del secreto a `Settings` y a `.env.example`. El YAML solo
   resuelve referencias contra campos de `Settings`; no lee el entorno crudo.
3. Bindealo a los roles que corresponda. El código no conoce nombres de modelo.
4. Cubre el contrato en `tests/test_live_models.py` (`-m live`): es lo que verifica
   que las capacidades declaradas se cumplen, cosa que `/v1/models` no publica.
5. Si el proveedor **no** habla `/chat/completions`, añade un adaptador de
   `LlmPort` y ramifica el registro por `protocol`
   ([ADR-0015](adr/0015-registro-declarativo-de-modelos.md)).

### Una herramienta estática nueva (p. ej. `deptrac`)

Hoy `ToolingReviewer` solo interpreta PHPStan. Hace falta:

- un parser para la herramienta;
- añadirla a `expected_tools` del perfil (la barrera esperará su artefacto);
- que el CI la publique.

Antes de invertir aquí, recuerda que la ingesta es un puente desechable
([ADR-0004](adr/0004-ingesta-diferida-por-artefacto-del-ci.md)).

### Un tipo nuevo en el estado del grafo

Añádelo a `checkpointer._STATE_TYPES`. Si no, la reanudación de un hilo pausado
depende del msgpack permisivo. Los **read models** derivados (como el recorrido de
019, `domain/trace.py`) no van aquí: no viajan por el estado.

### Leer el recorrido de una review (sin grafo)

`trace.read_trace(saver, thread_id)` (`src/agent_review/trace.py`) reconstruye del
checkpointer qué nodos corrieron, en qué orden y dónde está parada la review. Es
intencionadamente independiente del grafo compilado (D1 de la iniciativa 019): se
apoya solo en `BaseCheckpointSaver.list` y en la convención de LangGraph para las
aristas de salida (`branch:to:<nodo>` y los `Send` del fan-out en `__pregel_tasks`).
El proceso que lo consume construye su saver con
`checkpointer.build_reader_checkpointer` (solo lectura, sin `setup()`), nunca con el
`build_checkpointer` del worker. Si LangGraph cambia esa convención, lo delata
`tests/test_trace.py::test_recorrido_coincide_con_get_state_history`.

### Progreso del worker y envoltura de nodo (019)

El `ProgressPort` (`ports/progress.py`) anota, por review, el nodo en curso
(`start_node`/`finish_node`) y limpia la marca (`clear`). Es **best-effort**: sus
helpers (`start_node`, `finish_node`, `clear` del módulo) nunca propagan un fallo, y
`RedisProgress` degrada solo. Hay dos adaptadores y ambos pasan el mismo contrato
(`tests/test_progress.py`): `RedisProgress` (hash `reviews:active:<key>` con scripts
Lua atómicos, cuenta ramas del fan-out) y `NullProgress` (no-op, el default del
worker sin panel).

Se elige por configuración (`build_progress`, como `build_chat`), y **no** se editan
los nodos: se envuelven en un único punto, el *seam* de `graph/wrapper.node_wrapper`
(019 D2), aplicado al registrar cada nodo en `build_graph`. La 018 añadirá su traza
en ese mismo *seam*, no en una segunda envoltura. Al extenderlo, dos reglas:

1. **Preservar la firma del nodo** (`functools.wraps`): LangGraph lee de ella el
   `Runtime` que inyecta a `persona` y la `RetryPolicy` del nodo. Una envoltura que la
   pierda rompe el fan-out o el reintento (riesgo R1 del plan; lo fija `test_progress`).
2. **No tragarse excepciones**: el `finally` cierra la marca, pero deja pasar el
   `interrupt` (pausa) y el error del nodo.

El mismo *seam* comprueba la **cancelación** (019 W4): un `cancel: CancelCheck | None`
opcional que recibe `build_graph` igual que el progreso (no un estado global de módulo).
Si la review tiene marca `reviews:cancel:<key>`, la envoltura lanza `Cancelled` **antes**
de entrar al nodo. Al añadir un chequeo nuevo, dos reglas más:

3. **La frontera es el único punto seguro**: LangGraph no cancela un `invoke`, así que
   el aborto cae **entre** nodos, nunca a mitad de uno (riesgo R5).
4. **La marca no basta para una pausa**: una review pausada no pasa por ninguna
   frontera. Su cancelación viaja como un `ReviewRequest` marcado (`cancelled=True`) y
   `_consume` la aborta **antes** de tocar el grafo; comprobarlo después del `invoke`
   reanudaría la pausa y publicaría.

### La web del panel (019)

El panel (`web/`) es CQRS-lite ([ADR-0020](adr/0020-panel-como-servicio-aparte.md)):
**lee** y **encola comandos**, nunca muta el estado del grafo. Al extenderlo:

1. **Un puerto por fuente**: el estado (Redis) y el recorrido (Postgres) viven tras
   `PanelReadModel`; los endpoints se prueban con un fake en memoria, sin Redis ni
   Postgres. Un adaptador nuevo (otra fuente) se añade sin tocar los endpoints.
2. **La semántica de los comandos vive en `web/commands.py`**, no en los endpoints:
   `CommandService` resuelve cada comando contra la cola y el inbox y devuelve un
   `CommandEffect`. Los endpoints solo validan la sesión (`require_operador`) y traducen
   el `CommandError` a `409`. Antes de añadir un comando, decide su semántica por estado
   (spec §5.4/§5.5) y su efecto.
3. **La sesión viaja por cabecera o cookie**: `require_session` acepta
   `X-Panel-Session` (CLI, tests) y la cookie `panel_session` (navegador). Un endpoint
   nuevo que ejecute algo usa `require_operador`, nunca `require_session` a secas.

### Un lector de contexto nuevo
El `ContextReaderPort` (`ports/context.py`) da el repo a un modelo que razona: tres
operaciones de **solo lectura** —`read_file`, `grep` (literal) y `list_dir`— que el
bucle `run_tool_loop` (`adapters/tool_loop.py`) sirve turno a turno. Hay tres
adaptadores y todos pasan el mismo test de contrato:

1. **`FixtureContextReader`** (`adapters/context_fixture.py`): sirve un directorio
   local. Es el lector determinista del *replay* ([ADR-0017](adr/0017-replay-determinista-con-fingerprint.md)).
2. **`VcsContextReader`** (`adapters/context_vcs.py`): modo degradado por
   `VcsPort.get_file`. Sin árbol, `grep` y `list_dir` solo ven lo ya servido y los
   ficheros del diff; conviene sembrarlo (`seed`/`seed_findings`).
3. **`WorkspaceContextReader`** (`adapters/context_workspace.py`): HTTP contra los
   `/workspaces` del `sandbox-runner`; es el único que hace `grep` del árbol completo.

Para añadir uno: implementa los tres métodos, respeta `is_denied` (rutas vetadas),
devuelve `None` (no excepción) cuando una lectura no es posible y añade el adaptador a
`build_reader_factory` (`adapters/context_factory.py`) con su degradación. La fábrica
es lo que ve el consumidor: recibe `(repo, head_sha, base_sha)` y devuelve el puerto,
de modo que el bucle no sepa si detrás está el runner o el VCS. Un fallo del lector
dentro del bucle se sirve al modelo como error y la review sigue. Cubre el contrato en
`tests/test_context_ports.py` (paramétrico sobre los tres adaptadores) y el adaptador
nuevo en su propia suite.

### Una fuente de presupuesto nueva

El `BudgetPort` (`ports/budget.py`) cuenta tokens **por review** (`reserve`,
`commit`, `remaining`), con un contador compartido por el fan-out y el fix. Hay dos
adaptadores y ambos pasan el mismo test de contrato:

1. **`InMemoryBudget`** (`adapters/budget_memory.py`): `dict` por `review_key` con un
   `Lock`. Es el de los tests, el *replay* y el despliegue sin Redis.
2. **`RedisBudget`** (`adapters/budget_redis.py`): script Lua atómico (`INCRBY` con
   comprobación de tope y TTL). Es lo único que ven a la vez las ramas paralelas del
   fan-out y el `fix-worker`. Si Redis no responde, cae a un contador local.

Para añadir uno: implementa la semántica de `remaining` (`total − confirmado −
reservado sin confirmar`, que fija `tests/test_budget.py`), trata `False` de
`reserve` como vinculante y devuelve la reserva con `commit(..., 0)` en el error. Al
consumidor le llegue el puerto, no un entero: `reserve_tokens`/`release_tokens`
encapsulan las dos reglas para que ningún consumidor olvide mirar el resultado.

### Una receta nueva en el sandbox

Declárala en `checks:` del perfil y, si toca, en `verify:`. Es una decisión humana
revisada. El runner no ejecuta nada que no esté declarado y nunca usa una shell.

### La consola en vivo del sandbox (019)

`ContainerRuntime.start` acepta un `on_line(stream, line)` **opcional** con `stream`
en `{"stdout", "stderr"}`. El `Verifier` lo conecta al `ConsoleBuffer` (memoria del
runner, indexado por `run_key`) y `GET /verify/{run_key}/console` sirve ese buffer como
SSE. Para añadir una fuente de consola nueva basta con emitir por `on_line`: es
*best-effort* y nunca altera el `VerifyReport`, que se sigue acumulando por separado
(019 W8). El buffer es efímero: no persiste y un reinicio del runner da `404`.

### Cambiar la política

Edita `config/autonomy.yaml`. Claves válidas en `when`: `protected_subject`,
`severity`, `autofix_class`, `confidence` y `paths`. Una clave desconocida da
`PolicyError` al evaluar el primer hallazgo; añade un test en `test_policy`. Subir
`max_autonomy` es un cambio de comportamiento: actívalo solo con revisión.

### El canal de chat (Teams u otro)

El worker solo conoce `ChatPort` (`ports/chat.py`): **encola** un `ChatNotice` vía
`OutboxChatAdapter` (`chat_outbox.py`). El transporte vive en `teams_bot/`:

1. **Egress:** `TeamsBotService` drena `reviews:notices` y publica con
   `ChannelTransport` (hilo por PR, edición de tarjeta).
2. **Ingress:** `CardIngress` traduce las acciones; `sdk_handlers` cablea el SDK.
   La decisión pasa por `build_decision` y se **encola** en `reviews:jobs` (C4): el
   bot no reanuda el grafo.
3. Un canal nuevo (Slack, etc.) es otro consumidor de la outbox + otro ingress; el
   worker y el grafo no cambian. El SDK de Teams solo se importa en
   `sdk_client.py` / `sdk_handlers.py`.
4. Tests con fakes del cliente y de la outbox (`test_teams_*`, `test_hitl_e2e`);
   sin Teams real.

## 4. Invariantes que no se rompen

- **Cero escrituras en el repo revisado.** No puede existir ningún camino a commit,
  push o creación de ramas.
- **Un nodo con efectos externos es idempotente**, porque se reintenta.
- **La marca de resumen es la última escritura de `publish`.** El aviso de pausa no
  lleva marca.
- **`hitl` no lleva `RetryPolicy`.**
- **La identidad de ejecución, el hilo y el token de dedupe no divergen.**
- **La protección manda sobre la aprobación.**
- **El fixer nunca ve secretos ni el perfil completo.**
- **«Sin hallazgos» con una fuente sin cubrir se publica como incompleta**, nunca como
  limpia.
- **Ninguna persona compara su propia llamada contra un entero de presupuesto.** El
  gasto se reserva y se confirma contra el `BudgetPort` compartido; el hecho del
  corte (`budget_exhausted`) viaja, no se silencia.

## 5. Convenciones

- Código, comentarios, logs y textos publicados en **español** (sin tildes en el
  código fuente; con tildes en la documentación). Identificadores y rutas, en inglés.
- Commits: Conventional Commits en español (`feat(scope): …`).
- `mypy` estricto y `ruff` (línea de 100) sin excepciones nuevas.
- Documentación: estructura, nomenclatura y ciclo de vida en la regla
  [`documentacion`](../.agents/rules/documentacion.md); qué actualizar en cada cambio,
  en [`documentacion-mantenimiento`](../.agents/rules/documentacion-mantenimiento.md).
  Ambas se cargan solas en Cursor (`.cursor/rules/`) y en Claude Code
  (`.claude/rules/`) mediante enlaces simbólicos. `make docs-check` las valida.
