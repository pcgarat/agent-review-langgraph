Última modificación: 2026-10-16 | Creado: 2026-10-11

# Todo: Fase 3-E7 (desacople fuerte de la verificación)

Checklist del handoff durable. El diseño y las decisiones están en
`docs/iniciativas/004-fase3-e7-desacople/spec.md`; el estado del conjunto en `docs/iniciativas/README.md`.
Orden por dependencia. Spec aprobada el 2026-10-11; F14–F15 hechas.

Decisión de arranque (2026-10-11): handoff por `interrupt`; un único escritor en el
PR (el worker de fixes no publica); un único invocador del grafo (el worker de fixes
encola la resolución, reanuda el worker de reviews); el reintento con `stderr_tail`
entra en esta fase.

## Bloque 1 — Contrato y grafo (sin sandbox, con fakes)

- [x] **F14** Contrato del handoff
  - `FixStatus.PENDING`; `VerifyJob`, `FixWaitRequest` (`kind="fix"`),
    `FixResolution`/`FixResolutions`; `ApprovalRequest.kind="human"`;
    `ReviewRequest.fix_resolution`; serde (`_STATE_TYPES`) con los tipos nuevos.
  - Aceptación: `ReviewRequest` sin `fix_resolution` es exactamente el de hoy
    (compatibilidad); una `FixResolutions` round-trip por el checkpointer.
  - Verifica: `tests/test_fix_domain.py`, `tests/test_checkpointer.py`. ✅
  - Ficheros: `src/agent_review/domain/models.py`, `src/agent_review/checkpointer.py`.

- [x] **F15** `PausedReview` kind-aware
  - `PauseKind` (`human`/`fix`); `approval: ApprovalRequest | FixWaitRequest`
    elegido por `kind`; default `human` para entradas sin campo. Se adelantó al
    hacer F14 (mypy strict lo exige en cuanto `approval` deja de ser un tipo fijo).
    Incluye `human_request`/`fix_request`, `PausedStore.pause(..., kind=...)` y el
    narrow por `kind` en el canal (API/CLI/decision) y en `sweep_expired`.
  - Aceptación: una entrada de Redis de Fase 2 (sin `kind`) se lee como `human` y
    se comporta igual que hoy; una pausa `fix` se distingue sin ambigüedad. ✅
  - Verifica: `tests/test_paused.py` (incluye un JSON legado como fixture). ✅
  - Ficheros: `src/agent_review/domain/paused.py`, `src/agent_review/paused_store.py`,
    `src/agent_review/api/main.py`, `src/agent_review/cli.py`, `src/agent_review/decision.py`.

- [x] **F16** Nodo `fix_dispatch`, `fix_wait`, `publish_fix`, `notify_fix`
  - `fix_dispatch` sustituye al `fix` actual: hace gate `BLOCKED`, lectura del
    fichero, `propose` y validación; deja `PENDING` (con el `VerifyJob`) o terminal.
    `fix_wait` pausa con `FixWaitRequest` y al reanudar aplica `FixResolutions` y
    re-pausa si quedan `PENDING`. `publish_fix` publica lo terminal. Grafo:
    `... → fix_dispatch → publish → fix_wait → publish_fix → notify_fix → END`.
  - Aceptación: sin candidatos, no-op (grafo de Fase 2 idéntico); un candidato
    verificable acaba `PENDING` **sin** llamar al sandbox; `invalid_patch` es
    terminal sin gastar sandbox; `failed` con intentos re-propone.
  - Verifica: `tests/test_fix_node.py`, `tests/test_graph.py`. ✅
    `fix_wait` se corrigió para aplicar la wave devuelta por `interrupt` en la
    misma reanudación (el patrón previo la guardaba y volvía a pausar sin aplicar);
    cubierto por `tests/test_fix_e2e.py` (verificado / runner caído / reintento).
  - Ficheros: `src/agent_review/graph/build.py`, `src/agent_review/fix/node.py`,
    `src/agent_review/fix/selection.py`.

- [x] **F17** Worker de reviews kind-aware
  - `process_request` despacha la reanudación por tipo de pausa; `_on_paused`
    encola la wave cuando la pausa es de fix; `reconcile_paused` repone y re-encola
    (idempotente); `sweep_expired` solo actúa sobre pausas `human`.
  - Aceptación: la ruta de la review **no** contiene `SandboxPort.verify`; una
    reentrega estando pausado por fix no re-ejecuta `scope`/personas; reponer una
    pausa de fix re-encola la wave una vez.
  - Verifica: `tests/test_worker.py`, `tests/test_hitl_persistence.py`. ✅
    `_finish_resume` registra la nueva pausa cuando un resume vuelve a pausar
    (humana→fix) o limpia el inbox; `_on_paused` es idempotente (no re-registra ni
    re-avisa una pausa idéntica, para no resetear el TTL).
  - Ficheros: `src/agent_review/worker/main.py`.

