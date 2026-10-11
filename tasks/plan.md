Última modificación: 2026-10-11

# Implementation Plan: Fase 1 — Report-only

> **Estado: CÓDIGO COMPLETO (2026-10-11).** Las Tasks 1–10 están entregadas y la
> suite verde (`113 passed`, `ruff` limpio). Los dos únicos trabajos fuera del repo
> (el `POST` del CI, §7.1 de `docs/ingesta-diferida-ci-20261011.md`, y el smoke
> Nivel 2) **ya no bloquean** la Fase 1: el artefacto del CI es un puente
> desechable, no la cimentación (`docs/ingesta-vs-ejecucion-20261011.md`). No hay
> trabajo de código pendiente para dar Fase 1 por hecha.

## Overview

Cerrar el camino de valor del MVP: un evento de CI dispara una review real (diff
vía `twg`), al menos una persona produce hallazgos validados contra el contrato,
la política decide, y el resultado se publica como **comentarios inline en el
diff** (+ resumen general en el PR + Teams). Cero escritura de código en el repo
del PR. Sin HITL, sin sandbox, sin autofix.

La Fase 0 (dominio, `PolicyEngine`, grafo `scope → persona → merge → policy →
publish`, adaptadores esqueleto, API con idempotencia, compose) y la Fase 1
(cableado cola+worker, personas reales, publish inline, ingesta diferida y
verificación) están **entregadas**. Lo que resta es lo listado en "Trabajo
pendiente".

## Architecture Decisions

- **Report-only de verdad.** `max_autonomy: report` en `config/autonomy.yaml`.
  Las reglas que dan `escalate` / `fix_*` se degradan a `report` conservando la
  razón original. Evita "decisiones fantasma" que el grafo aún no puede ejecutar.
- **Persona estática antes que LLM.** Primera persona: parsear salida de
  herramientas del repo (PHPStan) sobre el diff. Valida el pipeline y el contrato
  sin coste de tokens. La persona LLM llega (opt-in) una vez el publish E2E está
  verde.
- **Cola mínima Redis.** La API hace `claim` + `RPUSH`; el worker hace `BLPOP` y
  ejecuta el grafo. Sin orquestador extra. Si Redis cae, se usa un fallback en
  memoria (tests/local) y se loguea el aviso; en producción, fallo ruidoso.
- **Contrato versionado en el repo.** Copiar (no invocar) el esquema de
  `ce-code-review` a `contracts/` y validar hallazgos contra él. El runtime de la
  skill no entra en el grafo.
- **Fixtures primero, PR real después.** Tests unitarios/integración con diff
  golden de `emotivoo-v2`. Smoke manual contra Bitbucket cuando `twg` y secretos
  estén disponibles (Nivel 2).
- **Inline primero, resumen después.** Cada hallazgo intenta anclarse con
  `diff-line` → `comment create --path --line`. Si el ancla falla, el hallazgo no
  se pierde: va al resumen general. Tope `max_inline_comments` (20), prioridad
  P0→P3. Sin `--pending`.
- **Fuera de alcance Fase 1:** `interrupt`, Bot Teams, Jira, sandbox, fix,
  mutation testing, checkpointer durable (útil en Fase 2; no bloquea report-only).
- **Ingesta diferida (cierre de la carrera).** El disparador no es el webhook de
  PR: la review se encola cuando el CI deposita el artefacto que necesita
  (`POST /webhook/tooling`). Detalle en `docs/ingesta-diferida-ci-20261011.md`.
  El webhook de PR se retira; si vuelve (Fase 2, personas LLM), lo hace como
  descubrimiento (`PENDING`), nunca como disparo de ejecución.

## Dependency graph

```
Contrato Finding (contracts/)                                  ✅
│
├── Persona estática (tooling → Finding[])                     ✅
│       │
│       └── Wiring worker: reviewers reales                     ✅
│
Cola Redis (API enqueue ↔ worker consume)                       ✅
│
├── Scope filtrado + TwgVcs endurecido                          ✅
│       │
│       └── Grafo compile + invoke desde worker                 ✅
│               │
│               ├── Publish inline (diff-line + comment --path/--line)  ✅
│               ├── Publish resumen PR                          ✅
│               └── Publish Teams                               ✅
│
├── Persona LLM (correctness, opt-in)                           ✅
└── Ingesta diferida (POST /webhook/tooling + PendingReview)    ✅
```

## Task List

### Slice A — Cimientos de Fase 1

