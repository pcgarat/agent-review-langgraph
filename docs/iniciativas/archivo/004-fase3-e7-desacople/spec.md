Última modificación: 2026-10-16 | Creado: 2026-10-11

# Fase 3-E7 — Desacople fuerte de la verificación (handoff durable)

Especificación del **desacople fuerte** de la verificación de fixes que la
Fase 3 deja explícitamente pendiente (`docs/iniciativas/archivo/003-fase3-fix-verificacion/spec.md`
§5.3, enmienda; `docs/iniciativas/archivo/003-fase3-fix-verificacion/todo.md`, notas). **No** cambia el contrato del
runner (`POST /verify` sigue siendo bloqueante e idempotente por `run_key`): lo
que cambia es **quién espera**. Hoy el que espera es el worker de reviews, que es
secuencial; aquí el que espera es un worker de fixes dedicado.

Complementa el spec de Fase 3 (aprobado) y `docs/iniciativas/archivo/002-fase2-hitl/spec.md`. El
estado vivo del plan está en `docs/iniciativas/README.md`; las tareas en
`docs/iniciativas/archivo/004-fase3-e7-desacople/todo.md`.

> **Estado:** implementada (2026-10-11). Las preguntas abiertas (§13) se aprueban
> con su opción por defecto de v1: una pausa por wave (Q1), comentario general de
> seguimiento hasta que F11 confirme la respuesta al hilo (Q2) y un aviso compacto en
> `notify_fix` (Q3). Q1 se revisa con la primera medición real.

