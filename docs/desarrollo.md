Última modificación: 2026-10-24 | Creado: 2026-10-11

# Desarrollo

Cómo trabajar en el repo y cómo extenderlo sin romper los invariantes. Antes de
tocar nada, lee [`CONTEXT.md`](../CONTEXT.md) y los ADR de la zona que vas a cambiar.

---

## 1. Puesta en marcha

```bash
make help        # lista todos los atajos, agrupados
make install     # uv sync --extra dev (Python 3.12; el CI fija UV_FROZEN=true)
make ci          # ruff check + ruff format --check + mypy (strict) + pytest + docs-check
```

`make ci` ejecuta lo mismo y en el mismo orden que `.github/workflows/ci.yml`. Los
tests `live` (contra Bitbucket real) quedan excluidos por defecto (`-m 'not live'`);
`make test-live` los pide en positivo (red y credenciales reales).

El `Makefile` es el índice de tareas del repo, no solo de calidad. Además de `make ci`:

```bash
make test T=tests/test_web.py     # acota la suite a una ruta o a -k
make fix                          # arregla lint y formato
make bootstrap                    # crea .env desde .env.example (idempotente, permisos 600)
make up                           # levanta el stack (docker compose up --build -d)
make panel                        # ídem con el perfil `panel` (panel de operación)
make claude                       # ídem con el perfil `claude` (claude-runner)
make panel-claude                 # perfiles `panel` y `claude` a la vez
make ensure-dev-secrets           # rellena URL y secretos locales vacíos; no toca `CLAUDE_GIT_TOKEN`
make logs S=api                   # sigue los logs de un servicio (por defecto worker)
make smoke PR=417                 # smoke Nivel 2 real contra un PR (docs/operacion.md §6)
make status                       # contenedores, URLs y prueba de credenciales
```

`tests/conftest.py` aísla la suite del host: desactiva el `.env` en `Settings` y
borra del proceso las variables que el modelo conoce. Los tests parten siempre de los
valores por defecto. Añadir un campo a `Settings` no obliga a tocar el `conftest`.

### Evaluación de la review (gold set)

El caso del PR #3 (`tests/eval/pr_3/`) se mide en *replay* dentro de `make test` (sin
red). Los comandos son:

```bash
make eval                  # informe replay por consola
EVAL_COMPARE=tests/eval/pr_3/expected_report.json make eval   # + delta por dominancia
make eval-tools            # replay con el rol `correctness` con herramientas (014)
make eval-live             # corrida real (marca live, a mano, fuera de CI)
make eval-record           # regraba recorded.json contra el modelo real
make eval-record-tools     # regraba la variante con herramientas (recorded_tools.json)
```

`make eval-record` es el **único** camino que produce grabaciones. Un cambio que
altera lo que recibe el modelo invalida el fingerprint y CI **falla** pidiendo
regrabar (ver [ADR-0017](adr/0017-replay-determinista-con-fingerprint.md)). El
harness vive en `tests/eval/harness.py` (sin paquete); la CLI, en `scripts/eval.py`.

Una grabación guarda `order`: la lista de llamadas en el orden en que ocurrieron,
con `seq`, `call_id`, `batch_index` y `turn`, y `calls` como índice por *fingerprint*.
El *replay* consume una llamada por vez, así que dos turnos con el mismo *prompt* no
se confunden. Una grabación anterior a ese formato (sin `order`) sigue sirviendo para
*replay* por *fingerprint*, pero `Recording.measurable_by_batch` es `False`: no se le
inventa el orden ni el lote.

Un caso declara sus ficheros en `fixtures/head/` y `fixtures/base/` (los árboles a
`head_sha` y `base_sha` de `labels.yaml`; los directorios tocados por el diff más los
que lea la grabación, Q4 de [014](iniciativas/014-revisor-con-herramientas/spec.md)).
El harness los carga solos: `CaseVcs` sirve el árbol a L1 (008) y `fixture_reader_factory`
construye el `FixtureContextReader` determinista del bucle de herramientas. La variante
con herramientas es **opt-in** por `REVIEW_TOOLS_ROLES`, igual que el worker, y graba
en `recorded_tools.json` para no pisar el baseline de 008.

