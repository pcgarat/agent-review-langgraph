Última modificación: 2026-10-11 | Creado: 2026-10-11

# Todo: Fase 3 (fix + verificación)

Checklist de la Fase 3. El diseño y las decisiones están en
`docs/fase3-fix-verificacion-20261011.md` (aprobado con enmiendas) y el estado del
conjunto en `tasks/plan.md`.

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

**Checkpoint (2026-10-11):** `make ci` en verde (lint, formato, mypy strict, **323
tests**). El camino pausa → decisión → fix → sugerencia verificada está cubierto
end-to-end con fakes (`tests/test_fix_e2e.py`), sin Docker ni red. Incluye el gate
duro de seguridad (D3 invertida): `never_auto_paths`/`protected_subject` → `BLOCKED`.

## Bloque 2 — Runner (depende de Q1 —resuelta— y de P6 —precondición—)

- [ ] **F7** `VerifyRequest`/`VerifyReport`/`SandboxPort.verify` + `HttpSandboxAdapter`
  - `run_key` idempotente; sondeo de `GET /runs/{id}` hasta `sandbox.timeout_s`.
  - Verifica: `tests/test_http_sandbox.py`.
- [ ] **F8** Servicio `sandbox_runner` (FastAPI interno)
  - Clon → `git apply --check`/`apply` → `setup` (red, `--no-scripts`) → checks
    (`--network none`) → destruir en `finally`. Sin `docker.sock`, sin privilegios.
  - Verifica: `tests/test_sandbox_runner.py` (fake de Docker contando creations/destructions).
- [ ] **F9** Perfil: `ci_image_digest`, recipes `verify`, `checks.targeted` (Q2)
  - Q2 **resuelto**: `checks.targeted = "bin/phpunit {targets}"`. Ya aplicado al
    perfil; falta `ci_image_digest` y que el runner sustituya `{targets}`.
- [ ] **F10** compose + settings + secretos (`SANDBOX_GIT_TOKEN`,
  `SANDBOX_REGISTRY_*` al runner, `SANDBOX_RUNNER_TOKEN`)

## Bloque 3 — Cierre

- [ ] **F11** Spike R1 real: elegir el `DeliveryFormatter` según lo que renderice Bitbucket
- [ ] **F12** E2E con fakes + smoke real
- [ ] **F13** README + `docs/fase3-*.md` + `tasks/plan.md`

## Precondiciones externas

- [ ] **P6** (Q4) revisar la heurística de tema protegido con seguridad — **bloquea F8**
- [x] **Q2** comando de tests dirigidos de `emotivoo-v2` — `bin/phpunit {targets}` ✅
- [ ] **Q3** token de Bitbucket de solo lectura del runner — bloquea F10
- [ ] Credencial de prueba para el spike R1 (F11)

## Notas / límites conocidos

- Bitbucket Cloud no tiene *suggested changes* como GitHub; E6 lo asume con el
  formatter de fallback.
- Un fix de un solo fichero puede chocar con reglas de PHPStan que exigen tocar el
  test (`ApplicationServiceMustHaveTestRule`): candidato a Fase 4 (multi-fichero).
- No hay `run_id` para el cliente: F5b es notablemente más simple contra un runner
  **síncrono** (un `POST /verify` bloqueante con `run_key` idempotente).
