Última modificación: 2026-10-16 | Creado: 2026-10-11

# Todo: Fase 3 (fix + verificación)

Checklist de la Fase 3. El diseño y las decisiones están en
`docs/iniciativas/003-fase3-fix-verificacion/spec.md` (aprobado con enmiendas) y el estado del
conjunto en `docs/iniciativas/README.md`.

Decisiones de arranque (2026-10-11): base pura primero; runner rootless en esta
máquina (Q1); verificación **desacoplada** de la ruta de review (E7, enmienda a
R2); P6 recategorizada como precondición del runner (Q4).

## Bloque 1 — Base pura (sin Docker, sin red; testeable con `make ci`)

- [x] **F1** Contrato de fix
  - `FixStatus`, `ProposedPatch`, `FixResult`, `CommandResult` en el dominio;
    `ReviewState.fixes` con reducer por `finding_id`; `ReviewOutcome.fixes`.
  - Verifica: `tests/test_fix_domain.py` ✅
- [x] **F2** Validación local del parche (pura)
  - Un fichero = el del hallazgo; un hunk; ≤ `max_fix_lines`; rango dentro de las
    líneas añadidas del diff. Fallo → `INVALID_PATCH` sin tocar el runner.
  - Verifica: `tests/test_fix_validation.py` ✅
- [x] **F3** `FixerPort` + `LlmFixer`
  - El diff se **deriva** del fichero real al `head_sha` (`build_unified_diff`), no
    del que devuelva el modelo. 2º intento con `stderr_tail`. Presupuesto compartido.
  - Verifica: `tests/test_llm_fixer.py` ✅
- [x] **F4** Guards y selección de candidatos
  - `max_fixes_per_pr: 3`, `max_fix_lines: 30` en `autonomy.yaml`. Candidatos:
    `APPROVED` + `fix_*`; `escalate` nunca. **D3 invertida (2026-10-11):** un
    hallazgo en `never_auto_paths` o con `protected_subject` se marca `BLOCKED`
    y **no** gasta fixer ni sandbox, aunque una persona lo apruebe.
  - Verifica: `tests/test_fix_selection.py`, `tests/test_fix_node.py` ✅
- [x] **F5** Nodo `fix` + seam de entrega (E6)
  - `fix` entre `hitl` y `publish`; no-op sin candidatos. `SuggestionFormatter` /
    `DiffFormatter`, elegido por `FIX_DELIVERY` (por defecto `diff`).
  - Verifica: `tests/test_fix_node.py`, `tests/test_fix_e2e.py` ✅
- [x] **F5b** Verificación desacoplada (E7)
  - `SandboxPort.verify` es **síncrono** (el runner recibe `run_key` idempotente).
    El desacople en la cola es de F7; el nodo `fix` ya es un paso aislado y
    reintentable, así que la publicación de otras reviews no comparte su estado.
  - Nota: el desacople *fuerte* (otra cola/consumidor) llega con el runner (F7).
- [x] **F6** Render y dedupe por `finding_id`
  - Inline con el bloque del formatter + "Verificado: …"; fallo con `stderr_tail`
    acotado; sección "Fixes" en el resumen; marca de dedupe por hallazgo intacta.
  - Verifica: `tests/test_fix_render.py` ✅

**Checkpoint (2026-10-11):** `make ci` en verde (lint, formato, mypy strict, **333
tests**). El camino pausa → decisión → fix → sugerencia verificada está cubierto
end-to-end con fakes (`tests/test_fix_e2e.py`), sin Docker ni red. Incluye el gate
duro de seguridad (D3 invertida): `never_auto_paths`/`protected_subject` → `BLOCKED`.
Y el contrato del perfil (`tests/test_profile.py`): claves conocidas y `expected_tools`
presente, para que la barrera de ingesta no vuelva a romperse en silencio.

## Bloque 2 — Runner (depende de Q1 —resuelta— y de P6 —precondición—)