Grabar necesita el rol `correctness` activo y una credencial real:
`LLM_REVIEWER_ENABLED=true make eval-record`, con `LLM_API_KEY` (o la clave que pida
`config/models.yaml`) en el `.env`. Una grabación nueva produce otros hallazgos: antes
de congelar `expected_report.json`, cada hallazgo publicado sin etiqueta se juzga
contra el `head_sha` del caso y se etiqueta en `labels.yaml` (el test exige
`fp_unknown == 0`).

El matcher mide **el defecto, no la redacción**
([ADR-0025](adr/0025-matcher-del-gold-set-mide-el-defecto.md)): `file`+`line` (≤ 5)
es ancla primaria y basta un fragmento discriminante; sin línea el `match` sigue
siendo conjuntivo. Un caso se valida al cargar: `line` sin `file`, una etiqueta sin
fragmento, un `id` repetido o un `expected`/`must_not_report` indistinguible fallan
con el `id` en el mensaje. Las etiquetas se reanclan contra el código del `head_sha`
(`fixtures/head`), nunca contra la grabación.

El informe (`harness.Report`, serializado por `to_json`) no es solo recall: lleva
`usage` (`prompt`, `completion` y el `reasoning` que va dentro de *completion*),
`operations` (peticiones servidas por herramienta y modo: `read_file:outline`,
`grep:regex`…) y `skipped` (ficheros omitidos por motivo, incluido `budget`). Los
tres se congelan con el baseline, porque una palanca que se paga y no se usa no se
ve en un total de tokens. Hay un baseline por configuración —sin herramientas
(`expected_report.json`) y con herramientas (`expected_report_tools.json`)— y **no
se comparan entre sí**: miden escenarios distintos. Hoy solo el primero está
congelado: `recorded_tools.json` caducó con el *prompt* de capacidades de 023 F1 y
`make eval-tools` pide regrabar; el test de la variante con herramientas se declara
omitido hasta que exista su baseline.

## 2. Mapa de tests