- [x] Task 1: Clamp report-only en política
- [x] Task 2: Contrato `Finding` versionado en `contracts/`
- [x] Task 3: Cola Redis entre API y worker
- [x] Task 4: Worker ejecuta el grafo
- [x] Task 10: Ingesta diferida (endpoint de tooling + `PendingReview` + TTL) y retirada del webhook de PR

### Checkpoint: Foundation

- [x] `uv run pytest` verde
- [x] Webhook de tooling → cola → worker → grafo con `StaticReviewer` de test publica comentario (fake VCS)
- [x] Con `max_autonomy: report`, ningún `Decision.action` es `fix_*`
- [x] Review encolada **solo** con los artefactos completos; una espera vencida encola una review degradada

### Slice B — Review real sin LLM

- [x] Task 5: Endurecer `TwgVcsAdapter` + filtros de scope + anclas inline
- [x] Task 6: Persona `tooling` (estática) → `Finding[]`
- [x] Task 7: Publish: inline + resumen + Teams

### Checkpoint: Core report-only

- [x] Fixture de diff produce ≥1 hallazgo → ≥1 `post_inline_comment` (o fallback a resumen)
- [x] Resumen general + payload Teams presentes
- [x] Cero llamadas a push/commit/branch en el adaptador VCS
- [ ] Review humano del formato inline + resumen  ← **pendiente (humano)**

### Slice C — Señal cualitativa

- [x] Task 8: Persona LLM `correctness` (salida validada)
- [x] Task 9: Smoke E2E documentado (PR o replay)

### Checkpoint: Fase 1 completa

- [x] Definición de hecho del diseño §13: hallazgos **inline** (+ resumen/Teams); cero escritura de código
- [x] README actualizado a "Fase 1"
- [ ] Listo para decidir si abrir Fase 2 (HITL Opción B)  ← **pendiente (humano)**

## Tasks detalladas

## Task 1: Clamp report-only en política — HECHO (2026-10-11)

**Description:** Asegurar que en Fase 1 ninguna decisión salga del techo
`report`. Las reglas `fix_*` / `escalate` que el grafo no puede cumplir se
degradan a `report` conservando `reason` + `matched_rule`.

**Acceptance criteria:**
- [x] `max_autonomy: report` en config de Fase 1 (`config/autonomy.yaml`)
- [x] Tests: hallazgo P0 / `gated_auto` P3 / `protected_subject` / `manual` terminan en `action=report` con razón no vacía
- [x] El grafo no intenta fix ni interrupt

**Verification:**
- [x] `uv run pytest tests/test_policy.py`
- [x] Manual: inspeccionar un `Decision` con hallazgo que antes era `escalate`

**Dependencies:** None

**Files touched:**
- `config/autonomy.yaml`
- `src/reviews/policy/engine.py`
- `tests/test_policy.py`

**Estimated scope:** Small

---

## Task 2: Contrato Finding versionado — HECHO (2026-10-10)

**Description:** Materializar el contrato de `ce-code-review` en el repo
(`contracts/findings-schema.json` + nota de rubric) y un validador fino que
rechace hallazgos mal formados antes del merge. Entregado: `contracts/` con
esquema, rubric y README; `reviews.domain.contracts` con validación y chequeo de
drift contra el modelo Pydantic.

**Acceptance criteria:**
- [x] Esquema JSON en `contracts/` alineado con `Finding` de dominio
- [x] Hallazgo inválido (confidence no anclada, evidence vacía) falla en validación
- [x] Documento corto que diga "copiado de ce-code-review, no invocar la skill"

**Verification:**
- [x] `uv run pytest tests/test_domain.py tests/test_contracts.py`
- [x] Diff del esquema revisado contra el modelo Pydantic actual (test de drift)

**Dependencies:** None (paralelizable con Task 1)

**Files touched:**
- `contracts/findings-schema.json`, `contracts/README.md`, `contracts/action-class-rubric.md`
- `src/reviews/domain/contracts.py`
- `tests/test_domain.py`, `tests/test_contracts.py`

**Estimated scope:** Small

---

## Task 3: Cola Redis entre API y worker — HECHO (2026-10-11)

**Description:** Tras el `claim` de idempotencia, la API encola el
`ReviewRequest` (JSON) en Redis. El worker lo consume. Sin esto, Fase 1 no
existe en runtime aunque el grafo compile.