- [x] **F7** `VerifyRequest`/`VerifyReport`/`SandboxPort.verify` + `HttpSandboxAdapter`
  - **Contrato síncrono (2026-10-11):** `POST /verify` bloqueante con `run_key`
    idempotente. Se descarta `POST /runs` + sondeo `GET /runs/{id}` del spec §5.3:
    el worker es secuencial, así que el sondeo no desacopla nada y solo añade
    estado de runs. Ver §5.3 (enmienda).
  - `build_sandbox` conecta el adaptador (`sandbox_enabled` + `sandbox_runner_url`);
    sin ellos el nodo `fix` queda `NOT_VERIFIABLE`.
  - Verifica: `tests/test_http_sandbox.py` ✅
- [x] **F8** Servicio `sandbox_runner` (FastAPI interno)
  - Clon → `git apply --check`/`apply` → setup → checks → destruir en `finally`.
    Sin `docker.sock`, sin privilegios. Puertos inyectables (`ContainerRuntime`,
    `WorkspacePort`, `ProfileSource`) para testear sin Docker.
  - Verifica: `tests/test_sandbox_runner.py` (fake de Docker contando creations/destructions) ✅
- [x] **F9** `ci_image_digest`, `checks.targeted` en el runner, y validación de `{targets}`
  - Hecho: `checks.targeted = "bin/phpunit {targets}"` (Q2); `verify` en el perfil y
    `profile.fix_recipes` como fuente única del orden (targeted antes de typecheck);
    contrato del perfil validado al cargar (`validate_profile` + `tests/test_profile.py`).
  - Hecho: `ci_image_digest` (solo `sha256:`, tag mutable → `NOT_VERIFIABLE`) y
    validación/expansión de `{targets}` en el runner (`sandbox_runner/recipes.py`).
- [x] **F10** compose + settings + secretos (`SANDBOX_GIT_TOKEN`,
  `SANDBOX_REGISTRY_*` al runner, `SANDBOX_RUNNER_TOKEN`)
  - Hecho; verificado con `tests/test_compose.py` (worker sin socket, runner sin
    puertos, secretos declarados) y los fail-closed de `tests/test_api.py`.
  - Smoke real del runner pendiente de P6 (rootless en el host del runner).

## Bloque 3 — Cierre

- [ ] **F11** Spike R1 real: elegir el `DeliveryFormatter` según lo que renderice Bitbucket
- [ ] **F12** E2E con fakes + smoke real — la parte de **fakes** (handoff desacoplado)
  la cubre 004-F23 (`tests/test_fix_e2e.py`); queda el **smoke real** (P6)
- [ ] **F13** README + `docs/fase3-*.md` + `docs/iniciativas/README.md`

> El desacople fuerte de E7 (que este bloque no cubre) tiene su propio spec y su
> propio checklist: `docs/iniciativas/004-fase3-e7-desacople/spec.md` y `docs/iniciativas/004-fase3-e7-desacople/todo.md`
> (F14–F28).

## Precondiciones externas

- [ ] **P6** (Q4) revisar la heurística de tema protegido con seguridad — **precondición del runner**: el smoke real no se ejecuta sin cerrarla
- [x] **Q2** comando de tests dirigidos de `emotivoo-v2` — `bin/phpunit {targets}` ✅
- [x] **Q3** token de Bitbucket de solo lectura del runner — recibido (2026-10-11); falta rotarlo en el `.env` del host del runner
- [ ] Credencial de prueba para el spike R1 (F11)

## Notas / límites conocidos

- Bitbucket Cloud no tiene *suggested changes* como GitHub; E6 lo asume con el
  formatter de fallback.
- Un fix de un solo fichero puede chocar con reglas de PHPStan que exigen tocar el
  test (`ApplicationServiceMustHaveTestRule`): candidato a Fase 4 (multi-fichero).
- Runner **síncrono**: un `POST /verify` bloqueante con `run_key` idempotente (sin
  `run_id`, sin sondeo). Ver F7.
- **E7 (desacople fuerte) implementado** en la iniciativa
  [004](../004-fase3-e7-desacople/spec.md): la verificación sale de la ruta de la
  review. El worker propone, valida y pausa (`fix_wait`); un `fix-worker` dedicado
  consume la wave, llama al runner y encola la reanudación con la resolución. Así el
  worker de reviews deja de estar ocupado ~20 min por fix.
