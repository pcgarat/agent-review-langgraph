# Implementation Plan: Fase 1 — Report-only

## Overview

Cerrar el camino de valor del MVP: un webhook de Bitbucket dispara una review
real (diff vía `twg`), al menos una persona produce hallazgos validados contra el
contrato, la política decide, y el resultado se publica como **comentarios inline
en el diff** (+ resumen general en el PR + Teams). Cero escritura de código en el
repo del PR. Sin HITL, sin sandbox, sin autofix.

Parte de la Fase 0 ya entregada: dominio, `PolicyEngine`, grafo
`scope → persona → merge → policy → publish`, adaptadores esqueleto (`TwgVcs`,
`TeamsWebhook`), API con idempotencia, compose. Lo que falta es **cablear**
(cola + worker), **personas reales**, y **verificar** contra un PR/fixture.

## Architecture Decisions

- **Report-only de verdad.** En Fase 1, `max_autonomy: report` en
  `config/autonomy.yaml` (o clamp en el motor). Las reglas que hoy dan
  `escalate` / `fix_*` se degradan a `report` con la razón original en el
  comentario. Evita “decisiones fantasma” que el grafo aún no puede ejecutar.
- **Persona estática antes que LLM.** Primera persona: parsear salida de
  herramientas del repo (PHPStan / CS Fixer sobre el diff o fixture). Valida el
  pipeline y el contrato sin coste de tokens. La persona LLM llega cuando el
  publish E2E ya está verde.
- **Cola mínima Redis.** La API hace `claim` + `RPUSH`; el worker hace `BLPOP` y
  ejecuta el grafo. Sin orquestador extra. Si Redis cae, fallar ruidoso en
  producción; en tests, cola en memoria.
- **Contrato versionado en el repo.** Copiar (no invocar) el esquema de
  `ce-code-review` a `contracts/` y validar hallazgos contra él. El runtime de
  la skill no entra en el grafo.
- **Fixtures primero, PR real después.** Tests unitarios/integración con diff
  golden de `emotivoo-v2`. Smoke manual opcional contra Bitbucket cuando `twg`
  y secretos estén disponibles.
- **Inline primero, resumen después.** Cada hallazgo intenta anclarse con
  `diff-line` → `comment create --path --line`. Si el ancla falla, el hallazgo
  no se pierde: va al resumen general. Tope `max_inline_comments` (20), prioridad
  P0→P3. Sin `--pending` (twg no puede publicar la review batch).
- **Fuera de alcance Fase 1:** `interrupt`, Bot Teams, Jira, sandbox, fix,
  mutation testing, checkpointer durable (útil en Fase 2; no bloquea report-only).
- **Ingesta diferida (cierre de la carrera).** El disparador deja de ser el webhook
  de PR: la review se encola cuando el CI deposita el artefacto que necesita
  (`POST /webhook/tooling`). Detalle en `docs/ingesta-diferida-ci-20261011.md`.
  El webhook de PR se retira; si vuelve (Fase 2, personas LLM), lo hace como
  descubrimiento (`PENDING`), nunca como disparo de ejecución.

## Dependency graph

```
Contrato Finding (contracts/)
    │
    ├── Persona estática (tooling → Finding[])
    │       │
    │       └── Wiring worker: reviewers reales
    │
Cola Redis (API enqueue ↔ worker consume)
    │
    ├── Scope filtrado + TwgVcs endurecido
    │       │
    │       └── Grafo compile + invoke desde worker
    │               │
    │               ├── Publish inline (diff-line + comment --path/--line)
    │               ├── Publish resumen PR
    │               └── Publish Teams
    │
    └── Persona LLM (después del publish E2E con estática)
```

## Task List

### Slice A — Cimientos de Fase 1

- [ ] Task 1: Clamp report-only en política
- [ ] Task 2: Contrato `Finding` versionado en `contracts/`
- [ ] Task 3: Cola Redis entre API y worker
- [ ] Task 4: Worker ejecuta el grafo
- [ ] Task 10: Ingesta diferida (endpoint de tooling + `PendingReview` + TTL) y retirada del webhook de PR

### Checkpoint: Foundation

- [ ] `uv run pytest` verde
- [ ] Webhook de tooling → cola → worker → grafo con `StaticReviewer` de test publica comentario (fake VCS)
- [ ] Con `max_autonomy: report`, ningún `Decision.action` es `fix_*`
- [ ] Review encolada **solo** con los artefactos completos; una espera vencida encola una review degradada

### Slice B — Review real sin LLM