**Acceptance criteria:**
- [x] `POST /webhook/tooling` aceptado → mensaje en cola (con la review `READY`)
- [x] Duplicado por `repo:pr:head_sha` (y por `ingest_key`/`review_key`) no re-encola
- [x] Test de API con cola en memoria inyectable / fakeredis

**Verification:**
- [x] `uv run pytest tests/test_api.py tests/test_queue.py`
- [x] Manual con compose: webhook de prueba → log del worker

**Dependencies:** None (paralelizable con 1–2; bloquea Task 4)

**Files touched:**
- `src/reviews/api/main.py`
- `src/reviews/queue.py` (nuevo)
- `src/reviews/idempotency.py`
- `tests/test_api.py`, `tests/test_queue.py`
- `docker-compose.yml`

**Estimated scope:** Medium

---

## Task 4: Worker ejecuta el grafo — HECHO (2026-10-11)

**Description:** Sustituir el `sleep` del worker por consumo de cola, wiring de
puertos (VCS, chat, policy, reviewers) e `invoke` del grafo compilado. Errores
con log estructurado; no silenciar fallos de `twg`/Teams.

**Acceptance criteria:**
- [x] Mensaje en cola → grafo corre de punta a punta
- [x] Reviewers inyectados desde el perfil del repo (`build_reviewers`)
- [x] Fallo de VCS no tumba el proceso entero (log + `except` en `process_request`)

**Verification:**
- [x] Test de integración worker+cola+grafo con fakes
- [x] `docker compose up` muestra log de review completada con fixture

**Dependencies:** Task 3

**Files touched:**
- `src/reviews/worker/main.py`
- `src/reviews/graph/build.py`
- `src/reviews/settings.py`
- `tests/test_worker.py` (nuevo)

**Estimated scope:** Medium

---

## Task 5: Endurecer TwgVcsAdapter + filtros de scope + anclas inline — HECHO (2026-10-10)

**Verificado en vivo** contra `cqm/emotivoo-v2` PR 417 con `twg` 1.3.5 (solo lectura).
Contrato real incorporado: `--output json --output-summary none`, `author.display_name`,
`source.commit.hash`, `source/destination.branch.name`, `diffstat[].old/new.path` y
`diff-line.matches[].toLine`. Smoke real en `tests/test_live_twg.py` (marcado `live`).

**Description:** Verificar comandos reales de `twg bb prs *`, filtrar ruido en
`scope`, y ampliar el puerto VCS para comentarios inline. Bitbucket exige líneas
presentes en el diff: el adaptador resuelve el ancla con `prs diff-line` antes
de `comment create --path --line`.

**Acceptance criteria:**
- [x] `VcsPort` expone `resolve_inline_anchor(repo, pr_id, path, text) -> InlineAnchor | None`
- [x] `VcsPort` expone `post_inline_comment(repo, pr_id, path, line, body, *, from_line=None)`
- [x] Tests unitarios con stdout golden de `diff-line` y del create (sin red)
- [x] `get_diff` / `post_comment` / inline no usan flags inventados
- [x] Scope excluye rutas configuradas (p.ej. `composer.lock`, `var/**`)
- [x] Nunca se llama con `--pending`

**Verification:**
- [x] `uv run pytest tests/test_twg_vcs.py`
- [x] Una llamada real `twg bb prs diff-line <id>` documentada (Task 9 / `test_live_twg.py`)

**Dependencies:** Task 4 recomendada (para usarlo en wiring); tests del adaptador
pueden ir en paralelo

**Files touched:**
- `src/reviews/ports/vcs.py`
- `src/reviews/adapters/twg_vcs.py`
- `src/reviews/graph/build.py` (filtro en scope)
- `config/repo_profile.emotivoo-v2.yaml`
- `tests/test_twg_vcs.py`, `tests/test_live_twg.py`
- `tests/fixtures/twg/`

**Estimated scope:** Medium

---

## Task 6: Persona `tooling` (estática) — HECHO (2026-10-10)

**Description:** Implementar `ReviewerPort` que, dado un diff y salida capturada
de `phpstan analyse --error-format=json`, emita `Finding[]` autoritativos. La
herramienta **no se ejecuta** desde el agente en Fase 1 (eso es Fase 3): se
consume su salida. Entrega: `ToolingReviewer` + `parse_unified_diff`/`best_line`
que solo anclan en líneas añadidas del diff, descartan hallazgos fuera del PR y
marcan temas protegidos como `manual`.

