Última modificación: 2026-10-11 | Creado: 2026-10-11

# Todo: Fase 1 (cerrada) · Fase 2 (especificada)

Checklist rápida. El detalle y las tareas viven en `tasks/plan.md` y en
`docs/fase2-hitl-20261011.md`.

## Fase 1 — Report-only (cerrado)

## Fase 1 — Report-only (cerrado)

### Código (cerrado)

- [x] **T1–T10** entregadas (clamp report, contrato, cola, worker, adaptador vCS,
      personas estática y LLM, publish inline+resumen+Teams, smoke, ingesta diferida)
- [x] Robustez: reintentos + DLQ, healthchecks + restart, latido del worker
- [x] Dedupe contra lo publicado (marca por hallazgo y por resumen)
- [x] Scope sin ruido + `max_files_per_pr`
- [x] Arranque reproducible (`scripts/bootstrap-env.sh` + validación de `twg`/secretos)
- [x] CI propio (`.github/workflows/ci.yml`) + `make ci`: `ruff` lint y formato,
      `mypy` strict, 182 tests en verde

### Pendiente (humano / externo, no es código)

- [ ] **P1** (opcional) El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado —
      sin él se pierde la anotación inline de un pipeline rojo, no la Fase 1
- [ ] **P2** Nivel 2 del smoke contra Bitbucket real — ✅ ejecutado con el PR 430
      (`docs/smoke-fase1-20261011.md` §5)
- [ ] **P3** Formato inline revisado por humano (`docs/decisiones-publish-inline-20261011.md`)
- [x] **P4** Decisión go/no-go para Fase 2 → **go** (2026-10-11)
- [ ] **P5** Medir la latencia real de una review (la cola es secuencial) — medición
      orientativa hecha (~15 s); falta carga real
- [ ] **P6** Revisar la heurística de tema protegido con seguridad (Fase 3)

### Preguntas abiertas

- [ ] ¿PR real de emotivoo-v2 o solo fixtures para el smoke? (P2)
- [ ] ¿Webhook de Teams listo para el smoke? (P2)
- [x] ¿Clamp en `autonomy.yaml` o solo en settings? → **YAML, con override que solo baja**
- [x] ¿El CI publica el artefacto en el agente? → **Puente desechable; opcional** (`docs/ingesta-diferida-ci-20261011.md`)
- [x] ¿`max_inline_comments`? → **20**

---

## Fase 2 (HITL) — tareas

Especificación completa en `docs/fase2-hitl-20261011.md`. Orden por dependencia;
checkpoints tras T4 y tras T9. Estado: **T1–T10 cerradas** (228 tests en verde).

### Fundación

- [x] **T1** — Blindar el clamp (`_AUTONOMY_RANK` unico; `escalate` no revienta)
  - Verifica: `tests/test_policy.py` ✅
- [x] **T2** — Contrato HITL (`HitlDecision`/`ApprovalRequest`/`PendingDecision`,
    `ReviewRequest.decision`, `ReviewOutcome.human_decisions`/`paused`) ✅
- [x] **T3** — Inbox de pausas (`PausedReview`/`PausedStore`) ✅
- [x] **T4** — Nodo `hitl` con `interrupt` + `publish` consciente de la decisión ✅
  - Nota: los modelos HITL viven en `domain/models.py` (no en `hitl.py`) para no
    crear un ciclo de imports con `ReviewRequest`.

### Checkpoint: tras T1–T4

- [x] El grafo pausa y el hilo queda durable con un `interrupt` pendiente
      (`tests/test_graph.py::test_hitl_*`). 195 tests en verde.

### Reanudación y canal (T5 ✅)

- [x] **T5** — Worker: estado pausado, resume con `Command` y no-op
  - Aceptación: reentrega estando pausada **no re-ejecuta** `scope`/personas;
    resume aplica `Command` sobre el mismo hilo; doble decisión es no-op.
  - Verifica: `tests/test_worker.py`, `tests/test_checkpointer.py`. ✅
    (`test_worker_reentrega_pausada_no_reejecuta_*`, `test_worker_resume_*`,
    `test_worker_doble_decision_es_noop`, `test_hilo_pausado_serializa_sin_avisos`)
  - Ficheros: `src/reviews/worker/main.py`, `src/reviews/graph/build.py`,
    `src/reviews/checkpointer.py` (allowlist del serde con los tipos HITL).