> **Entrega:** el handoff por cola está en marcha: el worker de reviews ya no toca
> el sandbox (`fix_dispatch` → `publish` → `fix_wait`); un `fix-worker` consume la
> wave, verifica y reanuda. Idempotencia por `run_key` (`reviews:fixes:done`), TTL
> (`FIX_VERIFY_TTL_S`) y poda (`FixWaitRetention`) cubiertas en `tests/`.
>
> **Cierre (2026-10-16):** F14–F28 cerradas (F25–F28 corrigen los hallazgos de la
> revisión del PR #3); docs vivas, README e índice al día. Queda solo el smoke real
> (criterio 9), precondicionado por **P6**.

---

## 1. El problema (por qué el adaptador síncrono no cumple E7)

E7 (`docs/iniciativas/archivo/003-fase3-fix-verificacion/spec.md` §2.1.2) exige que la verificación
**no bloquee la publicación de otras reviews**. Hoy no se cumple:

- `HttpSandboxAdapter.verify` es un `POST /verify` bloqueante con timeout
  `sandbox.timeout_s + 30 s` (hasta ~20 min; ~60 min con `max_fixes_per_pr: 3`).
- `run_forever` procesa los jobs **uno a uno**; mientras `apply_fixes` espera al
  runner, el bucle no vuelve a `dequeue`. Todas las demás reviews esperan.

El adaptador síncrono no es el culpable: es correcto y suficiente. El límite está
en **dónde se espera**. Ningún cambio de transporte (sondeo `POST /runs`) arregla
esto si el que sondea sigue siendo el worker secuencial; fingirlo en el adaptador
sería lo que esta spec evita.

El desacople real es un diseño distinto: **una cola/consumidor de fixes aparte y
la publicación de la sugerencia después**.

---

## 2. Decisión: handoff durable por `interrupt`, un único escritor

Se reutiliza la maquinaria de pausa durable que ya existe para el HITL (Fase 2).
El flujo pasa de "verificar dentro del nodo `fix`" a "**despachar** el fix, publicar
los hallazgos, **pausar**, y publicar la sugerencia al resolverse".

```
                              ┌──────────────────────── worker de REVIEWS (secuencial, sin sandbox)
scope → … → hitl → fix_dispatch → publish → fix_wait ──(interrupt)──┐
                                   │                                 │
                        publica hallazgos                  inbox(kind=fix) + cola de fixes
                                                                     │
        ┌──────────────────────── worker de FIXES (dedicado) ◄───────┘
        │  dequeue → SandboxPort.verify → encola ReviewRequest(fix_resolution)
        │
        └────► worker de REVIEWS: resume Command(resume=FixResolutions)
                     │
              fix_wait (aplica) ─┬─ ¿reintento? ──► re-pausa (otra wave)
                                 └─ terminal ─────► publish_fix (comentario de seguimiento) → notify_fix
```

Propiedades que fija la decisión:

1. **El worker de reviews nunca llama al sandbox.** `fix_dispatch` no verifica:
   propone el parche y lo valida localmente. El `POST /verify` bloqueante vive en
   el worker de fixes. La ruta de la review deja de tener el fix dentro.
2. **Un único escritor en el PR.** El worker de reviews publica los hallazgos; el
   worker de fixes **no publica nada**: solo verifica y encola una resolución. El
   worker de reviews, al reanudar, publica la sugerencia como **comentario de
   seguimiento**. Se conserva el modelo de "una pluma" de
   `docs/adr/0006-publicacion-inline-primero-resumen-al-final.md` (B6/Q1).
3. **La reanudación la ejecuta el worker de reviews** (como en Fase 2, D4), vía
   la **misma cola única**. El worker de fixes no construye el grafo ni habla con
   el checkpointer: no hay dos procesos invocando el mismo hilo.
4. **La espera es durable.** Si el worker de fixes o el runner caen, el `run_key`
   idempotente y la reentrega del job hacen que no se lance una segunda ejecución
   (R8) ni se pierda la review.
5. **El contrato del runner no cambia.** `VerifyRequest`/`VerifyReport` y el
   servicio `sandbox_runner` quedan igual. Solo se mueve el llamante.

### 2.1. Por qué la sugerencia se publica después (y no al despachar)

`VcsPort` solo tiene `post_inline_comment` y `post_comment`: no hay editar ni
borrar. Publicar la sugerencia "cuando se despacha" obligaría a un placeholder que
luego no se puede corregir. Diferirla a un **comentario de seguimiento** (general,
con la marca de dedupe por fix) es lo único coherente con el puerto actual y no
toca el hilo inline del hallazgo ni el contrato del resumen. Queda como pregunta
abierta (Q2) si vale la pena ampliar `VcsPort` para responder al hilo.

---

## 3. Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Mantener `POST /verify` y "sondearlo" desde el worker secuencial | El worker sigue siendo el que espera; no desacopla (ya descartado en el spec de Fase 3 §5.3) |
| Asyncio/threads dentro del worker de reviews | El worker ejecuta **un grafo** sobre Postgres con estado compartido; concurrencia interna añade riesgo sin separar procesos ni permisos. No aísla el trabajo no confiable |
| El worker de fixes publica la sugerencia | Dos escritores en el PR y una segunda ruta de publicación (rompe B6/Q1 y la marca de dedupe) |
| El worker de fixes reanuda el grafo | Dos procesos invocando el mismo `thread_id` sobre el checkpointer; sin lock cross-proceso. Se evita encolando la resolución para que reanude el worker de reviews |
| No publicar nada hasta que el fix resuelva | Retrasa **todos** los hallazgos ~20 min por fix; es lo que E7 prohíbe |
| Un `run_id` + sondeo solo para el worker de fixes | El contrato bloqueante ya es idempotente por `run_key`; añadir estado de runs no aporta y añade GC |

---

## 4. Alternativa de implementación descartada por el usuario (registro)

Queda constancia de que se evaluó que el worker de fixes reanudara el grafo
directamente. Se descarta por la concurrencia de dos invocadores sobre el mismo
hilo (§3). Si algún día el checkpointer ofreciera un lock cross-proceso, se podría
reconsiderar sin tocar el contrato.

---

## 5. Contrato (delta sobre `domain/models.py`)

### 5.1 Estado nuevo del fix

`FixStatus` gana un estado intermedio, el único que el nodo `fix_dispatch` no
puede cerrar por sí mismo:

```python
class FixStatus(StrEnum):
    PENDING = "pending_verification"   # parche válido; verificación despachada
    VERIFIED = "verified"
    FAILED = "failed"
    INVALID_PATCH = "invalid_patch"
    NOT_VERIFIABLE = "not_verifiable"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
```

### 5.2 Job de verificación y pausa de fix

```python
class VerifyJob(BaseModel):
    run_key: str              # review_key:finding_id:attempt — idempotencia
    finding_id: str
    attempt: int = Field(ge=1)
    repo: str
    head_sha: str
    patch: str
    recipes: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)

class FixWaitRequest(BaseModel):
    """Carga del `interrupt` del nodo `fix_wait`. Espejo de `ApprovalRequest`."""
    kind: Literal["fix"] = "fix"
    review_key: str
    repo: str
    pr_id: int
    head_sha: str
    jobs: list[VerifyJob] = Field(default_factory=list)

class FixResolution(BaseModel):
    run_key: str
    outcome: Literal["passed", "failed", "error"]
    checks: list[CommandResult] = Field(default_factory=list)
    image_digest: str | None = None
    error: str = ""           # solo si outcome == "error" (runner caído, 409, TTL)

class FixResolutions(BaseModel):
    """Valor del `Command(resume=...)`. Una wave resuelta de una vez."""
    items: list[FixResolution] = Field(default_factory=list)
```

`ReviewRequest` gana:

```python
    # Reanudación de una pausa de FIX: la resolución de la wave la produce el
    # worker de fixes y la aplica el worker de reviews (mismo transporte que la
    # decisión humana, `decision`).
    fix_resolution: FixResolutions | None = None
```

### 5.3 Inbox: un tipo de pausa, dos cargas

`PausedReview` es hoy `approval: ApprovalRequest`. Se generaliza **sin romper las
entradas en vuelo de Redis**:

```python
class PauseKind(StrEnum):
    HUMAN = "human"
    FIX = "fix"

class PausedReview(BaseModel):
    review_key: str
    request: ReviewRequest
    kind: PauseKind = PauseKind.HUMAN       # las entradas antiguas (sin campo) son human
    approval: ApprovalRequest | FixWaitRequest   # se elige por `kind`
    created_at: datetime
    ttl_s: float
    expired_enqueued_at: datetime | None = None
    expired_enqueues: int = 0
```

`ApprovalRequest` gana `kind: Literal["human"] = "human"` para que el discriminante
sea simétrico. La entrada antigua (con `approval` de tipo `ApprovalRequest` y sin
`kind`) sigue siendo válida: el default `human` reconstruye el mismo comportamiento.

---

## 6. Grafo (delta)

```
hitl → fix_dispatch → publish → fix_wait → publish_fix → notify_fix → END
                                   └── (sin pendientes) ──► END
```

| Nodo | Responsabilidad | Efectos |
|---|---|---|
| `fix_dispatch` | Sustituye al actual `fix`. Por cada candidato (`APPROVED` + `fix_*`): gate `BLOCKED`, leer fichero @`head_sha`, `FixerPort.propose`, validación local. Deja `PENDING` (con el `VerifyJob`) los verificables y **terminal** los `BLOCKED`/`SKIPPED`/`INVALID_PATCH`/`NOT_VERIFIABLE`. No-op sin candidatos | VCS (leer fichero) |
| `publish` | Sin cambios de orden ni de marcas. Un `FixResult` `PENDING` se renderiza como "_Fix en verificación (runner); se publicará como respuesta_" | VCS (comentarios) |
| `fix_wait` | Si hay `PENDING`, `interrupt(FixWaitRequest(...))` en un bucle (mismo patrón que `hitl`). Al reanudar aplica las `FixResolution`: `passed`→`VERIFIED`, `error`→`NOT_VERIFIABLE`, `failed`→ reintenta (propose con `stderr_tail`) si quedan intentos, si no `FAILED`. Si quedan `PENDING`, **vuelve a pausar** (nueva wave) | ninguno (puro) |
| `publish_fix` | Publica el resultado terminal como comentario de seguimiento (dedupe por fix). No-op si nada terminal | VCS (comentario) |
| `notify_fix` | Aviso Teams opcional del resultado del fix | Teams |

El worker de reviews, al detectar una pausa `FIX`, **registra el inbox y encola la
wave** en la cola de fixes (mismo punto que hoy hace `_on_paused` para el HITL).

### 6.1 El bucle de reintento cruza la frontera

Hoy el 2º intento vive dentro de `_fix_one` (`while attempts < max_attempts`). Con
la frontera propose/verify partida, el bucle se reparte:

- `fix_wait` recibe `failed` con `checks` (el `stderr_tail` real viaja en
  `FixResolution.checks`) y **vuelve a proponer** con `previous=FixResult(...)`,
  igual que hoy, pero ahora la pausa ocurre entre intentos.
- Un `failed` en el último intento es terminal `FAILED`; un `invalid_patch` en
  cualquier intento es terminal `INVALID_PATCH` (no se gasta sandbox de nuevo).

Esto mantiene el fixer (LLM) y la validación en el worker de reviews (donde vive el
`VcsPort` y el presupuesto de tokens) y el sandbox en el worker de fixes.

---

## 7. Worker de fixes

Proceso aparte (`python -m agent_review.fixworker.main`), **sin** grafo, **sin**
checkpointer y **sin** `VcsPort`. Su ciclo:

```
dequeue(job: FixPause)                 # una wave
  for each verify_job in job.jobs:
      report = sandbox.verify(VerifyRequest(run_key, repo, head_sha, patch, recipes, targets))
      resolution = FixResolution(run_key, "passed"/"failed", checks, image_digest)
  # error permanente del runner (SandboxError 4xx) -> outcome="error" con la razón
  queue.enqueue(review_request.model_copy(update={"fix_resolution": FixResolutions(items)}))
  ack(job)
```

- **Idempotencia:** `run_key` en el set `reviews:fixes:done` (TTL) para no repetir
  el runner; el runner además cachea por `run_key`. Un `dequeue` repetido tras una
  caída no lanza una segunda ejecución (R8).
- **Resume seguro:** no lo hace este worker. El worker de reviews solo reanuda si
  el hilo está pausado con un `fix_wait` y la wave pendiente contiene los `run_key`
  de la resolución; si no, **no-op** (evita aplicar una resolución a un hilo que ya
  avanzó).
- **Tope de espera:** si el worker de fixes no consume (apagado, runner caído), el
  barrido del worker de reviews reanuda con `outcome="error"` al vencer
  `FIX_VERIFY_TTL_S` → el fix queda `NOT_VERIFIABLE`, nunca colgado.
- **Concurrencia 1** en v1 (como el runner). Paralelizar la wave es un cambio
  posterior sin tocar el contrato.

### 7.1 Permisos (frontera de seguridad)

El worker de fixes solo necesita `REDIS_URL`, `SANDBOX_RUNNER_URL` +
`SANDBOX_RUNNER_TOKEN` y, para el timeout, `SANDBOX_TIMEOUT_S` (o el perfil de solo
lectura). **No** monta `docker.sock`, **no** tiene `TWG_*` ni el token de decisión y
**no** habla con Postgres. El runner sigue siendo el único que toca Docker (rootless)
y el único con `SANDBOX_GIT_TOKEN`.

---

## 8. Inbox, TTL y poda (desambiguación)

Todo lo que hoy asume "pausa = decisión humana" pasa a ser **kind-aware**. Puntos
exactos a tocar (verificados en el código actual):

| Punto | Hoy | Cambio |
|---|---|---|
| `graph.build_approval` | Construye `ApprovalRequest` | Nuevo `build_pause(state) -> ApprovalRequest \| FixWaitRequest \| None`; `build_approval` se queda para el HITL |
| `worker._on_paused` | Registra `approval` + aviso Teams | Ramifica por kind: `human` → igual; `fix` → registra `FixWaitRequest` **sin** aviso de decisión, y encola la wave |
| `worker.process_request` | `if snapshot.interrupts:` → `_resume_paused` | Despacha por `pause_kind(payload)`: `human` → actual; `fix` → resume con `request.fix_resolution` o no-op |
| `worker.sweep_expired` | Reanuda con `HitlDecision(expired)` | Solo pausas `human`. Nuevo `sweep_fix_expired` → reanuda con `FixResolutions(outcome="error")` al vencer `FIX_VERIFY_TTL_S` |
| `worker.reconcile_paused` | Repone pausas perdidas | Repone también las `fix` y **re-encola la wave** (idempotente) |
| `checkpointer.paused_thread_ids` | Lista hilos con `__interrupt__` | Añade `pause_kind` por payload, para que la poda y el barrido distingan |
| `checkpointer._is_completed` | `"__interrupt__" not in keys` | Igual (una pausa de fix no está completada) |
| `api./reviews/{review_key}` | Devuelve `PAUSED` + pending | Añade `kind`; para `fix` expone `state: "VERIFYING"` y los `run_key` |

### 8.1 Poda de pausas de fix

Un hilo pausado en `fix_wait` tiene `__interrupt__` y hoy quedaría **protegido para
siempre** (`CompletedOnlyRetention` no poda pausados). Se añade una política
`FixWaitRetention` (Strategy, reusa `RetentionPolicy` de T10) que **sí** poda un
hilo pausado en `fix_wait` cuando superó `FIX_WAIT_MAX_AGE_S` y su pausa ya expiró
(la review quedó `NOT_VERIFIABLE` y publicada): la dedupe vive en las marcas del PR,
así que podar no puede duplicar comentarios. Las pausas `human` siguen intocables.

---

## 9. Publicación

- **Hallazgos (nodo `publish`):** sin cambios, salvo que un `FixResult` `PENDING` se
  rotula "en verificación".
- **Fix resuelto (nodo `publish_fix`):** un comentario general de seguimiento con
  **marca de dedupe propia** (`<!-- <token>:fix -->`, legible por
  `list_review_markers`). Un `VERIFIED` lleva el bloque del `DeliveryFormatter`
  (E6) y la línea "verificado: …"; un `FAILED`/`INVALID_PATCH`/`NOT_VERIFIABLE`
  lleva el check que falló y su `stderr_tail` acotado. No se re-publica.
- **Resumen:** gana el estado final del fix (`_fix_reason_line` ya lo contempla;
  hay que dar entrada a `PENDING` en `_render_fix` para que no caiga en la rama de
  "no verificado").

---

## 10. Seguridad (delta sobre el spec de Fase 3 §7)

| Superficie | Control |
|---|---|
| Worker de fixes comprometido | Solo tiene Redis + token del runner; no tiene token de escritura del PR, ni `TWG_*`, ni Postgres, ni `docker.sock` |
| Worker de reviews | Sigue sin `docker.sock` y **deja de necesitar** `SANDBOX_RUNNER_URL` (nunca llama al runner) |
| Runner | Sin cambios: único punto que ejecuta código del PR, rootless, red cortada en checks |
| Prompt injection | Igual: el fixer solo produce un diff validado localmente; el sandbox no recibe credenciales de escritura |

---

## 11. Riesgos

| # | Riesgo | Mitigación | Estado |
|---|---|---|---|
| R1 | Un hilo pausado en `fix_wait` queda protegido y no se poda | `FixWaitRetention` (§8.1) | Se cubre aquí |
| R2 | Reanudar con una resolución que el hilo ya no espera | Check de `run_key` en la wave pendiente antes del resume; si no, no-op (§7) | Se cubre aquí |
| R3 | El worker de fixes cae entre el resume y el ack → doble resume | El hilo ya avanzó → el resume es no-op; `run_key` en el set evita repetir el runner | Se cubre aquí |
| R4 | Worker de fixes apagado / runner caído: fixes colgados | `FIX_VERIFY_TTL_S` → `NOT_VERIFIABLE` y publicación | Se cubre aquí |
| R5 | Dos writers por un resume humano y una resolución a la vez | Un hilo tiene **una** pausa; no puede haber `human` y `fix` simultáneos en la misma review | Por diseño |
| R6 | Cambio de forma de `PausedReview` rompe entradas de Redis en vuelo | Campo `kind` con default `human`; lectura tolerante (§5.3) | Se cubre aquí |
| R7 | Pérdida de cobertura de test en `tests/test_*` de pausa por el `kind` | Los tests de HITL no cambian; se añaden los de `fix` | Se cubre aquí |
| R8 | Latencia por wave: 3 fixes esperan al más lento | v1 secuencial por decisión (v1 ya asumía `max_fixes_per_pr: 3`); paralelizar la wave es posterior | Aceptado |

---

## 12. Criterios de hecho

1. **El worker de reviews no bloquea**: con una verificación de fix "en curso"
   (fake de sandbox que nunca responde), el worker procesa y publica **otras**
   reviews sin esperarla. Verificable: la ruta de la review no contiene ninguna
   llamada a `SandboxPort.verify`.
2. **La verificación se despacha**: un hallazgo aprobado con `fix_*` produce un
   `FixResult` `PENDING` y una wave en la cola de fixes **antes** de que termine la
   verificación; los hallazgos ya están publicados.
3. **Un único escritor**: la sugerencia se publica en `publish_fix` **una sola vez**
   (reentrega = no-op por marca `:fix`); el worker de fixes no publica nada.
4. **Reintento con `stderr_tail`**: un `failed` re-pausa y re-propone con el log del
   intento anterior; al agotar `max_fix_attempts` → `FAILED` terminal + publicación
   del fallo.
5. **TTL**: una wave sin resolver vence por `FIX_VERIFY_TTL_S` y el fix queda
   `NOT_VERIFIABLE`, publicado; nunca colgado.
6. **Idempotencia**: una reentrega del job de fix tras una caída no lanza una
   segunda verificación (`run_key`) ni aplica dos veces la resolución.
7. **Contrato intacto**: `VerifyRequest`/`VerifyReport` y el servicio
   `sandbox_runner` no cambian; `POST /verify` sigue siendo bloqueante e idempotente.
8. **Cero escrituras en el repo**: igual que Fase 3, sin `commit`/`push`/`branch`.
9. **Smoke real** (PR de `emotivoo-v2`): hallazgos publicados y, ~20 min después, la
   sugerencia verificada como comentario de seguimiento. Depende de **P6**.

---

## 13. Preguntas abiertas

- **Q1 — forma de la wave.** v1: una pausa por wave (todos los candidatos), el
  worker de fixes los verifica secuencialmente y resume de una vez. Alternativa:
  una pausa por hallazgo (mejor latencia del primer resultado, más pausas). Se
  decide con la primera medición real.
- **Q2 — comentario de seguimiento vs respuesta al hilo.** `VcsPort` no expone hoy
  editar/responder un comentario. Si el spike R1 (`F11`) confirma que Bitbucket
  permite responder al hilo del inline, la sugerencia podría ir ahí en vez de un
  comentario general. No bloquea.
- **Q3 — aviso de fix por Teams.** ¿`notify_fix` propio o se funde con el resumen?
  v1: un aviso compacto en `notify_fix`.

---

## 14. Patrones usados (y por qué encajan)

- **Handoff durable (Workflow/Choreography) sobre la cola + estado durable.** El
  mismo que ya usa el HITL: el `interrupt` es el estado de espera y la cola es el
  transporte. Reutilizarlo evita un tercer mecanismo de espera.
- **Ports & Adapters.** `SandboxPort` no cambia; cambia el proceso que lo usa. La
  frontera proceso↔proceso es la cola, detrás de un `FixQueue` (mismo contrato
  que `ReviewQueue`).
- **Strategy.** `FixWaitRetention` (poda) y `DeliveryFormatter` (formato) ya siguen
  el patrón; el segundo se reutiliza tal cual.
- **Idempotency key.** `run_key` hace segura la reentrega en las dos fronteras
  (cola de fixes y runner).

---

## 15. Estructura

```
src/agent_review/fixworker/main.py      # worker de fixes (cola → sandbox → resume)
src/agent_review/fixworker/__init__.py
src/agent_review/queue.py               # + FixQueue (reviews:fixes + DLQ)
src/agent_review/domain/models.py       # VerifyJob, FixWaitRequest, FixResolution(es), FixStatus.PENDING
src/agent_review/domain/paused.py       # PausedReview kind-aware
src/agent_review/graph/build.py         # fix_dispatch, fix_wait, publish_fix, notify_fix
src/agent_review/fix/node.py            # dispatch (propose+validate) y apply(resolution)
src/agent_review/worker/main.py         # _on_paused/_resume/sweep/reconcile kind-aware
src/agent_review/checkpointer.py        # pause_kind + FixWaitRetention + serde
src/agent_review/settings.py            # FIXES_*, FIX_VERIFY_TTL_S
docker-compose.yml                      # servicio fix-worker
```

Tests: `tests/test_fix_worker.py` (nuevo), `tests/test_fix_node.py`,
`tests/test_fix_render.py`, `tests/test_fix_e2e.py`, `tests/test_worker.py`,
`tests/test_paused.py`, `tests/test_checkpointer.py`, `tests/test_queue.py`,
`tests/test_compose.py`.

---

## 16. Comandos

```
make ci                                    # ruff + formato + mypy strict + pytest
uv run --extra dev pytest tests/test_fix*.py tests/test_worker.py tests/test_paused.py
docker compose up -d fix-worker            # nuevo servicio
```