**Decisión:** anclar sobre el **diff** (no sobre el JSON del linter) evita
depender de `diff-line` para cada hallazgo y garantiza líneas válidas.

**Acceptance criteria:**
- [x] `ToolingReviewer` (nombre `standards`) cumple `ReviewerPort`
- [x] Parser PHPStan JSON con fixture realista + parser de diff unificado
- [x] Hallazgos pasan validación del contrato (Task 2)
- [x] Integrado en el wiring del worker vía `ToolingOutputSource` (Task 6b)

**Decisión (Task 6b):** el origen de la salida de PHPStan en runtime es un
**artefacto** leído a través del puerto `ToolingOutputSource`
(`FileToolingOutputSource`). El worker construye la persona desde el perfil del
repo (`checks.typecheck`) y resuelve la ruta por review
(`var/tooling/{pr_id}/{head_sha}/{tool}.json`). El agente no ejecuta la
herramienta en Fase 1 (eso es el sandbox de Fase 3, otro adaptador del mismo
puerto). Artefacto ausente ⇒ `missing`/`unreadable`, y la review se marca
incompleta (no "0 hallazgos" silencioso).

El productor del artefacto vive en el repo revisado: en `emotivoo-v2`, el step de
PHPStan lo vuelca con `capture-on: always`. Con la ingesta diferida (Task 10), el
destino correcto es `POST /webhook/tooling` (ver "Trabajo pendiente"). `twg` no
descarga artefactos; el `POST` los persiste en la ruta que la plantilla resuelve.

**Verification:**
- [x] `uv run pytest tests/test_tooling_reviewer.py tests/test_tooling_source.py`
- [x] Grafo con solo esta persona + fixture → comentario con hallazgos P* coherentes

**Dependencies:** Task 2; Task 4 para wiring

**Files touched:**
- `src/reviews/adapters/tooling_reviewer.py`, `src/reviews/adapters/tooling_source.py`
- `src/reviews/adapters/diff_parser.py`, `src/reviews/adapters/text_utils.py`
- `src/reviews/ports/tooling.py`
- `tests/fixtures/phpstan/`, `tests/test_tooling_reviewer.py`, `tests/test_tooling_source.py`
- `src/reviews/worker/main.py`

**Estimated scope:** Medium

---

## Task 7: Publish — inline + resumen + Teams — HECHO (2026-10-11)

**Description:** El nodo `publish` deja de ser "un comentario general". Por cada
hallazgo (hasta el tope): resuelve ancla → inline; si falla, acumula en el
resumen. Luego un comentario general (índice + no anclados) y Teams teaser.
Cuerpo inline en español: severidad, título, por qué, suggested_fix opcional.

**Acceptance criteria:**
- [x] Hallazgo con ancla válida → exactamente una llamada `post_inline_comment`
- [x] Hallazgo sin ancla → aparece en el resumen general (no se pierde)
- [x] Con > `max_inline_comments`, prioriza P0→P3; el resto solo en resumen
- [x] Un solo MessageCard de Teams (teaser), no uno por hallazgo
- [x] FakeVcs en tests registra inlines + resumen por separado
- [x] Guard configurable (`autonomy.yaml` → `guards.max_inline_comments`)

**Verification:**
- [x] `uv run pytest tests/test_graph.py tests/test_teams.py`
- [ ] Review humano de un hilo inline de ejemplo  ← **pendiente (humano)**

**Dependencies:** Task 4; Task 5 para el adaptador real (tests con FakeVcs pueden
ir antes)

**Files touched:**
- `src/reviews/graph/build.py` (`publish`, `_render_*`)
- `src/reviews/ports/vcs.py`
- `src/reviews/adapters/teams_webhook.py`
- `config/autonomy.yaml`
- `tests/test_graph.py`

**Estimated scope:** Medium

---

## Task 8: Persona LLM `correctness` — HECHO (2026-10-11)

**Entrega:** `LlmReviewer` (`correctness`) con el puerto `LlmPort` inyectable
(`complete(system, prompt, max_tokens)`), validador de salida contra el contrato,
anclaje al diff igual que la persona estática (una línea inventada por el modelo
se descarta; cae al ancla por evidencia), tope `max_findings`, truncado de diff
por coste y `budget_tokens_per_pr` logueado. Cualquier fallo (proveedor caído,
JSON inválido, fields fuera de contrato) ⇒ 0 hallazgos, nunca excepción al grafo.
Adaptador incluido: `OpenAiCompatibleLlm` (contrato `/chat/completions`; vale
OpenAI, Azure, Groq, vLLM, Ollama). Opt-in via `LLM_REVIEWER_ENABLED` +
`LLM_MODEL`; desactivado por defecto en Fase 1.
La heurística de tema protegido se extrajo a `adapters/text_utils.py` (fuente
única compartida con `ToolingReviewer`).