- [x] **T6** — Canal CLI + `POST /reviews/{key}/decision`
  - Aceptación: CLI construye y encola la decisión con `actor`; el endpoint
    valida contra el inbox (`409` si no pausada o `finding_id` ajeno). ✅
    (`tests/test_cli.py`, `tests/test_api.py::test_decision_*`)
  - Ficheros: `src/reviews/cli.py`, `src/reviews/api/main.py`, `src/reviews/settings.py`,
    `pyproject.toml`.
  - Nota: el endpoint usa `{review_key:path}` porque la clave lleva `/`
    (`cqm/emotivoo-v2:42:sha:7`); `REVIEWS_DECISION_TOKEN` entra en el contrato de
    secretos de producción. La validación compartida vive en `reviews/decision.py`.

- [x] **T7** — Notificación de pausa + expiración
  - Aceptación: aviso por `notify_decision_pending` + comentario de PR **sin** pie
    de dedupe; `DECISION_TTL_S` reencola `EXPIRED`. ✅
    (`test_pausa_escribe_el_inbox_y_avisa_sin_publicar`,
    `test_aviso_de_pausa_sin_marca_de_dedupe`, `test_sweep_expired_reencola_sin_decision`,
    `test_resume_borra_la_pausa_del_inbox`)
  - Ficheros: `src/reviews/worker/main.py`, `src/reviews/graph/build.py`.
  - Nota: el aviso va por Teams; el comentario de PR sin marca queda disponible vía
    `render_pause_notice` (el endpoint de PR de Fase 2 difiere su uso). El barrido de
    vencidas vive en `worker.sweep_expired` y se integra en `run_forever`.

- [x] **T8** — Poda de checkpoints + estado en la API
  - Aceptación: se podan hilos **completados** por retención; un hilo pausado
    **nunca** se poda; `/pending` (o `/reviews/{key}`) muestra `PAUSED`. ✅
    (`test_prune_poda_completados_y_respeta_pausados`,
    `test_prune_nunca_poda_un_pausado_aunque_no_se_le_pase`,
    `test_estado_de_review_pausada`)
  - Verifica: `tests/test_checkpointer.py`, `tests/test_api.py`. ✅
  - Ficheros: `src/reviews/checkpointer.py`, `src/reviews/api/main.py`,
    `src/reviews/worker/main.py`, `src/reviews/settings.py`.
  - Nota: la API no habla con el checkpointer; `/reviews/{key}` responde `PAUSED`
    desde el inbox (404 si no consta pausada).

- [x] **T9** — E2E de pausa → decisión → reanudación → publish
  - Aceptación: inline + resumen sin duplicar; marcas de dedupe correctas. ✅
    (`tests/test_hitl_e2e.py`: artefacto → pausa → `reviews decide` → resume → publish,
    sin `twg` ni red)
  - Verifica: `tests/test_hitl_e2e.py`. ✅
  - Ficheros: `tests/test_hitl_e2e.py`.

### Checkpoint: tras T5–T9

- [x] Decidir y reanudar funciona de punta a punta; `make ci` en verde (227 tests).

### Deuda nueva (post-Fase 2)

        - [x] **T10** — Seam de retención: `RetentionPolicy` (Strategy) para desacoplar del saver ✅
  - Nota: `RetentionPolicy` (Protocol) + `CompletedOnlyRetention` (política por
    defecto) y `prune_threads` como motor público; `prune_completed_threads` queda
    como atajo. `list`/`delete_thread` quedan confinadas en el motor. Comportamiento
    observable idéntico (T8 intacto); test nuevo `test_prune_threads_aplica_la_politica_inyectada`.
  - Aceptación: la decisión «¿este hilo se poda?» vive en una `RetentionPolicy`
    (Protocol + `CompletedOnlyRetention`), testeable sin saver ni Postgres; su lógica
    es la actual (`_is_completed` + cutoff + exclusión de pausados) y **no cambia el
    comportamiento observable**. Las dos llamadas frágiles de LangGraph (`list`,
    `delete_thread`) quedan confinadas en un único adaptador, de modo que si el
    paquete implementa `prune` se añade un adaptador que **reusa la misma política**
    sin tocar el motor.
  - Motivo: los cuatro backends oficiales declaran `copy_thread`/`delete_for_runs`/
    `prune` como `NotImplementedError` (verificado en los wheels); la frontera que
    tocamos es volátil y hoy está soldada al saver. Es un **seam** (aislar lo que
    puede cambiar), **no** el `CheckpointerPort` que retiró el spec §4.9: sigue sin
    haber un segundo cliente.
  - Verifica: `tests/test_checkpointer.py` ✅ (los tres tests de T8 siguen verdes).
  - Ficheros: `src/reviews/checkpointer.py`, `tests/test_checkpointer.py`.