- [ ] Task 5: Endurecer `TwgVcsAdapter` + filtros de scope + anclas inline
- [ ] Task 6: Persona `tooling` (estática) → `Finding[]`
- [ ] Task 7: Publish: inline + resumen + Teams

### Checkpoint: Core report-only

- [ ] Fixture de diff produce ≥1 hallazgo → ≥1 `post_inline_comment` (o fallback a resumen)
- [ ] Resumen general + payload Teams presentes
- [ ] Cero llamadas a push/commit/branch en el adaptador VCS
- [ ] Review humano del formato inline + resumen

### Slice C — Señal cualitativa

- [ ] Task 8: Persona LLM `correctness` (salida validada)
- [ ] Task 9: Smoke E2E documentado (PR o replay)

### Checkpoint: Fase 1 completa

- [ ] Definición de hecho del diseño §13: hallazgos **inline** (+ resumen/Teams); cero escritura de código
- [ ] README actualizado a “Fase 1”
- [ ] Listo para decidir si abrir Fase 2 (HITL Opción B)

## Tasks detalladas

## Task 1: Clamp report-only en política

**Description:** Asegurar que en Fase 1 ninguna decisión salga del techo
`report`. Hoy `max_autonomy: fix_with_approval` y hay reglas `fix_*` /
`escalate` que el grafo no puede cumplir. Degradar a `report` conservando
`reason` + `matched_rule`.

**Acceptance criteria:**
- [ ] `max_autonomy: report` en config de Fase 1 (o flag de settings)
- [ ] Tests: hallazgo P0 / `gated_auto` P3 terminan en `action=report` con razón no vacía
- [ ] El grafo no intenta fix ni interrupt

**Verification:**
- [ ] `uv run pytest tests/test_policy.py`
- [ ] Manual: inspeccionar un `Decision` con hallazgo que antes era `escalate`

**Dependencies:** None

**Files likely touched:**
- `config/autonomy.yaml`
- `src/reviews/policy/engine.py`
- `tests/test_policy.py`

**Estimated scope:** Small

---

## Task 2: Contrato Finding versionado — HECHO (2026-10-10)

**Description:** Materializar el contrato de `ce-code-review` en el repo
(`contracts/findings-schema.json` + nota de rubrica) y un validador fino que
rechace hallazgos mal formados antes del merge. Entregado: `contracts/` con
esquema, rubrica y README; `reviews.domain.contracts` con validacion y chequeo
de drift contra el modelo Pydantic.

**Acceptance criteria:**
- [ ] Esquema JSON en `contracts/` alineado con `Finding` de dominio
- [ ] Hallazgo inválido (confidence no anclada, evidence vacía) falla en validación
- [ ] Documento corto que diga “copiado de ce-code-review, no invocar la skill”

**Verification:**
- [ ] `uv run pytest tests/test_domain.py` (+ test de schema si se añade)
- [ ] Diff del esquema revisado contra el modelo Pydantic actual

**Dependencies:** None (paralelizable con Task 1)

**Files likely touched:**
- `contracts/findings-schema.json`
- `contracts/README.md` o `contracts/action-class-rubric.md`
- `src/reviews/domain/` (solo si hace falta alinear campos)
- `tests/test_domain.py`

**Estimated scope:** Small

---

## Task 3: Cola Redis entre API y worker

**Description:** Tras el `claim` de idempotencia, la API encola el
`ReviewRequest` (JSON) en Redis. El worker lo consume. Sin esto, Fase 1 no
existe en runtime aunque el grafo compile.

**Acceptance criteria:**
- [ ] `POST /webhook/bitbucket` aceptado → mensaje en cola
- [ ] Duplicado por `repo:pr:head_sha` no re-encola
- [ ] Test de API con Redis fake / fakeredis o cola en memoria inyectable

**Verification:**
- [ ] `uv run pytest tests/test_api.py`
- [ ] Manual con compose: webhook de prueba → `LLEN` / log del worker

**Dependencies:** None (paralelizable con 1–2; bloquea Task 4)

**Files likely touched:**
- `src/reviews/api/main.py`
- `src/reviews/queue.py` (nuevo) o similar
- `tests/test_api.py`
- `docker-compose.yml` (solo si falta env)

**Estimated scope:** Medium

---

## Task 4: Worker ejecuta el grafo

**Description:** Sustituir el `sleep` del worker por consumo de cola, wiring de
puertos (VCS, chat, policy, reviewers) e `invoke` del grafo compilado. Errores
con log estructurado; no silenciar fallos de `twg`/Teams.