**Acceptance criteria:**
- [x] `LlmReviewer(name="correctness")` con puerto de modelo inyectable
- [x] Salida inválida → lista vacía + log, no excepción al grafo
- [x] Test con LLM fake que devuelve JSON válido/inválido
- [x] `budget_tokens_per_pr` consultado o al menos logueado

**Verification:**
- [x] `uv run pytest tests/test_llm_reviewer.py tests/test_openai_llm.py`
- [x] Grafo con tooling + correctness: merge deduplica por `(file, title)`
      (`test_e2e_estatica_y_llm_conviven_y_el_merge_deduplica`)

**Dependencies:** Tasks 2, 4, 6, 7 (publish ya estable)

**Files touched:**
- `src/reviews/adapters/llm_reviewer.py`, `src/reviews/adapters/openai_llm.py`
- `src/reviews/adapters/text_utils.py`
- `src/reviews/ports/llm.py`
- `src/reviews/settings.py`, `src/reviews/worker/main.py`
- `tests/test_llm_reviewer.py`, `tests/test_openai_llm.py`, `tests/test_e2e_tooling.py`

**Estimated scope:** Medium

---

## Task 9: Smoke E2E documentado — HECHO (2026-10-11)

**Entrega:** `docs/smoke-fase1-20261011.md` con dos niveles: Nivel 1 (replay local
sin red sobre los tests E2E, la "definición de hecho" diaria) y Nivel 2 (smoke real
contra Bitbucket: compose up, `POST /webhook/tooling` firmado con el JSON de
PHPStan, verificación de inline + resumen + Teams). README actualizado con las
personas `standards`/`correctness`/`null` y `.env.example` con las variables LLM.

**Acceptance criteria:**
- [x] `docs/` con pasos y variables necesarias
- [x] Resultado esperado descrito ("202 aceptado", inline por hallazgo, un teaser Teams)
- [x] Lista de secretos mínimos: webhook Bitbucket, Teams, credenciales twg

**Verification:**
- [ ] Ejecución manual una vez (Nivel 2; requiere `twg` + secretos)  ← **pendiente**
- [x] `uv run pytest` + `uv run ruff check src` verdes

**Dependencies:** Tasks 1–8

**Files touched:**
- `README.md`, `docs/smoke-fase1-20261011.md`, `.env.example`

**Estimated scope:** Small

---

## Task 10: Ingesta diferida (cierre de la carrera webhook↔CI) — HECHO (2026-10-11) · **PUENTE**

> **Naturaleza:** andamiaje desechable, no cimentación. Su complejidad
> (`PendingReview`, barrera de N, TTL) existe **solo** porque la salida llega de
> fuera y llega tarde. Al abrir el puerto `CheckRunner` (Fase 3) el disparo vuelve
> a ser el PR. No añadir features aquí; no endurecerlo. Ver
> `docs/ingesta-vs-ejecucion-20261011.md`.

**Description:** Sustituir el disparo por PR por un disparo por "CI completó el
artefacto". El CI del repo revisado hace `POST /webhook/tooling`; la API persiste
el artefacto y lleva un registro `PendingReview` por `repo:pr:head_sha`. La review
solo se encola cuando están los N artefactos del perfil (barrera) o cuando vence el
TTL (review degradada). El webhook de PR se retira. Diseño en
`docs/ingesta-diferida-ci-20261011.md`.

**Acceptance criteria:**
- [x] `POST /webhook/tooling` con firma HMAC, persistencia de artefacto e idempotencia por `(review, tool)`
- [x] `PendingReview` (`pending`/`ready`/`degraded`) con barrera de N y TTL de `ready`/`pending`
- [x] Claves separadas: `ingest_key` (evento de CI) y `review_key` (ejecución), para que no colisionen
- [x] El worker barre los TTL y encola las reviews degradadas
- [x] `/webhook/bitbucket` retirado; un test fija que ya no existe (`test_webhook_de_pr_retirado` → 404)
- [x] Tests de barrera, TTL, idempotencia y firma