- [x] **F18** `FixQueue`
  - Cola `reviews:fixes` + DLQ `reviews:fixes:dead`, mismo contrato que
    `ReviewQueue` (en memoria para tests). Late el `FixJob` (la wave + el
    `ReviewRequest` para reanudar). `done` set `reviews:fixes:done` por `run_key`.
  - Aceptación: encolar/dequeue/DLQ simétricos a la cola de reviews; un job de fix
    no se mezcla con la cola de reviews.
  - Verifica: `tests/test_queue.py`. ✅
  - Ficheros: `src/agent_review/queue.py`, `src/agent_review/domain/models.py`.

### Checkpoint: tras F14–F18

- [x] `make ci` en verde; despacho y pausa cubiertos con fakes; la ruta de la
      review no llama al sandbox. ✅ (426 tests; `build_sandbox` queda sin uso en
      el worker de reviews hasta que F19 lo mueva al fix-worker).

## Bloque 2 — Worker de fixes y runner

- [x] **F19** Servicio `fix-worker`
  - `python -m agent_review.fixworker.main`: `dequeue` → `SandboxPort.verify` por
    job → `FixResolutions` → encola el `ReviewRequest` con `fix_resolution`.
    Ajustes `FIXES_*`, `FIX_VERIFY_TTL_S`; servicio en `docker-compose.yml` (sin
    `docker.sock`, sin `TWG_*`, sin Postgres, sin token de decisión).
  - Aceptación: el fix-worker solo tiene Redis + token del runner; no publica nada;
    el compose no le da puertos ni socket.
  - Verifica: `tests/test_fix_worker.py`, `tests/test_compose.py`.
  - Ficheros: `src/agent_review/fixworker/main.py`,
    `src/agent_review/fixworker/__init__.py`, `src/agent_review/settings.py`,
    `docker-compose.yml`.

- [x] **F20** Idempotencia y durabilidad del handoff
  - `run_key` en el set `reviews:fixes:done` (TTL); el worker de reviews reanuda
    solo si el hilo está en `fix_wait` y la wave pendiente contiene el `run_key`;
    si no, no-op. `sweep_fix_expired` reanuda con `outcome="error"` al vencer
    `FIX_VERIFY_TTL_S`.
  - Aceptación: reentrega tras una caída no relanza la verificación ni aplica dos
    veces la resolución; una wave sin resolver vence → `NOT_VERIFIABLE` publicado.
  - Verifica: `tests/test_fix_worker.py`, `tests/test_worker.py`.
  - Ficheros: `src/agent_review/fixworker/main.py`, `src/agent_review/worker/main.py`.

- [x] **F21** Poda de pausas de fix
  - `FixWaitRetention` (Strategy, reusa `RetentionPolicy` de T10) poda un hilo en
    `fix_wait` cuando la wave expiró y la review quedó publicada; las pausas
    `human` siguen intocables. `paused_thread_ids` expone el `kind`.
  - Aceptación: un hilo de fix vencido se poda; uno `human` nunca.
  - Verifica: `tests/test_checkpointer.py`. ✅
    `pause_kind_of` lee el `kind` tanto del `Interrupt` (`item.value.kind`) como
    del payload crudo en dict; `prune_fix_threads` lo aplica sobre el listado ya
    hecho en el bucle del worker. Ajuste nuevo `FIX_WAIT_MAX_AGE_S` (3 días).
  - Ficheros: `src/agent_review/checkpointer.py`, `src/agent_review/worker/main.py`.

### Checkpoint: tras F19–F21

- [x] `fix-worker` levanta en compose, verifica con el fake de runner y el worker
      de reviews reanuda y aplica la resolución; la poda distingue `human`/`fix`.
      ✅ (F19–F21 verdes; `tests/test_compose.py`, `tests/test_fix_worker.py`,
      `tests/test_checkpointer.py`).

## Bloque 3 — Publicación, pruebas y cierre