**Acceptance criteria:**
- [ ] Mensaje en cola → grafo corre de punta a punta
- [ ] Reviewers inyectados (mínimo un stub/static hasta Task 6)
- [ ] Fallo de VCS no tumba el proceso entero (log + ack/retry documentado)

**Verification:**
- [ ] Test de integración worker+cola+grafo con fakes
- [ ] `docker compose up` muestra log de review completada con fixture

**Dependencies:** Task 3

**Files likely touched:**
- `src/reviews/worker/main.py`
- `src/reviews/graph/build.py` (solo si hace falta factory)
- `tests/test_worker.py` (nuevo)
- `src/reviews/settings.py`

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
- [ ] `VcsPort` expone `resolve_inline_anchor(repo, pr_id, path, text) -> InlineAnchor | None`
- [ ] `VcsPort` expone `post_inline_comment(repo, pr_id, path, line, body, *, from_line=None)`
- [ ] Tests unitarios con stdout golden de `diff-line` y del create (sin red)
- [ ] `get_diff` / `post_comment` / inline no usan flags inventados
- [ ] Scope excluye rutas configuradas (p.ej. `composer.lock`, `var/**`)
- [ ] Nunca se llama con `--pending`

**Verification:**
- [ ] `uv run pytest tests/test_twg_vcs.py`
- [ ] Opcional: una llamada real `twg bb prs diff-line <id>` documentada en Task 9

**Dependencies:** Task 4 recomendada (para usarlo en wiring); tests del adaptador
pueden ir en paralelo

**Files likely touched:**
- `src/reviews/ports/vcs.py`
- `src/reviews/adapters/twg_vcs.py`
- `src/reviews/graph/build.py` (filtro en scope)
- `config/repo_profile.emotivoo-v2.yaml`
- `tests/test_twg_vcs.py`
- `tests/fixtures/twg/` (goldens)

**Estimated scope:** Medium

---

## Task 6: Persona `tooling` (estática) — HECHO (2026-10-10)

**Description:** Implementar `ReviewerPort` que, dado un diff y salida capturada
de `phpstan analyse --error-format=json`, emita `Finding[]` autoritativos. La
herramienta **no se ejecuta** desde el agente en Fase 1 (eso es Fase 3): se
consume su salida. Entrega: `ToolingReviewer` + `parse_unified_diff`/`best_line`
que solo anclan en lineas anadidas del diff, descartan hallazgos fuera del PR y
marcan temas protegidos como `manual`.

**Decisión:** anclar sobre el **diff** (no sobre el JSON del linter) evita
depender de `diff-line` para cada hallazgo y garantiza lineas validas.

**Acceptance criteria:**
- [x] `ToolingReviewer` (nombre `standards`) cumple `ReviewerPort`
- [x] Parser PHPStan JSON con fixture realista + parser de diff unificado
- [x] Hallazgos pasan validación del contrato (Task 2)
- [x] Integrado en el wiring del worker vía `ToolingOutputSource` (Task 6b)

**Decisión (Task 6b):** el origen de la salida de PHPStan en runtime es un
**artefacto de CI** leído a través del puerto `ToolingOutputSource`
(`FileToolingOutputSource`). El worker construye la persona desde el perfil del
repo (`checks.typecheck`) y resuelve la ruta por review
(`var/tooling/{pr_id}/{head_sha}/{tool}.json`). El agente no ejecuta la herramienta
en Fase 1 (eso es el sandbox de Fase 3, otro adaptador del mismo puerto).
Artefacto ausente ⇒ 0 hallazgos, sin fallar.

El productor del artefacto vive en el repo revisado: en `emotivoo-v2`, el step de
PHPStan lo vuelca y lo sube con `capture-on: always` (rama
`chore/publicar-salida-phpstan-para-code-review`). La ruta coincide con la
plantilla por defecto y con `{head_sha}` = `BITBUCKET_COMMIT` (source head en
builds de PR). Pendiente para T9: `twg` no descarga artefactos; la bajada vía API
de Bitbucket queda como trabajo del worker.

**Verification:**
- [ ] `uv run pytest tests/test_tooling_reviewer.py`
- [ ] Grafo con solo esta persona + fixture → comentario con hallazgos P* coherentes

**Dependencies:** Task 2; Task 4 para wiring

**Files likely touched:**
- `src/reviews/adapters/tooling_reviewer.py` (nuevo)
- `tests/fixtures/phpstan/` o similar
- `tests/test_tooling_reviewer.py`
- `src/reviews/worker/main.py`

**Estimated scope:** Medium