**Files touched:**
- `src/reviews/api/main.py`
- `src/reviews/domain/pending.py` (nuevo)
- `src/reviews/pending_store.py` (nuevo)
- `src/reviews/worker/main.py`
- `src/reviews/domain/models.py`
- `config/repo_profile.emotivoo-v2.yaml`, `.env.example`
- `tests/test_pending.py`, `tests/test_api.py`, `tests/test_worker.py`

**Estimated scope:** Medium

---

## Trabajo pendiente (no bloquea el código, sí el "hecho" real)

| # | Pendiente | Tipo | Dónde | Bloquea |
|---|-----------|------|-------|---------|
| P1 | El CI de `emotivoo-v2` debe hacer `POST /webhook/tooling` (firmado) en vez de subir el artefacto a Bitbucket | Externo (deseable, **ya no bloqueante**) | repo revisado (§7.1 de la ingesta diferida) | Anotar inline un pipeline rojo; no la Fase 1 |
| P2 | Nivel 2 del smoke: un PR real, verificar inline + resumen + Teams | Verificación | manual con `twg` + secretos | Dar Fase 1 por hecha |
| P3 | Revisión humana del formato inline + resumen | Verificación | manual | Cerrar Checkpoint B |
| P4 | Decisión go/no-go para Fase 2 (HITL Opción B) | Humano | — | Abrir Fase 2 |

> **P1 es un interruptor, no la cimentación.** El endpoint existe y está probado;
> hasta que el CI publique el `POST`, toda review acaba degradada por TTL y se
> pierde la anotación inline de un pipeline rojo. Pero con `make stan`/`make deptrac`
> bloqueantes y en verde el artefacto aporta cero señal nueva: el puente se retira,
> la Fase 1 no depende de él. Decisión en `docs/ingesta-vs-ejecucion-20261011.md`.
>
> **Resuelto (2026-10-11):** las dos deudas técnicas que quedaban fuera de los
> pendientes humanos — dedupe de re-push y checkpointer durable — están
> implementadas. Ver "Deudas cerradas" abajo.

---

## Risks and Mitigations

| Risk | Impact | Mitigation | Estado |
|------|--------|------------|--------|
| Contrato `twg` distinto al adaptador | Alto | Goldens de stdout reales; Task 5 antes de smoke | ✅ Resuelto (twg 1.3.5 verificado) |
| Persona LLM ruidosa / cara | Medio | Tooling primero; umbral confidence; presupuesto | ✅ Opt-in + validación de contrato |
| Cola sin DLQ | Medio | Reintento con contador + log del `review_key` al agotar; DLQ explícita en Fase 2 si duele | ⚠️ Aceptado (Fase 2) |
| Ejecutar PHPStan "de verdad" sin sandbox | Alto | En Fase 1 solo fixtures/artefacto inyectado; sandbox = Fase 3 | ✅ Fuera de alcance |
| `escalate` en comentarios confunde | Bajo | Clamp a `report` + texto "habría escalado: …" | ✅ Clamp activo |
| Línea de Finding fuera del diff | Alto | Ancla sobre el diff; fallback a resumen | ✅ Cubierto por tests |
| Spam de inlines en PRs grandes | Alto | `max_inline_comments` (20) + prioridad por severidad | ✅ Guard activo |
| Re-push duplica inlines | Medio | Idempotencia por SHA + dedupe por comentarios previos del agente | ✅ Resuelto (checkpointer por hilo + marca de publicación por commit) |
| Review sin datos del CI (carrera webhook↔CI) | Alto | Ingesta diferida: encolar solo con artefactos completos (Task 10) | ✅ Resuelto |
| CI nunca publica el artefacto | Medio | TTL de ingesta → review degradada con aviso explícito (Task 10) | ⚠️ Aceptado (puente desechable; ver `docs/ingesta-vs-ejecucion-20261011.md`) |
| Elegir ingesta como arquitectura permanente | Medio | Puerto `CheckRunner`/`ExecResult` en Fase 3; el artefacto es un adaptador retirable | ⚠️ A vigilar |
| Estado del grafo perdido en un reinicio | Medio | Checkpointer durable (Postgres) por `thread_id` | ✅ Resuelto |

## Open Questions