| Zona | Tests |
|---|---|
| Contrato y dominio | `test_domain`, `test_contracts`, `test_fix_domain` |
| Política | `test_policy` |
| Ingesta y API | `test_api`, `test_pending`, `test_queue` |
| Grafo y publicación | `test_graph` (incluida la review parcial: cabecera, marca y lista plegable; L0 en publish; etiqueta preexistente; `ReviewContext` leído una vez), `test_scope` (clasificación, prioridad y cobertura de 012; ensamblaje L1), `test_e2e_tooling` |
| L0 / L1 (008) | `test_verification_l0` (anclaje estricto, cita, `pre_existing`), `test_diff_parser` (lado viejo del diff), `test_llm_reviewer` (L0 en `_to_finding`, L1 por lote y humildad), `test_profile` (`context_docs`) |
| Reintentos | `test_retry`, `test_revision_main` (regresiones B1–B10) |
| HITL | `test_hitl`, `test_hitl_e2e`, `test_hitl_persistence`, `test_paused`, `test_cli`, `test_checkpointer` |
| Recorrido y panel (019) | `test_trace` (recorrido reconstruido del `saver` sin grafo: completado, pausado humano/fix y a medias, contrastado contra `get_state_history`; lector sin DDL y sin saver), `test_progress` (semántica del `ProgressPort` sobre `fakeredis` y envoltura de nodo: firma/`Runtime`/`RetryPolicy` preservados), `test_cancel` (cancelación: `Cancelled` no se reintenta, la frontera aborta sin ejecutar el nodo, el worker borra el hilo y limpia el inbox), `test_web` (panel: endpoints `/api/state` y `/api/trace` con un read model falso, read model real sobre `fakeredis`, sesión con rol `401`/`403` y fail-closed de `PANEL_SESSION_SECRET`), `test_web_ui` (vistas HTML, botones por estado y rol, formulario de decisión y escapado de datos del PR), `test_web_commands` (comandos por estado: cancelar antes/después de publicar, reintentar, reanudar y **decidir** —022—, incluida la validación con `build_decision`), `test_bandeja` (la bandeja lista las pausas humanas, sella la primera respuesta y pide retirar la tarjeta si Teams está activo), `test_pr_url` y `test_web_launch` (lanzar una review con la URL del PR, ADR-0029) y `test_worker` (el worker resuelve el commit antes del grafo); `test_compose` cubre el perfil `panel` |
| Fix | `test_fix_selection`, `test_fix_validation`, `test_fix_node`, `test_fix_render`, `test_fix_e2e`, `test_llm_fixer` |
| Handoff (E7) | `test_fix_worker` (verificación e idempotencia del `fix-worker`), `test_queue` (colas `reviews:fixes`), `test_compose` (aislamiento del servicio) |
| Sandbox | `test_http_sandbox`, `test_sandbox_runner`, `test_sandbox_console` (consola en vivo: `on_line` no altera el `VerifyReport`, `Popen` separa `stdout`/`stderr` y emite al vuelo, buffer circular y SSE, `404` sin `run_key`), `test_compose` |
| Lectura de contexto (016) | `test_context_ports` (contrato paramétrico de los tres adaptadores, `FixtureContextReader` y el dialecto portable de 023), `test_outline` (índice de símbolos: AST, heurística declarada y fichero no indexable), `test_context_vcs`, `test_context_workspace`, `test_read_workspace` (workspace *bare* del runner y endpoints), `test_context_factory` (elección y degradación), `test_context_cache` (la caché de E/S solo cubre `read_file`), `test_tool_loop` (bucle, topes, citas, registro de lecturas y presupuesto) |
| Presupuesto (017) | `test_budget` (contrato paramétrico de `InMemoryBudget` y `RedisBudget` sobre `fakeredis`, reserva/confirmación y el consumo desde el bucle), `test_llm_reviewer` y `test_llm_fixer` (reserva, tope blando, corte y devolución en el error), `test_worker` (reparto `2×total/N` en `build_reviewers`), `test_graph` (el hecho del corte en el `ReviewOutcome`) |
| Razonamiento (021) | `test_reasoning` (sink y lector Redis sobre `fakeredis`: agrupación de trozos, TTL, tope, lotes paralelos, Redis caído), `test_openai_llm` (*streaming*, oyente roto, corte a mitad), `test_llm_reviewer` (diff numerado y eventos por lote), `test_web` (SSE y cierre) y `test_web_ui` (pestaña, sin `innerHTML`) |
| Histórico (021) | `test_history` (resumen completa/parcial/incompleta, almacén en memoria y sobre `fakeredis`, poda, Redis caído, el worker anota solo lo que no queda pausado), `test_web_ui` y `test_web` (página `/history` y `/api/history`) |
| Adaptadores | `test_twg_vcs`, `test_github_vcs`, `test_openai_llm`, `test_llm_reviewer`, `test_tooling_reviewer`, `test_tooling_source`, `test_text_utils` |
| Canal (chat) | `test_chat_outbox` (contrato `ChatNotice`, incluido `withdraw`, y outbox), `test_teams` (tarjeta, estado por usuario, `build_decision`), `test_claude_card` (lote de publicación y la tarjeta de `llm` sin `pause_kind`), `test_teams_threads` (hilo por PR y borrado de la tarjeta), `test_teams_bot_service` (consumo de la outbox, `withdraw`, pausa ya sellada y barrido), `test_teams_bot_settings` (secretos y Compose), `test_teams_bot_ingress` (acciones de tarjeta → encolado y borrado del mensaje; wiring del SDK con fakes), `test_hitl_e2e` (ciclo pausa → tarjeta → reanudación → aviso, bot caído, pausa vencida, CLI) |
| Modelos | `test_models_config` (contrato del YAML), `test_models_registry` (bindings y `probe`) |
| Perfil y worker | `test_profile`, `test_worker`, `test_claude_runner` (puerto del CLI, entorno del subproceso, una llamada por `head_sha`, aislamiento del servicio), `test_claude_parking` (parking, aviso único, login del operador y cupo) |
| Real (opt-in) | `test_live_twg` (`-m live`, `REVIEWS_LIVE_PR`), `test_live_models` (`-m live`, `LLM_API_KEY`; comprueba las capacidades declaradas) |
| Operación | `test_status` (`make status`: puertos, credencial aceptada o rechazada, módulo apagado sin prueba; sin red ni Docker), `test_ensure_dev_secrets` (rellena una vez los secretos locales y no inventa `CLAUDE_GIT_TOKEN`) |
| Evaluación (gold set) | `test_eval_harness` (matcher, métricas, dominancia, desglose de gasto y omisiones, replay), `test_eval_pr_3` (regresión del caso PR #3, *replay* sin herramientas y, cuando exista su baseline, con herramientas), `test_live_eval` (`-m live`, `LLM_API_KEY`; corrida real comparativa) |

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

### El CLI de Claude (025)

El puerto es `ClaudeRunner` (`ports/claude.py`): `run` devuelve stdout. El
worker lo llama en `ensure_claude_artifact` antes de `invoke`, y no vuelve a
llamar si el artefacto de ese `head_sha` ya es válido. El tiempo agotado, el
cupo y un proceso caído son excepciones distintas. Un JSON que no es la lista
de hallazgos lo rechaza el worker (`ClaudeInvalidOutput`); el CLI no lo
convierte en «cero hallazgos».

El adaptador `SubprocessClaudeRunner` (`adapters/claude_cli.py`) vive en el
servicio `claude-runner`, no en el worker. El argv lleva `--max-turns`,
`--allowedTools Read,Grep,Glob`, `--disallowedTools` (Bash, Edit, Write,
NotebookEdit, WebFetch, WebSearch) y `--permission-mode dontAsk`. El entorno
del hijo es la lista `CHILD_ENV_ALLOW`: no se construye tachando secretos. El
token de sesión, si el fichero sigue vigente, se inyecta aparte
(`CLAUDE_CODE_OAUTH_TOKEN`); no se hereda del padre. `CLAUDE_CONFIG_DIR` apunta
fuera del checkout. El checkout (`claude_runner/checkout.py`) quita los remotos
después del fetch y no arranca el CLI si alguno sigue; `core.symlinks` queda
en falso. Git corre con un entorno sin los tokens del proceso y con tope de
tiempo.

Sin sesión, `dispatch_job` mueve el job a `reviews:claude:parked` y no llama a
`invoke`, salvo que el job ya traiga `decision` o `fix_resolution`. El aviso
sale una vez (`claim_notice`). `POST /api/claude/session`
(`require_command_session`) escribe el fichero `0600` y suelta solo el motivo
`session`. El job que agota el cupo parkea con `quota` y no se reintenta.
Mientras `reviews:claude:notice:quota` sigue, el job siguiente parkea como
`quota-wait` (no gasta otra llamada) y vuelve cuando el aviso caduca. Un
artefacto ya válido no espera.

### Una persona nueva

1. Implementa `ReviewerPort` (`name` + `review(...) -> ReviewerResult`). Acepta
   `context: ReviewContext | None` (008); ignóralo si no usas L1.
2. Valida cada hallazgo con `domain.contracts.validate_finding` y ancla solo en
   líneas **añadidas** del diff. Si la persona emite hipótesis, invoca L0
   (`verification.l0.evaluate_l0`) y no copies `pre_existing` del modelo.
3. Si depende de una fuente externa, devuelve `tooling_status`. Si no, `None`.
4. Regístrala en `worker.build_reviewers` y añade su rol a `LLM_REVIEW_ROLES` si es
   LLM. El merge la tratará como una persona distinta para la fusión
   ([ADR-0014](adr/0014-criterio-de-merge-entre-personas.md)).

### Documentos de contexto del perfil (`context_docs`)

En el perfil del repo revisado (YAML):

```yaml
context_docs:
  glossary: CONTEXT.md
  decisions:
    - docs/adr/0013-secretos-por-entorno-fail-closed.md
  extra: ["docs/operacion.md#2"]
```

`scope` los lee una vez al `head_sha` (con recorte de sección si hay `#N`) y viajan
en `ReviewContext.docs`. El `LlmReviewer` los mete en cada lote bajo
`LLM_CONTEXT_MAX_CHARS` sin desplazar el diff. Sin la clave, solo hay fichero
completo del hunk.

### Un proveedor de modelo nuevo

1. Declara el proveedor y sus modelos en `config/models.yaml`: `protocol`,
   `base_url`, `api_key` (referencia `${VAR}`) y, por modelo, sus capacidades
   (`json_mode`, `context_window`).
2. Añade el campo del secreto a `Settings` y a `.env.example`. El YAML solo
   resuelve referencias contra campos de `Settings`; no lee el entorno crudo.
3. Bindealo a los roles que corresponda. El código no conoce nombres de modelo.
   Para usar el **mismo modelo con otros parámetros** en un rol (p. ej. el revisor
   que razona y el fixer que no), declara otra entrada con `api_model` (el nombre
   real en la API) y `params` (se aplican encima de los `defaults` del proveedor):
   es `deepseek-v4-flash-revisor` en el registro ([021](iniciativas/archivo/021-razonamiento-visible/spec.md) R10).
4. Cubre el contrato en `tests/test_live_models.py` (`-m live`): es lo que verifica
   que las capacidades declaradas se cumplen, cosa que `/v1/models` no publica.
5. El adaptador pide **siempre** *streaming* y acepta una respuesta normal si el
   proveedor lo ignora; el razonamiento (`reasoning_content`) sale en
   `LlmReply.reasoning` y, a trozos, por el `on_reasoning` opcional (021 R1/R2). Un
   adaptador nuevo que no razone puede ignorar `on_reasoning`.
6. Si el proveedor **no** habla `/chat/completions`, añade un adaptador de
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
depende del msgpack permisivo. Ejemplo: `ReviewCoverage`, `SkipReason` y `FileClass`
(012), o `Verdict` / `VerificationResult` (009), con un test de pausa y reanudación
en `test_checkpointer`. Los **read models** derivados (como el recorrido de
019, `domain/trace.py`) no van aquí: no viajan por el estado.

### Un verificador adversarial (009)

El `VerifierPort` (`ports/verifier.py`) es el segundo juicio entre `merge` y
`policy`. El adaptador `LlmVerifier` (`adapters/llm_verifier.py`) usa el bucle
`run_tool_loop` y un lector de `build_reader_factory` (016); el *prompt* **refuta**.
El nodo le pasa `repo`, `head_sha`, `base_sha` y el `diff` para que el lector pueda
comparar con la base (`pre_existing`) y sembrar el modo degradado. `Verdict` y
`VerificationResult` viven en `domain/models` (el puerto los reexporta)
para viajar en `ReviewState.verifications` y en `ReviewOutcome.verifications`.

Al extenderlo:

1. **Inyección como el fixer**: `build_verifier` en el worker; `build_graph(...,
   verifier=None)` es paso directo. No construyas el puerto dentro del grafo.
2. **Independencia del modelo**: el binding `verifier` no puede coincidir con un
   rol de revisión (`ModelsConfig` falla al cargar).
3. **Candidatos acotados**: el nodo filtra por umbral, grounded y
   `VERIFIER_MAX_FINDINGS`; el presupuesto es el de la review (017), no el del fix.
4. **No toques `severity`**: solo `verification`, evidencia y, en `CONFIRMED`, el
   `failure_scenario`. El gate de 011 es el dueño de la severidad publicada.

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
5. **La marca es un tombstone**: el worker no la retira al abortar. Sin hilo, cualquier
   job rezagado de la misma review (la reanudación del fix-worker, una decisión
   encolada) arrancaría desde `START`; la marca, con su TTL (`CANCEL_TTL_S`), hace que
   `_consume` lo aborte. Solo el comando «Reintentar» la retira. Por la misma razón una
   pausa de **fix** no se marca: la marca abortaría el job que resuelve la wave.

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
   (spec §5.4/§5.5) y su efecto. Una **decisión humana** ([022](iniciativas/archivo/022-decision-humana-en-el-panel/spec.md))
   no tiene semántica propia: se construye con `build_decision` (`decision.py`), la misma
   del CLI, la API y Teams, y se encola.
3. **La bandeja no es otro validador.** `GET /api/bandeja` lee las pausas
   humanas sin sello. `POST /api/bandeja/{key}/decision` llama a
   `DecisionSubmitter`, el mismo que la tarjeta. El detalle, si el proceso tiene
   Redis, usa ese submitter: no vuelvas a encolar una `HitlDecision` al lado.
4. **La sesión viaja por cabecera o cookie**: `require_session` acepta
   `X-Panel-Session` (CLI, tests) y la cookie `panel_session` (navegador). Un endpoint
   nuevo que ejecute algo usa `require_operador`, nunca `require_session` a secas.
5. **Lanzar una review no llama al VCS.** `POST /api/launch` (`web/launch.py`)
   parsea la URL y encola `launch=True`. El commit lo lee `resolve_launch` en el
   worker, antes de `graph.invoke`
   ([ADR-0029](adr/0029-lanzamiento-desde-el-panel.md)).

### Un lector de contexto nuevo
El `ContextReaderPort` (`ports/context.py`) da el repo a un modelo que razona: tres
operaciones de **solo lectura** —`read_file`, `grep` y `list_dir`— que el
bucle `run_tool_loop` (`adapters/tool_loop.py`) sirve turno a turno. Los **modos**
(`mode` de lectura y de patrón, `path`/`ref`, `files_only`, `glob`, `depth`) son
parámetros del puerto, no operaciones nuevas; un adaptador que no puede servirlos
lanza `ToolError` en vez de ignorarlos. Desde 023 el
puerto no devuelve `str`/`list`: devuelve **value objects** ([ADR-0026](adr/0026-contrato-de-lectura-con-tamano-real.md)):

- `ReadResult(content, total_bytes, truncated, served_range)`, o `None` si la ruta no
  existe o está vetada. `total_bytes` es el tamaño **real** del fichero en bytes UTF-8
  antes del rango y del recorte; `None` si la superficie no lo conoce.
- `ToolResult(items, total, truncated)` para `grep` y `list_dir`; `total` cuenta lo que
  había antes del tope y `None` significa «no medido», nunca cero.

Hay tres adaptadores y todos pasan el mismo test de contrato:

1. **`FixtureContextReader`** (`adapters/context_fixture.py`): sirve un directorio
   local (y, con `base_root`, el arbol de `base` para `ref="base"`). Es el lector
   determinista del *replay* ([ADR-0017](adr/0017-replay-determinista-con-fingerprint.md))
   y el que usa el gold set (`fixtures/head`, `fixtures/base`).
2. **`VcsContextReader`** (`adapters/context_vcs.py`): modo degradado por
   `VcsPort.get_file`. Sin árbol, `grep` y `list_dir` solo ven lo ya servido y los
   ficheros del diff; conviene sembrarlo (`seed`/`seed_findings`).
3. **`WorkspaceContextReader`** (`adapters/context_workspace.py`): HTTP contra los
   `/workspaces` del `sandbox-runner`; es el único que hace `grep` del árbol completo.

Encima de cualquiera de ellos puede ir un **`CachingReader`**
(`adapters/context_cache.py`, 023 C1): un decorador que comparte un caché **por
review** y cachea solo `read_file` (inmutable para una `ref` fija). `grep` y
`list_dir` no se cachean porque en el lector VCS dependen del *seed* del lote.
Aparte, el `ReadingRegistry` (`ports/context.py`) es el registro de lecturas de la
review: prueba las citas `path:line` (solo `full`/`range` de la misma `ref`) y
alimenta el bloque `LECTURAS` cuando `REVIEW_TOOLS_READ_STATE` está encendido.

Para añadir uno: implementa los tres métodos, respeta `is_denied` (rutas vetadas) en
**todos** los niveles de `list_dir`, usa `slice_lines` y `truncate_bytes` de
`ports/context.py` (no reimplementes el corte) y añade el adaptador
a `build_reader_factory` (`adapters/context_factory.py`) con su degradación. Distingue
los dos fracasos: una **lectura** imposible (`read_file` de algo ausente o vetado)
devuelve `None`; una **petición** imposible (rango invertido, `depth < 1`, patrón no
portable, `list_dir` de una ruta que no existe o que es un fichero) lanza `ToolError`
con un mensaje accionable, para que el modelo corrija en el turno siguiente. Un
directorio **vacío** sí es un `ToolResult` vacío: vacío e inexistente no se confunden
(L1 de 023). La fábrica
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

### Una fuente de razonamiento nueva (021)

El razonamiento tiene un solo punto de captura
([ADR-0023](adr/0023-razonamiento-por-streaming-y-stream-de-redis.md)):

1. **El adaptador** del modelo emite cada trozo por el `on_reasoning` opcional de
   `LlmPort.complete` y lo deja entero en `LlmReply.reasoning`. Un adaptador que no
   razone lo ignora; uno nuevo que sí razone solo tiene que llamarlo.
2. **Quien llama** solo pasa `on_reasoning` si alguien escucha (los dobles de test no
   tienen por qué aceptarlo) y le añade el contexto en un `ReasoningEvent` (`start`,
   `reasoning`, `end`, `error`; persona, lote *i/N*, `meta`).
3. **El destino** es un `ReasoningSink`: `emit_safely` lo hace *best-effort*. El
   adaptador Redis agrupa trozos y escribe `reviews:reasoning:<review_key>`; el panel
   lo lee con `ReasoningReader` y lo sirve por SSE (`web/reasoning.py`).

Para que otro rol (el fixer, el verificador de 009) emita su razonamiento, pasa el sink
al construirlo y emite con su `role`: el panel lo agrupa solo.

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
   bot no reanuda el grafo. Un lote de `pause_kind=publication` entra por
   `submit_batch` y no se acepta sobre una pausa del motor `llm`. Al encolar, el
   bot borra el mensaje. Un `ChatNotice` `withdraw` hace lo mismo desde otro
   proceso (la bandeja). Un aviso de pausa con la pausa ya sellada se descarta.
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
- **El panel no resuelve el commit de un lanzamiento.** Encola la URL ya parseada;
  el worker lee el SHA antes de invocar el grafo y no lo vuelve a leer en un
  reintento ([ADR-0029](adr/0029-lanzamiento-desde-el-panel.md)).
- **La protección manda sobre la aprobación.**
- **El fixer nunca ve secretos ni el perfil completo.**
- **«Sin hallazgos» con una fuente sin cubrir se publica como incompleta**, nunca como
  limpia.
- **Un hallazgo no se autopromueve.** Un `P0`–`P2` `unverified` lleva
  `failure_scenario`; un `verification` distinto de `unverified` lleva
  `verification_evidence`; ningún emisor declara más que su nivel (`LlmReviewer` →
  `unverified`, `ToolingReviewer` → `verified_static`) y ninguna capa cambia
  `severity` ([ADR-0021](adr/0021-cascada-de-verificacion-del-hallazgo.md)). Lo
  comprueba `Finding` al validarse.
- **Ninguna persona compara su propia llamada contra un entero de presupuesto.** El
  gasto se reserva y se confirma contra el `BudgetPort` compartido; el hecho del
  corte (`budget_exhausted`) viaja, no se silencia.
- **El token de la sesión de Claude no entra en Redis ni en el aviso.** El worker
  pregunta `GET /session`. El fichero `0600` lo escriben el panel y lo lee el
  runner.
- **Un secreto, una declaración.** El contrato de arranque se deriva de `Settings`
  (`secrets_for`), no de una lista a mano: cada campo secreto declara su descriptor
  `Secret` y el valor de ejemplo vive en `PLACEHOLDER_VALUES`
  ([ADR-0022](adr/0022-contrato-de-secretos-declarado.md)). Un campo secreto nuevo sin
  declarar rompe `tests/test_secrets.py`.
- **Un patrón de `grep` se lee igual en las cuatro superficies.** El puerto emite
  una sola cadena canónica (`portable_pattern`) válida en `re` y en `git grep -E`;
  lo que cada motor interpreta distinto se rechaza con `ToolError`, nunca se degrada
  a «sin coincidencias» ([ADR-0027](adr/0027-dialecto-portable-de-grep.md)).
  `glob` se mide contra la ruta relativa a `path`, con `*` que no cruza `/`.
- **La grabación de la medida contiene *todas* las llamadas, también las fallidas.**
  Un intento que lanza (`LlmTruncatedError`, `LlmTransientError`) guarda su tipo y
  su mensaje, y el *replay* levanta el mismo error; sin esa entrada el *replay* se
  salta un turno y aborta por *fingerprint* (023 T0c/D30). Un turno con contenido en
  blanco no es una review limpia: el bucle lo repite con un recordatorio, un lote que
  cierra sin JSON de hallazgos se reintenta y, si aun así no lo cierra, se parte en
  dos para que sus ficheros no salgan `failed` (solo se rinde con un único bloque).

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