---

## Task 7: Publish — inline + resumen + Teams

**Description:** El nodo `publish` deja de ser “un comentario general”. Por cada
hallazgo (hasta el tope): resuelve ancla → inline; si falla, acumula en el
resumen. Luego un comentario general (índice + no anclados) y Teams teaser.
Cuerpo inline en español: severidad, título, por qué, suggested_fix opcional.

**Acceptance criteria:**
- [ ] Hallazgo con ancla válida → exactamente una llamada `post_inline_comment`
- [ ] Hallazgo sin ancla → aparece en el resumen general (no se pierde)
- [ ] Con > `max_inline_comments`, prioriza P0→P3; el resto solo en resumen
- [ ] Un solo MessageCard de Teams (teaser), no uno por hallazgo
- [ ] FakeVcs en tests registra inlines + resumen por separado
- [ ] Guard configurable (settings o `autonomy.yaml` / repo_profile)

**Verification:**
- [ ] `uv run pytest tests/test_graph.py` (o `test_publish.py`)
- [ ] Review humano de un hilo inline de ejemplo (texto pegado en doc si no hay PR)

**Dependencies:** Task 4; Task 5 para el adaptador real (tests con FakeVcs pueden
ir antes)

**Files likely touched:**
- `src/reviews/graph/build.py` (`publish`, `_render_*`)
- `src/reviews/ports/vcs.py` (si aún falta el método en el Protocol)
- `src/reviews/adapters/teams_webhook.py`
- `config/autonomy.yaml` o settings (`max_inline_comments`)
- `tests/test_graph.py`

**Estimated scope:** Medium

---

## Task 8: Persona LLM `correctness` — HECHO (2026-10-11)

**Entrega:** `LlmReviewer` (`correctness`) con el puerto `LlmPort` inyectable
(`complete(system, prompt, max_tokens)`), validador de salida contra el contrato,
anclaje al diff igual que la persona estatica (una linea inventada por el modelo
se descarta; cae al ancla por evidencia), tope `max_findings`, truncado de diff
por coste y `budget_tokens_per_pr` logueado. Cualquier fallo (proveedor caido,
JSON invalido, fields fuera de contrato) => 0 hallazgos, nunca excepcion al grafo.
Adaptador incluido: `OpenAiCompatibleLlm` (contrato `/chat/completions`; vale
OpenAI, Azure, Groq, vLLM, Ollama). Opt-in via `LLM_REVIEWER_ENABLED` +
`LLM_MODEL`; desactivado por defecto en Fase 1.
La heuristica de tema protegido se extrajo a `adapters/text_utils.py` (fuente
unica compartida con `ToolingReviewer`).

**Acceptance criteria:**
- [x] `LlmReviewer(name="correctness")` con puerto de modelo inyectable
- [x] Salida invalida → lista vacia + log, no excepcion al grafo
- [x] Test con LLM fake que devuelve JSON valido/invalido
- [x] `budget_tokens_per_pr` consultado o al menos logueado

**Verification:**
- [x] `uv run pytest tests/test_llm_reviewer.py tests/test_openai_llm.py`
- [x] Grafo con tooling + correctness: merge deduplica por `(file, title)`
      (`test_e2e_estatica_y_llm_conviven_y_el_merge_deduplica`)

**Dependencies:** Tasks 2, 4, 6, 7 (publish ya estable)

**Files touched:**
- `src/reviews/adapters/llm_reviewer.py` (nuevo)
- `src/reviews/adapters/openai_llm.py` (nuevo)
- `src/reviews/adapters/text_utils.py` (nuevo)
- `src/reviews/ports/llm.py` (nuevo)
- `src/reviews/settings.py`, `src/reviews/worker/main.py`
- `tests/test_llm_reviewer.py`, `tests/test_openai_llm.py`, `tests/test_e2e_tooling.py`

**Estimated scope:** Medium

---

## Task 9: Smoke E2E documentado — HECHO (2026-10-11)

**Entrega:** `docs/smoke-fase1-20261011.md` con dos niveles: Nivel 1 (replay local
sin red sobre los tests E2E, la "definicion de hecho" diaria) y Nivel 2 (smoke real
contra Bitbucket: compose up, `POST /webhook/tooling` firmado con el JSON de
PHPStan, verificacion de inline + resumen + Teams). README actualizado con las
personas `standards`/`correctness`/`null` y `.env.example` con las variables LLM.