- [x] **F22** Render y dedupe del fix
  - `publish_fix` emite un comentario de seguimiento con marca propia `:fix`; un
    `PENDING` en `publish` se rotula "en verificación"; `_render_fix` da entrada a
    `PENDING` (no cae en "no verificado") y el resumen refleja el estado final.
  - Aceptación: reentrega = no-op (una sola sugerencia); `FAILED` lleva el
    `stderr_tail` acotado; `VERIFIED` usa el `DeliveryFormatter`.
  - Verifica: `tests/test_fix_render.py`. ✅
    Cubre el rotulo del `PENDING` en inline, el resumen ("en verificacion"), la
    marca `:fix` unica en el seguimiento y el acotado del log a `_MAX_TAIL_CHARS`.
  - Ficheros: `src/agent_review/graph/build.py`.

- [x] **F23** E2E del handoff con fakes
  - artefacto → HITL → aprobación → `fix_dispatch` → `publish` (hallazgos) →
    `fix_wait` → wave (fake de sandbox) → `publish_fix` (sugerencia verificada);
    caso runner caído → TTL → `NOT_VERIFIABLE`; caso `failed` → re-pausa →
    re-propuesta → `FAILED`. Sin red ni Docker.
  - Aceptación: todos los caminos sin duplicar comentarios ni sugerencias.
  - Verifica: `tests/test_fix_e2e.py`. ✅
    Los tres caminos (verificado / runner caído / reintento agotado) comprueban
    un unico inline, un unico seguimiento `:fix` y la re-pausa antes de agotar.
  - Ficheros: `tests/test_fix_e2e.py`.

- [x] **F24** Docs y estado
  - README (sección E7 + diagrama del grafo), `docs/iniciativas/004-fase3-e7-desacople/spec.md`
    (estado: implementada), `docs/iniciativas/README.md` y `docs/iniciativas/003-fase3-fix-verificacion/todo.md` (E7 cerrado).
  - Aceptación: el README describe el handoff y el worker de fixes; el estado del
    plan refleja lo hecho.
  - Verifica: revisión manual. ✅
    README (diagramas de componentes y grafo, sección "Handoff del fix"),
    `docs/arquitectura.md` (§1–§6), `docs/operacion.md` (§1–§7), `CONTEXT.md`
    (términos E7) y `docs/desarrollo.md` (mapa de tests).
  - Ficheros: `README.md`, `docs/arquitectura.md`, `docs/operacion.md`,
    `CONTEXT.md`, `docs/desarrollo.md`, `docs/iniciativas/004-fase3-e7-desacople/spec.md`,
    `docs/iniciativas/README.md`, `docs/iniciativas/003-fase3-fix-verificacion/todo.md`.
  - Cierre (2026-10-16): corregida la referencia falsa de `plan.md` (el smoke real
    es el criterio 9, no F24), fechas de los tres documentos al día e índice de
    iniciativas marcado "F14–F24 cerradas". Cobertura de los criterios 1–8 del
    spec: 1–2 `tests/test_worker.py` + `tests/test_fix_e2e.py`; 3 `tests/test_fix_render.py`;
    4–6 `tests/test_fix_e2e.py` + `tests/test_fix_worker.py`; 7 `tests/test_http_sandbox.py`
    + `tests/test_sandbox_runner.py` (contrato intacto); 8 por diseño: `VcsPort` no
    expone `push`/`create_branch`/`commit`. El criterio 9 (smoke real) queda
    pendiente de **P6** y no bloquea el cierre documental.

### Checkpoint: fin

- [x] `make ci` en verde (475 tests); smoke real pendiente de **P6**.

## Precondiciones externas

- [ ] **P6** revisión de seguridad del tema protegido — precondición del smoke real
      (el runner ejecuta código de PRs de terceros), igual que en Fase 3.
- [ ] **Q1 del spec (Fase 3-E7)** forma de la wave (una por wave vs una por
      hallazgo) — se decide con la primera medición real; v1 = una por wave.

## Notas / límites conocidos

- El contrato del runner **no cambia** (`POST /verify` bloqueante e idempotente por
  `run_key`); lo que cambia es quién espera.
- La sugerencia se publica como **comentario de seguimiento** (general) porque
  `VcsPort` no expone responder/editar un comentario. Si el spike R1 (`F11`)
  confirma que Bitbucket permite responder el hilo, se reevalúa (Q2).
- v1 verifica la wave secuencialmente (como el runner); paralelizarla es posterior
  y no toca el contrato.
- Se evaluó y **descartó** que el worker de fixes reanudara el grafo (dos
  invocadores del mismo hilo); queda registrado en el spec §4.