1. ~~¿Hay un PR abierto de `emotivoo-v2` usable para el smoke, o preferís solo replay/fixtures hasta tener credenciales?~~ → **Replay/fixtures primero; PR 417 identificado para el Nivel 2 (P2).**
2. ~~¿Modelo/provider para Task 8?~~ → **Puerto `LlmPort` + adaptador OpenAI-compatible; modelo por env (`LLM_MODEL`), opt-in.**
3. ¿El Incoming Webhook de Teams del canal objetivo ya existe, o hay que crearlo antes del Checkpoint Core?
4. ~~¿Clamp `max_autonomy: report` en config?~~ → **Sí, en `config/autonomy.yaml`.**
5. ~~`max_inline_comments`?~~ → **20** (confirmado).
6. ~~¿El step de PHPStan publica el artefacto en el agente (`POST /webhook/tooling`) o sigue en Bitbucket?~~ → **Decisión A (push al agente).** Implementado en el agente; falta desplegarlo en el CI del repo revisado (P1).
7. ~~¿Firma HMAC del CI (cabecera `X-Reviews-Signature`) o se acepta el POST dentro de la red?~~ → **HMAC-SHA256 del cuerpo crudo; `401` si la firma falta o no cuadra** (se relaja cuando el secreto sigue siendo `change-me`, para local).

## Parallelization

| Paralelo seguro | Secuencial |
|---|---|
| Task 1 ∥ Task 2 ∥ Task 3 | Task 4 después de 3 |
| Task 5 tests ∥ Task 6 parser | Task 6 wiring después de 4 |
| Task 7 después de 4 | Task 8 después de 6+7 |
| | Task 9 al final |

## Crítica / mejoras al propio plan

- **No meter sandbox en Fase 1** aunque el anexo A sea jugoso: mezclaría "reportar" con "ejecutar código del PR" y dispara la superficie de seguridad §8.
- **Una sola persona LLM** basta para validar coste/calidad; `security` puede esperar a Fase 1.1 si el ruido de `correctness` es alto.
- **El publish inline (T7) era camino crítico**: un resumen suelto no cumple el "hecho" de Fase 1. Se resolvió con anclaje por diff + fallback.
- **Tope de inlines**: PHPStan en un PR grande puede ser decenas de avisos. Pocos anclados de alta severidad > un muro en el diff. `max_inline_comments=20`.
- **La carrera webhook↔CI no era un bug de timing, era un error de diseño** (disparo ≠ dato listo). La ingesta diferida lo elimina por construcción; la lección es "modela el evento por el dato que necesitas, no por el hecho visible".
- **El artefacto del CI era la cimentación equivocada.** Con `stan`/`deptrac` bloqueantes y verdes, ingerir su salida aporta cero señal nueva; solo re-emite lo que el CI ya dijo. Un puente que exige un cambio en *otro* repo no debe sostener la "definición de hecho". La capacidad que sí es estructural es **ejecutar** (verificación tras fix): converge en un puerto `CheckRunner`/`ExecResult` en Fase 3. Ver `docs/ingesta-vs-ejecucion-20261011.md`.

## Deudas cerradas (2026-10-11)

Las dos deudas que la retrospescción inicial marcó como "a vigilar en Fase 2" se
adelantaron a Fase 1 porque ambas protegían el camino crítico del publish.

### D1 — Dedupe de comentarios en re-push

**Problema:** un `pipeline_id` nuevo sobre el mismo `sha` (reintento del CI, re-run
del pipeline) re-ejecutaba la review y **duplicaba** inlines y resumen. La
idempotencia de ingesta no lo cubría porque la clave de review incluye el
`pipeline_id`.

**Solución en dos capas:**

1. **Hilo por ejecución.** `ReviewRequest.thread_id = review_key`; el worker
   invoca el grafo con ese `thread_id`. El checkpointer (D2) devuelve el estado
   ya terminado y el worker no re-ejecuta.
2. **Marca de publicación por commit.** El resumen lleva un pie invisible
   `<!-- reviews:<sha256(commit)>:ok|:partial -->`. El `publish` lee los
   comentarios del PR (`list_review_markers`) y no re-publica si ya existe una
   review **completa** de ese commit. Los inlines se deduplican por igualdad de
   cuerpo, de modo que un resumen fallido tras los inlines no los repite en el
   reintento.

**Decisiones de diseño (y sus porqués):**
- La marca usa el **commit**, no el pipeline: es la identidad de lo que se
  publica en el PR. El pipeline solo decide el hilo del checkpointer.
- El sufijo `:ok` / `:partial` distingue completa de degradada: una review
  degradada (TTL, artefacto ausente) **no** debe bloquear a una completa que
  llegue después con los datos. Es el caso que la ingesta diferida existe para
  recuperar.
