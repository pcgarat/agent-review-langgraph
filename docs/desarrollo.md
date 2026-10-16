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
| Fix | `test_fix_selection`, `test_fix_validation`, `test_fix_node`, `test_fix_render`, `test_fix_e2e`, `test_llm_fixer` |
| Handoff (E7) | `test_fix_worker` (verificación e idempotencia del `fix-worker`), `test_queue` (colas `reviews:fixes`), `test_compose` (aislamiento del servicio) |
| Sandbox | `test_http_sandbox`, `test_sandbox_runner`, `test_compose` |
| Adaptadores | `test_twg_vcs`, `test_github_vcs`, `test_openai_llm`, `test_llm_reviewer`, `test_tooling_reviewer`, `test_tooling_source`, `test_text_utils` |
| Canal (chat) | `test_chat_outbox` (contrato `ChatNotice` y outbox `reviews:notices`), `test_teams` (render de la tarjeta, estado por usuario y Enviar bloqueado) |
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
depende del msgpack permisivo.

### Una receta nueva en el sandbox

Declárala en `checks:` del perfil y, si toca, en `verify:`. Es una decisión humana
revisada. El runner no ejecuta nada que no esté declarado y nunca usa una shell.

### Cambiar la política

Edita `config/autonomy.yaml`. Claves válidas en `when`: `protected_subject`,
`severity`, `autofix_class`, `confidence` y `paths`. Una clave desconocida da
`PolicyError` al evaluar el primer hallazgo; añade un test en `test_policy`. Subir
`max_autonomy` es un cambio de comportamiento: actívalo solo con revisión.

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