**Acceptance criteria:**
- [x] `docs/` con pasos y variables necesarias
- [x] Resultado esperado descrito ("202 aceptado", inline por hallazgo, un teaser Teams)
- [x] Lista de secretos minimos: webhook Bitbucket, Teams, credenciales twg

**Verification:**
- [ ] Ejecucion manual una vez (Nivel 2; requiere `twg` + secretos)
- [x] `uv run pytest` + `uv run ruff check src` verdes

**Dependencies:** Tasks 1–8

**Files touched:**
- `README.md`, `docs/smoke-fase1-20261011.md` (nuevo), `.env.example`

**Estimated scope:** Small

## Task 10: Ingesta diferida (cierre de la carrera webhook↔CI) — HECHO

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
- [x] `/webhook/bitbucket` retirado; un test fija que ya no existe
- [x] Tests de barrera, TTL, idempotencia y firma

**Files likely touched:**
- `src/reviews/api/main.py`
- `src/reviews/domain/pending.py` (nuevo)
- `src/reviews/pending_store.py` (nuevo)
- `src/reviews/worker/main.py`
- `src/reviews/domain/models.py`
- `tests/test_pending.py`, `tests/test_api.py`, `tests/test_worker.py`

**Estimated scope:** Medium

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Contrato `twg` distinto al adaptador | Alto | Goldens de stdout reales; Task 5 antes de smoke |
| Persona LLM ruidosa / cara | Medio | Tooling primero; umbral confidence; presupuesto |
| Cola sin DLQ | Medio | Log + no ack en error; DLQ explícita en Fase 2 si duele |
| Ejecutar PHPStan “de verdad” sin sandbox | Alto | En Fase 1 solo fixtures/stdout inyectado; sandbox = Fase 3 |
| `escalate` en comentarios confunde | Bajo | Clamp a `report` + texto “habría escalado: …” |
| Línea de Finding fuera del diff | Alto | Siempre `diff-line`; fallback a resumen |
| Spam de inlines en PRs grandes | Alto | `max_inline_comments` + prioridad por severidad |
| Re-push duplica inlines | Medio | Idempotencia por SHA + dedupe por comentarios previos del agente |
| Review sin datos del CI (carrera webhook↔CI) | Alto | Ingesta diferida: encolar solo con artefactos completos (Task 10) |
| CI nunca publica el artefacto | Medio | TTL de ingesta → review degradada con aviso explícito (Task 10) |

## Open Questions

1. ¿Hay un PR abierto de `emotivoo-v2` usable para el smoke, o preferís solo replay/fixtures hasta tener credenciales?
2. ¿Modelo/provider para Task 8 (OpenAI, Anthropic, local)? ¿Misma clave que usáis en otros agentes?
3. ¿El Incoming Webhook de Teams del canal objetivo ya existe, o hay que crearlo antes del Checkpoint Core?
4. ¿Confirmáis el clamp `max_autonomy: report` en config (cambio visible) frente a un override solo en settings de deploy?
5. ~~`max_inline_comments`?~~ → **20** (confirmado).
6. ¿El step de PHPStan de `emotivoo-v2` publica el artefacto en el agente (`POST /webhook/tooling`) o seguís subiéndolo a Bitbucket? (Decide A frente a B/C de `docs/ingesta-diferida-ci-20261011.md` §10.)
7. ¿Firma HMAC del CI (cabecera `X-Reviews-Signature`) o se acepta el POST dentro de la red?

## Parallelization

| Paralelo seguro | Secuencial |
|---|---|
| Task 1 ∥ Task 2 ∥ Task 3 | Task 4 después de 3 |
| Task 5 tests ∥ Task 6 parser | Task 6 wiring después de 4 |
| Task 7 después de 4 | Task 8 después de 6+7 |
| | Task 9 al final |

## Crítica / mejoras al propio plan

- **No meter sandbox en Fase 1** aunque el anexo A sea jugoso: mezclaría “reportar” con “ejecutar código del PR” y dispara la superficie de seguridad §8.
- **Una sola persona LLM** basta para validar coste/calidad; `security` puede esperar a Fase 1.1 si el ruido de `correctness` es alto.
- Si el equipo quiere señal mañana mismo: Tasks 1→3→4→6→7 con FakeVcs que simule
  anclas, y dejar 5/8/9 para cuando `twg` real esté listo. El publish inline (T7)
  es parte del camino crítico: un resumen suelto ya no cumple el “hecho” de Fase 1.
- **Crítica:** no publicar un inline por cada finding de tooling sin tope — PHPStan
  en un PR grande puede ser decenas de avisos. Mejor pocos anclados de alta
  severidad que un muro en el diff.