- La marca va **solo en el resumen** (la última escritura del publish): así, si
  el proceso muere entre inline y resumen, el reintento completa lo que falta en
  vez de quedar bloqueado.

**Ficheros:** `domain/models.py` (`thread_id`, `dedupe_token`),
`ports/vcs.py` (`ReviewMarker`, `list_review_markers`),
`adapters/twg_vcs.py` (`comment query` → `content.raw`),
`graph/build.py` (`publish`, `_published_markers`, `_is_published`, `_footer`).

### D2 — Checkpointer durable

**Problema:** el grafo corría sin persistir estado; un reinicio del worker perdía
el hilo y el HITL (Fase 2) no podía pausar/reanudar.

**Solución:** `reviews/checkpointer.py` construye un `PostgresSaver` (de
`langgraph-checkpoint-postgres`, ya en dependencias) con **serializer restringido**
a los tipos del dominio (`allowed_msgpack_modules`), de modo que no dependa del
msgpack permisivo que hoy avisa y en el futuro bloqueará los tipos no registrados.
Sin `DATABASE_URL` cae a `InMemorySaver` (tests/local/CI) y nunca lanza. El worker
lo construye en `build_runtime` y lo pasa a `graph.compile(checkpointer=...)`.

**Ficheros:** `checkpointer.py` (nuevo), `worker/main.py`
(`build_runtime`, `process_request`, `_completed_snapshot`),
`.env.example`, `README.md`.

**Verificación:** `tests/test_checkpointer.py` (serializer, hilo estable,
reentrega) + `tests/test_graph.py` (dedupe por commit, degradada que mejora,
reintento tras fallo del resumen). Además, smoke manual contra un Postgres
efímero que confirmó los 4 escenarios: reenvío, reinicio con mismo `thread_id`,
y pipeline nuevo sobre el mismo `sha`.

---


**Lo que funcionó bien**
- Fixtures primero: el publish E2E con `FakeVcs` encontró los problemas de anclaje antes de tocar Bitbucket.
- El contrato de `Finding` como pieza única validada por Pydantic + JSON Schema: permitió añadir la persona LLM sin tocar el grafo.
- Ingesta diferida como "un dato = un evento": el `PendingReview` hizo explícito un estado que antes era implícito (y erróneo).

**Señales de deuda a vigilar en Fase 2**
- **DLQ de cola:** un fallo transitorio ya no se pierde: `_consume` reencola con contador hasta `MAX_REVIEW_ATTEMPTS` y, agotado, descarta dejando el `review_key` en el log. Falta la DLQ explícita (hoy solo log) para no depender de reinyección manual; no válido cuando el grafo pausa y reanuda.
- **Canal de decisión del HITL:** aún sin definir (PR/Jira/CLI); es lo que convierte una review pausada en una decisión.
- **Dedupe por token:** si un humano copia el pie `<!-- reviews:… -->` a otro commit, el publish lo leería como ya publicado. Riesgo bajo (pie invisible), pero conviene no asumirlo en Fase 2.
- **Checkpointer acoplado a Postgres:** `build_checkpointer` abre una conexión `psycopg` síncrona y construye el `PostgresSaver` directamente, sin puerto. Para Report-only basta (una sola instancia, un solo `DATABASE_URL`), pero el `interrupt` del HITL invertirá la dirección: la reanudación llegará desde el *ingress* (webhook de PR/CI) en **otro proceso**, que necesita el mismo saver. Merece un `CheckpointerPort` (como `VcsPort`/`LlmPort`) para (a) no acoplar worker e ingress a Postgres, (b) poder testear el resume sin BD real, y (c) aislar la retención/poda. Es el primer trabajo de Fase 2, antes del `interrupt`.

**Fase 2 (HITL, Opción B) — boceto de alcance**
0. Extraer `CheckpointerPort` + adaptador `PostgresCheckpointer` e inyectarlo en worker e ingress (hoy el saver se construye en `build_runtime` y muere con el proceso del worker).
1. `interrupt` en el nodo de decisión + reanudación (el checkpointer durable ya está listo: D2).
2. Canal de respuesta (PR/Jira/CLI) y `Command(resume=...)` sobre el mismo `thread_id`.
3. Devolver el webhook de PR como **descubrimiento** (`PENDING`), nunca como disparo.
4. Retención/poda de checkpoints antiguos en Postgres.