### Resiliencia de nodo (deuda cerrada)

- [x] **Reintento de nodo (`RetryPolicy`)** — los nodos con efectos externos
  (`scope`, `persona`, `publish`) reintentan fallos transitorios **dentro** de la
  invocación, sin sacar el job de la cola. Clasificación propia en
  `reviews/errors.py` (`is_transient`), porque el `default_retry_on` de LangGraph
  **no** reintenta `RuntimeError` (nuestros adaptadores) ni `429` ni `TimeoutError`.
  - Verifica: `tests/test_retry.py` (9 tests) ✅
  - Ficheros: `src/reviews/errors.py`, `src/reviews/graph/build.py`,
    `src/reviews/adapters/twg_vcs.py`, `src/reviews/ports/llm.py`,
    `src/reviews/worker/main.py`, `src/reviews/settings.py`.
  - Detalle: `docs/reintento-de-nodo-20261011.md`.
- [x] **`twg` sin `timeout` (B8 de la revisión)** — `RetryPolicy` no puede con un
  proceso colgado (nunca lanza); `add_node(timeout=...)` solo aplica a nodos async,
  así que el techo vive en el `subprocess` y el `TimeoutExpired` se convierte en
  `TwgError` (transitorio). ✅

### Persistencia e interrupciones (revisión 2026-10-11)

- [x] **Decisión completa obligatoria** — `build_decision` rechaza una decisión que
  deje pendientes sin responder (CLI exit 2, API 409); la pista del aviso nombra
  todos los `finding_id`. Defensa en el nodo `hitl`: ante una decisión incompleta
  **vuelve a pausar** (bucle de `interrupt`), no lanza. ✅
  - Motivo: LangGraph guarda el valor del resume en el checkpoint antes de ejecutar
    el nodo; si el nodo lanzara, ese valor se re-entregaría en cada reanudación y el
    hilo quedaría atascado para siempre.
- [x] **`_is_completed` sin falsos completados** — un `__resume__` junto al
  `__interrupt__` (resume fallido o re-pausado) ya no cuenta como terminado: antes
  ese hilo se podaba y la review quedaba indecidible. ✅
- [x] **Pausas huérfanas** — hilo pausado sin entrada en el inbox (caída del worker
  entre el checkpoint y `_on_paused`): la reentrega sin decisión la repone y
  `reconcile_paused` (ciclo de poda) repone las demás, con `created_at` del
  checkpoint para que el TTL cuente desde la pausa real. ✅
- [x] **Checkpointer de producción** — con `ENVIRONMENT=production` no cae a memoria
  (falla el arranque); `ConnectionPool` con `check` y fallo rápido (10 s); un fallo
  al leer el hilo sube y reencola en vez de re-ejecutar desde `START`. ✅
- [x] **Enlace del PR en el aviso de pausa** — `_pr_url` leía un `vcs` que nunca
  estuvo en el estado; ahora el worker pasa el `VcsPort` (`build_vcs`). ✅
- [x] **Decisión tardía visible** — warning con el actor cuando su decisión llega a
  un hilo completado o inexistente (carrera con el `expired` del TTL). ✅
  - Verifica: `tests/test_hitl_persistence.py` (13 tests); suite: 266 en verde.
  - Ficheros: `src/reviews/decision.py`, `src/reviews/graph/build.py`,
    `src/reviews/checkpointer.py`, `src/reviews/worker/main.py`,
    `src/reviews/paused_store.py`, `src/reviews/settings.py`.
- [ ] **Smoke del checkpointer contra Postgres real** — el pool y el fallo de
  arranque en producción solo están probados sin base (no había Postgres local).

### Riesgo asumido (documentado, no una tarea)

- La decisión humana manda también sobre `never_auto_paths`/`protected_subject`, y se
  ratificó **sin segunda señal para Fase 3**: un actor podrá autorizar escritura en
  rutas nunca-auto cuando exista el ejecutor de fixes. Ver spec §2.1; se revisa si la
  Fase 3 detecta uso indebido.
